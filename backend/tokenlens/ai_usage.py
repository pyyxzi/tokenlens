"""Per-HTTP-call accounting. The callback receives only an explicit metadata allowlist."""
from __future__ import annotations

import contextvars
import contextlib
import functools
import inspect
import json
import logging
import threading
import time
import urllib.parse
import uuid

_context = contextvars.ContextVar("tokenlens_ai_usage", default=None)
_attempt = contextvars.ContextVar("tokenlens_ai_http_attempt", default=None)
_log = logging.getLogger("tokenlens.ai_usage")


def context_thread(*, target, args=(), kwargs=None, **options):
    context = contextvars.copy_context()
    return threading.Thread(target=context.run, args=(target, *args),
                            kwargs=kwargs or {}, **options)


def operation(name):
    def decorate(fn):
        signature = inspect.signature(fn)
        @functools.wraps(fn)
        def invoke(self, *args, **kwargs):
            current = _context.get()
            if current is not None:
                return fn(self, *args, **kwargs)
            bound = signature.bind(self, *args, **kwargs)
            token = _context.set({"callback": getattr(self, "usage_callback", None),
                                  "operation": name, "session": uuid.uuid4().hex,
                                  "model": bound.arguments.get("model") or
                                      (bound.arguments.get("connection") or {}).get("model") or self.data.get("model", "")})
            try:
                return fn(self, *args, **kwargs)
            finally:
                _context.reset(token)
        return invoke
    return decorate


def mark_http_attempt():
    active = _attempt.get()
    if active is not None:
        active["attempted"] = True


@contextlib.contextmanager
def call_role(role):
    token = _context.set(dict(_context.get() or {}, role=role))
    try:
        yield
    finally:
        _context.reset(token)


def tracked_http(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def invoke(*args, **kwargs):
        context = _context.get()
        if not context or not context.get("callback"):
            return fn(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        params = bound.arguments
        url = params.get("url") or params.get("base_url") or ""
        payload = params.get("payload") or {}
        event = {"event_id": uuid.uuid4().hex, "ts": int(time.time()),
                 "session": context["session"], "operation": context["operation"],
                 "model": payload.get("model") or context.get("model") or "unknown",
                 "provider": urllib.parse.urlsplit(url).hostname or "unknown",
                 "request_type": fn.__name__, "usage_known": False}
        event["request_role"] = context.get("role", "primary")
        attempt = {"attempted": False}
        token = _attempt.set(attempt)
        started = time.monotonic()
        result = None
        try:
            result = fn(*args, **kwargs)
            return result
        finally:
            _attempt.reset(token)
            if attempt["attempted"]:
                if isinstance(result, dict):
                    status, error_kind = result.get("status"), result.get("error_kind", "")
                    usage = {}
                else:
                    status = getattr(result, "status", None)
                    error_kind = getattr(result, "error_kind", "")
                    usage = getattr(result, "usage", {}) or {}
                    event["model"] = getattr(result, "model_echo", "") or event["model"]
                event.update({"status": status, "usage": usage,
                              "usage_known": "input" in usage and "output" in usage,
                              "outcome": ("cancelled" if error_kind == "cancelled" else
                                          "failed" if error_kind or status is None or status >= 400 else "completed"),
                              "elapsed_ms": round((time.monotonic() - started) * 1000)})
                try:
                    context["callback"](event)
                except Exception as exc:
                    # Avoid exception messages: database/network errors can carry private values.
                    _log.error("Could not record an AI HTTP call (%s)", type(exc).__name__)
    return invoke
