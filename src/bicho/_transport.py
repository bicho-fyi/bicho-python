import http.client
import json
import re
import time
import socket
import ssl
import queue
import threading
import ipaddress
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit


class DeliveryError(RuntimeError):
    def __init__(self, message: str = 'Bicho delivery failed', *, status: int | None = None, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def _retry_after(value):
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r'[0-9]+', value):
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


def _acknowledge(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def constant(value):
        raise ValueError()
    valid = False
    try:
        data = json.loads(body.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        valid = type(data) is dict and not data.keys() - {'partialSuccess'}
        if valid and 'partialSuccess' in data:
            partial = data['partialSuccess']
            valid = type(partial) is dict and not partial.keys() - {'rejectedLogRecords', 'errorMessage'}
            if valid:
                count = partial.get('rejectedLogRecords', 0)
                message = partial.get('errorMessage', '')
                valid = type(message) is str and (type(count) is int and 0 <= count <= 2 ** 63 - 1 or type(count) is str and bool(re.fullmatch(r'0|[1-9][0-9]{0,18}', count)) and int(count) <= 2 ** 63 - 1)
                if valid and (str(count) != '0' or message):
                    return DeliveryError('Bicho reported OTLP partial success; do not retry this event', status=200)
    except (ValueError, UnicodeError, RecursionError):
        valid = False
    return None if valid else DeliveryError('Bicho returned an invalid OTLP response', status=200)


def _attempt(config, payload, timeout):
    url = urlsplit(config.endpoint)
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection(url.hostname, url.port or (443 if url.scheme == 'https' else 80), timeout=timeout)
    active = None
    response = None
    status = None
    def expire():
        sock = active if active is not None else connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    headers = {'Content-Type': 'application/json', 'X-Bicho-Key': config.key, 'User-Agent': 'bicho.python/1.0.0'}
    if config.secret is not None:
        headers['X-Bicho-Secret'] = config.secret
    try:
        addresses = queue.Queue(maxsize=1)
        def resolve():
            try:
                addresses.put(socket.getaddrinfo(connection.host, connection.port, type=socket.SOCK_STREAM))
            except OSError:
                addresses.put(None)
        resolver = threading.Thread(target=resolve, daemon=True)
        resolver.start()
        candidates = addresses.get(timeout=max(0.001, deadline - time.monotonic()))
        if not candidates:
            raise OSError()
        connected = False
        for family, kind, proto, unused, address in candidates:
            if url.scheme == 'http' and not ipaddress.ip_address(address[0]).is_loopback:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            sock = socket.socket(family, kind, proto)
            connection.sock = sock
            sock.settimeout(remaining)
            try:
                sock.connect(address)
                connected = True
                break
            except OSError:
                sock.close()
        if not connected:
            raise OSError()
        if url.scheme == 'https':
            context = ssl.create_default_context()
            connection.sock = context.wrap_socket(connection.sock, server_hostname=url.hostname, do_handshake_on_connect=False)
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sock.do_handshake()
        if time.monotonic() >= deadline:
            raise TimeoutError()
        active = connection.sock
        connection.request('POST', '/v1/logs', body=payload, headers=headers)
        response = connection.getresponse()
        status = response.status
        if status != 200:
            return DeliveryError(f'Bicho returned HTTP {status}', status=status, retryable=status in (429, 502, 503, 504), retry_after=_retry_after(response.getheader('Retry-After')))
        if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return DeliveryError('Bicho returned an invalid OTLP response', status=200)
        body = bytearray()
        expected = response.length
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            chunk = response.read1(min(8192, 65537 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > 65536:
                return DeliveryError('Bicho response exceeds 64 KiB', status=200)
        if expected is not None and len(body) != expected:
            raise OSError()
        if time.monotonic() >= deadline:
            raise TimeoutError()
        return _acknowledge(body)
    except (OSError, http.client.HTTPException, queue.Empty):
        return DeliveryError('Bicho request could not be completed', status=status, retryable=True)
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def deliver(config, payload):
    deadline = time.monotonic() + config.timeout
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeliveryError('Bicho delivery deadline exceeded', retryable=True)
        error = _attempt(config, payload, min(5, remaining))
        if error is None:
            return
        if not error.retryable or attempt == 2:
            raise error
        delay = error.retry_after if error.retry_after is not None else 0.25 * 2 ** attempt
        if delay >= deadline - time.monotonic():
            raise error
        time.sleep(delay)
