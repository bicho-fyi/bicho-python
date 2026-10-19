# Bicho Python

[![Source version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbicho-fyi%2Fbicho-python%2Fmain%2Fpyproject.toml&query=%24.project.version&label=source)](pyproject.toml)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab.svg?logo=python)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![OTLP/HTTP JSON](https://img.shields.io/badge/OTLP-HTTP%20JSON-4de034.svg)](https://opentelemetry.io/docs/specs/otlp/)

Report Python exceptions and error messages to [Bicho](https://bicho.fyi) over OTLP/HTTP JSON. This native SDK uses the Python standard library, with no runtime dependencies. It requires Python 3.11 or newer.

## Install

The distribution name is `bicho-fyi`; the import name is `bicho`. Version 1.0.0 is not published to PyPI. From a checkout of [bicho-python](https://github.com/bicho-fyi/bicho-python), install locally:

```sh
python3.11 -m pip install .
```

## Configure

Use the generated `otlp.endpoint` and `otlp.headers['X-Bicho-Key']` from Bicho project registration:

```python
from bicho import Client, Config

tracker = Client(Config(
    endpoint="https://collector.example/v1/logs",
    key="YOUR_PUBLIC_KEY",
    service_name="storefront/python",
    environment="production",
    release="storefront@1.0.0",
))
```

`Config(endpoint, key, *, service_name='app/python', environment=None, release=None, secret=None, timeout=15)` keeps configuration per client. Metadata strings accept at most 512 UTF-8 bytes. The endpoint must use HTTPS, except for localhost or a loopback IP, and have the exact `/v1/logs` path. URL credentials, query strings, fragments, malformed authorities and normalized alternative paths are rejected. The transport does not use proxy environment variables or follow redirects.

Set `secret` only for trusted server applications reporting to projects that require `X-Bicho-Secret`. Do not embed it in distributed applications or use the core admin token for capture. Ingest credentials must contain 1 to 512 printable non-space ASCII characters.

## Report errors

```python
from bicho import DeliveryError

try:
    process_order()
except Exception as error:
    try:
        tracker.capture_exception(
            error,
            tags={"operation": "checkout"},
            contexts={"checkout": {"stage": "payment"}},
        )
    except DeliveryError as delivery:
        print(delivery)
    raise
```

`capture_exception` records the native exception type, bounded message, up to 32 traceback locations and up to eight cause/context messages. It does not read frame locals, source lines or function arguments. An exception without a traceback still reports its type and message. Normal exception string conversion can execute a custom exception's `__str__` method.

`capture_message("Catalog refresh failed", tags={"operation": "refresh"})` reports an ERROR message. Both capture methods accept optional keyword-only `tags` and `contexts` dictionaries and return the occurrence UID after a valid acknowledgment.

Capture is explicit. The SDK installs no exception hooks, logging handlers, framework integrations or shutdown handlers. It does not automatically collect requests, cookies, headers, sessions, user identities or environment variables. Reporting errors must not replace your application's normal exception handling.

## Keep retry identity

```python
from bicho import Event

event = (
    Event.message("Catalog refresh failed")
    .with_tag("operation", "refresh")
    .with_context("catalog", {"source": "import"})
)
uid = tracker.send(event)
```

`Event.message(text)`, `Event.exception(kind, message)` and `Event.from_exception(exc)` create an occurrence with a UUID and nanosecond timestamp. `with_tag`, `with_context`, `with_level` and `with_stacktrace` return copies that preserve that identity. Mutable context input is normalized and snapshotted when attached. Use the factory methods rather than the internal dataclass fields.

`event.uid` is the occurrence UID, not the stored Bicho event ID. The core derives its stored ID from the UID and authenticated project. Automatic retries reuse identical serialized bytes. For a manual retry after an ambiguous delivery, reuse the same Event and the same client configuration. Calling a capture method again creates another occurrence.

`with_level` accepts `debug`, `info`, `warning`, `error` and `fatal`, mapped to OTLP severities 5, 9, 13, 17 and 21. Plain messages must use error or fatal; exceptions may use any level. `with_stacktrace` accepts explicit stack text. Tags cannot overwrite `log.record.uid` or attributes under `exception.*`, `sdk.*`, `bicho.*`, `service.*` and `deployment.*`.

## Delivery and limits

Sending is synchronous and blocks the calling thread. There is no queue, background delivery worker or flush method. In an asynchronous application, the caller can use `await asyncio.to_thread(tracker.send, event)` to avoid blocking the event loop. Cancellation of that await does not cancel an already running thread's delivery.

The total delivery timeout defaults to 15 seconds; `timeout` accepts a finite positive number up to 60 seconds. Each HTTP attempt, including DNS, connection, headers and response-body reading, gets at most five seconds or the remaining deadline. Short-lived deadline and DNS helper threads do not provide background event delivery. A timed-out OS DNS lookup can finish later in its daemon thread, but cannot send an event.

Network failures and HTTP 429, 502, 503 and 504 allow up to three attempts in total. Retry-After accepts seconds or HTTP dates. The SDK never retries before that delay and returns the failure if the delay would consume the remaining deadline. Other HTTP failures, redirects, malformed acknowledgments and populated OTLP partial success are terminal. A lost or truncated acknowledgment is a retryable transport failure.

HTTP 200 must contain an `application/json` OTLP acknowledgment: `{}`, an empty `partialSuccess`, or a `partialSuccess` with zero rejected records and an empty error message. Unknown fields, wrong types and populated partial success fail delivery. `DeliveryError` exposes `status`, `retryable` and `retry_after`; its messages omit remote response bodies and raw transport errors. The retryable flag describes the failure, not whether the internal attempt/deadline budget remains. Invalid configuration or events raise `ValueError`.

Events must fit within 60 KiB after serialization; oversized events fail explicitly. Responses stop at 64 KiB. Messages and stack text stop at 16 KiB; tag strings stop at 512 bytes. At most 100 tags and 50 contexts are accepted, with keys limited to 128 bytes. Tags accept finite strings, integers, floats and booleans; integers outside signed int64 are sent as strings. Tag integers are limited to 256 bits.

Context normalization uses only exact built-in dictionaries, lists, tuples and scalar types. Other objects become `[Unsupported object]`, without calling their string, repr, iteration or serialization methods. Non-string dictionary keys are omitted. Normalization stops at five nested levels, 50 entries per collection and 300 visited values; strings stop at 2048 UTF-8 bytes and context integers above 256 bits become markers. The `bicho.contexts` attribute contains the normalized JSON. Invalid Unicode surrogates are replaced; byte clipping preserves valid UTF-8.

Credential-like keys, sensitive header name/value pairs, Basic/Bearer strings and HTTP(S) URL userinfo/query/fragment values are filtered. Config, client and event representations omit credentials and captured evidence. Filtering is best effort: arbitrary prose, filenames and explicit stack text can still contain private data. Review what your application sends.

There is no persistent offline queue or delivery guarantee. The SDK does not capture traces, metrics, sessions, replay, attachments or fatal process crashes. It does not upload source maps or perform automatic source-map/native symbolication.

## Development

```sh
PYTHONPATH=src python3.11 -m unittest discover -s tests -v
PYTHONPATH=src python3.14 -m unittest discover -s tests -v
uv build
uv venv --python 3.11 _temp/wheel-venv
uv pip install --python _temp/wheel-venv/bin/python dist/bicho_fyi-1.0.0-py3-none-any.whl
_temp/wheel-venv/bin/python -I -m unittest discover -s tests -v
```

The tests use real loopback HTTP collectors and require no hosted service or application credentials. Run them against both the minimum and a current Python version.

## License

Copyright 2026 [Alex Zappa](https://alex.zappa.dev/) ([FreshJuice](https://freshjuice.dev/)). Licensed under Apache-2.0. See [LICENSE](LICENSE) and [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).
