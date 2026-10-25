"""Credential-free public metadata and bounded redaction of streaming deltas."""
from __future__ import annotations

import base64
import re
import urllib.parse


def _secrets(value):
    if isinstance(value, str):
        value = [value]
    return tuple(dict.fromkeys(item for item in (value or ()) if isinstance(item, str) and item))


def _forms(secret):
    raw = secret.encode('utf-8')
    encoded = ''.join('%%%02X' % byte for byte in raw)
    double = urllib.parse.quote(encoded, safe='')
    standard = base64.b64encode(raw).decode()
    urlsafe = base64.urlsafe_b64encode(raw).decode()
    return tuple(dict.fromkeys((secret, urllib.parse.quote(secret, safe=''),
                               urllib.parse.quote_plus(secret), encoded,
                               double, urllib.parse.quote(double, safe=''),
                               standard, standard.rstrip('='), urlsafe, urlsafe.rstrip('='))))


def contains_secret(value, secrets):
    if not isinstance(value, str):
        return False
    decoded = value
    for _ in range(3):
        decoded = urllib.parse.unquote(decoded)
    for secret in _secrets(secrets):
        if secret in value or secret in decoded:
            return True
        if any(form in value for form in _forms(secret)):
            return True
    return False


def sanitize_public(value, secrets):
    """Redact known credentials, including percent/base64 encoded echoes."""
    secrets = _secrets(secrets)
    if isinstance(value, str):
        for secret in secrets:
            for form in sorted(_forms(secret), key=len, reverse=True):
                if form:
                    if '%' in form:
                        value = re.sub(re.escape(form), '[redacted]', value, flags=re.I)
                    else:
                        value = value.replace(form, '[redacted]')
        return '[redacted]' if contains_secret(value, secrets) else value
    if isinstance(value, dict):
        return {sanitize_public(key, secrets) if isinstance(key, str) else key:
                sanitize_public(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_public(item, secrets) for item in value]
    return value


def same_origin(before, after):
    def origin(value):
        parsed = urllib.parse.urlsplit(value)
        return parsed.scheme.lower(), (parsed.hostname or '').lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        return origin(before) == origin(after)
    except (TypeError, ValueError):
        return False


def validate_destination(value, secrets=()):
    """Accept an HTTP API URL with no credential-bearing URL components."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('API URL 地址格式无效。')
    try:
        parts = urllib.parse.urlsplit(value)
        _ = parts.port
    except ValueError:
        raise ValueError('API URL 地址格式无效。') from None
    if (parts.scheme not in ('http', 'https') or not parts.hostname or
            parts.username is not None or parts.password is not None or parts.fragment or
            '\\' in value or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise ValueError('API URL 必须是有效的 HTTP 或 HTTPS 地址，不可包含鉴权信息。')
    sensitive_names = {'key', 'apikey', 'api_key', 'api-key', 'access_token', 'token',
                       'authorization', 'password', 'secret', 'auth', 'credential', 'signature'}
    for name, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if urllib.parse.unquote(name).lower() in sensitive_names:
            raise ValueError('API URL 不能包含密钥或鉴权查询参数。')
    if contains_secret(value, secrets):
        raise ValueError('请将 API Key 仅填写在密钥字段中。')
    return value


def header_secrets(headers):
    out = []
    for name, value in (headers or {}).items():
        if not isinstance(value, str):
            continue
        if name.lower() == 'authorization':
            out.append(value.split(' ', 1)[-1])
        elif name.lower() in ('x-api-key', 'x-goog-api-key'):
            out.append(value)
    return _secrets(out)


class StreamRedactor:
    """Keep only a bounded suffix per text channel until a secret can be ruled out.

    ``push(event)`` returns zero or more safe events; ``finish()`` flushes safe
    tails. A result event flushes tails before its already sanitized metadata.
    Never forward the unsanitized event in addition to the returned events.
    """
    def __init__(self, secrets):
        self.secrets = _secrets(secrets)
        self.forms = tuple(dict.fromkeys(form for secret in self.secrets for form in _forms(secret)))
        self.pending = {}

    def push(self, event):
        if not isinstance(event, dict):
            return [sanitize_public(event, self.secrets)]
        kind = event.get('type')
        if kind not in ('content', 'reasoning') or not isinstance(event.get('text'), str):
            flushed = self.finish() if kind == 'result' else []
            return flushed + [sanitize_public(event, self.secrets)]
        previous = self.pending.get(kind, ({}, ''))[1]
        text = previous + event['text']
        cut = len(text)
        # Retain only a suffix that could actually be the beginning of a key;
        # ordinary text is emitted immediately rather than delayed by key length.
        for form in self.forms:
            subject, needle = (text.lower(), form.lower()) if '%' in form else (text, form)
            start = subject.rfind(needle[:1], max(0, len(text) - len(form) + 1))
            while start >= 0:
                if needle.startswith(subject[start:]):
                    cut = min(cut, start)
                start = subject.rfind(needle[:1], max(0, len(text) - len(form) + 1), start)
        for form in self.forms:
            for match in re.finditer(re.escape(form), text, re.I if '%' in form else 0):
                if match.start() < cut < match.end():
                    cut = match.start()
                    break
        self.pending[kind] = (event, text[cut:])
        if not cut:
            return []
        return [sanitize_public({**event, 'text': text[:cut]}, self.secrets)]

    def finish(self):
        out = [sanitize_public({**event, 'text': text}, self.secrets)
               for event, text in self.pending.values() if text]
        self.pending.clear()
        return out
