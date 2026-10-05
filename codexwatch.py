"""CodexWatch: read-only human-focused viewer of local Codex JSONL activity."""

import argparse
import collections
import heapq
import hashlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import time
from urllib.parse import unquote, urlsplit

TAIL_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
HISTORY_BYTES = 2 * 1024 * 1024
MAX_SESSIONS = 4096
MALFORMED_NOTICE = "Malformed record skipped; inspect original log for details."
RECORD_ERRORS = (TypeError, ValueError, AttributeError, RecursionError, UnicodeError)


def clean(value):
    text = str(value)
    text = re.sub(r"[\ud800-\udfff]", "\ufffd", text)
    # Recorded commands/results are data: do not let escape sequences control the terminal.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def norm(path):
    text = str(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text)).rstrip("\\/")


def under(path, root):
    if not path:
        return False
    p, r = norm(path), norm(root)
    return p == r or p.startswith(r + os.sep)


def text_parts(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (text_parts(x) for x in value)))
    if isinstance(value, dict):
        for key in ("text", "content"):
            if key in value:
                return text_parts(value[key])
    return ""


def show_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def command_text(command):
    if not isinstance(command, list):
        return str(command or "")
    command = [str(x) for x in command]
    for flag in ("-Command", "-command", "-c"):
        if flag in command:
            index = command.index(flag)
            if index + 1 < len(command):
                shell = command[0].replace("\\", "/").rsplit("/", 1)[-1]
                return shell + ":\n" + command[index + 1]
    return subprocess.list2cmdline(command)


def visible(record):
    """Skip malformed records without preventing subsequent activity from displaying."""
    try:
        if not isinstance(record, dict):
            raise TypeError("Expected an object")
        stamp = record.get("timestamp")
        if stamp is not None and not isinstance(stamp, str):
            raise TypeError("Expected a timestamp string")
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            raise TypeError("Expected a payload object")
        call_id = payload.get("call_id")
        if call_id is not None and not isinstance(call_id, str):
            raise TypeError("Expected a call identifier string")
        entries = _visible(record)
        for kind, body in entries:
            if not isinstance(body, str):
                raise TypeError("Expected display text")
            kind.encode("utf-8")
            body.encode("utf-8")
        return entries
    except RECORD_ERRORS:
        return [("VIEWER NOTICE", MALFORMED_NOTICE)]


def _visible(record):
    """Allowlist user-visible data. Never display raw/encrypted reasoning or system prompts."""
    outer, p = record.get("type"), record.get("payload", {})
    if not isinstance(p, dict):
        return []
    kind = p.get("type")
    if outer == "response_item":
        if kind == "reasoning":
            summary = text_parts(p.get("summary", []))
            return [("REASONING SUMMARY", summary)] if summary else []
        if kind == "message" and p.get("role") == "assistant":
            if p.get("channel") == "analysis" or p.get("phase") == "analysis":
                return []
            text = text_parts(p.get("content", []))
            return [("MESSAGE " + str(p.get("phase") or ""), text)] if text else []
        if kind == "agent_message":
            text = text_parts(p.get("content", []))
            return [("AGENT MESSAGE -> " + str(p.get("recipient", "")), text)] if text else []
        if kind in ("function_call", "custom_tool_call"):
            name = p.get("name")
            name = name if isinstance(name, str) and name else "tool"
            call = p.get("call_id", "")
            value = p.get("arguments", p.get("input", ""))
            return [(f"ACTION {name} [{call}]", show_value(value))]
        if kind in ("function_call_output", "custom_tool_call_output"):
            return [(f"RESULT [{p.get('call_id', '')}]", show_value(p.get("output", "")))]
    if outer == "event_msg":
        if kind == "viewer_notice":
            return [("VIEWER NOTICE", p.get("text", "Log record skipped"))]
        if kind in ("task_started", "task_complete", "turn_aborted"):
            return [
                (
                    "STATUS",
                    {
                        "task_started": "Turn started (recorded)",
                        "task_complete": "Turn completed (recorded)",
                        "turn_aborted": "Turn interrupted (recorded)",
                    }[kind],
                )
            ]
        if kind == "item_completed":
            item = p.get("item", {})
            if not isinstance(item, dict):
                return []
            t = item.get("type")
            # Messages and reasoning also appear as response_item; skip those duplicates.
            if t == "CommandExecution":
                result = item.get("aggregated_output") or item.get("formatted_output")
                if not result:
                    result = "\n".join(filter(None, (item.get("stdout"), item.get("stderr"))))
                body = f"Folder: {item.get('cwd', '')}\n\n{command_text(item.get('command', ''))}"
                body += f"\n\nExit code: {item.get('exit_code')}\n{result or ''}"
                return [("COMMAND", body)]
            if t == "FileChange":
                return [
                    (
                        "FILE CHANGE",
                        show_value(
                            {
                                k: item[k]
                                for k in ("changes", "status", "stdout", "stderr")
                                if k in item
                            }
                        ),
                    )
                ]
            if t == "SubAgentActivity":
                return [
                    (
                        "SUBAGENT",
                        show_value(
                            {
                                k: item[k]
                                for k in ("kind", "agent_thread_id", "agent_path")
                                if k in item
                            }
                        ),
                    )
                ]
            if t == "Plan":
                return [("PLAN", text_parts(item.get("text", "")))]
            if t == "ImageView":
                return [("IMAGE VIEW", str(item.get("path", "")))]
        # Legacy rollouts used these events instead of completed items.
        if kind in ("agent_message", "agent_reasoning"):
            return [
                (
                    "MESSAGE" if kind == "agent_message" else "REASONING SUMMARY",
                    str(p.get("message", p.get("text", ""))),
                )
            ]
    return []


class Log:
    """One rollout cursor with minimal metadata; reads only complete bounded records."""

    def __init__(self, path, meta):
        self.path = path
        self.meta = {
            k: meta[k]
            for k in ("id", "cwd", "timestamp", "agent_path", "agent_nickname", "thread_source")
            if k in meta and isinstance(meta[k], str)
        }
        self.id = str(meta.get("id") or path.stem)
        self.parent = meta.get("parent_thread_id")
        spawn = meta.get("source", {})
        if isinstance(spawn, dict):
            spawn = spawn.get("subagent", {})
            if isinstance(spawn, dict):
                spawn = spawn.get("thread_spawn", {})
                if isinstance(spawn, dict):
                    self.parent = self.parent or spawn.get("parent_thread_id")
        if not isinstance(self.parent, str):
            self.parent = None
        self.offset = 0
        self.discarding = False
        source_label = self.meta.get("thread_source") or "main"
        if source_label == "user":
            source_label = "main"
        self.label = (
            (self.meta.get("agent_path") or self.meta.get("agent_nickname") or source_label)
            + " "
            + self.id
        )

    def records(self, initial=False, replay=False):
        # Offset advances only over complete lines; partial UTF-8/JSON is retried.
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0
                self.discarding = False
            with self.path.open("rb") as f:
                if initial:
                    f.seek(max(0, size - TAIL_BYTES))
                    if f.tell():
                        self.discarding = not f.readline(MAX_RECORD_BYTES + 1).endswith(b"\n")
                else:
                    f.seek(self.offset)
                while True:
                    start = f.tell()
                    line = f.readline(MAX_RECORD_BYTES + 1)
                    if len(line) > MAX_RECORD_BYTES or self.discarding:
                        first_chunk = not self.discarding
                        self.discarding = not line.endswith(b"\n")
                        self.offset = f.tell()
                        if first_chunk and not initial:
                            yield {
                                "type": "event_msg",
                                "payload": {
                                    "type": "viewer_notice",
                                    "text": "Oversized record skipped; inspect original log for details.",
                                },
                            }
                        if not line:
                            break
                        continue
                    if not line or not line.endswith(b"\n"):
                        self.offset = start
                        break
                    self.offset = f.tell()
                    if initial and not replay:
                        continue
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            yield record
                        else:
                            yield {
                                "type": "event_msg",
                                "payload": {"type": "viewer_notice", "text": MALFORMED_NOTICE},
                            }
                    except (ValueError, UnicodeError, RecursionError):
                        yield {
                            "type": "event_msg",
                            "payload": {"type": "viewer_notice", "text": MALFORMED_NOTICE},
                        }
        except (OSError, PermissionError):
            return


class Catalog:
    """Discover recent sessions and associate their working folders and child threads."""

    def __init__(self, roots):
        self.roots, self.logs = roots, {}

    def discover(self):
        def scan_paths():
            for root in self.roots:
                for folder, _, files in os.walk(root):
                    for name in files:
                        if name.endswith(".jsonl"):
                            path = Path(folder) / name
                            try:
                                yield (path.stat().st_mtime, str(path), path)
                            except OSError:
                                continue

        recent = heapq.nlargest(MAX_SESSIONS, scan_paths())
        allowed = {entry[2] for entry in recent}
        self.logs = {p: log for p, log in self.logs.items() if p in allowed}
        for _, _, path in recent:
            if path in self.logs:
                continue
            try:
                with path.open("rb") as f:
                    line = f.readline(1024 * 1024)
                if not line.endswith(b"\n"):
                    continue
                row = json.loads(line)
                if (
                    isinstance(row, dict)
                    and row.get("type") == "session_meta"
                    and isinstance(row.get("payload"), dict)
                ):
                    self.logs[path] = Log(path, row["payload"])
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                pass

    def matching(self, folders):
        matched = {
            log.id
            for log in self.logs.values()
            if any(under(log.meta.get("cwd"), folder) for folder in folders)
        }
        # Include descendants even if they work in another checkout or temporary directory.
        while True:
            added = {log.id for log in self.logs.values() if log.parent in matched}
            if added <= matched:
                break
            matched.update(added)
        return [log for log in self.logs.values() if log.id in matched]


def find_git():
    """Search only absolute PATH entries: Windows would otherwise try the current folder."""
    names = ("git.exe",) if os.name == "nt" else ("git",)
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip('"')
        if not folder or not os.path.isabs(folder):
            continue
        for name in names:
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def worktrees(repo):
    git = find_git()
    if not git:
        return []
    try:
        # Git's ownership checks stay active; fsmonitor is off so repo config runs nothing.
        result = subprocess.run(
            [
                git,
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(repo),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=True,
        )
        return [
            Path(x[9:])
            for x in result.stdout.decode("utf-8", "replace").split("\0")
            if x.startswith("worktree ")
        ]
    except (OSError, subprocess.SubprocessError):
        return []


def pick_folder(catalog):
    recent = {}
    for log in catalog.logs.values():
        cwd = log.meta.get("cwd")
        if cwd and Path(cwd).is_dir():
            stamp = str(log.meta.get("timestamp", ""))
            recent[cwd] = max(recent.get(cwd, ""), stamp)
    choices = sorted(recent, key=recent.get, reverse=True)[:15]
    print("\nChoose a project (recent Codex folders):\n")
    for n, folder in enumerate(choices, 1):
        print(f"  {n:2}. {clean(folder)}")
    print("\n   B. Browse for a folder\n   Q. Quit\nOr paste a folder path.")
    while True:
        value = input("\nFolder / number [B]: ").strip().strip('"')
        if value.lower() == "q":
            return None
        if not value or value.lower() == "b":
            try:
                import tkinter as tk
                from tkinter import filedialog

                window = tk.Tk()
                window.withdraw()
                window.attributes("-topmost", True)
                value = filedialog.askdirectory(title="Choose repository to watch", parent=window)
                window.destroy()
                if not value:
                    continue
            except Exception as exc:
                print(f"Folder picker unavailable: {clean(exc)}. Paste a path instead.")
                continue
        if value.isdigit() and 1 <= int(value) <= len(choices):
            value = choices[int(value) - 1]
        path = Path(os.path.expandvars(value)).expanduser()
        if path.is_dir():
            return path.absolute()
        print("That folder does not exist. Try again.")


def local_time(stamp):
    if not isinstance(stamp, str):
        return ""
    try:
        return (
            dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%H:%M:%S")
        )
    except (ValueError, OverflowError, OSError):
        return clean(stamp)


def readable(value, level=0):
    """Unpack nested JSON tool envelopes without removing command/result text."""
    if level > 12:
        return show_value(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        if isinstance(parsed, (dict, list)):
            return readable(parsed, level + 1)
        return value
    if isinstance(value, list):
        return "\n\n".join(readable(x, level + 1) for x in value)
    if isinstance(value, dict):
        if value.get("type") in ("text", "output_text", "input_text"):
            return readable(value.get("text", ""), level + 1)
        if value.get("type") in ("image", "audio"):
            return f"[{value['type'].title()} attachment; view it in Codex]"
        parts = []
        for key, item in value.items():
            if key in ("chunk_id", "call_id", "original_token_count", "annotations", "_meta"):
                continue
            label = {
                "cmd": "Command",
                "workdir": "Folder",
                "exit_code": "Exit code",
                "wall_time_seconds": "Time (seconds)",
                "session_id": "Process session",
                "max_output_tokens": "Output token limit",
            }.get(key, key.replace("_", " ").capitalize())
            body = readable(item, level + 1)
            if key in ("output", "content", "text", "input", "arguments"):
                parts.append(body)
            elif "\n" in body:
                parts.append(label + ":\n" + textwrap.indent(body, "  "))
            else:
                parts.append(label + ": " + body)
        return "\n".join(parts)
    return str(value)


def file_references(text, cwd):
    """Resolve explicit references to existing local files; never open anything."""
    candidates = []
    for match in re.finditer(r"!?\[[^\]\n]*\]\((?:<([^>]+)>|([^\s)]+))\)", text):
        candidates.append(match.group(1) or match.group(2))
    candidates.extend(re.findall(r"`([^`\n]+)`", text))
    candidates.extend(m.group(2) for m in re.finditer(r"""(["'])([^\n]*?)\1""", text))
    for line in text.splitlines():
        candidates.append(line.strip())
        if ": " in line:
            candidates.append(line.split(": ", 1)[1].strip())
    candidates.extend(
        re.findall(r"""(?:file:///[A-Za-z]:[^\s<>"')]*|[A-Za-z]:[\\/][^\s<>"')]+)""", text)
    )
    candidates.extend(
        re.findall(r"(?<![\w:])(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,10}(?!\w)", text)
    )
    found = []
    seen = set()
    for candidate in candidates[:2048]:
        candidate = candidate.strip().strip('`<>"').rstrip(".,;")
        if len(candidate) > 4096:
            continue
        candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
        if candidate.startswith("file:"):
            try:
                uri = urlsplit(candidate)
            except (ValueError, RecursionError):
                continue
            if uri.netloc not in ("", "localhost"):
                continue
            candidate = unquote(uri.path)
            if re.match(r"^/[A-Za-z]:", candidate):
                candidate = candidate[1:]
        elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate) and not re.match(
            r"^[A-Za-z]:[\\/]", candidate
        ):
            continue
        if candidate.startswith("\\\\?\\"):
            candidate = candidate[4:]
        if re.match(r"^/[A-Za-z]:[\\/]", candidate):
            candidate = candidate[1:]
        if (
            not candidate
            or candidate.startswith(("\\\\", "//"))
            or any(ord(c) < 32 for c in candidate)
        ):
            continue
        try:
            path = Path(candidate)
            if not path.is_absolute():
                if not cwd:
                    continue
                path = Path(cwd) / path
            # Check the real target, but link the spelling the message used (symlinks, 8.3 names).
            target = path.resolve()
            if str(target).startswith(("\\\\", "//")):
                continue
            if target.is_file() and norm(target) not in seen:
                seen.add(norm(target))
                found.append(Path(os.path.normpath(path)))
                if len(found) >= 128:
                    break
        except (OSError, ValueError):
            continue
    return found


class ConsoleView:
    """Format the human feed and keep bounded labels, calls, and deduplication state."""

    COLORS = {
        "Thinking summary": "96",
        "Progress": "94",
        "Answer": "1;92",
        "Action": "33",
        "Output": "37",
        "Command result": "34",
        "Files changed": "35",
        "Agent message": "95",
        "Agent activity": "95",
        "Plan": "96",
        "Image viewed": "35",
        "Status": "90",
    }

    def __init__(
        self,
        full=False,
        plain=False,
        raw=False,
        max_lines=28,
        links="auto",
        details=False,
        color_mode="auto",
    ):
        self.full, self.raw, self.max_lines = full, raw, max_lines
        self.details = details or full or raw
        self.calls = collections.OrderedDict()
        self.recent_entries = collections.OrderedDict()
        self.command_results = collections.OrderedDict()
        self.tags, self.names = collections.OrderedDict(), {}
        self.next_tag = 1
        self.width = max(40, min(110, shutil.get_terminal_size((100, 30)).columns - 2))
        self.color = (
            sys.stdout.isatty()
            and not plain
            and color_mode != "off"
            and (color_mode == "on" or "NO_COLOR" not in os.environ)
        )
        if self.color and os.name == "nt":
            try:
                import ctypes

                kernel = ctypes.windll.kernel32
                # Declare pointer-sized handles: ctypes otherwise assumes a 32-bit int.
                kernel.GetStdHandle.argtypes = [ctypes.c_uint]
                kernel.GetStdHandle.restype = ctypes.c_void_p
                kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
                kernel.GetConsoleMode.restype = ctypes.c_int
                kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                kernel.SetConsoleMode.restype = ctypes.c_int
                handle = kernel.GetStdHandle(-11)
                mode = ctypes.c_uint()
                self.color = bool(
                    kernel.GetConsoleMode(handle, ctypes.byref(mode))
                    and kernel.SetConsoleMode(handle, mode.value | 4)
                )
            except (OSError, AttributeError):
                self.color = False
        supported = bool(
            os.environ.get("WT_SESSION")
            or os.environ.get("TERM_PROGRAM") in ("vscode", "WezTerm", "iTerm.app")
            or os.environ.get("TERM", "").startswith("xterm-kitty")
        )
        self.links = (
            sys.stdout.isatty() and not plain and (links == "on" or (links == "auto" and supported))
        )

    def hyperlink(self, path, label):
        label = clean(label)
        if not self.links:
            return label
        return f"\x1b]8;;{path.as_uri()}\x1b\\{label}\x1b]8;;\x1b\\"

    def linked_text(self, text, paths, cwd):
        """Protect filename spans while wrapping and remove redundant standalone links."""
        tokens, aliases = {}, {}
        name_counts = collections.Counter(path.name for path in paths)
        for index, path in enumerate(paths[:128]):
            token = "\ue000" + str(index).zfill(max(4, len(path.name) - 2)) + "\ue001"
            tokens[token] = (path, path.name)
            for alias in (str(path), path.as_posix(), path.as_uri()):
                aliases[alias] = token
            if name_counts[path.name] == 1:
                aliases[path.name] = token
            else:
                tokens[token] = (path, path.parent.name + "/" + path.name)
            try:
                relative = path.relative_to(Path(cwd))
                aliases[str(relative)] = aliases[relative.as_posix()] = token
            except (ValueError, TypeError):
                pass

        def markdown(match):
            target = (match.group(2) or match.group(3)).strip()
            target = re.sub(r":\d+(?::\d+)?$", "", target)
            return aliases.get(target, match.group(0))

        text = re.sub(r"!?\[([^\]\n]*)\]\((?:<([^>]+)>|([^\s)]+))\)", markdown, text)
        if aliases:
            pattern = "|".join(re.escape(x) for x in sorted(aliases, key=len, reverse=True))
            text = re.sub(
                r"(?<![\w])(?:" + pattern + r")(?![\w])", lambda m: aliases[m.group(0)], text
            )
        seen, output = set(), []
        for line in text.splitlines():
            key = line.strip().strip("`")
            if key in tokens and key in seen:
                continue
            seen.add(key)
            output.append(line)
        return "\n".join(output), tokens

    def expand_links(self, text, tokens):
        for token, (path, label) in tokens.items():
            text = text.replace(token, self.hyperlink(path, label if self.links else str(path)))
        return text

    def is_duplicate(self, log, record, kind, body):
        """Exact-event deduplication with a fixed-size cache; later updates remain visible."""
        digest = hashlib.blake2b((kind + "\n" + body).encode("utf-8"), digest_size=16).digest()
        key = (log.id, record.get("timestamp") or "", digest)
        if key in self.recent_entries:
            return True
        self.recent_entries[key] = True
        while len(self.recent_entries) > 512:
            self.recent_entries.popitem(last=False)
        return False

    def attachments(self, content, log, compact=False, images_only=False):
        paths = file_references(content, log.meta.get("cwd"))
        image_ext = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".tif", ".tiff")
        if images_only:
            paths = [p for p in paths if p.suffix.lower() in image_ext]
        if not paths:
            return
        if compact:
            for path in paths[:4]:
                label = path.name if self.links else str(path)
                print("    Open: " + self.hyperlink(path, label))
            if len(paths) > 4:
                self.notice(f"    + {len(paths) - 4} more file references (--details to list all)")
            return
        self.notice(
            "  Referenced files"
            + (" · Ctrl+click to open" if self.links else " · copy a path to open")
        )
        for path in paths:
            kind = (
                "Image"
                if path.suffix.lower()
                in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".tif", ".tiff")
                else "File"
            )
            print("  " + self.paint(kind + "  ", "36") + self.hyperlink(path, path.name))
            self.notice("        " + str(path))

    def paint(self, text, color):
        return f"\x1b[{color}m{text}\x1b[0m" if self.color else text

    def agent(self, log):
        if log.id not in self.tags:
            if len(self.tags) >= MAX_SESSIONS:
                oldest, _ = self.tags.popitem(last=False)
                self.names.pop(oldest, None)
            self.tags[log.id] = f"A{self.next_tag:02}"
            self.next_tag += 1
            name = log.meta.get("agent_path")
            name = name.rsplit("/", 1)[-1] if name else log.meta.get("thread_source", "main")
            name = {"user": "Main agent", "guardian_review": "Approval reviewer"}.get(
                name, name.replace("_", " ").capitalize()
            )
            nickname = log.meta.get("agent_nickname")
            if nickname:
                name += f" ({nickname})"
            self.names[log.id] = clean(name)
        return self.tags[log.id] + "  " + self.names[log.id]

    def notice(self, text):
        print(self.paint(clean(text), "90"), flush=True)

    def short(self, text, limit=None):
        text = " ".join(clean(text).split())
        limit = limit or max(20, self.width - 24)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def focused(self, log, record, kind, body):
        """Human conversation gets space; routine machine detail gets one line."""
        p = record.get("payload", {})
        # Some log records explicitly carry null rather than omitting the tool name.
        name = p.get("name")
        name = name if isinstance(name, str) else ""
        key = (log.id, p.get("call_id"))
        if kind.startswith("ACTION "):
            self.calls[key] = name or kind[7:].split(" [", 1)[0]
            while len(self.calls) > 256:
                self.calls.popitem(last=False)
        elif kind.startswith("RESULT"):
            name = self.calls.pop(key, "")
        coordination = any(
            word in name
            for word in (
                "spawn_agent",
                "send_message",
                "followup_task",
                "wait_agent",
                "interrupt_agent",
                "request_user_input",
                "send_message_to_thread",
            )
        )
        if coordination and kind.startswith("RESULT"):
            kind = "AGENT MESSAGE -> " + name.replace("_", " ")
        if kind == "SUBAGENT":
            try:
                activity = json.loads(body)
                body = (
                    f"{activity.get('kind', 'Activity')} · {activity.get('agent_path', 'Subagent')}"
                )
            except (ValueError, AttributeError):
                pass
        if coordination and kind.startswith("ACTION "):
            try:
                args = json.loads(body)
            except (ValueError, RecursionError):
                args = {}
            if isinstance(args, dict):
                message = args.get("message") or args.get("prompt")
                target = args.get("target") or args.get("task_name") or args.get("threadId")
                if message:
                    body = (f"To: {target}\n" if target else "") + message
                elif args.get("questions"):
                    body = readable(args["questions"])
                else:
                    body = readable(args)
            kind = "AGENT MESSAGE -> " + (name.replace("_", " ") or "Coordination")
        human = kind.startswith(("MESSAGE", "REASONING", "AGENT MESSAGE", "SUBAGENT", "PLAN"))
        reviewer = log.meta.get("thread_source") == "guardian_review"
        content = clean(readable(body)).strip()
        if human and not reviewer:
            return False, kind, body
        stamp = local_time(record.get("timestamp"))
        tag = self.agent(log).split("  ", 1)[0]
        lines = content.splitlines()
        error_content = content
        if kind == "COMMAND":
            item = p.get("item", {})
            error_content = str(
                item.get("aggregated_output")
                or item.get("formatted_output")
                or (str(item.get("stdout", "")) + "\n" + str(item.get("stderr", "")))
            )
        # Classify diagnostic-shaped reports, not every mention of the word error.
        # Successful summaries such as "PASS: 0 new error(s)" are routine output.
        diagnostic_lines = [
            x.strip()
            for x in error_content.splitlines()
            if not re.search(
                r"\b(?:0\s+(?:new\s+)?errors?(?:\(s\))?|no\s+errors?|"
                r"0\s+(?:tests?\s+)?failed|failed\s*[:=]\s*0|errors?\s*[:=]\s*0)\b",
                x,
                re.I,
            )
        ]
        failure_lines = [
            x
            for x in diagnostic_lines
            if re.search(
                r"(?:^\s*(?:\[error\]|error\s*[:\[]|fatal\b|"
                r"traceback\s*\(most recent|unhandled\s+exception|FAILED\b)|"
                r"\berror\s+[A-Z]+\d+\s*:|\b\w*(?:Error|Exception)\s*:|"
                r"\b[1-9]\d*\s+(?:new\s+)?errors?\b)",
                x,
                re.I,
            )
        ]
        warning_lines = [
            x
            for x in diagnostic_lines
            if re.search(r"\b(denied|blocked|rejected|warning)\b", x, re.I)
        ]
        exits = re.findall(r"Exit code:\s*(-?\d+)", content, re.I)
        failed_exit = next((x for x in exits if x != "0"), None)
        if reviewer:
            if kind == "STATUS" or kind.startswith(("ACTION", "REASONING")):
                return True, kind, body
            if re.search(r'"outcome"\s*:\s*"(allow|allowed)"', body, re.I):
                return True, kind, body
        if kind == "STATUS":
            self.notice(f"  {stamp}  {tag}  {self.short(content)}")
            return True, kind, body
        title = "Activity"
        summary = ""
        review_denied = reviewer and bool(
            re.search(r'"outcome"\s*:\s*"(deny|denied|reject|rejected)"', body, re.I)
        )
        technical = kind.startswith("RESULT") or kind in ("COMMAND", "FILE CHANGE") or reviewer
        error = technical and not reviewer and (failed_exit is not None or bool(failure_lines))
        attention = technical and (review_denied or bool(warning_lines))
        result_key = (log.id, stamp, tuple(exits))
        if kind == "COMMAND" and exits and not error and not attention:
            self.command_results[result_key] = True
            while len(self.command_results) > 256:
                self.command_results.popitem(last=False)
        elif (
            kind.startswith("RESULT")
            and not error
            and not attention
            and result_key in self.command_results
        ):
            return True, kind, body
        if error:
            title = "ERROR"
            summary = f"Exit {failed_exit}" if failed_exit else "Error reported"
        elif attention:
            title = "ATTENTION"
            summary = "Approval blocked" if review_denied else "Warning or blocker reported"
        elif kind.startswith("ACTION "):
            title = "Action"
            name = name or kind[7:].split(" [", 1)[0]
            summary = name.replace("_", " ")
            if name == "exec":
                actions = []
                for token, label in (
                    ("exec_command", "shell command"),
                    ("apply_patch", "edit files"),
                    ("web__run", "web lookup"),
                    ("view_image", "inspect image"),
                ):
                    if token in body:
                        actions.append(label)
                summary = ", ".join(actions) if actions else "run tools"
            elif name not in ("apply_patch",):
                first = next((x for x in lines if x.strip()), "")
                if first:
                    summary += " · " + first
        elif kind == "COMMAND":
            title = "Command"
            summary = ("Completed · exit " + exits[-1]) if exits else "Result recorded"
        elif kind.startswith("RESULT"):
            title = "Result"
            summary = ("Exit " + ", ".join(dict.fromkeys(exits))) if exits else "Output recorded"
            if coordination:
                summary = (
                    name.replace("_", " ")
                    + " · "
                    + next((x for x in lines if x.strip()), "recorded")
                )
            summary += f" · {len(lines)} lines"
        elif kind == "FILE CHANGE":
            title = "Files"
            try:
                data = json.loads(body)
                changed = data.get("changes", {})
                filenames = (
                    list(changed)
                    if isinstance(changed, dict)
                    else [str(x.get("path", "file")) for x in changed if isinstance(x, dict)]
                )
                summary = ", ".join(x.replace("\\", "/").rsplit("/", 1)[-1] for x in filenames)
                summary = summary or "Changes recorded"
            except (ValueError, AttributeError, TypeError):
                summary = "Changes recorded"
        elif kind == "IMAGE VIEW":
            title, summary = "Image", "Preview inspected"
        else:
            summary = (
                "Approval review recorded" if reviewer else (lines[0] if lines else "Recorded")
            )
        limit = max(20, self.width - len(f"  {stamp}  {tag}  {title}: "))
        paths = file_references(content, log.meta.get("cwd"))
        summary, tokens = self.linked_text(self.short(summary, limit), paths, log.meta.get("cwd"))
        color = {
            "Action": "33",
            "Command": "34",
            "Result": "90",
            "Files": "35",
            "Image": "35",
            "ATTENTION": "33",
            "ERROR": "1;91",
        }.get(title, "90")
        print(
            self.paint(f"  {stamp}  ", "90")
            + self.paint(tag, "1")
            + "  "
            + self.paint(title + ": ", color)
            + self.paint(self.expand_links(summary, tokens), "91" if error else "90"),
            flush=True,
        )
        if error or attention:
            for line in (failure_lines + warning_lines)[:3]:
                print(
                    self.paint("    " + self.short(line, self.width - 6), "91" if error else "33")
                )
        linked_paths = {path for token, (path, _) in tokens.items() if token in summary}
        for path in paths:
            if (
                path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
                and path not in linked_paths
            ):
                print("    Image: " + self.hyperlink(path, path.name if self.links else str(path)))
        return True, kind, body

    def emit(self, log, record, kind, body):
        # Keep data/format failures scoped to one record, including during history replay.
        try:
            self._emit(log, record, kind, body)
        except RECORD_ERRORS:
            self.notice(MALFORMED_NOTICE)

    def _emit(self, log, record, kind, body):
        if kind == "VIEWER NOTICE":
            self.notice(body)
            return
        if self.is_duplicate(log, record, kind, body):
            return
        if not self.details:
            handled, kind, body = self.focused(log, record, kind, body)
            if handled:
                return
        if self.raw:
            print(f"\n[{local_time(record.get('timestamp'))}] [{clean(log.label)}] {clean(kind)}")
            print(clean(body), flush=True)
            self.attachments(clean(readable(body)), log)
            return
        title = {
            "REASONING SUMMARY": "Thinking summary",
            "MESSAGE commentary": "Progress",
            "MESSAGE final_answer": "Answer",
            "COMMAND": "Command result",
            "FILE CHANGE": "Files changed",
            "STATUS": "Status",
            "SUBAGENT": "Agent activity",
            "PLAN": "Plan",
            "IMAGE VIEW": "Image viewed",
        }.get(kind, "Message")
        detail = ""
        if kind.startswith("ACTION "):
            title = "Action"
            detail = kind[7:].split(" [", 1)[0].replace("_", " ")
        elif kind.startswith("RESULT"):
            title = "Output"
        elif kind.startswith("AGENT MESSAGE"):
            title = "Agent message"
            detail = kind.split(" -> ", 1)[-1]
        content = clean(readable(body)).strip()
        paths = file_references(content, log.meta.get("cwd"))
        content, tokens = self.linked_text(content, paths, log.meta.get("cwd"))
        color = self.COLORS.get(title, "36")
        stamp = local_time(record.get("timestamp"))
        if title == "Status":
            print(self.paint(f"  {stamp}  {self.agent(log)}  |  {content}", "90"), flush=True)
            return
        print("\n" + self.paint("─" * self.width, color))
        print(self.paint(f"  {stamp}  {self.agent(log)}", "1"))
        print(self.paint("  " + title + (" · " + clean(detail) if detail else ""), color))
        print()
        lines = []
        for line in content.splitlines() or ["(No text recorded)"]:
            lines.extend(
                textwrap.wrap(
                    line.expandtabs(4),
                    width=self.width - 4,
                    replace_whitespace=False,
                    drop_whitespace=False,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                or [""]
            )
        hidden = 0
        if not self.full and len(lines) > self.max_lines:
            hidden = len(lines) - self.max_lines
            lines = lines[: self.max_lines]
        for line in lines:
            print("  " + self.expand_links(line, tokens))
        if hidden:
            self.notice(f"  … {hidden} more lines. Launch with --full to display complete entries.")
        displayed = "\n".join(lines)
        # Link artifacts outside the text preview exactly once, without another file list.
        for token, (path, label) in tokens.items():
            if token in content and token not in displayed:
                print("    File: " + self.hyperlink(path, label if self.links else str(path)))
        print(flush=True)


def main(argv=None):
    """Select a folder, replay bounded history, then follow new local records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", help="Project folder; omit for picker")
    parser.add_argument(
        "--history",
        type=int,
        default=50,
        help="Recent visible entries on startup (default 50; 0 for live only)",
    )
    parser.add_argument("--log-root", type=Path, help="Override Codex sessions folder")
    parser.add_argument("--duration", type=float, help="Stop after N seconds (for verification)")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Display complete entries without compacting long output",
    )
    parser.add_argument("--plain", action="store_true", help="Disable terminal colors")
    parser.add_argument(
        "--color",
        choices=("auto", "on", "off"),
        default="auto",
        help="Color preference; on overrides NO_COLOR in interactive terminals",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Original detailed display with JSON and full session IDs",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=28,
        help="Maximum displayed lines per entry (default 28; --full overrides)",
    )
    parser.add_argument(
        "--links",
        choices=("auto", "on", "off"),
        default="auto",
        help="Clickable local file references in compatible terminals (default auto)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Show expanded technical actions and output (default emphasizes human messages)",
    )
    args = parser.parse_args(argv)
    if args.history < 0 or (args.duration is not None and args.duration < 0):
        parser.error("history and duration must be non-negative")
    if args.max_lines < 1:
        parser.error("max-lines must be at least 1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    root = args.log_root or codex_home / "sessions"
    if not root.is_dir():
        parser.error(f"No local session directory: {root}")
    view = ConsoleView(
        args.full, args.plain, args.raw, args.max_lines, args.links, args.details, args.color
    )
    catalog = Catalog([root])
    print(view.paint("\n  CODEXWATCH", "1;36"))
    view.notice("  Read-only activity feed  ·  Ctrl+C to stop")
    catalog.discover()
    given = (
        Path(os.path.expandvars(args.repo)).expanduser().absolute()
        if args.repo
        else pick_folder(catalog)
    )
    if given is None:
        return 0
    repo = given.resolve()
    if not repo.is_dir():
        parser.error(f"Folder does not exist: {repo}")
    # Sessions may record either spelling of a symlinked folder (for example /var on macOS).
    aliases = [repo] + ([given] if norm(given) != norm(repo) else [])
    folders = aliases + worktrees(repo)
    print(f"\n  Project  {clean(repo)}")
    view.notice("  Thinking summaries · progress · actions · command results · file changes")
    view.notice("  Saved activity may lag the app. Private reasoning is unavailable.")
    view.notice(
        "  File links: Ctrl+click a filename to open."
        if view.links
        else "  Local file paths are copyable. Use Windows Terminal for clickable filenames."
    )
    if not args.full and not args.raw:
        view.notice(
            f"  {'Detailed' if args.details else 'Focus'} view · messages up to {args.max_lines} lines · routine actions in one line"
        )
        view.notice("  --details expands technical output; --full displays complete entries.")
    tracked = set()
    backlog = []
    history_size = 0
    serial = 0
    matching = sorted(
        catalog.matching(folders),
        key=lambda log: (
            log.meta.get("thread_source") == "guardian_review",
            -log.path.stat().st_mtime,
        ),
    )
    print(f"\n  {len(matching)} saved sessions found (includes historical sessions).")
    for n, log in enumerate(matching):
        tracked.add(log.path)
        label = view.agent(log)
        if args.raw or n < 8:
            print(f"    {clean(log.label) if args.raw else label}")
        for record in log.records(initial=True, replay=args.history > 0):
            for kind, body in visible(record):
                if kind == "VIEWER NOTICE":
                    view.emit(log, record, kind, body)
                    continue
                # Startup history has count AND text budgets; never keep full raw records.
                body = body[:16384] + (
                    "\n[Startup preview shortened; see original log.]" if len(body) > 16384 else ""
                )
                context = {
                    "timestamp": record.get("timestamp") or "",
                    "payload": {
                        key: record.get("payload", {}).get(key)
                        for key in ("type", "name", "call_id")
                    },
                }
                if kind == "COMMAND":
                    context["payload"]["item"] = {"aggregated_output": body}
                serial += 1
                heapq.heappush(backlog, (context["timestamp"], serial, log, context, kind, body))
                history_size += len(body)
                while backlog and (len(backlog) > args.history or history_size > HISTORY_BYTES):
                    history_size -= len(heapq.heappop(backlog)[-1])
    if len(matching) > 8 and not args.raw:
        view.notice(
            f"    + {len(matching) - 8} other sessions; their labels appear when they have activity."
        )
    if args.history:
        view.notice(f"\n  RECENT HISTORY  ·  Last {args.history} entries")
        backlog.sort(key=lambda x: x[0])
        for _, _, log, record, kind, body in backlog:
            view.emit(log, record, kind, body)
    backlog.clear()
    print(
        view.paint(
            f"\n  LIVE WATCH  ·  {len(tracked)} saved sessions  ·  Waiting for new activity\n",
            "1;32",
        ),
        flush=True,
    )
    start = last_scan = last_heartbeat = last_worktrees = time.monotonic()
    while args.duration is None or time.monotonic() - start < args.duration:
        now = time.monotonic()
        if now - last_scan >= 4:
            catalog.discover()
            if now - last_worktrees >= 60:
                folders = aliases + worktrees(repo)
                last_worktrees = now
            tracked.intersection_update(catalog.logs)
            matching = catalog.matching(folders)
            last_scan = now
        had_events = False
        for log in matching:
            if log.path not in tracked:
                tracked.add(log.path)
                view.notice(f"\n  New matching agent/session: {view.agent(log)}")
            for record in log.records():
                for kind, body in visible(record):
                    had_events = True
                    view.emit(log, record, kind, body)
        if had_events:
            last_heartbeat = now
        if now - last_heartbeat >= 30:
            view.notice(
                f"\n  Watching {len(tracked)} saved sessions · no new visible activity in 30 seconds"
            )
            last_heartbeat = now
        time.sleep(1)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nWatch stopped. Agents were not interrupted.")
