# Bicho Python

Native Python SDK for explicit exception and message reporting over OTLP/HTTP JSON. Read README.md, src/bicho/ and relevant tests before editing.

- Python 3.11+, standard library only at runtime. Distribution: bicho-fyi; import: bicho.
- Keep capture synchronous and explicit. Do not install global handlers, logging hooks, framework integrations or a delivery queue implicitly.
- Preserve occurrence IDs, timestamps and serialized bytes across retries. Never retry populated OTLP partial success or follow redirects with credentials.
- Keep the delivery deadline effective through DNS, TLS, headers and body reads. Respect Retry-After without retrying early.
- Ingest credentials authorize capture only. Never use an admin token for capture or expose credentials in errors, repr output or Git.
- Normalize bounded built-in values without invoking arbitrary context-object methods. Do not collect frame locals, source lines or function arguments.
- Run `PYTHONPATH=src python3.11 -W error -m unittest discover -s tests -v` and repeat on a current Python version. Normal tests must not require sibling repositories or hosted services.
- Build with `uv build`; inspect the wheel and source distribution, then run the suite against an isolated installed wheel with `python -I -W error -m unittest discover -s tests -v`.
- Keep scratch files and sibling-dependent integration checks in ignored _temp/. Commit or push only with task-specific "yolo" approval; publication requires separate approval.
- Preserve the complete Apache-2.0 license and attribution.
