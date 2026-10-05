import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
import contextlib
import io
import ctypes
from unittest import mock

spec = importlib.util.spec_from_file_location("watch", Path(__file__).parents[1] / "codexwatch.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def line(kind, payload):
    return (
        json.dumps(
            {"type": kind, "timestamp": "2026-10-03T14:00:00Z", "payload": payload},
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


class WatchTests(unittest.TestCase):
    def test_only_actual_error_reports_are_red(self):
        log = w.Log(Path("a"), {"id": "a"})
        cases = (
            ("PASS console 0 new error(s) in Editor console 0.1s", None),
            ("Exit code: 0\n0 errors\nfailed: 0", None),
            ("Checking error handling and failure recovery", None),
            ("Warning: action blocked pending approval", "ATTENTION"),
            ("Permission denied", "ATTENTION"),
            ("Exit code: 1\nPermission denied", "ERROR"),
            ("source.cs(89,37): error CS0103: Unknown name", "ERROR"),
            ("ValueError: invalid value", "ERROR"),
            ("ERROR: build failed", "ERROR"),
        )
        for body, severity in cases:
            with self.subTest(body=body):
                view = w.ConsoleView()
                view.color = True
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    view.emit(log, {}, "RESULT [id]", body)
                rendered = output.getvalue()
                if severity:
                    self.assertIn(severity + ":", rendered)
                else:
                    self.assertNotIn("ATTENTION:", rendered)
                    self.assertNotIn("ERROR:", rendered)
                self.assertEqual("\x1b[1;91m" in rendered, severity == "ERROR")
                self.assertEqual("\x1b[91m" in rendered, severity == "ERROR")
        reviewer = w.Log(Path("review"), {"id": "review", "thread_source": "guardian_review"})
        view = w.ConsoleView()
        view.color = True
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(reviewer, {}, "MESSAGE final_answer", '{"outcome":"deny","reason":"blocked"}')
        self.assertIn("ATTENTION:", output.getvalue())
        self.assertNotIn("\x1b[91m", output.getvalue())
        self.assertNotIn("\x1b[1;91m", output.getvalue())

    def test_explicit_color_overrides_inherited_no_color_but_not_plain_or_redirection(self):
        with (
            mock.patch.dict(w.os.environ, {"NO_COLOR": "1"}),
            mock.patch.object(w.os, "name", "posix"),
            mock.patch.object(sys.stdout, "isatty", return_value=True),
        ):
            self.assertFalse(w.ConsoleView().color)
            self.assertTrue(w.ConsoleView(color_mode="on").color)
            self.assertFalse(w.ConsoleView(color_mode="off").color)
            self.assertFalse(w.ConsoleView(plain=True, color_mode="on").color)
            with mock.patch.object(sys.stdout, "isatty", return_value=False):
                self.assertFalse(w.ConsoleView(color_mode="on").color)

    def test_windows_color_setup_uses_pointer_sized_handles(self):
        kernel = mock.MagicMock()
        kernel.GetStdHandle.return_value = 0x123456789
        kernel.GetConsoleMode.return_value = 1
        kernel.SetConsoleMode.return_value = 1
        with (
            mock.patch.object(sys.stdout, "isatty", return_value=True),
            mock.patch.dict(w.os.environ, {}, clear=True),
            mock.patch.object(w.os, "name", "nt"),
            mock.patch.object(ctypes, "windll", mock.Mock(kernel32=kernel), create=True),
        ):
            view = w.ConsoleView()
        self.assertTrue(view.color)
        self.assertEqual(kernel.GetConsoleMode.argtypes[0], ctypes.c_void_p)
        self.assertEqual(kernel.SetConsoleMode.argtypes[0], ctypes.c_void_p)
        self.assertEqual(kernel.GetConsoleMode.call_args.args[0], 0x123456789)

    def test_compact_actions_and_human_cards_have_distinct_colors(self):
        log = w.Log(Path("a"), {"id": "a"})
        view = w.ConsoleView()
        view.color = True
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(log, {"payload": {"name": "exec"}}, "ACTION exec", "exec_command")
            view.emit(log, {}, "MESSAGE commentary", "Progress update")
            view.emit(log, {}, "REASONING SUMMARY", "Emitted summary")
            view.emit(log, {}, "AGENT MESSAGE -> send_message", "Coordination update")
            view.emit(log, {}, "RESULT [failure]", "Exit code: 1\nPermission denied")
        rendered = output.getvalue()
        for code in ("33", "94", "96", "95", "1;91"):
            self.assertIn(f"\x1b[{code}m", rendered)
        self.assertIn("\x1b[0m", rendered)

    def test_summary_only_and_no_private_instructions(self):
        r = json.loads(
            line(
                "response_item",
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Public summary"}],
                    "encrypted_content": "PRIVATE",
                    "raw_content": "PRIVATE",
                },
            )
        )
        self.assertEqual(w.visible(r), [("REASONING SUMMARY", "Public summary")])
        for role in ["system", "developer", "user"]:
            self.assertEqual(
                w.visible(
                    json.loads(
                        line(
                            "response_item",
                            {"type": "message", "role": role, "content": [{"text": "PRIVATE"}]},
                        )
                    )
                ),
                [],
            )
        self.assertEqual(
            w.visible(
                json.loads(
                    line(
                        "response_item",
                        {
                            "type": "message",
                            "role": "assistant",
                            "channel": "analysis",
                            "content": [{"text": "PRIVATE"}],
                        },
                    )
                )
            ),
            [],
        )

    def test_new_descendants_and_worktree_matching(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            repo.mkdir()

            def create(name, meta):
                (root / f"{name}.jsonl").write_bytes(line("session_meta", meta))

            create("main", {"id": "main", "cwd": str(repo)})
            create(
                "child", {"id": "child", "cwd": str(root / "elsewhere"), "parent_thread_id": "main"}
            )
            create("other", {"id": "other", "cwd": str(root / "repo-other")})
            c = w.Catalog([root])
            c.discover()
            self.assertEqual({x.id for x in c.matching([repo])}, {"main", "child"})
            create(
                "grandchild",
                {"id": "grandchild", "cwd": str(root / "scratch"), "parent_thread_id": "child"},
            )
            create("worktree", {"id": "worktree", "cwd": str(root / "wt")})
            c.discover()
            self.assertEqual(
                {x.id for x in c.matching([repo, root / "wt"])},
                {"main", "child", "grandchild", "worktree"},
            )

    def test_partial_utf8_then_append_and_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "live.jsonl"
            p.write_bytes(line("session_meta", {"id": "one"}))
            log = w.Log(p, {"id": "one"})
            self.assertEqual(list(log.records(initial=True)), [])
            message = line(
                "response_item",
                {"type": "message", "role": "assistant", "content": [{"text": "Snow \u2603"}]},
            )
            cut = message.index("\u2603".encode("utf-8")) + 1
            with p.open("ab") as f:
                f.write(message[:cut])
            self.assertEqual(list(log.records()), [])
            with p.open("ab") as f:
                f.write(message[cut:])
            self.assertEqual(w.visible(list(log.records())[0])[0][1], "Snow \u2603")
            self.assertEqual(list(log.records()), [])
            p.write_bytes(line("event_msg", {"type": "task_complete"}))
            self.assertEqual(w.visible(list(log.records())[0])[0][0], "STATUS")

    def test_complete_recorded_actions_and_terminal_sanitization(self):
        body = "x" * 50000
        r = json.loads(
            line("response_item", {"type": "function_call_output", "call_id": "c1", "output": body})
        )
        self.assertEqual(len(w.visible(r)[0][1]), 50000)
        r = json.loads(
            line(
                "event_msg",
                {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": "echo snow",
                        "cwd": "repo",
                        "aggregated_output": "snow",
                        "exit_code": 0,
                    },
                },
            )
        )
        self.assertIn("Exit code: 0\nsnow", w.visible(r)[0][1])
        self.assertEqual(
            w.command_text([r"C:\bin\pwsh.exe", "-Command", "echo one\necho two"]),
            "pwsh.exe:\necho one\necho two",
        )
        self.assertEqual(w.clean("\x1b[2Jhello\x1b]0;title\x07"), "hello")

    def test_end_to_end_live_feed_discovers_new_child(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            repo.mkdir()
            logs = root / "logs"
            logs.mkdir()
            main = logs / "main.jsonl"
            main.write_bytes(line("session_meta", {"id": "main", "cwd": str(repo)}))

            def append():
                with main.open("ab") as f:
                    f.write(b"{invalid json}\n")
                    f.write(line("event_msg", {"type": "viewer_notice", "text": None}))
                    f.write(
                        (
                            json.dumps(
                                {
                                    "type": "response_item",
                                    "payload": {
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [{"text": "\ud800"}],
                                    },
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    f.write(
                        line(
                            "response_item",
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"text": "LIVE_OUTPUT_MARKER"}],
                            },
                        )
                    )
                child = logs / "child.jsonl"
                child.write_bytes(
                    line(
                        "session_meta",
                        {
                            "id": "child",
                            "parent_thread_id": "main",
                            "cwd": str(root / "other"),
                            "agent_path": "/root/test-child",
                        },
                    )
                    + line(
                        "response_item",
                        {
                            "type": "reasoning",
                            "summary": [{"text": "CHILD_SUMMARY_MARKER"}],
                            "encrypted_content": "MUST_NOT_APPEAR",
                        },
                    )
                )

            timer = threading.Timer(1.5, append)
            timer.start()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(w.__file__)),
                        str(repo),
                        "--log-root",
                        str(logs),
                        "--history",
                        "0",
                        "--duration",
                        "6",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=12,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("LIVE_OUTPUT_MARKER", result.stdout)
                self.assertIn(w.MALFORMED_NOTICE, result.stdout)
                self.assertIn("CHILD_SUMMARY_MARKER", result.stdout)
                self.assertNotIn("MUST_NOT_APPEAR", result.stdout)
                self.assertIn("New matching agent/session", result.stdout)
            finally:
                timer.join()

    def test_readable_nested_output(self):
        wrapped = json.dumps(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "output": "Build succeeded.\n0 errors.",
                                "exit_code": 0,
                                "chunk_id": "NOISE",
                            }
                        ),
                    }
                ]
            }
        )
        pretty = w.readable(wrapped)
        self.assertIn("Build succeeded.\n0 errors.", pretty)
        self.assertIn("Exit code: 0", pretty)
        self.assertNotIn("NOISE", pretty)
        self.assertNotIn("\\n", pretty)

    def test_compact_and_full_display_and_unique_labels(self):
        a = w.Log(Path("a"), {"id": "same-prefix-A", "agent_path": "/root/shader_review"})
        b = w.Log(Path("b"), {"id": "same-prefix-B", "agent_path": "/root/shader_review"})
        view = w.ConsoleView(plain=True, max_lines=2, details=True)
        self.assertNotEqual(view.agent(a), view.agent(b))
        content = "\n".join("Line " + str(n) for n in range(8))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(a, {"timestamp": "2026-10-03T14:00:00Z"}, "RESULT [long-id]", content)
        shown = output.getvalue()
        self.assertIn("A01  Shader review", shown)
        self.assertIn("6 more lines", shown)
        self.assertNotIn("Line 7", shown)
        self.assertNotIn("long-id", shown)
        self.assertNotIn("\x1b", shown)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            w.ConsoleView(full=True, plain=True).emit(a, {}, "RESULT [id]", content)
        self.assertIn("Line 7", output.getvalue())

    def test_file_links_spaces_unicode_relative_and_deduplication(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            image = repo / "snow scene ü.png"
            image.write_bytes(b"fixture")
            code = repo / "shader.py"
            code.write_text("# fixture")
            text = f'[Preview](<{image}:12>)\nImage: "{image}"\n`shader.py`\n[URI]({image.as_uri()})\nmissing.png\nhttps://example.com/image.png'
            self.assertEqual(
                {w.norm(p.resolve()) for p in w.file_references(text, str(repo))},
                {w.norm(image.resolve()), w.norm(code.resolve())},
            )
            with mock.patch.object(sys.stdout, "isatty", return_value=True):
                view = w.ConsoleView(links="on")
            link = view.hyperlink(image, "snow scene ü.png")
            self.assertIn(image.as_uri(), link)
            self.assertIn("%20", link)
            self.assertIn("%C3%BC", link)
            self.assertTrue(link.startswith("\x1b]8;;file:"))
            self.assertEqual(w.clean(link), "snow scene ü.png")
            log = w.Log(Path("log"), {"id": "a", "cwd": str(repo)})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                view.emit(log, {}, "MESSAGE final_answer", f"Preview: `{image}`")
            self.assertIn("snow scene", output.getvalue())
            self.assertIn(image.as_uri(), output.getvalue())

    def test_plain_redirected_and_off_links_do_not_emit_escapes(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "preview.png"
            image.write_bytes(b"fixture")
            for kwargs in ({"plain": True, "links": "on"}, {"links": "off"}, {"links": "on"}):
                with mock.patch.object(sys.stdout, "isatty", return_value=False):
                    view = w.ConsoleView(**kwargs)
                self.assertEqual(view.hyperlink(image, "preview.png"), "preview.png")
            with mock.patch.object(sys.stdout, "isatty", return_value=True):
                view = w.ConsoleView(links="off")
                self.assertFalse(view.links)

    def test_focus_routine_output_budget_and_preserved_human_messages(self):
        log = w.Log(Path("a"), {"id": "a", "agent_path": "/root/test"})
        view = w.ConsoleView(plain=True)
        body = "\n".join(["Exit code: 0", "0 errors."] + ["machine output " * 100] * 100)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(log, {}, "RESULT [id]", body)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn("ATTENTION", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(
                log,
                {},
                "MESSAGE commentary",
                "I found the cause.\nThe snow shader uses the wrong normal.",
            )
        self.assertIn("The snow shader uses the wrong normal.", output.getvalue())

    def test_missing_null_and_invalid_tool_names_render_without_crashing(self):
        log = w.Log(Path("a"), {"id": "a"})
        for name in (None, "", 42, [], {}):
            with self.subTest(name=name):
                view = w.ConsoleView(plain=True)
                record = {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": name,
                        "call_id": "call1",
                        "arguments": "{}",
                    },
                }
                events = w.visible(record)
                self.assertEqual(events, [("ACTION tool [call1]", "{}")])
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    for kind, body in events:
                        view.emit(log, record, kind, body)
                    view.emit(
                        log,
                        {"payload": {"name": name, "call_id": "call1"}},
                        "RESULT [call1]",
                        "Exit code: 0",
                    )
                    view.emit(
                        log,
                        {"payload": {"name": name}},
                        "MESSAGE commentary",
                        "Work continues.",
                    )
                self.assertIn("Action: tool", output.getvalue())
                self.assertIn("Work continues.", output.getvalue())
        self.assertEqual(
            w.visible({"type": "response_item", "payload": {"type": "function_call"}}),
            [("ACTION tool []", "")],
        )

    def test_focus_errors_and_coordination_survive(self):
        log = w.Log(Path("a"), {"id": "a"})
        view = w.ConsoleView(plain=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(
                log, {}, "RESULT [id]", "Exit code: 1\nPermission denied\n" + ("details\n" * 200)
            )
        self.assertIn("ERROR", output.getvalue())
        self.assertIn("Permission denied", output.getvalue())
        self.assertLessEqual(len(output.getvalue().splitlines()), 4)
        rec = {"payload": {"type": "function_call", "name": "send_message", "call_id": "msg1"}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(
                log,
                rec,
                "ACTION send_message [msg1]",
                json.dumps(
                    {"target": "shader_agent", "message": "Please check the light direction."}
                ),
            )
        self.assertIn("Please check the light direction.", output.getvalue())
        self.assertIn("To: shader_agent", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(
                log, {"payload": {"call_id": "msg1"}}, "RESULT [msg1]", "Delivered to shader agent"
            )
        self.assertIn("Agent message", output.getvalue())

    def test_focus_approval_allow_is_quiet_but_rejection_is_visible(self):
        log = w.Log(Path("a"), {"id": "a", "thread_source": "guardian_review"})
        view = w.ConsoleView(plain=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(
                log,
                {},
                "MESSAGE final_answer",
                json.dumps({"outcome": "allow", "reason": "routine"}),
            )
        self.assertEqual(output.getvalue(), "")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(
                log,
                {},
                "MESSAGE final_answer",
                json.dumps({"outcome": "deny", "reason": "Action blocked: permission denied"}),
            )
        self.assertIn("ATTENTION", output.getvalue())

    def test_inline_links_and_duplicate_standalone_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "readable image.png"
            image.write_bytes(b"fixture")
            log = w.Log(Path("a"), {"id": "a", "cwd": folder})
            with mock.patch.object(sys.stdout, "isatty", return_value=True):
                view = w.ConsoleView(links="on")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                view.emit(
                    log,
                    {"timestamp": "now"},
                    "MESSAGE final_answer",
                    f"[Image](<{image}>)\n`{image}`",
                )
                view.emit(
                    log,
                    {"timestamp": "now"},
                    "MESSAGE final_answer",
                    f"[Image](<{image}>)\n`{image}`",
                )
            self.assertEqual(output.getvalue().count(image.as_uri()), 1)
            self.assertNotIn("Referenced files", output.getvalue())

    def test_oversized_records_do_not_block_next_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.jsonl"
            path.write_bytes(line("session_meta", {"id": "a"}))
            log = w.Log(path, {"id": "a"})
            list(log.records(initial=True))
            with path.open("ab") as stream:
                stream.write(b"x" * 256 + b"\n" + line("event_msg", {"type": "task_complete"}))
            with mock.patch.object(w, "MAX_RECORD_BYTES", 128):
                records = list(log.records())
            self.assertEqual(records[0]["payload"]["type"], "viewer_notice")
            self.assertEqual(records[-1]["payload"]["type"], "task_complete")

    def test_git_lookup_ignores_relative_path_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            name = "git.exe" if w.os.name == "nt" else "git"
            planted = Path(folder) / name
            planted.write_bytes(b"")
            planted.chmod(0o755)
            with (
                contextlib.chdir(folder),
                mock.patch.dict(w.os.environ, {"PATH": "." + w.os.pathsep + "relative"}),
            ):
                self.assertIsNone(w.find_git())
                self.assertEqual(w.worktrees(Path(folder)), [])
            with mock.patch.dict(w.os.environ, {"PATH": folder}):
                self.assertEqual(w.find_git(), str(planted))

    def test_worktrees_lists_registered_checkouts(self):
        if not w.find_git():
            self.skipTest("Git is not installed")
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "repo"
            repo.mkdir()
            subprocess.run([w.find_git(), "init", "-q", str(repo)], check=True)
            found = [w.norm(path) for path in w.worktrees(repo)]
            self.assertIn(w.norm(repo.resolve()), found)

    def test_caches_are_bounded_and_private_metadata_is_not_retained(self):
        log = w.Log(
            Path("a"), {"id": "a", "base_instructions": "PRIVATE", "creator_user_id": "PRIVATE"}
        )
        self.assertNotIn("base_instructions", log.meta)
        self.assertNotIn("creator_user_id", log.meta)
        with mock.patch.object(w, "MAX_SESSIONS", 2):
            view = w.ConsoleView(plain=True)
            for n in range(3):
                view.agent(w.Log(Path(str(n)), {"id": str(n)}))
            self.assertEqual(len(view.tags), 2)
            self.assertEqual(len(view.names), 2)

    def test_timestamp_escapes_are_sanitized_in_all_display_modes(self):
        log = w.Log(Path("synthetic"), {"id": "synthetic"})
        stamp = "\x1b[2J\x1b]0;untrusted title\x07invalid"
        for options in ({}, {"details": True}, {"raw": True}):
            with self.subTest(options=options):
                view = w.ConsoleView(plain=True, **options)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    view.emit(log, {"timestamp": stamp}, "MESSAGE commentary", "SAFE_MESSAGE")
                shown = output.getvalue()
                self.assertIn("SAFE_MESSAGE", shown)
                self.assertIn("invalid", shown)
                self.assertNotIn("\x1b", shown)
                self.assertNotIn("untrusted title", shown)

    def test_history_skips_bad_records_and_preserves_next_message(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, logs = root / "repo", root / "logs"
            repo.mkdir()
            logs.mkdir()
            malformed = [
                {"type": "event_msg", "payload": {"type": "viewer_notice", "text": None}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "stdout": ["text"],
                            "exit_code": 0,
                        },
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec",
                        "call_id": [],
                        "arguments": "{}",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": [],
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"text": "bad"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"text": "\ud800"}],
                    },
                },
                {"type": "response_item", "payload": None},
            ]
            valid = {
                "type": "response_item",
                "timestamp": None,
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": "NEXT_MESSAGE"}],
                },
            }
            data = line("session_meta", {"id": "synthetic", "cwd": str(repo)})
            data += b"{invalid json}\nnull\n"
            data += b"".join((json.dumps(row) + "\n").encode() for row in malformed + [valid])
            data += line(
                "response_item",
                {"type": "message", "role": "assistant", "content": [{"text": "DATED_MESSAGE"}]},
            )
            (logs / "synthetic.jsonl").write_bytes(data)
            for options in ([], ["--details"], ["--raw"]):
                with self.subTest(options=options):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        result = w.main(
                            [
                                str(repo),
                                "--log-root",
                                str(logs),
                                "--duration",
                                "0",
                                "--plain",
                                *options,
                            ]
                        )
                    self.assertEqual(result, 0)
                    self.assertIn(w.MALFORMED_NOTICE, output.getvalue())
                    self.assertIn("NEXT_MESSAGE", output.getvalue())
                    self.assertIn("DATED_MESSAGE", output.getvalue())
                    self.assertNotIn("\ud800", output.getvalue())

    def test_rendering_failure_does_not_stop_following_records(self):
        log = w.Log(Path("synthetic"), {"id": "synthetic"})
        view = w.ConsoleView(plain=True)
        record = {"payload": {"type": "function_call", "name": "send_message", "call_id": "a"}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            for kind, body in w.visible(
                {
                    "type": "response_item",
                    "payload": {
                        **record["payload"],
                        "arguments": json.dumps({"target": "agent", "message": 42}),
                    },
                }
            ):
                view.emit(log, record, kind, body)
            view.emit(log, {}, "MESSAGE commentary", "FOLLOWING_MESSAGE")
        self.assertIn(w.MALFORMED_NOTICE, output.getvalue())
        self.assertIn("FOLLOWING_MESSAGE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
