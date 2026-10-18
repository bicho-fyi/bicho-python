from dataclasses import dataclass, field, replace
import json
import time
import uuid
import ipaddress
import math
import re
from urllib.parse import urlsplit
from typing import Self
from ._privacy import normalize, scrub, sensitive
from ._transport import DeliveryError, deliver

__version__ = '1.0.0'
__all__ = ['Client', 'Config', 'DeliveryError', 'Event', '__version__']


@dataclass(frozen=True, repr=False)
class Config:
    endpoint: str
    key: str = field(repr=False)
    service_name: str = field(default='app/python', kw_only=True)
    environment: str | None = field(default=None, kw_only=True)
    release: str | None = field(default=None, kw_only=True)
    secret: str | None = field(default=None, repr=False, kw_only=True)
    timeout: float = field(default=15, kw_only=True)

    def __post_init__(self) -> None:
        valid = False
        if type(self.endpoint) is str and re.fullmatch(r'https?://[^/?#@\\\s\x00-\x20\x7f]+/v1/logs', self.endpoint):
            try:
                url = urlsplit(self.endpoint)
                host = url.hostname
                port = url.port
                authority = url.netloc
                if host and '%' not in host:
                    if ':' in host:
                        shape = re.fullmatch(r'\[[0-9a-fA-F:]+\](?::[0-9]+)?', authority)
                    else:
                        shape = re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?', authority) and len(host) <= 253 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in host.split('.'))
                    try:
                        loopback = ipaddress.ip_address(host).is_loopback
                    except ValueError:
                        loopback = host == 'localhost'
                    valid = bool(shape and (port is None or 1 <= port <= 65535) and (url.scheme == 'https' or loopback))
            except ValueError:
                pass
        if not valid:
            raise ValueError('Expected an HTTPS /v1/logs endpoint; HTTP is allowed only on loopback')
        for name, credential in (('key', self.key), ('secret', self.secret)):
            if name == 'secret' and credential is None:
                continue
            if type(credential) is not str or not re.fullmatch(r'[\x21-\x7e]{1,512}', credential):
                raise ValueError('Invalid ingest credential')
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 60 or not math.isfinite(self.timeout):
            raise ValueError('Delivery timeout must be positive and at most 60 seconds')
        for value in (self.service_name, self.environment, self.release):
            try:
                valid = value is None or type(value) is str and len(value.encode('utf-8')) <= 512
            except UnicodeError:
                valid = False
            if not valid:
                raise ValueError('Metadata must be UTF-8 and at most 512 bytes')
        if not self.service_name:
            raise ValueError('Service name must not be empty')

    def __repr__(self):
        return 'Config(credentials=[Filtered])'


def _text(value, limit):
    if type(value) is not str:
        raise ValueError('Expected a string')
    return scrub(value, limit)


def _key(key):
    if type(key) is not str or not key or len(key.encode('utf-8', 'replace')) > 128:
        raise ValueError('Expected a nonempty key of at most 128 bytes')
    return scrub(key, 128)


@dataclass(frozen=True, repr=False)
class Event:
    _message: str
    _uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    _timestamp: str = field(default_factory=lambda: str(time.time_ns()))
    _kind: str | None = None
    _stack: str | None = None
    _level: str = 'error'
    _tags: str = '{}'
    _contexts: str = '{}'

    @property
    def uid(self) -> str:
        return self._uid

    @classmethod
    def message(cls, message: str) -> Self:
        return cls(_text(message, 16384))

    @classmethod
    def exception(cls, kind: str, message: str) -> Self:
        if type(kind) is not str or not kind:
            raise ValueError('Exception kind must not be empty')
        return replace(cls.message(message), _kind=_text(kind, 512))

    @classmethod
    def from_exception(cls, exc: BaseException) -> Self:
        if not isinstance(exc, BaseException):
            raise ValueError('Expected an exception')
        def message(error):
            try:
                return scrub(str(error), 16384)
            except Exception:
                return '[Unavailable exception message]'
        event = cls.exception(type(exc).__name__, message(exc))
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            frames.append(f'  File "{scrub(code.co_filename, 512)}", line {tb.tb_lineno}, in {scrub(code.co_name, 128)}')
            if len(frames) > 32:
                frames.pop(0)
            tb = tb.tb_next
        event = event.with_stacktrace('Traceback (most recent call last):\n' + '\n'.join(frames))
        causes = []
        seen = {id(exc)}
        cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        while cause is not None and id(cause) not in seen and len(causes) < 8:
            seen.add(id(cause))
            causes.append({'type': scrub(type(cause).__name__, 512), 'message': scrub(message(cause))})
            cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
        return event.with_context('error_chain', causes) if causes else event

    def with_tag(self, key: str, value: str | int | float | bool) -> Self:
        key = _key(key)
        if type(value) not in (str, int, float, bool) or type(value) is float and not math.isfinite(value):
            raise ValueError('Tags require finite scalar values')
        if type(value) is int and value.bit_length() > 256:
            raise ValueError('Tag integer is too large')
        tags = json.loads(self._tags)
        tags[key] = '[Filtered]' if sensitive(key) else scrub(value, 512) if type(value) is str else value
        if len(tags) > 100:
            raise ValueError('At most 100 tags are allowed')
        return replace(self, _tags=json.dumps(tags, ensure_ascii=False, allow_nan=False))

    def with_context(self, key: str, value: object) -> Self:
        key = _key(key)
        contexts = json.loads(self._contexts)
        contexts[key] = '[Filtered]' if sensitive(key) else normalize(value)
        if len(contexts) > 50:
            raise ValueError('At most 50 contexts are allowed')
        return replace(self, _contexts=json.dumps(normalize(contexts), ensure_ascii=False, allow_nan=False))

    def with_level(self, level: str) -> Self:
        if type(level) is not str or level not in ('debug', 'info', 'warning', 'error', 'fatal'):
            raise ValueError('Invalid severity')
        return replace(self, _level=level)

    def with_stacktrace(self, stacktrace: str) -> Self:
        return replace(self, _stack=_text(stacktrace, 16384))

    def __repr__(self):
        return f'Event(uid={self.uid!r})'


def _attribute(key, value):
    kind = type(value)
    field = 'boolValue' if kind is bool else 'doubleValue' if kind is float else 'intValue' if kind is int and -(2 ** 63) <= value < 2 ** 63 else 'stringValue'
    if kind is int:
        value = str(value)
    return {'key': key, 'value': {field: value}}


class Client:
    def __init__(self, config: Config) -> None:
        if type(config) is not Config:
            raise ValueError('Expected a Config')
        self._config = config

    def __repr__(self):
        return 'Client(config=[Filtered])'

    def capture_message(self, text: str, *, tags: dict[str, str | int | float | bool] | None = None, contexts: dict[str, object] | None = None) -> str:
        return self.send(self._decorate(Event.message(text), tags, contexts))

    def capture_exception(self, exc: BaseException, *, tags: dict[str, str | int | float | bool] | None = None, contexts: dict[str, object] | None = None) -> str:
        return self.send(self._decorate(Event.from_exception(exc), tags, contexts))

    def _decorate(self, event, tags, contexts):
        for values in (tags, contexts):
            if values is not None and type(values) is not dict:
                raise ValueError('Tags and contexts must be dictionaries')
        for key, value in (tags or {}).items():
            event = event.with_tag(key, value)
        for key, value in (contexts or {}).items():
            event = event.with_context(key, value)
        return event

    def _payload(self, event):
        if type(event) is not Event:
            raise ValueError('Expected an Event')
        if event._kind is None and event._level not in ('error', 'fatal'):
            raise ValueError('Plain messages must use error or fatal severity')
        config = self._config
        resource = [_attribute('service.name', scrub(config.service_name, 512))]
        for key, value in (('deployment.environment.name', config.environment), ('service.version', config.release)):
            if value is not None:
                resource.append(_attribute(key, scrub(value, 512)))
        attrs = [_attribute(key, value) for key, value in json.loads(event._tags).items() if not re.match(r'^(?:log\.record\.uid$|exception\.|sdk\.|bicho\.|service\.|deployment\.)', key)]
        attrs.extend([_attribute('log.record.uid', event.uid), _attribute('sdk.name', 'bicho.python'), _attribute('sdk.version', __version__)])
        if event._kind is not None:
            attrs.extend([_attribute('exception.type', event._kind), _attribute('exception.message', event._message)])
        if event._stack is not None:
            attrs.append(_attribute('exception.stacktrace', event._stack))
        if event._contexts != '{}':
            attrs.append(_attribute('bicho.contexts', event._contexts))
        log = {'timeUnixNano': event._timestamp, 'severityNumber': {'debug': 5, 'info': 9, 'warning': 13, 'error': 17, 'fatal': 21}[event._level], 'body': {'stringValue': event._message}, 'attributes': attrs}
        payload = json.dumps({'resourceLogs': [{'resource': {'attributes': resource}, 'scopeLogs': [{'scope': {'name': 'bicho.python', 'version': __version__}, 'logRecords': [log]}]}]}, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
        if len(payload) > 60 * 1024:
            raise ValueError('Bicho event exceeds 60 KiB')
        return payload

    def send(self, event: Event) -> str:
        payload = self._payload(event)
        deliver(self._config, payload)
        return event.uid
