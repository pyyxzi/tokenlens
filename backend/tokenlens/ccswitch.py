"""Read-only access to CC Switch connections, without exporting credentials.

The database is queried on demand: no watcher, copy, migration, or persistent
secret cache is created. ``catalog`` is safe for the browser; ``resolve`` is
server-only and deliberately rereads the saved provider before each operation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.parse

try:
    import tomllib
except ImportError:  # Python 3.10 is a supported, dependency-free build target.
    tomllib = None


MAX_PROVIDERS = 512
MAX_CONFIG_BYTES = 1024 * 1024
MAX_TOTAL_CONFIG_BYTES = 8 * 1024 * 1024
MAX_MODELS = 64
SUPPORTED_APPS = {"claude", "claude-desktop", "codex", "gemini", "opencode", "mcode"}
_FORMATS = {"anthropic": "anthropic", "openai_chat": "openai",
            "openai_responses": "responses", "gemini_native": "gemini",
            "openai": "openai", "responses": "responses", "gemini": "gemini"}
_MESSAGES = {
    "not_found": "未找到 CC Switch 配置，请先在 CC Switch 中添加供应商。",
    "unreadable": "暂时无法读取 CC Switch 配置，请刷新重试。",
    "invalid_schema": "CC Switch 数据库结构不兼容，请升级 CC Switch 后重试。",
    "invalid_config": "该供应商配置格式无法读取，请在 CC Switch 中检查配置。",
    "unsupported_app": "该应用类型暂不支持直接检测。",
    "unsupported_protocol": "该供应商的接口协议暂不支持直接检测。",
    "unsafe_endpoint": "接口地址含鉴权信息或格式无效，请在 CC Switch 中使用独立 API Key。",
    "missing_key": "未配置 API Key；登录授权型供应商请改用 API Key 连接。",
    "missing_model": "请选择或填写要检测的模型。",
    "invalid_model": "模型名称格式无效。",
    "connection_not_found": "该 CC Switch 连接已变更或删除，请刷新连接列表。",
}


class CCSwitchError(ValueError):
    """A fixed, credential-free message suitable for a local API response."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES.get(code, _MESSAGES["invalid_config"]))


def _json(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise CCSwitchError("invalid_config")
    try:
        result = json.loads(value)
    except (ValueError, RecursionError):
        raise CCSwitchError("invalid_config") from None
    if not isinstance(result, dict):
        raise CCSwitchError("invalid_config")
    return result


def _string(value, limit=4096):
    return value.strip() if isinstance(value, str) and len(value) <= limit else ""


def _home() -> Path:
    override = _string(os.environ.get("CCSWITCH_HOME"))
    if override:
        return Path(override).expanduser()
    user_home = Path.home()
    # CC Switch's Tauri Store is outside the provider directory. Only inspect
    # fixed application-owned paths, never a path supplied by an HTTP request.
    if os.name == "nt":
        stores = [Path(os.environ.get("APPDATA", user_home / "AppData/Roaming"))
                  / "com.ccswitch.desktop/app_paths.json"]
    elif sys.platform == "darwin":
        stores = [user_home / "Library/Application Support/com.ccswitch.desktop/app_paths.json"]
    else:
        stores = [Path(os.environ.get("XDG_DATA_HOME", user_home / ".local/share"))
                  / "com.ccswitch.desktop/app_paths.json"]
    for store in stores:
        try:
            if store.stat().st_size > MAX_CONFIG_BYTES:
                continue
            settings = _json(store.read_text(encoding="utf-8"))
            configured = _string(settings.get("app_config_dir_override"))
            candidate = Path(configured).expanduser() if configured else None
            if candidate and candidate.is_dir():
                return candidate
        except (OSError, CCSwitchError):
            continue
    default = user_home / ".cc-switch"
    if os.name == "nt" and not (default / "cc-switch.db").is_file():
        legacy = _string(os.environ.get("HOME"))
        candidate = Path(legacy) / ".cc-switch" if legacy else None
        if candidate and (candidate / "cc-switch.db").is_file():
            return candidate
    return default


def _read_providers():
    home = _home()
    db = home / "cc-switch.db"
    if db.is_file():
        conn = None
        try:
            # Do not use immutable=1: it would ignore committed WAL contents.
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.75)
            conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            columns = {r[1] for r in conn.execute("PRAGMA table_info(providers)")}
            if not {"id", "app_type", "name", "settings_config"}.issubset(columns):
                raise CCSwitchError("invalid_schema")
            optional = (",CASE WHEN length(CAST(meta AS BLOB))<=1048576 THEN meta ELSE NULL END AS meta"
                        if "meta" in columns else ", '{}' AS meta")
            optional += ",is_current" if "is_current" in columns else ",0 AS is_current"
            # Apply byte limits inside SQL, before SQLite materializes JSON.
            # A corrupted oversized cell must not allocate unbounded memory.
            conn.set_progress_handler(lambda: 1, 1_000_000)
            cursor = conn.execute(
                "SELECT substr(id,1,4096) AS id,substr(app_type,1,64) AS app_type,"
                "substr(name,1,161) AS name,CASE WHEN length(CAST(settings_config AS BLOB))<=? "
                "THEN settings_config ELSE NULL END AS settings_config" + optional +
                " FROM providers ORDER BY app_type,name,id LIMIT ?",
                (MAX_CONFIG_BYTES, MAX_PROVIDERS + 1,))
            rows, byte_count, truncated = [], 0, False
            for record in cursor:
                row = dict(record)
                byte_count += sum(len(v.encode("utf-8")) for v in row.values() if isinstance(v, str))
                if len(rows) >= MAX_PROVIDERS or byte_count > MAX_TOTAL_CONFIG_BYTES:
                    truncated = True
                    break
                rows.append(row)
            return rows, "database", truncated
        except CCSwitchError:
            raise
        except (sqlite3.Error, OSError, ValueError):
            raise CCSwitchError("unreadable") from None
        finally:
            if conn:
                conn.close()
    legacy = home / "config.json"
    if not legacy.is_file():
        raise CCSwitchError("not_found")
    try:
        if legacy.stat().st_size > MAX_CONFIG_BYTES:
            raise CCSwitchError("invalid_config")
        data = _json(legacy.read_text(encoding="utf-8"))
        rows = []
        for app in sorted(SUPPORTED_APPS):
            manager = data.get(app)
            if not isinstance(manager, dict):
                continue
            providers = manager.get("providers", {})
            if not isinstance(providers, dict):
                continue
            for provider_id, provider in providers.items():
                if not isinstance(provider, dict):
                    continue
                rows.append({"id": str(provider_id), "app_type": app,
                             "name": provider.get("name", app),
                             "settings_config": provider.get("settingsConfig", {}),
                             "meta": provider.get("meta", {}),
                             "is_current": provider_id == manager.get("current")})
                if len(rows) > MAX_PROVIDERS:
                    return rows[:MAX_PROVIDERS], "legacy", True
        return rows, "legacy", False
    except (OSError, UnicodeError):
        raise CCSwitchError("unreadable") from None


def _connection_id(row):
    # CC Switch IDs are only unique within an app. Neither raw IDs nor paths
    # need to leave the server, and the length-prefixed tuple cannot collide.
    parts = [str(row.get("app_type", "")), str(row.get("id", ""))]
    raw = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return "ccs_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _toml_string(value):
    value = value.strip()
    if value.startswith("'"):
        end = value.find("'", 1)
        if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#")):
            raise CCSwitchError("invalid_config")
        return value[1:end]
    if not value.startswith('"'):
        raise CCSwitchError("invalid_config")
    # TOML basic strings share JSON escapes plus \UXXXXXXXX.
    value = re.sub(r"\\U([0-9a-fA-F]{8})", lambda m: chr(int(m.group(1), 16)), value)
    try:
        parsed, end = json.JSONDecoder().raw_decode(value)
    except (ValueError, OverflowError):
        raise CCSwitchError("invalid_config") from None
    if not isinstance(parsed, str) or (value[end:].strip() and not value[end:].lstrip().startswith("#")):
        raise CCSwitchError("invalid_config")
    return parsed


def _toml_path(value):
    pattern = r"(?:[A-Za-z0-9_-]+|'[^']*'|\"(?:[^\"\\]|\\.)*\")"
    matches = list(re.finditer(pattern, value))
    if not matches:
        return ()
    cursor = 0
    result = []
    for index, match in enumerate(matches):
        gap = value[cursor:match.start()].strip()
        if gap != ("" if index == 0 else "."):
            return ()
        raw = match.group()
        result.append(_toml_string(raw) if raw[0] in "'\"" else raw)
        cursor = match.end()
    return tuple(result) if not value[cursor:].strip() else ()


def _connection_toml(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise CCSwitchError("invalid_config")
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except (ValueError, RecursionError):
            raise CCSwitchError("invalid_config") from None
    # On 3.10 extract only scalar connection settings. Unrelated arrays, inline
    # tables and multiline tool instructions are intentionally never interpreted.
    result, table, multiline = {}, (), None
    wanted = {"model", "model_provider", "base_url", "wire_api", "env_key", "api_key", "profile"}
    for line in text.splitlines():
        stripped = line.strip()
        if multiline:
            if multiline in stripped:
                multiline = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            found = re.match(r"^\[([^\n]+)\]\s*(?:#.*)?$", stripped)
            table = (_toml_path(found.group(1)) if found else ()) or ("__ignored__",)
            continue
        found = re.match(r"^([^=]+?)\s*=\s*(.*)$", stripped)
        if not found:
            continue
        path = _toml_path(found.group(1))
        value = found.group(2).strip()
        if value.startswith(('"""', "'''")):
            delimiter = value[:3]
            if delimiter not in value[3:]:
                multiline = delimiter
            if path and path[-1] in wanted and (not table or table[0] in {"model_providers", "profiles"}):
                raise CCSwitchError("invalid_config")
            continue
        full = table + path
        if not path or path[-1] not in wanted or (table and table[0] not in {"model_providers", "profiles"}):
            continue
        parsed = _toml_string(value)
        target = result
        for part in full[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise CCSwitchError("invalid_config")
        if full[-1] in target:
            raise CCSwitchError("invalid_config")
        target[full[-1]] = parsed
    return result


def _safe_url(raw, key=""):
    raw = _string(raw)
    if not raw or any(ord(c) < 33 for c in raw) or "\\" in raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None \
                or parts.password is not None or parts.query or parts.fragment:
            return ""
        parts.port  # Validate port syntax and range.
        decoded = urllib.parse.unquote(raw)
        if key and (key in raw or key in decoded):
            return ""
        if re.search(r"(?i)(?:sk-|AIza)[A-Za-z0-9_-]{12,}", decoded):
            return ""
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    except ValueError:
        return ""


def _model(value):
    value = _string(value, 256)
    return value if value and not any(ord(c) < 33 or ord(c) == 127 for c in value) else ""


def _extract(row):
    app = _string(row.get("app_type"), 64)
    if app not in SUPPORTED_APPS:
        raise CCSwitchError("unsupported_app")
    config = _json(row.get("settings_config"))
    meta = _json(row.get("meta") or "{}")
    models, key, raw_url = [], "", ""
    protocol = ""
    if app in {"claude", "claude-desktop"}:
        env = config.get("env", {})
        if not isinstance(env, dict):
            raise CCSwitchError("invalid_config")
        key = _string(env.get("ANTHROPIC_API_KEY")) or _string(env.get("ANTHROPIC_AUTH_TOKEN"))
        raw_url = _string(env.get("ANTHROPIC_BASE_URL")) or "https://api.anthropic.com"
        protocol = "anthropic"
        models = [env.get("ANTHROPIC_MODEL")]
        models += [value for name, value in sorted(env.items())
                   if re.fullmatch(r"ANTHROPIC_DEFAULT_[A-Z0-9_]+_MODEL", name)]
    elif app == "gemini":
        env = config.get("env", {})
        if not isinstance(env, dict):
            raise CCSwitchError("invalid_config")
        key = _string(env.get("GEMINI_API_KEY")) or _string(env.get("GOOGLE_API_KEY"))
        raw_url = _string(env.get("GOOGLE_GEMINI_BASE_URL")) or _string(env.get("GEMINI_BASE_URL")) \
            or "https://generativelanguage.googleapis.com/v1beta"
        protocol = "gemini"
        models = [env.get("GEMINI_MODEL"), env.get("GOOGLE_GEMINI_MODEL")]
    elif app == "codex":
        auth = config.get("auth", {})
        if not isinstance(auth, dict):
            raise CCSwitchError("invalid_config")
        parsed = _connection_toml(config.get("config", ""))
        profile = _string(parsed.get("profile"), 256)
        profiles = parsed.get("profiles", {})
        selected = profiles.get(profile, {}) if isinstance(profiles, dict) else {}
        if not isinstance(selected, dict):
            selected = {}
        active = _string(selected.get("model_provider")) or _string(parsed.get("model_provider"))
        providers = parsed.get("model_providers", {})
        provider = providers.get(active, {}) if isinstance(providers, dict) else {}
        if not isinstance(provider, dict):
            raise CCSwitchError("invalid_config")
        # A missing named provider must never accidentally call the official API.
        if active and active != "openai" and not provider:
            raise CCSwitchError("invalid_config")
        raw_url = _string(provider.get("base_url")) or _string(parsed.get("base_url")) \
            or ("https://api.openai.com/v1" if not active or active == "openai" else "")
        env_key = _string(provider.get("env_key"), 256)
        key = _string(auth.get("OPENAI_API_KEY")) or _string(provider.get("api_key")) \
            or (_string(os.environ.get(env_key)) if env_key else "")
        wire = _string(provider.get("wire_api")) or _string(parsed.get("wire_api")) or "responses"
        protocol = {"responses": "responses", "chat": "openai", "chat_completions": "openai"}.get(wire, "")
        models = [selected.get("model") or parsed.get("model")]
    else:
        options = config.get("options", {})
        if not isinstance(options, dict):
            raise CCSwitchError("invalid_config")
        key = _string(options.get("apiKey"))
        raw_url = _string(options.get("baseURL"))
        npm = _string(config.get("npm"), 256)
        protocol = {"@ai-sdk/openai": "openai", "@ai-sdk/openai-compatible": "openai",
                    "@ai-sdk/anthropic": "anthropic", "@ai-sdk/google": "gemini"}.get(npm, "")
        configured_models = config.get("models", {})
        if isinstance(configured_models, dict):
            models = list(configured_models)[:MAX_MODELS]
    fmt = _string(meta.get("apiFormat"), 64)
    if fmt and app != "codex":
        protocol = _FORMATS.get(fmt, "")
    safe_url = _safe_url(raw_url, key)
    choices = []
    for value in models:
        name = _model(value)
        if name and name not in choices and (not key or key not in name):
            choices.append(name)
            if len(choices) >= MAX_MODELS:
                break
    error = "unsupported_protocol" if not protocol else "unsafe_endpoint" if not safe_url else "missing_key" if not key else ""
    return {"model": choices[0] if choices else "", "models": choices,
            "base_url": safe_url, "api_key": key, "protocol": protocol, "error": error}


def catalog() -> dict:
    """Return a bounded public catalog. This function never returns API keys."""
    try:
        rows, source, truncated = _read_providers()
    except CCSwitchError as exc:
        return {"available": False, "source": "ccswitch", "connections": [],
                "error": exc.code, "message": str(exc), "truncated": False}
    connections = []
    for row in rows:
        app = _string(row.get("app_type"), 64)
        if app not in SUPPORTED_APPS:
            continue
        try:
            extracted = _extract(row)
            error = extracted["error"]
        except (CCSwitchError, RecursionError, TypeError, ValueError) as exc:
            error = exc.code if isinstance(exc, CCSwitchError) else "invalid_config"
            extracted = {"model": "", "models": [], "base_url": "", "api_key": "", "protocol": ""}
        name = _string(row.get("name"), 160) or app
        key = extracted["api_key"]
        if key and key in name:
            name = app
        name = re.sub(r"[\x00-\x1f\x7f]", " ", name)
        connections.append({"id": _connection_id(row), "name": name, "app_type": app,
                            "model": extracted["model"], "models": extracted["models"],
                            "base_url": extracted["base_url"], "protocol": extracted["protocol"],
                            "has_key": bool(key), "configured": not error,
                            "is_current": bool(row.get("is_current")),
                            "error": error, "reason": _MESSAGES.get(error, "")})
    return {"available": True, "source": source, "connections": connections,
            "error": "", "message": "", "truncated": truncated}


def resolve(connection_id: str, model: str = "", *, protocol: str = "",
            allow_missing_model: bool = False) -> dict:
    """Resolve a current saved provider for a server-side request only.

    Model overrides let the user test another model on the same configured
    endpoint. Provider IDs, paths, endpoints and credentials cannot be supplied
    through this API: they always come from CC Switch's trusted local store.
    ``protocol`` overrides only a whitelisted wire format. Model discovery uses
    ``allow_missing_model`` because GET /models does not require a default model.
    """
    if not isinstance(protocol, str) or (protocol and
                     protocol not in {"openai", "responses", "anthropic", "gemini"}):
        raise CCSwitchError("unsupported_protocol")
    if not isinstance(connection_id, str) or not re.fullmatch(r"ccs_[0-9a-f]{32}", connection_id):
        raise CCSwitchError("connection_not_found")
    rows, _, _ = _read_providers()
    row = next((row for row in rows if _connection_id(row) == connection_id), None)
    if row is None:
        raise CCSwitchError("connection_not_found")
    try:
        extracted = _extract(row)
    except CCSwitchError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise CCSwitchError("invalid_config") from None
    # A protocol choice changes only the wire format. Endpoints and credentials
    # are always reread from the local provider; an unsupported saved format can
    # be repaired in the picker without accepting an unsafe URL or missing key.
    if not extracted["base_url"]:
        raise CCSwitchError("unsafe_endpoint")
    if not extracted["api_key"]:
        raise CCSwitchError("missing_key")
    if not protocol and extracted["error"]:
        raise CCSwitchError(extracted["error"])
    selected = _model(model) if model else extracted["model"]
    if model and not selected:
        raise CCSwitchError("invalid_model")
    if not selected and not allow_missing_model:
        raise CCSwitchError("missing_model")
    if extracted["api_key"] in selected:
        raise CCSwitchError("invalid_model")
    return {"model": selected, "base_url": extracted["base_url"],
            "api_key": extracted["api_key"], "protocol": protocol or extracted["protocol"]}
