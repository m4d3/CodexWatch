# Security

Treat local logs as untrusted data. Do not add shell evaluation, executable links
that run automatically, or inline rendering that interprets log control sequences.
The viewer prints sanitized text and generates local file links only on user request
through a mouse click. Filesystem inspection and Git require normal user privileges;
do not run the viewer as administrator.

Report suspected vulnerabilities through GitHub private vulnerability reporting
when enabled. If it is unavailable, ask for a private reporting channel without
posting an exploit or private data in a public issue. No maintainer address is
embedded in this template.

Supported code is the latest published version. Rollout format compatibility is
best effort. No production security certification is claimed.
