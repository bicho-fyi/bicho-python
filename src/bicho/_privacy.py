import itertools
import math
import re

_SENSITIVE = re.compile(r'password|passwd|secret|token|auth|cookie|api[ _-]?key|email|phone|address|^user$|^ip$', re.I)
_AUTH = re.compile(r'\b(?:Bearer|Basic)\s+[^\s<>"\']+', re.I)
_URL = re.compile(r'https?://[^\s<>"\')]+', re.I)


def sensitive(key):
    return bool(_SENSITIVE.search(key))


def scrub(value, limit=2048):
    value = value[:limit].encode('utf-8', 'replace')[:limit].decode('utf-8', 'ignore')
    value = _AUTH.sub('[Filtered]', value)
    def clean(match):
        raw = re.split('[?#]', match[0], maxsplit=1)[0]
        return re.sub(r'^(https?://)[^/]*@', r'\1', raw, flags=re.I)
    return _URL.sub(clean, value).encode('utf-8')[:limit].decode('utf-8', 'ignore')


def normalize(value):
    budget = 300
    def visit(item, depth):
        nonlocal budget
        if depth > 5 or budget == 0:
            return '[Truncated]'
        budget -= 1
        kind = type(item)
        if kind is str:
            return scrub(item)
        if item is None or kind in (bool, int):
            return item if kind is not int or item.bit_length() <= 256 else '[Large integer]'
        if kind is float:
            return item if math.isfinite(item) else '[Non-finite number]'
        if kind in (list, tuple):
            if len(item) == 2 and type(item[0]) is str and sensitive(item[0]):
                return [scrub(item[0], 128), '[Filtered]']
            return [visit(child, depth + 1) for child in item[:50]]
        if kind is dict:
            return {scrub(key, 128): '[Filtered]' if sensitive(key) else visit(child, depth + 1)
                    for key, child in itertools.islice(item.items(), 50) if type(key) is str}
        return '[Unsupported object]'
    return visit(value, 0)
