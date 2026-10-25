"""Bounded, credential-free model catalogs for saved CC Switch connections.

Only the connection ID and a whitelisted wire protocol come from the browser.
Each operation rereads the saved endpoint and credential. Listing models sends
GET requests only: inclusion in a catalog is not proof of inference capability.
"""
from __future__ import annotations

import http.client
import io
import base64
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ccswitch
from .ai_usage import tracked_http
from .promptlab import _http_events, _TransportTimeout, resolve_endpoint


MAX_MODELS = 1000
MAX_PAGES = 8
MAX_PAGE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
TOTAL_TIMEOUT = 15.0
PAGE_SIZE = 200
PROTOCOLS = frozenset({"openai", "responses", "anthropic", "gemini"})
_MESSAGES = {
    "auth_rejected": "服务商拒绝鉴权，请在 CC Switch 中检查 API Key。",
    "models_unsupported": "该服务商没有提供模型列表接口，可手动填写模型名称。",
    "http_error": "服务商暂时无法返回模型列表，请稍后重试。",
    "network_error": "无法连接该服务商，请检查网络和 CC Switch 中的接口地址。",
    "timeout": "获取模型列表超时，请重试。",
    "invalid_response": "服务商返回的模型列表格式无效，可手动填写模型名称。",
    "response_too_large": "模型列表响应过大，已停止读取；可手动填写模型名称。",
    "catalog_limit": "模型列表较多，已显示本次获取范围内的模型。",
    "invalid_pagination": "服务商的分页信息无效，当前只显示已取得的模型。",
    "unsupported_pagination": "服务商使用了不支持的分页格式，当前只显示已取得的模型。",
    "entries_omitted": "部分模型信息无效，已显示可读取的模型。",
    "redirect_rejected": "模型列表被重定向到其他服务，已停止获取，请检查接口地址。",
}


def resolve_for_test(connection_id: str, model: str = "", protocol: str = "") -> dict:
    """Server-only test parameters, with endpoint and key bound to local storage."""
    return ccswitch.resolve(connection_id, model, protocol=protocol)


def _models_url(base: str, protocol: str) -> str:
    # Derive the same gateway/version prefix used by inference, including saved
    # addresses that already contain another protocol's terminal API route.
    parts = urllib.parse.urlsplit(resolve_endpoint(base, protocol, "__catalog"))
    suffix = {"openai": "/chat/completions", "responses": "/responses",
              "anthropic": "/messages", "gemini": "/models/__catalog:generateContent"}[protocol]
    path = parts.path[:-len(suffix)] + "/models"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _secret_echo(value: str, key: str) -> bool:
    """Reject key echoes even when a provider puts URL/base64 escapes in a field."""
    decoded = value
    for _ in range(2):
        decoded = urllib.parse.unquote(decoded)
    encoded = {base64.b64encode(key.encode()).decode(),
               base64.urlsafe_b64encode(key.encode()).decode()} if key else set()
    return bool(key and (key in value or key in decoded or
                        any(secret in value or secret.rstrip("=") in value for secret in encoded))) or bool(
        re.search(r"(?i)(?:sk-|AIza)[A-Za-z0-9_-]{12,}", decoded))


def _safe_text(value, key: str, limit: int, *, model=False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        return ""
    value = value.strip()
    if not value or not value.isprintable() or _secret_echo(value, key):
        return ""
    if model and any(c.isspace() for c in value):
        return ""
    return value


def _page_models(obj, protocol: str, key: str):
    if isinstance(obj, list):
        entries = obj
    elif isinstance(obj, dict) and isinstance(obj.get("data"), list):
        entries = obj["data"]
    elif isinstance(obj, dict) and isinstance(obj.get("models"), list):
        entries = obj["models"]
    else:
        raise ValueError("invalid_response")
    models, omitted = [], False
    for entry in entries:
        if isinstance(entry, str):
            model_id, group = entry, ""
        elif isinstance(entry, dict):
            model_id = entry.get("id") or entry.get("name")
            group = entry.get("owned_by") or entry.get("provider") or entry.get("group") or ""
        else:
            omitted = True
            continue
        if protocol == "gemini" and isinstance(model_id, str) and model_id.startswith("models/"):
            model_id = model_id[7:]
        model_id = _safe_text(model_id, key, 256, model=True)
        clean_group = _safe_text(group, key, 80)
        if not model_id:
            omitted = True
            continue
        if group and not clean_group:
            omitted = True
        models.append({"id": model_id, "group": clean_group})
    return models, omitted


def _next_page(obj, protocol: str, key: str):
    """Return a safe opaque cursor, never follow a URL returned by a provider."""
    if not isinstance(obj, dict):
        return None, ""
    more = obj.get("has_more", False)
    if not isinstance(more, bool):
        return None, "invalid_pagination"
    token = obj.get("nextPageToken")
    if token is not None and not isinstance(token, str):
        return None, "invalid_pagination"
    if token:
        if protocol != "gemini":
            return None, "unsupported_pagination"
        safe_token = _safe_text(token, key, 2048)
        return (("pageToken", safe_token), "") if safe_token else (None, "invalid_pagination")
    if more:
        cursor = _safe_text(obj.get("last_id"), key, 256, model=True)
        return (("after_id", cursor), "") if cursor else (None, "invalid_pagination")
    links = obj.get("links")
    if obj.get("next") or obj.get("next_url") or obj.get("next_cursor") \
            or isinstance(links, dict) and links.get("next"):
        return None, "unsupported_pagination"
    return None, ""


def _fetch_page(request, limit, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    events = _http_events(request, request.full_url, remaining, max_body_bytes=limit)
    body, status, expected = bytearray(), 0, None
    try:
        for kind, value in events:
            if kind == 'headers':
                status = value.status
                if not 200 <= status < 300:
                    raise urllib.error.HTTPError(request.full_url, status, 'model catalog request failed', value.headers, io.BytesIO())
                declared = value.headers.get('Content-Length')
                if declared is not None:
                    if not str(declared).isdigit():
                        raise ValueError('invalid_response')
                    expected = int(declared)
                    if expected > limit:
                        raise OverflowError
            else:
                body.extend(value)
    except _TransportTimeout:
        raise TimeoutError from None
    except ValueError as exc:
        if 'maximum body size' in str(exc):
            raise OverflowError from None
        raise
    finally:
        events.close()
    if expected is not None and len(body) != expected:
        raise ValueError('invalid_response')
    return status, bytes(body)


@tracked_http
def discover(connection_id: str, protocol: str = "") -> dict:
    """Fetch a bounded public model catalog from a saved local connection.

    ``ok`` means at least one complete valid catalog page was read. ``partial``
    means the result is incomplete, including dropped invalid entries. Neither
    ``ok`` nor model inclusion establishes compatibility with inference tests.
    All messages are fixed text; provider errors and response URLs never leave
    this function. Resolution errors raise ``CCSwitchError`` before any request.
    """
    saved = ccswitch.resolve(connection_id, protocol=protocol, allow_missing_model=True)
    proto, key = saved["protocol"], saved["api_key"]
    url = _models_url(saved["base_url"], proto)
    out = {"ok": False, "supported": False, "source": "remote", "protocol": proto,
           "models": [], "count": 0, "status": 0, "ms": 0, "pages": 0,
           "partial": False, "truncated": False, "error": "", "message": ""}
    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT
    seen_ids, seen_cursors, byte_count = set(), set(), 0
    cursor = None
    omitted = False
    try:
        for _ in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            query = []
            if proto == "gemini":
                query.append(("pageSize", str(PAGE_SIZE)))
            elif proto == "anthropic":
                query.append(("limit", str(PAGE_SIZE)))
            if cursor:
                query.append(cursor)
            page_url = url + ("?" + urllib.parse.urlencode(query) if query else "")
            req = urllib.request.Request(page_url, method="GET", headers={
                "Accept": "application/json", "User-Agent": "TokenLens model catalog"})
            if proto == "anthropic":
                req.add_header("x-api-key", key)
                req.add_header("anthropic-version", "2023-06-01")
            elif proto == "gemini":
                req.add_header("x-goog-api-key", key)
            else:
                req.add_header("Authorization", "Bearer " + key)
            limit = min(MAX_PAGE_BYTES, MAX_TOTAL_BYTES - byte_count)
            if limit <= 0:
                raise OverflowError
            status, body = _fetch_page(req, limit, deadline)
            out["status"] = status
            out["supported"] = True
            byte_count += len(body)
            try:
                obj = json.loads(body.decode("utf-8"))
                models, page_omitted = _page_models(obj, proto, key)
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError("invalid_response") from None
            out["pages"] += 1
            out["ok"] = True
            omitted = omitted or page_omitted
            for model in models:
                if model["id"] in seen_ids:
                    continue
                if len(out["models"]) >= MAX_MODELS:
                    out.update(partial=True, truncated=True, error="catalog_limit")
                    break
                seen_ids.add(model["id"])
                out["models"].append(model)
            if out["truncated"]:
                break
            cursor, pagination_error = _next_page(obj, proto, key)
            if pagination_error:
                out.update(partial=True, error=pagination_error)
                break
            if cursor is None:
                break
            if cursor in seen_cursors:
                out.update(partial=True, error="invalid_pagination")
                break
            seen_cursors.add(cursor)
            if len(out["models"]) >= MAX_MODELS:
                out.update(partial=True, truncated=True, error="catalog_limit")
                break
        else:
            out.update(partial=True, truncated=True, error="catalog_limit")
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        if exc.code in (401, 403):
            error = "auth_rejected"
        elif exc.code in (404, 405, 501):
            error = "models_unsupported"
        elif 300 <= exc.code < 400:
            error = "redirect_rejected"
        else:
            error = "http_error"
        out["supported"] = exc.code not in (404, 405, 501)
        out["error"] = error
        out["partial"] = bool(out["pages"])
        exc.close()
    except OverflowError:
        out.update(partial=True, truncated=True, error="response_too_large")
    except (TimeoutError, socket.timeout):
        out.update(partial=bool(out["pages"]), error="timeout")
    except urllib.error.URLError as exc:
        error = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network_error"
        out.update(partial=bool(out["pages"]), error=error)
    except (OSError, http.client.HTTPException):
        out.update(partial=bool(out["pages"]), error="network_error")
    except ValueError:
        out.update(partial=bool(out["pages"]), error="invalid_response")
    if omitted:
        out["partial"] = True
        out["error"] = out["error"] or "entries_omitted"
    out["count"] = len(out["models"])
    out["ms"] = round((time.monotonic() - started) * 1000)
    out["message"] = _MESSAGES.get(out["error"], "")
    return out
