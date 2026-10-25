# -*- coding: utf-8 -*-
"""
tokenlens · 数据源适配器

每个适配器负责：定位某一款编程工具在本机的用量落盘位置 → 解析 → 归一化成 UsageRecord。

覆盖四类：
  · CLI 智能体    —— Claude Code / Codex CLI / Gemini CLI / OpenCode / Aider
  · 编辑器与插件  —— Cline(及 Roo/Kilo) / Continue / Copilot Chat / Cursor / Trae / Windsurf
  · 桌面端        —— WorkBuddy / DeepSeek Harness / Cherry Studio / LM Studio / Ollama
  · 通用兜底      —— 结构不固定的厂商扫描 + 大模型 API 日志

对于「用量仅在云端计量、本地不落盘」的工具（Cursor、Trae 等），
适配器仍然会探测安装痕迹并如实上报状态，避免统计口径出现黑箱。
"""
from __future__ import annotations

import csv
import contextlib
import glob
import io
import json
import logging
import os
import re
import sqlite3
import time
import threading
import hashlib
import ntpath
import posixpath
from pathlib import Path
from typing import Iterator, List, Optional

try:
    # DeepSeek Harness stores its authoritative per-call ledger as zstd JSONL.
    # Keep this optional so older installs can still scan projection caches.
    import zstandard as _zstandard
except Exception:  # pragma: no cover - depends on the host installation
    _zstandard = None

from .models import UsageRecord, SourceReport, Status, to_epoch, _i
from .pricing import estimate_cost

_LOG = logging.getLogger("tokenlens.adapters")
_PARSE_CONTEXT = threading.local()
# Semantic changes must invalidate unchanged source files on the next scan.
# The previous snapshot remains available until a replacement succeeds.
PARSER_VERSION = "11-mirror-and-crossfile-dedup"


def stable_event_id(tool: str, source: str, scope: str = "", explicit: str = "", payload=None) -> str:
    """Build a collision-resistant, replay-stable event identity.

    Request IDs are only unique within a provider/account/session in practice.
    Include the adapter, source file and scope so two unrelated API logs may
    legitimately reuse an ID while an unchanged line replayed during an
    incremental scan remains idempotent.  A canonical payload is used when a
    provider omitted an explicit request/message ID.
    """
    # An explicit request/message ID is authoritative; do not include mutable
    # response details in that case, otherwise a replay with a filled-in cost
    # would evade deduplication.  Payload hashing is the fallback for old logs.
    identity = str(explicit or "")
    if not identity:
        identity = json.dumps(payload if payload is not None else "", ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"), default=str)
    body = [str(tool or ""), os.path.abspath(str(source or "")), str(scope or ""), identity]
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    return "%s:%s" % (str(tool or "source"), digest[:40])


@contextlib.contextmanager
def parsing_diagnostics(cancelled=None):
    """Per-file diagnostics survive legacy adapters that catch decoding errors."""
    old = getattr(_PARSE_CONTEXT, "issues", None)
    old_cancelled = getattr(_PARSE_CONTEXT, "cancelled", None)
    issues = []
    _PARSE_CONTEXT.issues = issues
    _PARSE_CONTEXT.cancelled = cancelled
    try:
        yield issues
    finally:
        _PARSE_CONTEXT.issues = old
        _PARSE_CONTEXT.cancelled = old_cancelled


def check_parse_cancelled():
    cancelled = getattr(_PARSE_CONTEXT, "cancelled", None)
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled while reading source")


def source_connection(path):
    """A bounded read-only connection to a third-party, untrusted database."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    deadline = time.monotonic() + 60
    cancelled = getattr(_PARSE_CONTEXT, "cancelled", None)
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline or bool(cancelled and cancelled())), 10000)
    if hasattr(connection, "setlimit"):
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * 1024 * 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
    return connection


def walk_readable(root):
    try:
        os.stat(root)
    except FileNotFoundError:
        return iter(())
    def fail(exc):
        raise exc
    return os.walk(root, onerror=fail)


def file_fingerprint(path, dependencies=()):
    """Include WAL state and declared cross-file dependencies in freshness."""
    parts = [("parser_version", PARSER_VERSION)]
    paths = [path] + sorted(set(dependencies))
    if path.lower().endswith((".db", ".sqlite", ".sqlite3", ".vscdb")):
        paths.extend((path + "-wal", path + "-journal"))
    for p in paths:
        try:
            st = os.stat(p)
            parts.append((p, st.st_size, st.st_mtime_ns, file_change_time(p, st), st.st_dev, st.st_ino))
        except FileNotFoundError:
            parts.append((p, None))
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def file_change_time(path, stat):
    """Windows st_ctime is creation time; query NTFS change time for rewrites."""
    if os.name != "nt":
        return stat.st_ctime_ns
    import ctypes
    from ctypes import wintypes
    class BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
                    ("creation", "access", "write", "change")] + [("attributes", wintypes.DWORD)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    handle = create(path, 0x80, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "file change time unavailable")
    try:
        get_info = kernel.GetFileInformationByHandleEx
        get_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        get_info.restype = wintypes.BOOL
        info = BasicInfo()
        if not get_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "file change time unavailable")
        return info.change
    finally:
        close = kernel.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close(handle)

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")

MAX_TEXT_SCAN = 200 * 1024 * 1024   # 单文件上限，超过则跳过（防止误吞巨型二进制）


# ==========================================================================
# 基础工具
# ==========================================================================

def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def iter_lines(path: str, encoding: str = "utf-8", limit: Optional[int] = None):
    """流式按行读取，1MB 缓冲，二进制长行不会炸内存。"""
    with io.open(path, "r", encoding=encoding, errors="strict",
                 buffering=1 << 20) as fh:
        idx = 0
        while True:
            if limit is not None and idx >= limit:
                break
            check_parse_cancelled()
            line = fh.readline(16 * 1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 16 * 1024 * 1024:
                raise ValueError("source line exceeds 16 MiB characters; previous snapshot retained")
            idx += 1
            yield line


def safe_json(line: str):
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        issues = getattr(_PARSE_CONTEXT, "issues", None)
        if issues is not None:
            issues.append("invalid JSON usage record")
        return None


def dig(obj, *paths, default=None):
    """按多条候选路径取值（兼容不同版本字段名）。"""
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif isinstance(cur, list) and key.isdigit() and int(key) < len(cur):
                cur = cur[int(key)]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return default


def first_nested(obj, keys, depth=4):
    """在嵌套字典里找第一个命中的键。"""
    if depth <= 0 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            got = first_nested(v, keys, depth - 1)
            if got not in (None, "", [], {}):
                return got
    else:
        for v in obj:
            got = first_nested(v, keys, depth - 1)
            if got not in (None, "", [], {}):
                return got
    return None


def file_stat(path: str):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return -1, 0.0


def dir_size(paths) -> int:
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def newest_mtime(paths) -> float:
    best = 0.0
    for p in paths:
        try:
            best = max(best, os.path.getmtime(p))
        except OSError:
            pass
    return best


def glob_files(*patterns, recursive=True) -> List[str]:
    out: List[str] = []
    for pat in patterns:
        out.extend(glob.glob(expand(pat), recursive=recursive))
    seen, uniq = set(), []
    for p in out:
        if os.path.isfile(p) and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def norm_project(cwd: str) -> str:
    """Return a stable full-path identity without consulting the host filesystem.

    Imported Windows logs may be read on POSIX and vice versa. Windows drive
    and UNC paths are case-insensitive; POSIX paths retain case. Display aliases
    belong to managed_projects, never to this grouping key.
    """
    if not cwd:
        return ""
    text = str(cwd).strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("//?/"):
        text = text[4:]
        if text.lower().startswith("unc/"):
            text = "//" + text[4:]
    if re.match(r"^[A-Za-z]:", text) or text.startswith("//"):
        normalized = ntpath.normcase(ntpath.normpath(text)).replace("\\", "/").casefold()
        return normalized if re.match(r"^[a-z]:/$", normalized) else normalized.rstrip("/")
    return posixpath.normpath(text)


def is_mostly_binary(path: str, probe: int = 4096) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(probe)
    except OSError:
        return True
    if not chunk:
        return True
    if b"\x00" in chunk:
        return True
    return False


# ---- 隐私护栏 ----------------------------------------------------------
# 通用扫描会遍历第三方产品的数据目录，必须主动绕开凭据类文件，
# 既不误读密钥，也避免把「登录态」当成用量。
SECRET_DIRS = {"auth", "authentication", "credentials", "credential", "secrets",
               "cookies", "oauth", "oauth2", "keychain", "tokens", "session-keys",
               "login", "accounts", "auth-tokens"}
SECRET_STEMS = {"auth", "auth.json", "credentials", "credential", "secrets", "secret",
                "cookies", "login", "oauth", "id_rsa", "id_ed25519", "accesstoken",
                "access_token", "refresh_token", "apikey", "api_key", "keychain",
                "auth.json.backup", "known_hosts", "netrc"}
SECRET_HINTS = ("credential", "secret", "password", "passwd", "apikey", "api_key",
                "access_token", "refresh_token", "private_key", "authorization",
                "oauth", "keychain", "session_key", "sessionkey", "cookie")

# 本程序自身与同类宿主的数据目录，绝不扫描
SELF_DIRS = ("tokenlens",)


def is_secret_path(path: str) -> bool:
    parts = [p.lower() for p in str(path).replace("/", os.sep).split(os.sep) if p]
    if any(p in SECRET_DIRS for p in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    stem = name.rsplit(".", 1)[0]
    if name in SECRET_STEMS or stem in SECRET_STEMS:
        return True
    flat = name.replace("-", "_").replace(" ", "")
    if any(h in flat for h in SECRET_HINTS):
        return True
    return False


def is_self_path(path: str) -> bool:
    parts = [p.lower() for p in str(path).replace("/", os.sep).split(os.sep) if p]
    return any(p in SELF_DIRS for p in parts)


# ==========================================================================
# 适配器基类
# ==========================================================================

class Adapter:
    id = "base"
    name = "未命名"
    vendor = ""
    category = "cli"
    color = "#7c8cf8"
    glyph = "?"
    homepage = ""
    detail = ""
    # 即使能解析出 token，也希望额外上报侧写指标（会话数、日志数等）。
    # 仅对成本可控的适配器开启，避免每轮扫描都做全盘遍历。
    WANT_SIDE = False
    QUALITY = "exact"
    INCREMENTAL_JSONL = False

    def __init__(self, cfg):
        self.cfg = cfg

    # --- 需要子类实现 --------------------------------------------------
    def enumerate(self) -> List[str]:
        """返回本次需要扫描的文件列表。"""
        return []

    def parse(self, path: str) -> Iterator[UsageRecord]:
        return iter(())

    def probe(self) -> tuple[bool, str]:
        """返回 (是否检测到该工具, 说明)。用于「仅检测」型数据源。"""
        return False, ""

    # --- 通用实现 ------------------------------------------------------
    def roots(self) -> List[str]:
        return []

    def cache_dependencies(self, path):
        return ()

    def report_shell(self) -> SourceReport:
        return SourceReport(
            id=self.id, name=self.name, vendor=self.vendor, category=self.category,
            color=self.color, glyph=self.glyph, homepage=self.homepage, note=self.detail,
            quality=self.QUALITY,
        )


# ==========================================================================
# 1. Claude Code
# ==========================================================================

class ClaudeCodeAdapter(Adapter):
    id = "claude-code"
    name = "Claude Code"
    vendor = "Anthropic"
    category = "cli"
    color = "#d97757"
    glyph = "C"
    homepage = "https://claude.com/claude-code"
    detail = "读取 ~/.claude/projects/**/*.jsonl 中每条 assistant 消息的 usage 字段（含缓存读写）"
    INCREMENTAL_JSONL = True

    def enumerate(self):
        return glob_files(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"))

    def parse(self, path):
        yield from self.parse_lines(path, iter_lines(path), {})

    def parse_lines(self, path, lines, state):
        for line in lines:
            if '"usage"' not in line:
                continue
            o = safe_json(line)
            if not isinstance(o, dict) or o.get("type") != "assistant":
                continue
            msg = o.get("message") or {}
            u = msg.get("usage") or {}
            model = str(msg.get("model") or "")
            if not u or model == "<synthetic>":
                continue
            inp = _i(u.get("input_tokens"))
            out = _i(u.get("output_tokens"))
            cr = _i(u.get("cache_read_input_tokens"))
            cw = _i(u.get("cache_creation_input_tokens"))
            # Anthropic 的 thinking token 已包含在 output_tokens 内，不重复计入
            think = _i(dig(u, "output_tokens_details.thinking_tokens", default=0))
            if inp + out + cr + cw <= 0:
                continue
            # Claude message/request IDs are stable across log replays.  Keep
            # the session in the scope because provider IDs are not globally
            # unique.  Older rows without an ID still get a canonical line
            # identity, which deduplicates exact replays without token-based
            # collapsing of separate turns.
            explicit_id = (o.get("uuid") or o.get("requestId") or o.get("request_id")
                           or msg.get("id") or msg.get("uuid") or "")
            event_id = stable_event_id(
                self.id, path, str(o.get("sessionId") or ""), str(explicit_id),
                o)
            yield UsageRecord(
                tool=self.id, ts=to_epoch(o.get("timestamp")), model=model,
                project=norm_project(o.get("cwd") or ""), session=o.get("sessionId") or "",
                input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=0,
                cost=estimate_cost(model, inp, out, cr, cw), src=path,
                event_id=event_id,
                extra={"request_id": str(explicit_id), "cwd": o.get("cwd") or "", "branch": o.get("gitBranch") or "",
                       "version": o.get("version") or "", "thinking_tokens": think},
            )


# ==========================================================================
# 2. Codex CLI
# ==========================================================================

class CodexAdapter(Adapter):
    id = "codex"
    name = "Codex CLI"
    vendor = "OpenAI"
    category = "cli"
    color = "#10a37f"
    glyph = "⟩"
    homepage = "https://github.com/openai/codex"
    detail = "解析 Codex 会话的逐次调用用量，排除分支会话继承的历史；兼容旧版 token_count 日志"
    INCREMENTAL_JSONL = True

    def _state_db(self):
        for name in ("state_5.sqlite", "state_4.sqlite", "state.sqlite"):
            p = os.path.join(HOME, ".codex", name)
            if os.path.exists(p):
                return p
        return ""

    def enumerate(self):
        if self.cfg.get("codex_quick"):
            db = self._state_db()
            return [db] if db else []
        return glob_files(os.path.join(HOME, ".codex", "sessions", "**", "rollout-*.jsonl"))

    def scan_db(self, path):
        """快速模式：直接读 state_*.sqlite 的 threads.tokens_used。"""
        recs = []
        con = None
        try:
            con = source_connection(path)
            con.row_factory = sqlite3.Row
            for r in con.execute(
                "SELECT id, cwd, model, tokens_used, created_at, updated_at, title, source, cli_version "
                "FROM threads WHERE tokens_used > 0"
            ):
                ts = to_epoch(r["updated_at"])
                model = r["model"] or "codex"
                total = _i(r["tokens_used"])
                recs.append(UsageRecord(
                    tool=self.id, ts=ts, model=model, project=norm_project(r["cwd"] or ""),
                    session=r["id"] or "", input=total, output=0,
                    cost=estimate_cost(model, total, 0), src=path,
                    extra={"quick": True, "title": r["title"] or "", "cwd": r["cwd"] or ""},
                ))
        finally:
            if con is not None:
                con.close()
        return recs

    def parse(self, path):
        yield from self.parse_lines(path, iter_lines(path), {})

    def parse_lines(self, path, lines, state):
        cwd = state.get("cwd", "")
        project = norm_project(cwd)
        project_cwd = cwd
        absolute_path = os.path.abspath(path)
        model = state.get("model", "codex")
        provider = state.get("provider", "")
        version = state.get("version", "")
        # 会话 ID 可从文件名尾部取：rollout-2026-04-04T22-27-45-<uuid>.jsonl
        base = os.path.basename(path)
        parts = base.replace("rollout-", "").replace(".jsonl", "").split("-")
        session = state.get("session") or ("-".join(parts[-5:]) if len(parts) >= 5 else base)
        owner = state.get("owner", "")
        active_thread = state.get("active_thread", "")
        response_ledger = state.get("response_ledger", False)
        previous_totals = state.get("previous_totals")
        # The scanner persists exact event IDs in SQLite instead of growing a
        # Python set/cursor with every request. Standalone parsing keeps its API.
        seen_events = None if state.get("database_dedup") else set()
        token_keys = ("input_tokens", "cached_input_tokens", "output_tokens",
                      "cache_write_input_tokens", "reasoning_output_tokens")
        def checkpoint():
            nonlocal project, project_cwd
            if cwd != project_cwd:
                project, project_cwd = norm_project(cwd), cwd
            state.update(cwd=cwd, model=model, provider=provider, version=version,
                         session=session, previous_totals=previous_totals,
                         owner=owner, active_thread=active_thread,
                         response_ledger=response_ledger)
        def remember_unrecorded(event_id):
            # Equal/zero counters produce no database row. A small exact
            # cursor set covers their replays; excessive state switches the
            # next change to a full parse instead of weakening correctness.
            if state.get("database_dedup"):
                ignored = state.setdefault("unrecorded_event_ids", [])
                if len(ignored) < 512:
                    ignored.append(event_id)
                else:
                    state["unrecorded_events_overflow"] = True

        for line in lines:
            if '"session_meta"' in line:
                o = safe_json(line)
                if not isinstance(o, dict) or o.get("type") != "session_meta":
                    continue
                pl = (o or {}).get("payload") or {}
                # session_id may identify the root conversation for every
                # subagent; id is the owner of this rollout/thread.
                thread = pl.get("id") or pl.get("session_id") or ""
                if owner:
                    # Forks contain a second session_meta and a replay of the
                    # parent's history with freshly written timestamps. Those
                    # rows belong to the parent, even when copied today.
                    active_thread = str(thread or active_thread)
                    checkpoint()
                    continue
                owner = str(thread or session)
                # Older forks omit the copied parent's session_meta, but
                # still begin with its replayed counters before switching
                # thread_settings_applied to the new owner.
                root_session = str(pl.get("session_id") or owner)
                active_thread = str(pl.get("forked_from_id") or owner) if root_session != owner else owner
                cwd = pl.get("cwd") or ""
                provider = pl.get("model_provider") or ""
                version = pl.get("cli_version") or ""
                session = owner
                m = pl.get("model")
                if m:
                    model = str(m)
                checkpoint()
                continue
            if '"turn_context"' in line:
                o = safe_json(line)
                pl = (o or {}).get("payload") or {}
                if pl.get("cwd"):
                    cwd = pl["cwd"]
                m = dig(pl, "model", "model_name", "model_slug", default=None)
                if m:
                    model = str(m)
                checkpoint()
                continue
            if '"thread_settings_applied"' in line:
                o = safe_json(line)
                pl = (o or {}).get("payload") or {}
                if pl.get("thread_id"):
                    active_thread = str(pl["thread_id"])
                elif owner and active_thread != owner:
                    # Old Codex emits an ownerless marker after a copied
                    # prefix; it marks the point at which the new thread's
                    # own cumulative stream starts.
                    active_thread = owner
                m = dig(pl, "thread_settings.model", default=None)
                if m:
                    model = str(m)
                checkpoint()
                continue
            if '"token_count"' not in line and '"token_usage_record"' not in line:
                continue
            o = safe_json(line)
            if not isinstance(o, dict):
                continue
            payload = o.get("payload") or {}
            if o.get("type") == "token_usage_record":
                # Current Codex versions provide an authoritative per-response
                # ledger. Its thread_id remains intact in forked histories;
                # unlike token_count, it also includes compaction requests.
                thread = str(payload.get("thread_id") or owner or session)
                if owner and thread != owner:
                    state["inherited_usage_skipped"] = True
                    continue
                usage = payload.get("usage")
                if not isinstance(usage, dict) or not usage:
                    issues = getattr(_PARSE_CONTEXT, "issues", None)
                    if issues is not None:
                        issues.append("malformed token_usage_record usage")
                    continue
                # Unknown-only payloads must not switch the parser into
                # native-ledger mode: doing so would silently discard the
                # valid legacy token_count mirror that follows it.
                if not any(key in usage for key in token_keys):
                    issues = getattr(_PARSE_CONTEXT, "issues", None)
                    if issues is not None:
                        issues.append("token_usage_record has no recognized usage fields")
                    continue
                response_id = str(payload.get("response_id") or "")
                identity = (thread, response_id) if response_id else (
                    thread, str(payload.get("turn_id") or ""),
                    str(o.get("timestamp") or ""), tuple(_i(usage.get(k)) for k in token_keys))
                # The owner and provider response ID survive exported copies.
                # The path is deliberately absent from this ledger identity.
                event_id = "codex-response-" + hashlib.sha256(json.dumps(identity,
                           ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
                if seen_events is not None:
                    if event_id in seen_events:
                        continue
                    seen_events.add(event_id)
                elif state.get("register_cumulative_event"):
                    if not state["register_cumulative_event"](event_id):
                        continue
                raw_in = _i(usage.get("input_tokens"))
                cached = _i(usage.get("cached_input_tokens"))
                out = _i(usage.get("output_tokens"))
                cw = _i(usage.get("cache_write_input_tokens"))
                reason = _i(usage.get("reasoning_output_tokens"))
                inp = max(0, raw_in - cached)
                if inp + out + cached + cw <= 0:
                    remember_unrecorded(event_id)
                    continue
                response_ledger = True
                active_thread = thread
                checkpoint()
                yield UsageRecord(
                    tool=self.id, ts=to_epoch(o.get("timestamp")), model=model, project=project,
                    session=owner or thread or session, input=inp, output=out, cache_read=cached,
                    cache_write=cw, reasoning=0,
                    cost=estimate_cost(model, inp, out, cached, cw), src=path,
                    event_id=event_id,
                    extra={"request_id": response_id, "turn_id": str(payload.get("turn_id") or ""),
                           "cwd": cwd, "provider": provider, "version": version,
                           "reasoning_output_tokens": reason, "usage_source": "token_usage_record"},
                )
                continue
            if o.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            if not info:
                continue
            last = info.get("last_token_usage") or {}
            tot = info.get("total_token_usage") or {}
            if owner and active_thread and active_thread != owner:
                # Retain the inherited cumulative baseline for older forks
                # that continue it when their own work starts.
                if tot:
                    previous_totals = {k: _i(tot.get(k)) for k in token_keys}
                    checkpoint()
                state["inherited_usage_skipped"] = True
                continue
            if response_ledger:
                # Once a source has emitted the authoritative per-response
                # ledger, token_count is an aggregate mirror.  Keep ignoring
                # it for the remainder of this rollout; accepting a later
                # cumulative segment would double count native responses.
                continue
            # Use the original event, before deriving a delta. A replay of an
            # older cumulative count must not rewind the counter checkpoint.
            identity = (str(payload.get("request_id") or payload.get("turn_id") or ""),
                        str(o.get("timestamp") or ""),
                        tuple(_i((tot or last).get(k)) for k in token_keys), bool(tot))
            event_id = "codex-" + hashlib.sha256(json.dumps([absolute_path, identity],
                       ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            if seen_events is not None:
                if identity in seen_events:
                    continue
                seen_events.add(identity)
            elif tot and state.get("register_cumulative_event"):
                if not state["register_cumulative_event"](event_id):
                    continue
            if tot:
                current = {k: _i(tot.get(k)) for k in token_keys}
                if previous_totals is not None:
                    if current == previous_totals:
                        remember_unrecorded(event_id)
                        continue
                    if all(current[k] >= previous_totals[k] for k in token_keys):
                        use = {k: current[k] - previous_totals[k] for k in token_keys}
                    else:
                        # A compaction/reset starts a new counter segment.
                        use = last or current
                else:
                    use = last or current
                previous_totals = current
            else:
                use = last
            checkpoint()
            raw_in = _i(use.get("input_tokens"))
            cached = _i(use.get("cached_input_tokens"))
            out = _i(use.get("output_tokens"))
            cw = _i(use.get("cache_write_input_tokens"))
            reason = _i(use.get("reasoning_output_tokens"))
            # Codex 的 input_tokens 已包含 cached，避免重复计数
            inp = max(0, raw_in - cached)
            if inp + out + cached + cw <= 0:
                if tot:
                    remember_unrecorded(event_id)
                continue
            ts = to_epoch(o.get("timestamp"))
            yield UsageRecord(
                tool=self.id, ts=ts, model=model, project=project,
                session=session, input=inp, output=out, cache_read=cached,
                cache_write=cw, reasoning=0,
                cost=estimate_cost(model, inp, out, cached, cw), src=path,
                event_id=event_id,
                extra={"request_id": hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
                       "cwd": cwd, "provider": provider, "version": version,
                       "reasoning_output_tokens": reason},
            )

    def parse_quick(self, path):
        return iter(())

    def scan_extra(self):
        if self.cfg.get("codex_quick"):
            return self._quick_from_db()
        return []

    def probe(self):
        p = os.path.join(HOME, ".codex")
        return os.path.isdir(p), p


# ==========================================================================
# 3. Gemini CLI
# ==========================================================================

class GeminiCliAdapter(Adapter):
    id = "gemini-cli"
    name = "Gemini CLI"
    vendor = "Google"
    category = "cli"
    color = "#4285f4"
    glyph = "G"
    homepage = "https://github.com/google-gemini/gemini-cli"
    detail = "读取 Gemini CLI JSON / JSONL 会话；按消息 ID 重放更新，避免缓存和重复消息被重复计数"

    def enumerate(self):
        paths = glob_files(os.path.join(HOME, ".gemini", "tmp", "*", "chats", "**", "*.jsonl"))
        paths += glob_files(os.path.join(HOME, ".gemini", "tmp", "*", "chats", "**", "*.json"))
        # Resumed legacy JSON sessions migrate to JSONL; count the current file.
        return [path for path in paths if not path.endswith(".json") or path[:-5] + ".jsonl" not in paths]

    def parse(self, path):
        session = os.path.basename(path).rsplit(".", 1)[0]
        project = ""
        root = os.path.join(os.path.dirname(os.path.dirname(path)), ".project_root")
        if os.path.exists(root):
            try:
                project = open(root, encoding="utf-8", errors="replace").read().strip()
            except OSError:
                project = ""
        messages = {}
        def keep(message):
            if not isinstance(message, dict):
                raise ValueError("invalid Gemini message")
            identifier = str(message.get("id") or hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            if identifier not in messages and len(messages) >= 200_000:
                raise ValueError("Gemini session exceeds 200,000 messages")
            # Keep only accounting fields, not potentially huge prompt bodies.
            messages[identifier] = {key: message[key] for key in ("id", "type", "tokens", "model", "modelVersion", "timestamp", "lastUpdated") if key in message}
        if path.lower().endswith(".json"):
            with open(path, encoding="utf-8-sig") as stream:
                doc = json.load(stream)
            if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
                raise ValueError("invalid legacy Gemini session")
            session = str(doc.get("sessionId") or session)
            for message in doc["messages"]:
                keep(message)
        else:
            for line in iter_lines(path):
                if not line.strip():
                    continue
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("invalid Gemini JSONL record")
                if "$set" in obj:
                    updates = obj["$set"]
                    if not isinstance(updates, dict):
                        raise ValueError("invalid Gemini metadata record")
                    session = str(updates.get("sessionId") or session)
                    if "messages" in updates:
                        messages.clear()
                        for message in updates["messages"]:
                            keep(message)
                elif "$rewindTo" in obj:
                    target = str(obj["$rewindTo"])
                    keys = list(messages)
                    remove = keys[keys.index(target):] if target in messages else keys
                    for key in remove:
                        messages.pop(key, None)
                elif "$patch" in obj:
                    patch = obj["$patch"]
                    if not isinstance(patch, dict):
                        raise ValueError("invalid Gemini message patch")
                    for key in patch.get("removeIds", []):
                        messages.pop(str(key), None)
                    # Current patches update content/tool calls only. Account
                    # for explicit token changes if a future schema adds them.
                    for change in patch.get("updates", []):
                        key = str(change.get("id", ""))
                        if key in messages:
                            messages[key].update({k: change[k] for k in ("tokens", "model", "timestamp") if k in change})
                    order = patch.get("orderIds")
                    if isinstance(order, list):
                        messages = {**{str(key): messages[str(key)] for key in order if str(key) in messages},
                                    **messages}
                elif obj.get("type") in ("gemini", "user", "info", "error", "warning") or "tokens" in obj:
                    keep(obj)
                elif "sessionId" in obj:
                    session = str(obj["sessionId"] or session)
        for identifier, o in messages.items():
            t = o.get("tokens")
            if not isinstance(t, dict) or o.get("type") not in ("gemini", None):
                continue
            raw_input = _i(t.get("input"))
            out = _i(t.get("output"))
            cached = _i(t.get("cached"))
            thoughts = _i(t.get("thoughts"))
            if cached > raw_input:
                raise ValueError("Gemini cached tokens exceed reported prompt tokens")
            inp = raw_input - cached
            if inp + out + cached + thoughts <= 0:
                continue
            model = str(dig(o, "model", "modelVersion", default="gemini"))
            explicit_id = identifier
            event_id = stable_event_id(
                self.id, path, session, str(explicit_id), o)
            yield UsageRecord(
                tool=self.id, ts=to_epoch(o.get("timestamp") or o.get("lastUpdated")),
                model=model, project=norm_project(project), session=session,
                input=inp, output=out, cache_read=cached, reasoning=thoughts,
                # Gemini's ``thoughts`` counter is a separately reported
                # output-like quantity.  Include it in the same estimate used
                # by store re-pricing and ledger breakdowns.
                cost=estimate_cost(model, inp, out, cached, reasoning=thoughts), src=path,
                extra={"project_root": project, "request_id": str(explicit_id)},
                event_id=event_id,
            )


# ==========================================================================
# 3.5 DeepSeek Harness（DSH / dsh-desktop）
#
#   DSH_HOME 默认在 %APPDATA%\dsh-desktop\harness。会话原始事件流是
#   sessions/<项目>/<会话>/session.v3.jsonl.zstd（zstd 压缩），
#   但 harness 会把每个会话的**投影缓存**写成明文 JSON：
#       storages/session_projcache/sessions/session-<uuid>.json
#   其中 record.rows.tokenUsage.val.totals 就是权威的会话级用量，
#   并且带 modelSelection（provider / model / reasoningEffort），
#   所以无需解压即可精确取数——也就不必引入任何第三方压缩库。
# ==========================================================================

class DeepSeekHarnessAdapter(Adapter):
    id = "deepseek-harness"
    name = "DeepSeek Harness"
    vendor = "DeepSeek"
    category = "desktop"
    color = "#4d6bfe"
    glyph = "DS"
    homepage = "https://www.deepseek.com"
    detail = ("递归读取 Harness 会话投影缓存中的 tokenUsage（未缓存输入 / 输出 / 缓存读 / 缓存写）；"
              "仅统计本机仍保留投影数据的会话，已被 Harness 清理或未投影的云端历史无法从本机恢复")
    WANT_SIDE = True

    HOME_ENV = "DSH_HOME"
    CANDIDATE_HOMES = (
        "%APPDATA%/dsh-desktop/harness",
        "%LOCALAPPDATA%/dsh-desktop/harness",
        "~/.dsh",
        "~/.dsh-desktop",
        "~/.deepseek-harness",
        "~/.deepseek",
    )

    def _homes(self):
        out, seen = [], set()
        env = os.environ.get(self.HOME_ENV)
        for c in ([env] if env else []) + list(self.CANDIDATE_HOMES):
            if not c:
                continue
            try:
                p = os.path.abspath(expand(c))
            except Exception:
                continue
            if os.path.isdir(p) and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    # 不同 Harness 版本会把投影缓存直接放在 session_projcache 下，或迁移到
    # storages/sessions。递归读取这些专用目录，避免只扫第一层导致漏掉历史会话。
    CACHE_RELATIVE_DIRS = (
        ("storages", "session_projcache", "sessions"),
        ("storages", "session_projcache"),
        ("storages", "sessions"),
        ("storage", "session_projcache", "sessions"),
        ("storage", "session_projcache"),
    )

    def _cache_dirs(self):
        out, seen = [], set()
        for h in self._homes():
            for rel in self.CACHE_RELATIVE_DIRS:
                p = os.path.join(h, *rel)
                if os.path.isdir(p) and p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def enumerate(self):
        out, seen = [], set()
        event_sessions = set()
        # Projection JSON contains one cumulative total per session.  Prefer
        # the event stream when present because it has one usage object per
        # assistant call and therefore preserves the history timeline.
        #
        # 注意：事件流是 zstd 压缩的。缺少 zstandard 时这里必须**明确告警**——
        # 曾经的静默回退会让明细流水从「几百条调用」塌缩成「每个会话一条」，
        # 用户看到的就是"全都挤到一条了"，而且没有任何提示。
        if _zstandard is None:
            self.event_stream_disabled = True
            _LOG.warning(
                "DeepSeek Harness 事件流无法解压（缺少 zstandard）："
                "将退化为每个会话仅一条累计记录。请执行 pip install zstandard 后重扫。")
        if _zstandard is not None:
            for h in self._homes():
                root = os.path.join(h, "sessions")
                if not os.path.isdir(root):
                    continue
                for cur, _dirs, files in walk_readable(root):
                    for f in files:
                        if not f.lower().endswith(".zstd"):
                            continue
                        fp = os.path.join(cur, f)
                        if is_secret_path(fp):
                            continue
                        try:
                            if 0 < os.path.getsize(fp) <= MAX_TEXT_SCAN:
                                seen.add(fp)
                                out.append(fp)
                                # Parent directory is session-<uuid> in Harness.
                                sid = os.path.basename(os.path.dirname(fp))
                                if sid.startswith("session-"):
                                    event_sessions.add(sid)
                        except OSError:
                            pass
        for d in self._cache_dirs():
            for cur, _dirs, files in walk_readable(d):
                for f in files:
                    if not f.lower().endswith(".json"):
                        continue
                    fp = os.path.join(cur, f)
                    if fp in seen or is_secret_path(fp):
                        continue
                    try:
                        if 0 < os.path.getsize(fp) <= MAX_TEXT_SCAN:
                            # 只有在「该会话的事件流确实会被解析」时才跳过投影文件。
                            # 缺 zstandard 时事件流解不了，投影是唯一数据源，必须保留。
                            stem = os.path.splitext(f)[0]
                            if stem in event_sessions and _zstandard is not None:
                                continue
                            seen.add(fp)
                            out.append(fp)
                    except OSError:
                        pass
        self.enumeration_complete = len(out) <= 4000
        # 供 parse() 做内容级比对：投影文件名与 session 目录名对不上时，
        # 文件名快路径失效，但投影 JSON 内部的会话 ID 仍然可靠。
        self._event_sessions = event_sessions
        return out[:4000]

    def _parse_event_stream(self, path):
        """Yield one UsageRecord per Harness assistant/message event."""
        if _zstandard is None:
            return
        try:
            raw = open(path, "rb")
            # Buffered readline keeps memory bounded for long-running
            # sessions; projection caches are small but event streams can be
            # hundreds of MB after decompression.
            reader = io.BufferedReader(
                _zstandard.ZstdDecompressor().stream_reader(raw), 1 << 20)
        except Exception:
            raise
        session = os.path.basename(os.path.dirname(path))
        if session.startswith("session-"):
            session = session[len("session-"):]
        cwd = ""
        fallback_model = ""
        try:
            maximum_line = 16 * 1024 * 1024
            maximum_stream = max(1, min(4096, int(self.cfg.get("max_file_mb") or 400))) * 1024 * 1024
            read_bytes = 0
            while True:
                check_parse_cancelled()
                line = reader.readline(maximum_line + 1)
                if not line:
                    break
                read_bytes += len(line)
                if len(line) > maximum_line or read_bytes > maximum_stream:
                    raise ValueError("compressed event stream exceeds the decompressed size limit; previous snapshot retained")
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ValueError("invalid JSON in compressed event stream; previous snapshot retained") from exc
                if not isinstance(event, dict):
                    raise ValueError("compressed event must be an object; previous snapshot retained")
                if event.get("type") == "session":
                    cwd = str(event.get("cwd") or "")
                    fallback_model = str(event.get("model") or "")
                    continue
                if event.get("type") != "assistant/message":
                    continue
                payload = event.get("data") or {}
                usage = payload.get("usage") or {}
                if not isinstance(usage, dict):
                    continue
                inp = _i(usage.get("inputTokens", usage.get("input_tokens", 0)))
                out = _i(usage.get("outputTokens", usage.get("output_tokens", 0)))
                cr = _i(usage.get("cacheReadTokens", usage.get("cache_read_tokens", 0)))
                cw = _i(usage.get("cacheWriteTokens", usage.get("cache_write_tokens", 0)))
                if inp + out + cr + cw <= 0:
                    continue
                msg = payload.get("message") or {}
                source = msg.get("source") if isinstance(msg, dict) else {}
                if not isinstance(source, dict):
                    source = {}
                model = str(source.get("model") or fallback_model or "deepseek-harness")
                provider = str(source.get("provider") or "")
                ts = to_epoch(event.get("time")) or int(os.path.getmtime(path))
                yield UsageRecord(
                    tool=self.id, ts=ts, model=model,
                    project=norm_project(cwd), session=session,
                    input=inp, output=out, cache_read=cr, cache_write=cw,
                    requests=1,
                    cost=estimate_cost(model, inp, out, cr, cw),
                    src=path,
                    extra={
                        "provider": provider,
                        "turn": _i(payload.get("turn")),
                        "step": _i(payload.get("step")),
                        "reasoning_tokens": _i(usage.get("reasoningTokens", usage.get("reasoning_tokens", 0))),
                    },
                )
        finally:
            try:
                reader.close()
                raw.close()
            except Exception:
                pass

    def _load(self, path):
        for enc in ("utf-8", "utf-16"):
            try:
                with io.open(path, "r", encoding=enc, errors="strict") as fh:
                    return json.load(fh)
            except (UnicodeError, ValueError):
                continue
            except OSError:
                raise
        raise ValueError("unreadable session JSON")

    def parse(self, path):
        if path.lower().endswith(".zstd"):
            yield from self._parse_event_stream(path)
            return
        obj = self._load(path)
        if not isinstance(obj, dict):
            return
        rec = obj.get("record") or obj.get("session") or obj
        if not isinstance(rec, dict):
            return
        rows = rec.get("rows") or rec.get("values") or rec

        def row(key):
            r = rows.get(key)
            if not isinstance(r, dict):
                return r
            return r.get("val", r.get("value", r))

        usage = row("tokenUsage") or row("usage") or rec.get("tokenUsage") or rec.get("usage") or {}
        totals = usage.get("totals") if isinstance(usage, dict) else None
        if not isinstance(totals, dict) and isinstance(usage, dict):
            totals = usage
        if not isinstance(totals, dict):
            return
        inp = _i(dig(totals, "uncachedInputTokens", "inputTokens", "input_tokens",
                     "promptTokens", "prompt_tokens", default=0))
        out = _i(dig(totals, "outputTokens", "output_tokens", "completionTokens",
                     "completion_tokens", default=0))
        cr = _i(dig(totals, "cacheReadTokens", "cache_read_tokens", "cachedInputTokens",
                    "cached_tokens", default=0))
        cw = _i(dig(totals, "cacheWriteTokens", "cache_write_tokens", default=0))
        if inp + out + cr + cw <= 0:
            return

        ident = rec.get("identity") or obj.get("identity") or {}
        cwd = str(ident.get("cwd") or "")
        title = str(row("title") or "")
        sel = row("modelSelection") or rec.get("modelSelection") or {}
        last_used = sel.get("lastUsed") if isinstance(sel, dict) else None
        model = ""
        provider = ""
        effort = ""
        if isinstance(last_used, dict):
            model = str(last_used.get("model") or "")
            provider = str(last_used.get("provider") or "")
            effort = str(last_used.get("reasoningEffort") or "")
        if not model:
            model = "deepseek-harness"

        stats = row("sessionStats") or rec.get("sessionStats") or {}
        steps = _i(stats.get("steps")) if isinstance(stats, dict) else 0
        turns = _i(stats.get("turns")) if isinstance(stats, dict) else 0
        meta = row("sessionListMetadata") or rec.get("sessionListMetadata") or {}
        last_at = _i(meta.get("lastPromptAt")) if isinstance(meta, dict) else 0
        ts = to_epoch(last_at) or to_epoch(ident.get("createdAt")) or \
            int(os.path.getmtime(path))

        session = str(rec.get("id") or obj.get("id") or os.path.basename(path))
        if session.startswith("session-"):
            session = session[len("session-"):]
        if session.endswith(".json"):
            session = session[:-len(".json")]
        if _zstandard is not None and ("session-" + session) in getattr(self, "_event_sessions", ()):
            # 该会话的事件流（逐条账本）会被解析；这份累计投影是镜像。
            # 文件名快路径只覆盖「投影文件名 == session 目录名」的情况，
            # 这里按投影内容里的会话 ID 兜底，命名不一致也不双计。
            return

        yield UsageRecord(
            tool=self.id, ts=ts, model=model,
            project=norm_project(cwd), session=session,
            input=inp, output=out, cache_read=cr, cache_write=cw,
            requests=max(1, steps or turns or 1),
            cost=estimate_cost(model, inp, out, cr, cw),
            src=path,
            extra={
                "title": title,
                "provider": provider,
                "reasoning_effort": effort,
                "turns": turns,
                "steps": steps,
                "cwd": cwd,
                "context_window": _i((row("contextPressure") or {}).get("contextWindow"))
                if isinstance(row("contextPressure"), dict) else 0,
            },
        )

    def probe(self):
        for d in self._cache_dirs():
            if os.path.isdir(d) and any(x.lower().endswith(".json") for x in
                                        (os.listdir(d) if os.path.isdir(d) else [])):
                return True, d
        for h in self._homes():
            return True, h
        return False, ""

    def side_stats(self):
        st = {"sessions": 0, "size": 0, "last_activity": 0, "event_logs": 0,
              "decoder": "zstandard" if _zstandard is not None else "missing"}
        for h in self._homes():
            for cur, dirs, files in walk_readable(os.path.join(h, "storages")):
                for f in files:
                    fp = os.path.join(cur, f)
                    try:
                        st["size"] += os.path.getsize(fp)
                    except OSError:
                        continue
                    if f.lower().endswith(".json"):
                        st["sessions"] += 1
            sess = os.path.join(h, "sessions")
            for cur, dirs, files in walk_readable(sess):
                for f in files:
                    if f.endswith(".zstd"):
                        st["event_logs"] += 1
                        try:
                            st["last_activity"] = max(
                                st["last_activity"], int(os.path.getmtime(os.path.join(cur, f))))
                        except OSError:
                            pass
        return st

    def warnings(self):
        """把「解压器缺失导致明细塌缩」这件事显式报给用户。

        这是本项目踩过的真实事故：zstandard 没装时适配器会静默退化为
        "每个会话一条累计记录"，明细流水从几百条变成几条，界面上毫无提示，
        用户只会觉得"怎么全挤到一条了"。
        """
        if _zstandard is not None:
            return []
        st = self.side_stats()
        if not st.get("event_logs"):
            return []
        return ["检测到 %d 个 DeepSeek Harness 会话事件流，但当前环境缺少 zstandard，"
                "无法逐条解析调用明细，只能按会话汇总（明细会明显偏少）。"
                "请执行 pip install zstandard 后重新扫描。" % st["event_logs"]]


# ==========================================================================
# 4. OpenCode / Mimocode（同一血统）
# ==========================================================================

class OpenCodeAdapter(Adapter):
    QUALITY = "aggregate"
    id = "opencode"
    name = "OpenCode / Mimocode"
    vendor = "OpenCode"
    category = "cli"
    color = "#f59e0b"
    glyph = "◍"
    homepage = "https://opencode.ai"
    detail = "读取 ~/.local/share/opencode 与 mimocode 的 SQLite session 表（含成本字段）"

    def _dbs(self):
        out = []
        for name in ("opencode", "mimocode"):
            p = os.path.join(HOME, ".local", "share", name, "%s.db" % name)
            if os.path.exists(p):
                out.append((name, p))
        # 兼容 XDG / 旧版本位置
        for pat in (os.path.join(APPDATA, "opencode", "*.db"),
                    os.path.join(LOCALAPPDATA, "opencode", "*.db")):
            for p in glob.glob(pat):
                out.append(("opencode", p))
        return out

    def enumerate(self):
        return [p for _, p in self._dbs()]

    def parse_session_table(self, con, db, label):
        cols = {r[1] for r in con.execute("PRAGMA table_info(session)")}
        recs = []
        token_cols = {"tokens_input", "tokens_output", "tokens_reasoning",
                      "tokens_cache_read", "tokens_cache_write"}
        if token_cols & cols:
            sql = ("SELECT * FROM session WHERE COALESCE(tokens_input,0)+COALESCE(tokens_output,0)"
                   "+COALESCE(tokens_reasoning,0)+COALESCE(tokens_cache_read,0)"
                   "+COALESCE(tokens_cache_write,0) > 0")
            for row in con.execute(sql):
                r = dict(row)
                model = r.get("model") or ""
                if isinstance(model, str) and model.startswith("{"):
                    try:
                        mj = json.loads(model)
                        model = dig(mj, "id", "modelID", default=model)
                    except Exception:
                        pass
                inp = _i(r.get("tokens_input"))
                out = _i(r.get("tokens_output"))
                cr = _i(r.get("tokens_cache_read"))
                cw = _i(r.get("tokens_cache_write"))
                rs = _i(r.get("tokens_reasoning"))
                actual = r.get("cost") is not None
                cost = float(r["cost"]) if actual else estimate_cost(model, inp, out, cr, cw)
                recs.append(UsageRecord(
                    tool=self.id, ts=to_epoch(r.get("time_created")),
                    model=str(model or label), project=norm_project(r.get("directory") or ""),
                    session=r.get("id") or "", input=inp, output=out, cache_read=cr,
                    cache_write=cw, reasoning=rs, cost=cost, src=db,
                    cost_kind="actual" if actual else ("estimated" if cost else "unknown"),
                    extra={"title": r.get("title") or "", "engine": label},
                ))
            return recs
        # 没有 token 列 → 退化为解析 message.data
        for row in con.execute("SELECT data, time_created, session_id, id FROM message"):
            raw = row[0]
            if not raw or "token" not in raw.lower():
                continue
            o = safe_json(raw)
            if not isinstance(o, dict):
                continue
            t = first_nested(o, {"tokens", "usage"})
            if not isinstance(t, dict):
                continue
            inp = _i(dig(t, "input", "prompt_tokens", "tokens_input", default=0))
            out = _i(dig(t, "output", "completion_tokens", "tokens_output", default=0))
            rs = _i(dig(t, "reasoning", default=0))
            cr = _i(dig(t, "cache.read", "cache_read", "cached", default=0))
            cw = _i(dig(t, "cache.write", "cache_write", default=0))
            if inp + out + rs + cr + cw <= 0:
                continue
            model = str(dig(o, "modelID", "model.id", "model", default=label))
            recs.append(UsageRecord(
                tool=self.id, ts=to_epoch(row[1]), model=model, project="",
                session=row[2] or "", input=inp, output=out, cache_read=cr,
                cache_write=cw, reasoning=rs,
                cost=estimate_cost(model, inp, out, cr, cw), src=db,
                cost_kind="estimated" if estimate_cost(model, inp, out, cr, cw) else "unknown",
                extra={"engine": label},
            ))
        return recs

    def parse(self, path):
        return iter(())

    @staticmethod
    def _label(path):
        low = path.lower()
        if "mimocode" in low:
            return "mimocode"
        return "opencode"

    def scan_db(self, path, label=None):
        label = label or self._label(path)
        try:
            con = source_connection(path)
            con.row_factory = sqlite3.Row
        except Exception:
            raise
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "session" not in names:
                return []
            return self.parse_session_table(con, path, label)
        except Exception:
            raise
        finally:
            try:
                con.close()
            except Exception:
                pass

    def probe(self):
        dbs = self._dbs()
        return bool(dbs), (dbs[0][1] if dbs else "")


# ==========================================================================
# 5. WorkBuddy / CodeBuddy 桌面版
# ==========================================================================

class WorkBuddyAdapter(Adapter):
    QUALITY = "aggregate"
    id = "workbuddy"
    name = "WorkBuddy / CodeBuddy"
    vendor = "腾讯"
    category = "desktop"
    color = "#3b82f6"
    glyph = "W"
    homepage = "https://www.workbuddy.cn"
    detail = "读取 ~/.workbuddy/workbuddy.db 的 session_usage 表，并关联会话模型与项目"

    def enumerate(self):
        db = os.path.join(HOME, ".workbuddy", "workbuddy.db")
        return [db] if os.path.exists(db) else []

    def parse(self, path):
        return iter(())

    def scan_db(self, path):
        try:
            con = source_connection(path)
            con.row_factory = sqlite3.Row
        except Exception:
            raise
        recs = []
        try:
            meta = {}
            try:
                for r in con.execute(
                    "SELECT id, title, model, cwd, created_at, updated_at, source_mode FROM sessions"
                ):
                    meta[r["id"]] = dict(r)
            except Exception:
                pass
            for r in con.execute("SELECT * FROM session_usage"):
                sid = r["session_id"]
                used = _i(r["used"])
                if used <= 0:
                    continue
                m = meta.get(sid, {})
                model = m.get("model") or "workbuddy"
                credits = 0.0
                cj = r["credit_json"]
                if cj:
                    try:
                        credits = sum(float(v) for v in json.loads(cj).values())
                    except Exception:
                        credits = 0.0
                cost = estimate_cost(model, used, 0)
                recs.append(UsageRecord(
                    tool=self.id, ts=to_epoch(m.get("created_at") or r["updated_at"]),
                    model=str(model), project=norm_project(m.get("cwd") or ""),
                    session=sid, input=used, output=0, cost=cost, src=path,
                    extra={"title": m.get("title") or "", "credits": round(credits, 4),
                           "window": _i(r["size"]), "cwd": m.get("cwd") or "",
                           "mode": m.get("source_mode") or ""},
                ))
        except Exception:
            raise
        finally:
            try:
                con.close()
            except Exception:
                pass
        return recs

    def probe(self):
        p = os.path.join(HOME, ".workbuddy", "workbuddy.db")
        return os.path.exists(p), p


# ==========================================================================
# 5.5 ZCode（智谱 GLM 官方 Harness）
#
#   用量在 ~/.zcode/cli/db/db.sqlite 的 model_usage 表：一行一次模型调用，
#   带 provider_id / model_id / 四类 token / 状态 / 起止时间（毫秒 epoch），
#   可按 session_id 关联 session 表取标题与工作目录。
#
#   实测 303 行确认了两处口径差异，写错就会虚增 token：
#     · input_tokens **已包含** cache_read_input_tokens
#       （raw_usage_json 里 totalTokens == inputTokens + outputTokens），
#       所以 UsageRecord 的未缓存输入必须减去缓存读，否则缓存读算两遍；
#     · reasoning_tokens **已包含**在 output_tokens 内（raw_usage_json 只是
#       额外给一个细分字段，totalTokens 并未再加它），与 Claude Code 的
#       thinking token 同理，故 reasoning 记 0、原始值放 extra 备查。
#   另外 attempt_index 全为 0、logical_request_id 无重复 → 逐行累加即真实
#   用量，不会因重试重复计数。
#
#   只列 db.sqlite：同目录 artifacts/ 是工具输出、log/ 是诊断日志、v2/ 下
#   credentials.json 是登录凭据，体量大且都无用量，用通用目录扫描会把它们
#   一起吞进来（凭据必须绕开）。
# ==========================================================================

class ZCodeAdapter(Adapter):
    id = "zcode"
    name = "ZCode"
    vendor = "智谱 AI"
    category = "cli"
    color = "#14b8a6"
    glyph = "ZC"
    homepage = "https://zcode.z.ai"
    detail = "读取 ~/.zcode/cli/db/db.sqlite 的 model_usage 表，按会话关联项目与标题"

    DB = os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite")

    def enumerate(self):
        return [self.DB] if os.path.exists(self.DB) else []

    def parse(self, path):
        return iter(())

    def scan_db(self, path):
        try:
            con = source_connection(path)
            con.row_factory = sqlite3.Row
        except Exception:
            raise
        recs = []
        try:
            meta = {}
            try:
                for r in con.execute("SELECT id, title, directory FROM session"):
                    meta[r["id"]] = (r["title"] or "", r["directory"] or "")
            except Exception:
                pass
            for r in con.execute(
                "SELECT session_id, model_id, provider_id, input_tokens, output_tokens,"
                " cache_read_input_tokens, cache_creation_input_tokens, reasoning_tokens,"
                " started_at, completed_at, status, query_source, agent, mode,"
                " duration_ms, tool_call_count, retry_count FROM model_usage"
            ):
                raw_in = _i(r["input_tokens"])
                cr = _i(r["cache_read_input_tokens"])
                cw = _i(r["cache_creation_input_tokens"])
                out = _i(r["output_tokens"])
                inp = max(0, raw_in - cr)      # input_tokens 含缓存读，扣掉才是净输入
                if inp + out + cr + cw <= 0:
                    continue
                model = str(r["model_id"] or "zcode")
                title, directory = meta.get(r["session_id"], ("", ""))
                recs.append(UsageRecord(
                    tool=self.id, ts=to_epoch(r["completed_at"] or r["started_at"]),
                    model=model, project=norm_project(directory),
                    session=r["session_id"] or "",
                    input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=0,
                    cost=estimate_cost(model, inp, out, cr, cw), src=path,
                    extra={"title": title, "cwd": directory,
                           "provider": r["provider_id"] or "",
                           "status": r["status"] or "",
                           "query_source": r["query_source"] or "",
                           "agent": r["agent"] or "", "mode": r["mode"] or "",
                           "duration_ms": _i(r["duration_ms"]),
                           "tool_calls": _i(r["tool_call_count"]),
                           "retry_count": _i(r["retry_count"]),
                           "reasoning_tokens": _i(r["reasoning_tokens"]),
                           "raw_input_tokens": raw_in},
                ))
        except Exception:
            raise
        finally:
            try:
                con.close()
            except Exception:
                pass
        return recs

    def probe(self):
        return os.path.exists(self.DB), self.DB


# ==========================================================================
# 5.6 Kimi（月之暗面）
#
#   两代产品，路径与字段名都不同，必须同时支持：
#     · 新版 Kimi Code CLI（TypeScript，现行）
#         ~/.kimi-code/sessions/<workDirKey>/<sessionId>/agents/<agentId>/wire.jsonl
#       数据根可用 KIMI_CODE_HOME 重定向。
#     · 旧版 kimi-cli（Python，已归档）
#         ~/.kimi/sessions/<group-id>/<session-id>/wire.jsonl
#
#   两代的 token 字段名不同（新版驼峰、旧版下划线），这里用 dig() 同时
#   给出多条候选路径，不依赖版本探测：
#       输入 input_other / inputOther      输出 output
#       缓存读 input_cache_read / inputCacheRead
#       缓存写 input_cache_creation / inputCacheCreation
#
#   ⚠ 关键陷阱：新版 wire.jsonl 里同一份用量会**同时**出现「轮次级(turn)」
#   与「会话级(session)」两种记录，后者是累计总量。两者一起累加会成倍
#   虚增，所以这里先判作用域：有轮次记录就只累加轮次；只有会话级记录时
#   按会话取**最大值**（累计值单调递增，末值即总量），绝不求和。
#
#   Kimi 不记录 USD，成本一律按价目表估算（pricing.py 已有 kimi / kimi-k2*
#   条目，'kimi-for-coding' 会命中 'kimi' 前缀）。
#
#   只扫 sessions/ 下的 wire.jsonl：同目录的 credentials/ 是 OAuth 凭据
#   （旧版 ~/.kimi/kimi.json 会把 workDir 映射成 sessionId，只读它做项目名，
#   不含任何 token 或密钥）。
# ==========================================================================

class KimiAdapter(Adapter):
    id = "kimi"
    name = "Kimi Code CLI"
    vendor = "月之暗面"
    category = "cli"
    color = "#6d5efc"
    glyph = "K"
    homepage = "https://www.kimi.com/code"
    detail = ("解析 ~/.kimi-code 与 ~/.kimi 下各会话 agents/*/wire.jsonl 的 token 用量"
              "（只计轮次记录，避免会话级累计值重复累加）")

    # 会话级作用域的取值：命中即视为累计总量，不能与轮次记录一起求和
    _SESSION_SCOPE = ("session", "session_scope", "sessionscope", "cumulative",
                      "total", "totals", "aggregate", "aggregated", "summary",
                      "session_total", "sessiontotal")
    # 轮次作用域的取值
    _TURN_SCOPE = ("turn", "request", "call", "message", "step", "invocation",
                   "assistant", "step_usage")

    def _roots(self):
        """数据根：优先读 KIMI_CODE_HOME，其次两个默认目录。都存在就都扫。"""
        out = []
        env = os.environ.get("KIMI_CODE_HOME")
        cands = ([expand(env)] if env else []) + [
            os.path.join(HOME, ".kimi-code"),
            os.path.join(HOME, ".kimi"),
        ]
        for r in cands:
            if r and os.path.isdir(r) and r not in out:
                out.append(r)
        return out

    def _wire_files(self, root):
        # 新版：sessions/<workDirKey>/<sessionId>/agents/<agentId>/wire.jsonl
        # 旧版：sessions/<group-id>/<session-id>/wire.jsonl
        return glob_files(os.path.join(root, "sessions", "**", "wire.jsonl"))

    def enumerate(self):
        self._session_index()
        out = []
        for root in self._roots():
            out.extend(self._wire_files(root))
        return out

    def _session_index(self):
        """sessionId → workDir。新版 session_index.jsonl + 旧版 kimi.json。"""
        cached = getattr(self, "_session_lookup", None)
        if cached is not None:
            return cached
        idx = {}
        for root in self._roots():
            p = os.path.join(root, "session_index.jsonl")
            for line in (iter_lines(p) if os.path.isfile(p) else ()):
                o = safe_json(line)
                if not isinstance(o, dict):
                    continue
                sid = dig(o, "sessionId", "session_id", "id")
                wd = dig(o, "workDir", "work_dir", "cwd", "directory")
                if sid and wd:
                    idx[str(sid)] = str(wd)
            j = os.path.join(root, "kimi.json")
            o = None
            if os.path.exists(j):
                try:
                    with io.open(j, "r", encoding="utf-8", errors="replace") as fh:
                        o = json.loads(fh.read() or "{}")
                except Exception:
                    o = None
            if isinstance(o, dict):
                # 旧版是 {workDir: sessionId}，键值方向与新版相反
                for k, v in o.items():
                    if isinstance(v, str) and v:
                        idx.setdefault(v, k)
        self._session_lookup = idx
        return idx

    def cache_dependencies(self, path):
        return [os.path.join(root, name) for root in self._roots()
                for name in ("session_index.jsonl", "kimi.json")]

    @classmethod
    def _scope_of(cls, o):
        """判断一行记录的作用域，返回 "turn" / "session" / ""（未知）。

        作用域字段可能出现在顶层，也可能嵌在 message/data/record 里，所以
        顶层与嵌套都查一遍。
        """
        keys = ("scope", "level", "aggregate", "aggregation", "granularity",
                "usageScope", "usage_scope", "kind", "recordScope", "record_scope")
        cands = [o]
        for path in ("data", "message", "record", "payload", "usage", "token_usage"):
            v = o.get(path)
            if isinstance(v, dict):
                cands.append(v)
        for d in cands:
            for key in keys:
                v = d.get(key)
                if isinstance(v, str) and v.strip():
                    low = v.strip().lower()
                    if any(t in low for t in cls._TURN_SCOPE):
                        return "turn"
                    if any(t in low for t in cls._SESSION_SCOPE):
                        return "session"
        # 没有显式作用域时，看有没有轮次标识
        if dig(o, "turnId", "turn_id", "turn", "messageId", "message_id",
               "requestId", "request_id") not in (None, ""):
            return "turn"
        return ""

    @classmethod
    def _net_input(cls, u, inp, out, cr):
        """通用字段名（input / inputTokens…）的口径不定，这里按总量判别。

        如果这个 input 已含缓存读，则 total == input + output；
        如果它不含，则 total == input + output + cache_read。
        所以「input + output + cache_read > total」⇔ input 已含缓存读，
        此时必须扣除，否则缓存读被算两遍。没有 total 就保持原值。
        """
        tot = _i(dig(u, "total", "total_tokens", "totalTokens", "totalTokensCount"))
        if cr and tot and inp + out + cr > tot:
            return max(0, inp - cr)
        return inp

    def _usage_of(self, o):
        """取 (usage_dict, scope)。取不到用量返回 (None, "")。"""
        u = dig(o, "usage", "token_usage", "tokenUsage",
                "message.usage", "data.usage", "payload.usage")
        if not isinstance(u, dict):
            return None, ""
        return u, self._scope_of(o)

    @staticmethod
    def _session_dir(parts):
        """从路径片段里取出会话目录名。

        新版 …/<sessionId>/agents/<agentId>/wire.jsonl：往上找 'agents'，
        取它**前一段**才是会话目录（子智能体目录名是 agent_xxx，不是
        'agents'，直接取 parts[-2] 会错拿成 agentId）。
        旧版 …/<session-id>/wire.jsonl：取 parts[-2]。
        """
        if "agents" in parts:
            i = len(parts) - 1 - parts[::-1].index("agents")
            if i >= 1:
                return parts[i - 1]
        return parts[-2] if len(parts) >= 2 else ""

    def parse(self, path):
        idx = self._session_index()
        parts = [p for p in str(path).replace("/", os.sep).split(os.sep) if p]
        session = self._session_dir(parts)
        project = norm_project(idx.get(session, ""))
        new_style = "agents" in parts

        turns, session_scoped, unknown = [], [], []
        seen = set()
        for line in iter_lines(path):
            if "token" not in line and "usage" not in line:
                continue
            o = safe_json(line)
            if not isinstance(o, dict):
                continue
            u, scope = self._usage_of(o)
            if not u:
                continue
            out = _i(dig(u, "output", "output_tokens", "outputTokens",
                         "completion_tokens", "completionTokens"))
            cr = _i(dig(u, "input_cache_read", "inputCacheRead",
                        "cache_read_input_tokens", "cacheReadTokens", "cached"))
            cw = _i(dig(u, "input_cache_creation", "inputCacheCreation",
                        "cache_creation_input_tokens", "cacheCreationTokens"))
            # 优先用语义明确的 input_other（= 未命中缓存的输入）
            if dig(u, "input_other", "inputOther") is not None:
                inp = _i(dig(u, "input_other", "inputOther"))
            else:
                inp = _i(dig(u, "input_tokens", "inputTokens", "input",
                             "prompt_tokens", "promptTokens"))
                inp = self._net_input(u, inp, out, cr)
            if inp + out + cr + cw <= 0:
                continue
            ts = to_epoch(dig(o, "timestamp", "time", "created_at", "createdAt",
                              "ts", "time_created", "updated_at", "date"))
            model = str(dig(o, "model", "model_name", "modelName", "model_id",
                            "modelId") or ("kimi-for-coding" if new_style else "kimi"))
            sid = str(dig(o, "sessionId", "session_id", "session") or session) or session
            # 逐行去重：同一轮用量若被重复写入，不能算两次
            k = (sid, ts, model, inp, out, cr, cw)
            if k in seen:
                continue
            seen.add(k)
            rec = UsageRecord(
                tool=self.id, ts=ts, model=model, project=project, session=sid,
                input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=0,
                cost=estimate_cost(model, inp, out, cr, cw), src=path,
                extra={"work_dir": idx.get(session, ""), "scope": scope or "auto",
                       "file": os.path.basename(path)},
            )
            {"turn": turns, "session": session_scoped}.get(scope, unknown).append(rec)

        # 无作用域标注的记录按会话分别判别：单调不减且翻倍的序列是
        # 累计快照。同会话已有逐轮明细时，累计快照只是镜像——把它当
        # 轮次累加会成倍虚增（Codex 1.1.1 修过同类问题）；镜像必须丢弃。
        if unknown:
            turns_by_session = {}
            for r in turns:
                turns_by_session.setdefault(r.session, []).append(r)
            by_session = {}
            for r in unknown:
                by_session.setdefault(r.session, []).append(r)
            for sid, recs in by_session.items():
                seq = [r.total for r in recs]
                cumulative = (len(seq) >= 3
                              and all(seq[i] >= seq[i - 1] for i in range(1, len(seq)))
                              and seq[-1] >= (seq[0] or 1) * 2 and seq[-1] > seq[0])
                if not cumulative:
                    turns.extend(recs)
                    continue
                labeled = turns_by_session.get(sid)
                if labeled is None:
                    # 本文件没有该会话的逐轮明细可对照：维持会话级语义取末值
                    session_scoped.extend(recs)
                elif seq[-1] >= 0.9 * sum(r.total for r in labeled):
                    continue  # 覆盖了轮次总量的单调累计 = 镜像，丢弃
                else:
                    turns.extend(recs)

        # 有轮次记录的会话只信轮次（会话级累计是镜像）；没有轮次明细的
        # 会话保留会话级末值，且按模型分别取——多模型会话各有各的累计。
        turn_sessions = {r.session for r in turns}
        best = {}
        for r in session_scoped:
            if r.session in turn_sessions:
                continue
            key = (r.session, r.model)
            cur = best.get(key)
            if cur is None or r.total >= cur.total:
                best[key] = r
        return iter(turns + list(best.values()))

    def probe(self):
        roots = self._roots()
        for r in roots:
            f = self._wire_files(r)
            if f:
                return True, f[0]
        if roots:
            return False, roots[0]
        return False, ""


# ==========================================================================
# 5.7 Claude 桌面版（Cowork / 本地智能体会话）
# ==========================================================================
#
#   · 用量在 %LOCALAPPDATA%\Claude-3p\local-agent-mode-sessions\<账号>\<空间>\ 下；
#     本机只有 Claude-3p 带这个目录，%LOCALAPPDATA%\Claude 里只有配置文件。
#   · audit.jsonl 的 type=="result" 行是逐次提问的汇总：modelUsage 按模型给出
#     inputTokens/outputTokens/cacheReadInputTokens/cacheCreationInputTokens 和官方
#     costUSD。实测 thinkingTokens 已含在 outputTokens 内，所以 reasoning 记 0。
#   · usage-ledger\*.ndjson 是同一次记账的更细粒度落盘（每次请求一行）。两个平面
#     的孪生行时间戳只差 1~2 毫秒，一起相加会成倍虚增。
#     ⚠ 实测 audit 偶尔把上一批并进下一行（窗口滑动一格），所以以 usage-ledger 为准：
#       与 ledger 时间戳对得上的 audit 行整行丢弃，只有 ledger 没记过的 audit 行
#       （更早的历史）才保留。这样既不重复也不丢。
#   · 项目名只能取同目录元数据 json 的 userSelectedFolders（会话里的 cwd 指向自己的
#     outputs 目录，不是用户项目）。元数据文件名 == sessionId，但 audit 的父目录名
#     可能是 cliSessionId，两种都要能对上。
# ==========================================================================

class ClaudeDesktopAdapter(Adapter):
    id = "claude-desktop"
    name = "Claude Desktop"
    vendor = "Anthropic"
    category = "desktop"
    color = "#cc785c"
    glyph = "CD"
    homepage = "https://claude.ai/download"
    detail = "读取 Claude 桌面版本地智能体会话的 audit.jsonl 与 usage-ledger（含官方花费与缓存读写）"

    HOME_ENV = "CLAUDE_DESKTOP_HOME"
    SUBDIR = "local-agent-mode-sessions"
    # 同一批在两处落盘的时间差实测 1~2 毫秒，放宽到 60 秒以容忍时钟抖动
    TWIN_WINDOW = 60.0

    def __init__(self, cfg):
        super().__init__(cfg)
        self._meta_by_stem = None
        self._meta_by_cli = None
        self._link_map = None
        self._ledger_ts = None

    # --- 路径 ---

    def _roots(self):
        env = os.environ.get(self.HOME_ENV)
        if env:
            return [expand(env)]
        return [os.path.join(LOCALAPPDATA, "Claude-3p"),
                os.path.join(LOCALAPPDATA, "Claude")]

    def _spaces(self):
        out = []
        for root in self._roots():
            base = os.path.join(root, self.SUBDIR)
            for space in sorted(glob.glob(os.path.join(base, "*", "*"))):
                if os.path.isdir(space):
                    out.append(space)
        return out

    # --- 跨文件索引：元数据 + ledger 已记过的时间戳 ---

    def _index(self):
        """把两个平面读成一张索引。

        scanner 会先调 enumerate() 再并发调 parse()，索引在这里一次建好，
        之后各线程只读，不再有跨文件依赖。
        """
        if self._meta_by_stem is not None:
            return
        by_stem, by_cli = {}, {}
        ledger_ts = {}
        for space in self._spaces():
            for j in glob_files(os.path.join(space, "*.json")):
                try:
                    with io.open(j, "r", encoding="utf-8", errors="replace") as fh:
                        o = json.load(fh)
                except (OSError, ValueError):
                    continue
                if not isinstance(o, dict):
                    continue
                sid = str(o.get("sessionId") or "")
                cli = str(o.get("cliSessionId") or "")
                if sid:
                    by_stem[sid] = o
                if cli:
                    by_cli[cli] = o
                if not sid and not cli:
                    stem = os.path.splitext(os.path.basename(j))[0]
                    if stem.startswith("local_"):
                        by_stem[stem] = o
            for f in glob_files(os.path.join(space, "usage-ledger", "*.ndjson")):
                for line in iter_lines(f):
                    o = safe_json(line)
                    if not isinstance(o, dict):
                        continue
                    sid = str(o.get("sessionId") or "")
                    if sid:
                        ledger_ts.setdefault(sid, []).append(to_epoch(o.get("ts")))
        link = {}
        for name, o in by_stem.items():
            cli = str(o.get("cliSessionId") or "")
            if cli:
                link[name] = cli
                link[cli] = name
        # 守卫标志最后置位：scanner 可能同时从多个工作线程调 parse()，
        # 先写 _meta_by_stem 会让别的线程看到「索引已就绪」却读到 None 的互链表。
        self._meta_by_cli = by_cli
        self._link_map = link
        self._ledger_ts = ledger_ts
        self._meta_by_stem = by_stem

    def _meta_for(self, name):
        if not name:
            return None
        return self._meta_by_stem.get(name) or self._meta_by_cli.get(name)

    def _ids(self, *names):
        """目录名 / sessionId / cliSessionId 归成一族，便于跨平面比对。"""
        out = set()
        for n in names:
            n = str(n or "")
            if not n:
                continue
            out.add(n)
            other = self._link_map.get(n)
            if other:
                out.add(other)
        return out

    def _has_ledger_twin(self, names, ts):
        """这个 audit 行是否与 usage-ledger 的某一行是同一次记账。"""
        if ts <= 0:
            return False
        for n in self._ids(*names):
            for other in self._ledger_ts.get(n, ()):
                if abs(float(ts) - float(other)) <= self.TWIN_WINDOW:
                    return True
        return False

    # --- 采集 ---

    def enumerate(self):
        self._index()          # 先把跨文件索引建好
        files = []
        for space in self._spaces():
            files.extend(glob_files(os.path.join(space, "*", "audit.jsonl")))
            files.extend(glob_files(os.path.join(space, "usage-ledger", "*.ndjson")))
        return files

    def cache_dependencies(self, path):
        # Audit rows depend on ledger membership and session metadata. A ledger
        # arriving later must invalidate an otherwise unchanged audit file.
        dependencies = []
        for space in self._spaces():
            dependencies.extend(glob_files(os.path.join(space, "*.json")))
            if os.path.basename(path).lower() == "audit.jsonl":
                dependencies.extend(glob_files(os.path.join(space, "usage-ledger", "*.ndjson")))
        return dependencies

    def parse(self, path):
        self._index()
        if os.path.basename(path).lower() == "audit.jsonl":
            return self._parse_audit(path)
        return self._parse_ledger(path)

    @staticmethod
    def _project_of(meta):
        folders = (meta or {}).get("userSelectedFolders")
        if isinstance(folders, (list, tuple)) and folders:
            return norm_project(folders[0])
        return ""

    @staticmethod
    def _clean_model(name):
        # 形如 claude-sonnet-5[1m]：方括号是上下文窗口标记，不是模型名的一部分
        txt = re.sub(r"\[[^\]]{0,16}\]\s*$", "", str(name or "")).strip()
        return txt or str(name or "")

    def _row(self, ts, raw, u, session, project, path, extra):
        """两个平面的字段名不完全一样，统一在这里取值。"""
        if not isinstance(u, dict):
            return None
        inp = _i(dig(u, "inputTokens", "input_tokens", "promptTokens"))
        out = _i(dig(u, "outputTokens", "output_tokens", "completionTokens"))
        cr = _i(dig(u, "cacheReadInputTokens", "cacheReadTokens", "cache_read_input_tokens"))
        cw = _i(dig(u, "cacheCreationInputTokens", "cacheWriteTokens",
                    "cache_creation_input_tokens"))
        if inp + out + cr + cw <= 0:
            return None
        model = self._clean_model(raw)
        raw_cost = dig(u, "costUSD", "cost.usd", default=None)
        actual = raw_cost is not None
        cost = float(raw_cost) if actual else 0.0
        if not actual:
            cost = estimate_cost(model, inp, out, cr, cw)
        extra = dict(extra)
        extra["raw_model"] = raw
        extra["thinking_tokens"] = _i(dig(u, "thinkingTokens", "thinking_tokens"))
        explicit_id = (extra.get("request_id") or extra.get("requestId") or
                       extra.get("uuid") or extra.get("id") or "")
        event_id = stable_event_id(
            self.id, path, json.dumps([session, model], separators=(",", ":")), str(explicit_id),
            {"ts": ts, "model": raw, "usage": u, "extra": extra})
        return UsageRecord(
            tool=self.id, ts=ts, model=model, project=project, session=session,
            input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=0,
            cost=cost, src=path, extra=extra,
            event_id=event_id,
            cost_kind="actual" if actual else ("estimated" if cost else "unknown"),
            cost_source="source-report" if actual else "price-table",
        )

    def _parse_audit(self, path):
        dir_name = os.path.basename(os.path.dirname(path))
        for line in iter_lines(path):
            if '"result"' not in line or '"modelUsage"' not in line:
                continue
            o = safe_json(line)
            if not isinstance(o, dict) or o.get("type") != "result":
                continue
            usage = o.get("modelUsage") or {}
            if not isinstance(usage, dict) or not usage:
                continue          # 实测存在 modelUsage 为空字典的零用量行
            ts = to_epoch(o.get("_audit_timestamp") or o.get("timestamp"))
            sid = str(o.get("session_id") or "")
            if self._has_ledger_twin((dir_name, sid), ts):
                continue          # 同一批已由 usage-ledger 记账，避免成倍虚增
            meta = self._meta_for(dir_name) or self._meta_for(sid)
            session = str((meta or {}).get("sessionId") or sid or dir_name)
            project = self._project_of(meta)
            extra = {"title": str((meta or {}).get("title") or ""),
                     "uuid": str(o.get("uuid") or ""),
                     "request_id": str(o.get("requestId") or o.get("request_id") or ""),
                     "num_turns": _i(o.get("num_turns"))}
            for raw, u in sorted(usage.items()):
                rec = self._row(ts, raw, u, session, project, path, extra)
                if rec is not None:
                    yield rec

    def _parse_ledger(self, path):
        for line in iter_lines(path):
            if '"models"' not in line:
                continue
            o = safe_json(line)
            if not isinstance(o, dict):
                continue
            usage = o.get("models") or {}
            if not isinstance(usage, dict) or not usage:
                continue
            ts = to_epoch(o.get("ts"))
            sid = str(o.get("sessionId") or "")
            meta = self._meta_for(sid)
            session = str((meta or {}).get("sessionId") or sid)
            project = self._project_of(meta)
            extra = {"title": str((meta or {}).get("title") or ""),
                     "origin": str(o.get("origin") or ""),
                     "surface": str(o.get("surface") or ""),
                     "request_id": str(o.get("requestId") or o.get("request_id") or
                                         o.get("id") or o.get("uuid") or "")}
            for raw, u in sorted(usage.items()):
                rec = self._row(ts, raw, u, session, project, path, extra)
                if rec is not None:
                    yield rec

    def probe(self):
        for root in self._roots():
            base = os.path.join(root, self.SUBDIR)
            if not os.path.isdir(base):
                continue
            for space in self._spaces():
                f = glob_files(os.path.join(space, "*", "audit.jsonl"))
                if f:
                    return True, f[0]
                f = glob_files(os.path.join(space, "usage-ledger", "*.ndjson"))
                if f:
                    return True, f[0]
            return False, base
        return False, ""


# ==========================================================================
# 5.8 QoderWork（阿里云）
# ==========================================================================
#
#   · 用量只在 ~/.qoderwork/logs/sessions/<项目编码>/<会话>/segments/*.jsonl 的
#     turn.finished 事件里，data 带 input_tokens/output_tokens/
#     cache_read_input_tokens/cache_creation_input_tokens。
#   · 项目名在 session.config.loaded 的 project_root，模型名在 turn.started 的 model，
#     都不在 turn.finished 自己身上，所以要读完整个分段文件再产出。
#   · ⚠ 同目录 projects/**/*.jsonl 里也有 usage 字样，但四个 token 全是 0，不是用量源。
# ==========================================================================

class QoderWorkAdapter(Adapter):
    id = "qoderwork"
    name = "QoderWork"
    vendor = "阿里云"
    category = "cli"
    color = "#0ea5e9"
    glyph = "QW"
    homepage = "https://qoder.com"
    detail = "读取 ~/.qoderwork/logs/sessions/**/segments/*.jsonl 中每轮 turn.finished 的 token 用量"

    HOME_ENV = "QODERWORK_HOME"

    def _roots(self):
        env = os.environ.get(self.HOME_ENV)
        if env:
            return [expand(env)]
        return [os.path.join(HOME, ".qoderwork")]

    def enumerate(self):
        return glob_files(*[
            os.path.join(r, "logs", "sessions", "*", "*", "segments", "*.jsonl")
            for r in self._roots()
        ])

    def parse(self, path):
        session = os.path.basename(os.path.dirname(os.path.dirname(path)))
        project, model, rows = "", "", []
        for line in iter_lines(path):
            if ('"turn.finished"' not in line and '"project_root"' not in line
                    and '"turn.started"' not in line
                    and '"model.request.started"' not in line):
                continue
            o = safe_json(line)
            if not isinstance(o, dict):
                continue
            kind = str(o.get("type") or "")
            data = o.get("data") if isinstance(o.get("data"), dict) else {}
            if kind == "session.config.loaded":
                project = norm_project(data.get("project_root") or "")
                continue
            if data.get("model"):
                model = str(data.get("model") or "")
            if kind != "turn.finished":
                continue
            inp = _i(data.get("input_tokens"))
            out = _i(data.get("output_tokens"))
            cr = _i(data.get("cache_read_input_tokens"))
            cw = _i(data.get("cache_creation_input_tokens"))
            if inp + out + cr + cw <= 0:
                continue          # 失败轮次四个 token 全是 0
            rows.append((to_epoch(o.get("ts")), inp, out, cr, cw,
                         str(o.get("turn_id") or ""), str(data.get("reason") or ""), model))
        for ts, inp, out, cr, cw, turn_id, reason, row_model in rows:
            name = row_model or model or "qoder-work"
            yield UsageRecord(
                tool=self.id, ts=ts, model=name, project=project, session=session,
                input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=0,
                cost=estimate_cost(name, inp, out, cr, cw), src=path,
                extra={"turn_id": turn_id, "reason": reason, "raw_model": model},
            )

    def probe(self):
        for root in self._roots():
            base = os.path.join(root, "logs", "sessions")
            if not os.path.isdir(base):
                continue
            f = self.enumerate()
            if f:
                return True, f[0]
            return False, base
        return False, ""


# ==========================================================================
# 6. Cline / Roo Code / Kilo Code（VS Code 系插件）
# ==========================================================================

class ClineFamilyAdapter(Adapter):
    id = "cline"
    name = "Cline / Roo / Kilo"
    vendor = "开源插件"
    category = "extension"
    color = "#8b5cf6"
    glyph = "◆"
    homepage = "https://cline.bot"
    detail = "读取 VS Code 系 globalStorage 下各任务 ui_messages.json 的 tokensIn/tokensOut/cost"

    EXT_IDS = (
        "saoudrizwan.claude-dev",        # Cline
        "rooveterinaryinc.roo-cline",    # Roo Code
        "kilocode.kilo-code",            # Kilo Code
        "claude-dev.claude-dev",
    )
    HOSTS = ("Code", "Code - Insiders", "Cursor", "Trae", "Trae CN", "Windsurf", "VSCodium")

    def enumerate(self):
        out = []
        for host in self.HOSTS:
            base = os.path.join(APPDATA, host, "User", "globalStorage")
            for ext in self.EXT_IDS:
                out.extend(glob_files(os.path.join(base, ext, "tasks", "*", "ui_messages.json")))
        return out

    def parse(self, path):
        try:
            with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
                items = json.load(fh)
        except Exception:
            raise
        if not isinstance(items, list):
            return

        # 任务级元信息
        task_dir = os.path.dirname(path)
        model, cwd = "", ""
        for name in ("task_metadata.json", "api_conversation_history.json"):
            fp = os.path.join(task_dir, name)
            if os.path.exists(fp):
                try:
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        meta = json.load(fh)
                except Exception:
                    continue
                if isinstance(meta, dict):
                    model = model or str(dig(meta, "modelId", "model", "modelInfo.modelId", default="") or "")
                    cwd = cwd or str(dig(meta, "cwd", "workspace", "environment_details.cwd", default="") or "")

        session = os.path.basename(task_dir)
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("say") != "api_req_started":
                continue
            payload = it.get("text")
            if isinstance(payload, str):
                obj = safe_json(payload) or {}
            elif isinstance(payload, dict):
                obj = payload
            else:
                continue
            inp = _i(dig(obj, "tokensIn", "tokens_in", default=0))
            out = _i(dig(obj, "tokensOut", "tokens_out", default=0))
            cr = _i(dig(obj, "cacheReads", "cache_reads", default=0))
            cw = _i(dig(obj, "cacheWrites", "cache_writes", default=0))
            if inp + out + cr + cw <= 0:
                continue
            raw_cost = dig(obj, "cost", default=None)
            actual = raw_cost is not None
            cost = float(raw_cost) if actual else 0.0
            mdl = str(dig(obj, "modelInfo.modelId", default="") or model or "cline")
            inp = max(0, inp - cr)
            if not actual:
                cost = estimate_cost(mdl, inp, out, cr, cw)
            host = ""
            for part in path.split(os.sep):
                if part in ClineFamilyAdapter.HOSTS:
                    host = part
                    break
            yield UsageRecord(
                tool=self.id, ts=to_epoch(it.get("ts")), model=mdl,
                project=norm_project(cwd), session=session,
                input=inp, output=out, cache_read=cr, cache_write=cw,
                cost=cost, src=path,
                cost_kind="actual" if actual else ("estimated" if cost else "unknown"),
                extra={"host": host, "cwd": cwd},
            )

    def probe(self):
        files = self.enumerate()
        if files:
            return True, os.path.dirname(os.path.dirname(os.path.dirname(files[0])))
        for host in self.HOSTS:
            base = os.path.join(APPDATA, host, "User", "globalStorage")
            for ext in self.EXT_IDS:
                if os.path.isdir(os.path.join(base, ext)):
                    return True, os.path.join(base, ext)
        return False, ""


# ==========================================================================
# 7. Continue.dev
# ==========================================================================

class ContinueAdapter(Adapter):
    id = "continue"
    name = "Continue.dev"
    vendor = "Continue"
    category = "extension"
    color = "#06b6d4"
    glyph = "→"
    homepage = "https://continue.dev"
    detail = "读取 ~/.continue/sessions/*.json 中的 usage 记录"

    def enumerate(self):
        return glob_files(os.path.join(HOME, ".continue", "sessions", "*.json"))

    def parse(self, path):
        o = None
        if os.path.getsize(path) < 40_000_000:
            with open(path, encoding="utf-8", errors="replace") as fh:
                o = safe_json(fh.read())
        if not isinstance(o, dict):
            return
        session = o.get("sessionId") or os.path.basename(path).rsplit(".", 1)[0]
        title = o.get("title") or ""
        for h in (o.get("history") or []):
            if not isinstance(h, dict):
                continue
            u = h.get("usage")
            if not isinstance(u, dict):
                continue
            inp = _i(dig(u, "promptTokens", "input_tokens", "prompt_tokens", default=0))
            out = _i(dig(u, "completionTokens", "output_tokens", "completion_tokens", default=0))
            cr = _i(dig(u, "cacheRead", "cache_read", default=0))
            cw = _i(dig(u, "cacheWrite", "cache_write", default=0))
            if inp + out + cr + cw <= 0:
                continue
            model = str(dig(h, "model.title", "model.model", "model", default="continue"))
            yield UsageRecord(
                tool=self.id, ts=to_epoch(dig(h, "timestamp", "date", default=os.path.getmtime(path))),
                model=model, project="", session=session, input=inp, output=out,
                cache_read=cr, cache_write=cw,
                cost=estimate_cost(model, inp, out, cr, cw), src=path,
                extra={"title": title},
            )

    def probe(self):
        p = os.path.join(HOME, ".continue")
        return os.path.isdir(p), p


# ==========================================================================
# 8. GitHub Copilot Chat（VS Code）
# ==========================================================================

class CopilotAdapter(Adapter):
    id = "copilot"
    name = "GitHub Copilot"
    vendor = "GitHub"
    category = "extension"
    color = "#22c55e"
    glyph = "✈"
    homepage = "https://github.com/features/copilot"
    detail = "统计 VS Code 聊天会话文件中的 token 用量；早期版本不回写 token 则仅上报会话数"

    HOSTS = ("Code", "Code - Insiders", "VSCodium")

    def enumerate(self):
        out = []
        for host in self.HOSTS:
            for pat in ("emptyWindowChatSessions", "chatSessions", "chatEditingSessions"):
                out.extend(glob_files(os.path.join(
                    APPDATA, host, "User", "globalStorage", pat, "*.json"),
                    os.path.join(APPDATA, host, "User", "workspaceStorage", "*", pat, "*.json")))
        return out

    def parse(self, path):
        if is_mostly_binary(path) or os.path.getsize(path) > 80 * 1024 * 1024:
            return
        with open(path, encoding="utf-8", errors="replace") as fh:
            o = safe_json(fh.read())
        if not isinstance(o, dict):
            return
        session = str(dig(o, "sessionId", "id", default=os.path.basename(path)))
        for req in (o.get("requests") or []):
            if not isinstance(req, dict):
                continue
            t = first_nested(req, {"tokens", "usage", "tokenCount", "promptTokens"})
            inp = _i(dig(req, "promptTokens", "tokens.prompt", "usage.prompt_tokens", default=0))
            out = _i(dig(req, "completionTokens", "tokens.completion", "usage.completion_tokens", default=0))
            if isinstance(t, dict):
                inp = inp or _i(dig(t, "prompt", "input", "prompt_tokens", default=0))
                out = out or _i(dig(t, "completion", "output", "completion_tokens", default=0))
            if inp + out <= 0:
                continue
            model = str(dig(req, "modelId", "model.id", "model", default="copilot"))
            yield UsageRecord(
                tool=self.id,
                ts=to_epoch(dig(req, "timestamp", "creationDate", default=0)) or int(os.path.getmtime(path)),
                model=model, project="", session=session, input=inp, output=out,
                cost=estimate_cost(model, inp, out), src=path,
            )

    def probe(self):
        for host in self.HOSTS:
            base = os.path.join(APPDATA, host, "User", "globalStorage")
            for ext in ("github.copilot-chat", "github.copilot"):
                if os.path.isdir(os.path.join(base, ext)):
                    return True, os.path.join(base, ext)
        return False, ""


# ==========================================================================
# 9. Cursor（云端计量 · 本地仅检测）
# ==========================================================================

class CursorAdapter(Adapter):
    QUALITY = "detection"
    id = "cursor"
    name = "Cursor"
    vendor = "Anysphere"
    category = "editor"
    color = "#e879f9"
    glyph = "▮"
    homepage = "https://cursor.com"
    detail = "本地 state.vscdb 只保存会话结构与补全行数，token 由 Cursor 云端计量，故不纳入本地合计"

    def enumerate(self):
        return []

    def probe(self):
        base = os.path.join(APPDATA, "Cursor", "User", "globalStorage", "state.vscdb")
        return os.path.exists(base), base

    def side_stats(self):
        """额外产出一份「非 token 用量」侧写：补全采纳行数 / 会话数。"""
        base = os.path.join(APPDATA, "Cursor", "User", "globalStorage", "state.vscdb")
        stats = {"composers": 0, "tabAcceptedLines": 0, "composerAcceptedLines": 0,
                 "days": 0, "last_activity": 0}
        if not os.path.exists(base):
            return stats
        try:
            con = source_connection(base)
            try:
                stats["composers"] = con.execute(
                    "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND value IS NOT NULL"
                ).fetchone()[0]
            except Exception:
                pass
            last = 0
            for k, v in con.execute(
                "SELECT key, value FROM ItemTable WHERE key LIKE 'aiCodeTracking.dailyStats%'"):
                try:
                    o = json.loads(v)
                except Exception:
                    continue
                stats["tabAcceptedLines"] += _i(o.get("tabAcceptedLines"))
                stats["composerAcceptedLines"] += _i(o.get("composerAcceptedLines"))
                stats["days"] += 1
                try:
                    last = max(last, int(to_epoch(o.get("date") + "T23:59:59")))
                except Exception:
                    pass
            stats["last_activity"] = last
            con.close()
        except Exception:
            pass
        return stats


# ==========================================================================
# 10. Trae / Trae CN（数据加密 · 仅检测）
# ==========================================================================

class TraeAdapter(Adapter):
    QUALITY = "detection"
    id = "trae"
    name = "Trae / Trae CN"
    vendor = "字节跳动"
    category = "editor"
    color = "#38bdf8"
    glyph = "T"
    homepage = "https://www.trae.ai"
    detail = "ModularData 下的会话库为加密格式，本地无法解出 token；用量以官方账单为准"

    def _dirs(self):
        return [os.path.join(APPDATA, "Trae"), os.path.join(APPDATA, "Trae CN")]

    def enumerate(self):
        return []

    def probe(self):
        found = [d for d in self._dirs() if os.path.isdir(d)]
        return bool(found), (found[0] if found else "")

    def side_stats(self):
        st = {"engines": 0, "dbs": 0, "size": 0, "last_activity": 0}
        for d in self._dirs():
            md = os.path.join(d, "ModularData")
            if not os.path.isdir(md):
                continue
            for cur, _dirs, files in walk_readable(md):
                for f in files:
                    if f.endswith(".db"):
                        fp = os.path.join(cur, f)
                        st["dbs"] += 1
                        try:
                            st["size"] += os.path.getsize(fp)
                            st["last_activity"] = max(st["last_activity"], int(os.path.getmtime(fp)))
                        except OSError:
                            pass
            st["engines"] += len([x for x in os.listdir(md) if os.path.isdir(os.path.join(md, x))])
        return st


# ==========================================================================
# 11. Windsurf / Codeium
# ==========================================================================

class WindsurfAdapter(Adapter):
    QUALITY = "detection"
    id = "windsurf"
    name = "Windsurf / Codeium"
    vendor = "Codeium"
    category = "editor"
    color = "#34d399"
    glyph = "W"
    homepage = "https://windsurf.com"
    detail = "检测本地缓存与日志；token 在 Codeium 云端计量，本地不落盘"

    def _paths(self):
        return [os.path.join(HOME, ".windsurf"), os.path.join(HOME, ".codeium"),
                os.path.join(APPDATA, "Windsurf"),
                os.path.join(APPDATA, "com.chao.windsurf-account-manager")]

    def enumerate(self):
        return []

    def probe(self):
        found = [p for p in self._paths() if os.path.exists(p)]
        return bool(found), (found[0] if found else "")

    def side_stats(self):
        st = {"logs": 0, "size": 0, "last_activity": 0}
        for p in self._paths():
            if not os.path.isdir(p):
                continue
            for cur, _d, files in walk_readable(p):
                for f in files:
                    fp = os.path.join(cur, f)
                    try:
                        st["size"] += os.path.getsize(fp)
                        st["last_activity"] = max(st["last_activity"], int(os.path.getmtime(fp)))
                        st["logs"] += 1
                    except OSError:
                        pass
        return st


# ==========================================================================
# 12. Cherry Studio
# ==========================================================================

class CherryStudioAdapter(Adapter):
    QUALITY = "heuristic"
    id = "cherry-studio"
    name = "Cherry Studio"
    vendor = "Cherry Studio"
    category = "desktop"
    color = "#fb7185"
    glyph = "🍒"
    homepage = "https://cherry-ai.com"
    detail = "会话与用量记录保存在 Electron IndexedDB(LevelDB) 中，做只读尽力解析"

    WORDS = (b"prompt_tokens", b"completion_tokens", b"total_tokens")

    def _dir(self):
        return os.path.join(APPDATA, "CherryStudio")

    def enumerate(self):
        d = os.path.join(self._dir(), "IndexedDB")
        if not os.path.isdir(d):
            return []
        out = []
        for cur, _dd, files in walk_readable(d):
            for f in files:
                fp = os.path.join(cur, f)
                try:
                    if 1024 < os.path.getsize(fp) < 120 * 1024 * 1024:
                        out.append(fp)
                except OSError:
                    pass
        return out

    _NUM = re.compile(rb'"?(prompt_tokens|completion_tokens|total_tokens)"?\s*[:=]?\s*[^\d]{0,6}(\d{1,12})')

    def parse(self, path):
        """在 LevelDB 原始块里定位 usage 三元组；能读多少算多少。"""
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            anchor_ts = int(os.path.getmtime(path))
        except OSError:
            raise
        if not any(w in data for w in self.WORDS):
            return
        # 以 total_tokens 为锚点，向左右各 400 字节找同组的 prompt/completion
        for m in re.finditer(rb"total_tokens", data):
            lo = max(0, m.start() - 400)
            hi = min(len(data), m.end() + 400)
            seg = data[lo:hi]
            vals = {}
            for k, v in self._NUM.findall(seg):
                key = k.decode()
                if key not in vals:
                    try:
                        vals[key] = int(v)
                    except ValueError:
                        pass
            total = vals.get("total_tokens", 0)
            inp = vals.get("prompt_tokens", 0)
            out = vals.get("completion_tokens", 0)
            # 阈值过滤：LevelDB 块里混着大量干扰字节，宁可少报也不要假数据
            if total < 200 or total > 20_000_000:
                continue
            if inp + out <= 0 or abs(inp + out - total) > max(32, total * 0.15):
                continue
            yield UsageRecord(
                tool=self.id, ts=anchor_ts, model="cherry-studio", project="", session="",
                input=inp, output=out, cost=estimate_cost("", inp, out), src=path,
                # LevelDB 压实期间同一份 usage 会同时留在 .log 和 .ldb 里；
                # 按内容三元组建身份，让插入层把跨文件副本去重掉。
                # 代价：两次完全相同数值的真实调用也会被合并（近似口径下
                # 少计优于多计）。
                event_id="cherry-studio:" + hashlib.sha256(
                    ("%d/%d/%d" % (inp, out, total)).encode("ascii")).hexdigest()[:40],
                extra={"heuristic": True, "precision": "approx"},
            )

    def probe(self):
        p = self._dir()
        return os.path.isdir(p), p


# ==========================================================================
# 13. LM Studio
# ==========================================================================

class LMStudioAdapter(Adapter):
    QUALITY = "heuristic"
    id = "lmstudio"
    CATEGORY_LOCAL = True
    name = "LM Studio"
    vendor = "LM Studio"
    category = "desktop"
    color = "#a78bfa"
    glyph = "⌘"
    homepage = "https://lmstudio.ai"
    detail = "读取本地对话记录中统计到的 token 数（完全离线推理，无 API 费用）"

    def enumerate(self):
        pats = [os.path.join(HOME, ".lmstudio", "**", "*.json"),
                os.path.join(APPDATA, "LM Studio", "**", "*.json"),
                os.path.join(HOME, ".cache", "lm-studio", "**", "*.json")]
        out = []
        for p in glob_files(*pats):
            if any(x in p for x in ("node_modules", "extensions", "Cache", "GPUCache")):
                continue
            out.append(p)
        return out

    def parse(self, path):
        if os.path.getsize(path) > 30 * 1024 * 1024 or is_mostly_binary(path):
            return
        with open(path, encoding="utf-8", errors="replace") as fh:
            o = safe_json(fh.read())
        if not isinstance(o, (dict, list)):
            return
        model = str(dig(o, "model", "modelIdentifier", default="lmstudio") if isinstance(o, dict) else "lmstudio")
        hits = []

        def walk(node, depth=0):
            """返回本子树已入账的 (input, output) 合计。

            父节点的累计 usage 与子节点逐条之和完全相等时，父节点只是
            聚合包装——两条都入账会把同一批 token 算两遍。
            """
            if depth > 6 or len(hits) > 200:
                return (0, 0)
            children = (node.values() if isinstance(node, dict)
                        else node if isinstance(node, list) else ())
            child_sums = [walk(v, depth + 1) for v in children]
            child_inp = sum(t[0] for t in child_sums)
            child_out = sum(t[1] for t in child_sums)
            if isinstance(node, dict):
                inp = _i(dig(node, "promptTokens", "prompt_tokens", "inputTokens", default=0))
                out = _i(dig(node, "completionTokens", "completion_tokens", "outputTokens", default=0))
                if inp + out > 0 and (inp, out) != (child_inp, child_out):
                    hits.append((inp, out, node))
                    return (child_inp + inp, child_out + out)
            return (child_inp, child_out)

        walk(o)
        fallback = int(os.path.getmtime(path))
        for i, (inp, out, node) in enumerate(hits):
            yield UsageRecord(
                tool=self.id, ts=to_epoch(dig(node, "createdAt", "timestamp", default=0)) or fallback,
                model=str(dig(node, "model", default=model)), project="",
                session=os.path.basename(path), input=inp, output=out, cost=0.0,
                src=path, extra={"local": True},
            )

    def probe(self):
        p = os.path.join(HOME, ".lmstudio")
        return os.path.isdir(p), p


# ==========================================================================
# 14. Ollama（本地推理）
#     服务端日志通常不含 token 数，但部分配置 / 代理日志里会带上
#     prompt_eval_count / eval_count，这里做一次通用抽取，取不到就不报。
# ==========================================================================

class OllamaAdapter(Adapter):
    QUALITY = "heuristic"
    id = "ollama"
    CATEGORY_LOCAL = True
    name = "Ollama"
    vendor = "Ollama"
    category = "desktop"
    color = "#94a3b8"
    glyph = "◍"
    homepage = "https://ollama.com"
    detail = ("本地推理不产生 API 费用；若日志/代理里记录了 prompt_eval_count 与 eval_count，"
              "会作为本地推理量采集，成本记为 0")

    PATHS = ("~/.ollama", "%LOCALAPPDATA%/Ollama", "%APPDATA%/Ollama",
             "~/.cache/ollama", "%LOCALAPPDATA%/ollama")

    def _dirs(self):
        return [expand(p) for p in self.PATHS if os.path.isdir(expand(p))]

    def enumerate(self):
        out = []
        for d in self._dirs():
            for cur, dirs, files in walk_readable(d):
                dirs[:] = [x for x in dirs if x not in ("models", "blobs")
                           and x.lower() not in SECRET_DIRS]
                for f in files:
                    if f.lower().endswith((".log", ".json", ".jsonl", ".ndjson", ".txt")):
                        fp = os.path.join(cur, f)
                        if is_secret_path(fp):
                            continue
                        try:
                            if 0 < os.path.getsize(fp) <= MAX_TEXT_SCAN:
                                out.append(fp)
                        except OSError:
                            pass
        self.enumeration_complete = len(out) <= 200
        return out[:200]

    def parse(self, path):
        if is_mostly_binary(path):
            return
        api = ApiLogAdapter(self.cfg)
        if path.lower().endswith((".json", ".jsonl", ".ndjson")):
            records = api.parse(path)
        else:
            def log_records():
                for line in iter_lines(path):
                    yield from api._from_text(line, path,
                        to_epoch(api._match(api._TS, line, str)) or int(os.path.getmtime(path)))
            records = log_records()
        for record in records:
            record.tool = self.id
            record.ts = record.ts or int(os.path.getmtime(path))
            record.cost = 0.0
            record.cost_kind = "free"
            record.cost_source = "local-inference"
            record.session = record.session or os.path.basename(path)
            record.extra["local"] = True
            yield record

    def probe(self):
        ds = self._dirs()
        return bool(ds), (ds[0] if ds else "")

    def side_stats(self):
        st = {"models": 0, "size": 0, "last_activity": 0}
        md = os.path.join(HOME, ".ollama", "models", "manifests")
        if os.path.isdir(md):
            for cur, _d, files in walk_readable(md):
                for f in files:
                    st["models"] += 1
                    try:
                        st["last_activity"] = max(st["last_activity"], int(os.path.getmtime(os.path.join(cur, f))))
                    except OSError:
                        pass
        return st


# ==========================================================================
# 15. Aider
# ==========================================================================

class AiderAdapter(Adapter):
    QUALITY = "aggregate"
    id = "aider"
    name = "Aider"
    vendor = "Aider"
    category = "cli"
    color = "#facc15"
    glyph = "🅰"
    homepage = "https://aider.chat"
    detail = "扫描项目中的 .aider.chat.history.md 与 ~/.aider 下的用量记录"

    ROOTS = ("Desktop", "Documents", "Projects", "code", "src", "dev", "work")

    def enumerate(self):
        out = glob_files(os.path.join(HOME, ".aider", "**", "*.json"),
                         os.path.join(HOME, ".aider", "**", "*.jsonl"))
        for root in self.ROOTS:
            base = os.path.join(HOME, root)
            if not os.path.isdir(base):
                continue
            for cur, dirs, files in walk_readable(base):
                depth = cur[len(base):].count(os.sep)
                if depth >= 3:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if d not in (
                    "node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build")]
                for f in files:
                    if f == ".aider.chat.history.md" or f.startswith(".aider."):
                        out.append(os.path.join(cur, f))
        self.enumeration_complete = len(out) <= 2000
        return out[:2000]

    _LINE = re.compile(
        r"tokens?[^\n]{0,40}?([\d,]{2,})\s*sent[^\n]{0,40}?([\d,]{2,})\s*received", re.I)

    def parse(self, path):
        if not path.endswith(".md"):
            if is_mostly_binary(path):
                return
        try:
            with open(path, encoding="utf-8", errors="strict") as fh:
                text = fh.read()
        except OSError:
            raise
        cur_model = "aider"
        for line in text.splitlines():
            m = re.match(r"^#{2,4}\s+(.+)$", line.strip())
            if m and re.search(r"gpt|claude|gemini|deepseek|qwen|glm|o[13]|llama|mistral", m.group(1), re.I):
                # 标题行常带日期时间前缀（"#### 2024-05-01 10:00 gpt-4o"）；
                # 整行当模型名会让估价永远匹配不到，剥掉前缀只留模型部分。
                name = re.sub(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([\sT]\d{1,2}:\d{2}(:\d{2})?)?\s*",
                              "", m.group(1)).strip()
                if name:
                    cur_model = name
            mm = self._LINE.search(line)
            if mm:
                inp = int(mm.group(1).replace(",", ""))
                out = int(mm.group(2).replace(",", ""))
                if inp + out <= 0:
                    continue
                yield UsageRecord(
                    tool=self.id, ts=to_epoch(int(os.path.getmtime(path))), model=cur_model,
                    project=norm_project(os.path.dirname(path)), session=os.path.basename(path),
                    input=inp, output=out,
                    cost=estimate_cost(cur_model, inp, out), src=path,
                )

    def probe(self):
        p = os.path.join(HOME, ".aider")
        return os.path.isdir(p), p


# ==========================================================================
# 16. 通用 API 日志（用户自定义目录）
# ==========================================================================

# 各类 API 响应的 usage 形态（供 API 日志适配器与通用厂商扫描器共用）
RX_OPENAI = re.compile(
    r'"prompt_tokens"\s*:\s*(\d+)[^}]{0,240}?"completion_tokens"\s*:\s*(\d+)', re.S)
RX_ANTHRO = re.compile(
    r'"input_tokens"\s*:\s*(\d+)[^}]{0,240}?"output_tokens"\s*:\s*(\d+)', re.S)
RX_EVAL = re.compile(
    r'"prompt_eval_count"\s*:\s*(\d+)[^}]{0,240}?"eval_count"\s*:\s*(\d+)', re.S)
RX_CACHE_R = re.compile(r'"cache_read_input_tokens"\s*:\s*(\d+)')
RX_CACHE_W = re.compile(r'"cache_creation_input_tokens"\s*:\s*(\d+)')
RX_CACHE_R2 = re.compile(r'"(?:cached_tokens|cache_read_tokens|cacheReads)"\s*:\s*(\d+)')
RX_MODEL = re.compile(r'"model"\s*:\s*"([^"]{1,80})"')
RX_TS = re.compile(
    r'"(?:timestamp|created|created_at|time|ts|date|updatedAt)"\s*:\s*"?([0-9T:\-\.\+Z ]{10,32})"?')


class ApiLogAdapter(Adapter):
    id = "api-log"
    name = "大模型 API 调用日志"
    vendor = "自定义目录 / 网关"
    category = "api"
    color = "#f97316"
    glyph = "⇄"
    homepage = ""
    detail = ("扫描你指定的目录 + 常见网关默认目录，自动识别 OpenAI / Anthropic 兼容格式的 "
              "usage 记录（JSONL、日志、JSON、CSV）")
    INCREMENTAL_JSONL = True

    EXTS = (".jsonl", ".log", ".json", ".txt", ".csv", ".ndjson")
    SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist",
                 "build", "Cache", "GPUCache", "Code Cache"}
    # 开箱即用的网关 / 代理日志目录（存在才会被扫描）
    BUILTIN_ROOTS = (
        "~/.litellm", "~/.cache/litellm", "~/litellm",
        "~/one-api/logs", "~/new-api/logs", "~/gpt-load/logs",
        "%APPDATA%/LiteLLM", "%LOCALAPPDATA%/LiteLLM",
        "~/llm-logs", "~/logs/llm", "~/.gpt-load",
    )

    _OPENAI = RX_OPENAI
    _ANTHRO = RX_ANTHRO
    _EVAL = RX_EVAL
    _CACHE_R = RX_CACHE_R
    _CACHE_W = RX_CACHE_W
    _CACHE_R2 = RX_CACHE_R2
    _MODEL = RX_MODEL
    _TS = RX_TS

    def roots(self):
        return [expand(r) for r in self.BUILTIN_ROOTS] + \
               [expand(r) for r in (self.cfg.get("api_log_roots") or [])]

    def enumerate(self):
        roots = self.roots()
        out = []
        for r in roots:
            if not os.path.isdir(r):
                continue
            for cur, dirs, files in walk_readable(r):
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS
                           and d.lower() not in SECRET_DIRS]
                for f in files:
                    if f.lower().endswith(self.EXTS):
                        fp = os.path.join(cur, f)
                        if is_secret_path(fp) or is_self_path(fp):
                            continue
                        try:
                            # JSONL/NDJSON are streamed with a bounded line
                            # buffer. Keep them visible above the legacy text
                            # heuristic cap; Scanner enforces max_file_mb and
                            # reports an oversized source without erasing it.
                            if f.lower().endswith((".jsonl", ".ndjson")) or os.path.getsize(fp) <= MAX_TEXT_SCAN:
                                out.append(fp)
                        except OSError:
                            pass
        self.enumeration_complete = len(out) <= 4000
        return out[:4000]

    def parse(self, path):
        if is_mostly_binary(path):
            return
        if path.lower().endswith(".csv"):
            yield from self._parse_csv(path)
            return
        if path.lower().endswith(".json"):
            with open(path, encoding="utf-8-sig") as fh:
                obj = json.load(fh)
            yield from self._from_object(obj, path)
            return
        for line in iter_lines(path, encoding="utf-8-sig"):
            if not line.strip():
                continue
            if path.lower().endswith((".jsonl", ".ndjson")) or line.lstrip().startswith(("{", "[")):
                obj = json.loads(line)
                yield from self._from_object(obj, path)
            elif "usage" in line or "tokens" in line or "eval_count" in line:
                yield from self._from_text(line, path, to_epoch(self._match(self._TS, line, str)))

    def parse_lines(self, path, lines, state):
        for line in lines:
            if line.strip():
                yield from self._from_object(json.loads(line), path)

    def _from_object(self, obj, path, inherited_model="", inherited_ts=0):
        if isinstance(obj, list):
            for child in obj:
                yield from self._from_object(child, path, inherited_model, inherited_ts)
            return
        if not isinstance(obj, dict):
            return
        model = str(dig(obj, "model", "model_name", default=inherited_model) or "")
        ts = to_epoch(dig(obj, "timestamp", "created", "created_at", "time", "ts", "date",
                          "updatedAt", default=inherited_ts))
        usage = obj.get("usage")
        if not isinstance(usage, dict) and any(k in obj for k in
                ("prompt_tokens", "input_tokens", "prompt_eval_count")):
            usage = obj
        if isinstance(usage, dict):
            local = "prompt_eval_count" in usage
            openai = "prompt_tokens" in usage or "input_tokens_details" in usage
            raw_input = _i(dig(usage, "prompt_tokens", "input_tokens", "prompt_eval_count", default=0))
            out = _i(dig(usage, "completion_tokens", "output_tokens", "eval_count", default=0))
            cr = _i(dig(usage, "prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens",
                        "cache_read_input_tokens", "cache_read_tokens", "cached_tokens", default=0))
            cw = _i(dig(usage, "cache_creation_input_tokens", "cache_write_input_tokens", default=0))
            included_reasoning = _i(dig(usage, "completion_tokens_details.reasoning_tokens",
                                       "output_tokens_details.reasoning_tokens", default=0))
            reasoning = included_reasoning or _i(dig(usage, "reasoning_tokens", "reasoningTokens", default=0))
            # OpenAI Chat/Responses details are a subset of the output total.
            # Store the visible portion separately so total/cost add reasoning
            # exactly once. Flat reasoning counters are explicitly separate,
            # except in OpenRouter-style logs (nested usage.cost object or a
            # provider field) where completion_tokens already includes them.
            openrouter = isinstance(usage.get("cost"), dict) or "provider" in obj
            if not included_reasoning and reasoning and openrouter:
                included_reasoning = reasoning
            if included_reasoning > out:
                raise ValueError("reasoning detail exceeds reported output tokens")
            out -= included_reasoning
            if openai and cr > raw_input:
                raise ValueError("cached detail exceeds reported input tokens")
            inp = raw_input - cr if openai else raw_input
            if inp + out + cr + cw + reasoning > 0:
                name = model or os.path.basename(path)
                raw_cost = dig(obj, "cost_usd", "costUSD", "cost", default=None)
                actual = raw_cost is not None
                cost = float(raw_cost) if actual else (0.0 if local else estimate_cost(name, inp, out, cr, cw, reasoning))
                project = str(dig(obj, "project", "project_name", "projectName", "metadata.project", "cwd", default="") or "")
                session = str(dig(obj, "session_id", "sessionId", "session", "conversation_id", "conversationId", default="") or "")
                request_id = str(dig(obj, "request_id", "requestId", "id", "uuid", default="") or "")
                event_id = stable_event_id(self.id, path, session or project, request_id, obj)
                yield UsageRecord(tool=self.id, ts=ts, model=name,
                    project=norm_project(project), session=session,
                    input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=reasoning,
                    cost=cost, src=path, event_id=event_id,
                    cost_kind=("actual" if actual else "free" if local else "estimated" if cost else "unknown"),
                    cost_source="source-report" if actual else "local-inference" if local else "price-table",
                    extra={"file": os.path.basename(path), "style":"local-eval" if local else "openai" if openai else "anthropic",
                           "request_id": request_id, "raw_input_tokens":raw_input,
                           "actual_cost": raw_cost if actual else None,
                           "project": project, "session": session,
                           "cache_read": cr, "cache_write": cw, "reasoning": reasoning})
            return
        # Common gateway envelopes and JSON arrays; do not traverse arbitrary
        # prompt/message text and mistake quoted examples for billed calls.
        for key in ("response", "data", "result", "records", "events", "responses"):
            if isinstance(obj.get(key), (dict, list)):
                yield from self._from_object(obj[key], path, model, ts)

    def _parse_csv(self, path):
        try:
            fh = io.open(path, "r", encoding="utf-8", errors="replace", newline="")
            sample = fh.read(20000)
            fh.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            rd = csv.DictReader(fh, dialect=dialect)
            if not rd.fieldnames:
                return
            norm = {c.strip().lower(): c for c in rd.fieldnames if c}
            pc = next((norm[k] for k in ("prompt_tokens", "input_tokens", "input") if k in norm), None)
            cc = next((norm[k] for k in ("completion_tokens", "output_tokens", "output") if k in norm), None)
            crc = next((norm[k] for k in ("cache_read", "cache_read_tokens", "cached_tokens", "cached_input_tokens") if k in norm), None)
            cwc = next((norm[k] for k in ("cache_write", "cache_write_tokens", "cache_creation_input_tokens") if k in norm), None)
            rc = next((norm[k] for k in ("reasoning", "reasoning_tokens", "thoughts") if k in norm), None)
            mc = next((norm[k] for k in ("model", "model_name") if k in norm), None)
            tc = next((norm[k] for k in ("timestamp", "time", "created", "date", "ts") if k in norm), None)
            pcj = next((norm[k] for k in ("project", "project_name", "cwd") if k in norm), None)
            sc = next((norm[k] for k in ("session", "session_id", "conversation_id") if k in norm), None)
            ic = next((norm[k] for k in ("id", "request_id", "requestid", "uuid") if k in norm), None)
            costc = next((norm[k] for k in ("cost_usd", "costusd", "cost", "actual_cost") if k in norm), None)
            if not pc and not cc:
                return
            for row in rd:
                inp = _i(row.get(pc)) if pc else 0
                out = _i(row.get(cc)) if cc else 0
                cr = _i(row.get(crc)) if crc else 0
                cw = _i(row.get(cwc)) if cwc else 0
                reasoning = _i(row.get(rc)) if rc else 0
                # Provider-style CSV totals include cache/reasoning details;
                # normalized `input` / `output` columns are already exclusive.
                if pc and pc.strip().lower() == "prompt_tokens":
                    if cr > inp:
                        raise ValueError("cached detail exceeds reported input tokens")
                    inp -= cr
                if cc and cc.strip().lower() == "completion_tokens":
                    if reasoning > out:
                        raise ValueError("reasoning detail exceeds reported output tokens")
                    out -= reasoning
                if inp + out + cr + cw + reasoning <= 0:
                    continue
                model = (row.get(mc) if mc else "") or os.path.basename(path)
                project = str(row.get(pcj) or "") if pcj else ""
                session = str(row.get(sc) or "") if sc else ""
                request_id = str(row.get(ic) or "") if ic else ""
                actual_raw = row.get(costc) if costc else None
                actual = actual_raw not in (None, "")
                cost = float(actual_raw) if actual else estimate_cost(str(model), inp, out, cr, cw, reasoning)
                event_id = stable_event_id(self.id, path, session or project, request_id, dict(row))
                yield UsageRecord(
                    tool=self.id, ts=to_epoch(row.get(tc)) if tc else 0,
                    model=str(model), project=norm_project(project), session=session,
                    input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=reasoning,
                    cost=cost, src=path, event_id=event_id,
                    cost_kind="actual" if actual else ("estimated" if cost else "unknown"),
                    cost_source="source-report" if actual else "price-table",
                    extra={"file": os.path.basename(path), "csv": True,
                           "request_id": request_id, "actual_cost": actual_raw if actual else None,
                           "project": project, "session": session,
                           "cache_read": cr, "cache_write": cw, "reasoning": reasoning},
                )
        except Exception:
            raise
        finally:
            try:
                fh.close()
            except Exception:
                pass

    @staticmethod
    def _match(rx, text, cast=int):
        m = rx.search(text)
        if not m:
            return None
        try:
            return cast(m.group(1))
        except Exception:
            return None

    def _from_text(self, text, path, ts):
        m = self._OPENAI.search(text)
        style = "openai"
        if not m:
            m = self._ANTHRO.search(text)
            style = "anthropic"
        if not m:
            m = self._EVAL.search(text)      # Ollama / llama.cpp 风格
            style = "local-eval"
        if not m:
            return
        inp = int(m.group(1))
        out = int(m.group(2))
        if inp + out <= 0 or inp > 50_000_000 or out > 50_000_000:
            return
        cr = self._match(self._CACHE_R, text) or self._match(self._CACHE_R2, text) or 0
        cw = self._match(self._CACHE_W, text) or 0
        if style == "openai":
            inp = max(0, inp - cr)
        model = self._match(self._MODEL, text, str) or os.path.basename(path)
        yield UsageRecord(
            tool=self.id, ts=ts or 0, model=str(model), project="", session="",
            input=inp, output=out, cache_read=int(cr), cache_write=int(cw),
            cost=(0.0 if style == "local-eval" else estimate_cost(str(model), inp, out, cr, cw)),
            src=path, cost_kind="free" if style == "local-eval" else "",
            extra={"file": os.path.basename(path), "style": style},
        )

    def probe(self):
        for r in self.roots():
            if os.path.isdir(r):
                return True, r
        return False, ""


# ==========================================================================
# 17. 通用厂商扫描器
#
#     很多产品的本地存储结构随版本变化、也没有官方文档，逐一定制不现实。
#     这里用一套「通用 token 抽取」逻辑统一覆盖：
#       · SQLite：只要某张表同时存在「prompt 类」与「completion 类」数值列，就按表汇总
#       · 文本  ：正则识别 OpenAI / Anthropic / Ollama 三种 usage 形态
#     子类只需声明 ID、名称与候选目录。
# ==========================================================================

_TOKEN_IN = ("prompt_tokens", "input_tokens", "tokensin", "prompttokens",
             "prompt_eval_count", "inputtoken", "in_tokens", "prompt_token_count")
_TOKEN_OUT = ("completion_tokens", "output_tokens", "tokensout", "completiontokens",
              "eval_count", "outputtoken", "out_tokens", "completion_token_count")
_NUMERIC = ("int", "integer", "real", "numeric", "number", "bigint", "double", "float")


class _VendorScanAdapter(Adapter):
    QUALITY = "heuristic"
    PATHS = ()
    EXTS = (".json", ".jsonl", ".ndjson", ".log", ".sqlite", ".db", ".sqlite3", ".vscdb")
    SKIP_DIRS = {"node_modules", ".git", "Cache", "caches", "GPUCache", "Code Cache",
                 "CachedData", "blob_storage", "Crashpad", "crashpad", "tmp", "system",
                 "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "GrShaderCache",
                 "Service Worker", "compile-server", "jcef", "index", "plugins",
                 "consentOptions", "log-idea", "Local Storage", "memwatch", "telemetry",
                 "crash", "dumps", "dumps-v2", "installer", "updater"}
    MAX_FILES = 500
    HISTORY_ONLY = True      # 只扫可能含用量的子目录名（加速，避免全盘遍历）
    CATEGORY_LOCAL = False   # True = 本地推理，成本记 0

    # ---------------- 路径 ----------------
    def _paths(self):
        out = []
        for p in self.PATHS:
            p = expand(p)
            if any(ch in p for ch in "*?["):
                out.extend(x for x in glob.glob(p, recursive=True) if os.path.isdir(x))
            elif os.path.isdir(p):
                out.append(p)
        return out

    def enumerate(self):
        seen, out = set(), []
        for root in self._paths():
            for cur, dirs, files in walk_readable(root):
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS
                           and d.lower() not in SECRET_DIRS]
                depth = cur[len(root):].count(os.sep)
                if depth >= 6:
                    dirs[:] = []
                for f in files:
                    if not f.lower().endswith(self.EXTS):
                        continue
                    fp = os.path.join(cur, f)
                    if fp in seen or is_secret_path(fp) or is_self_path(fp):
                        continue
                    try:
                        if 0 < os.path.getsize(fp) <= MAX_TEXT_SCAN:
                            seen.add(fp)
                            out.append(fp)
                    except OSError:
                        pass
                    if len(out) >= self.MAX_FILES:
                        self.enumeration_complete = False
                        return out
        return out

    # ---------------- 解析 ----------------
    @staticmethod
    def _tok_cols(cols):
        """cols: [(name, decltype)] → (输入列, 输出列) 或 None"""
        inp = out = None
        for name, decl in cols:
            low = (name or "").lower()
            numeric = any(t in (decl or "").lower() for t in _NUMERIC) or not decl
            if not numeric:
                continue
            if inp is None and any(low == k or low.endswith("_" + k) or k in low
                                   for k in _TOKEN_IN):
                inp = name
            elif out is None and any(low == k or low.endswith("_" + k) or k in low
                                     for k in _TOKEN_OUT):
                out = name
        return (inp, out) if (inp and out) else None

    def _scan_sqlite(self, path):
        recs = []
        try:
            con = source_connection(path)
        except Exception:
            raise
        try:
            con.row_factory = sqlite3.Row
            tabs = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tabs:
                try:
                    quoted_table = '"' + t.replace('"', '""') + '"'
                    cols = [(r[1], r[2]) for r in con.execute(f'PRAGMA table_info({quoted_table})')]
                except Exception:
                    continue
                pair = self._tok_cols(cols)
                if not pair:
                    continue
                ci, co = pair
                # 找时间列（可选）
                tcol = None
                for name, _decl in cols:
                    if (name or "").lower() in ("timestamp", "created_at", "createdat",
                                                "time_created", "timecreated", "ts",
                                                "date", "updated_at", "time"):
                        tcol = name
                        break
                mcol = None
                for name, _decl in cols:
                    if (name or "").lower() in ("model", "model_id", "modelid", "model_name"):
                        mcol = name
                        break
                # Aggregate only at the precision the source can support:
                # model + calendar day.  MAX(model) over a whole table used to
                # silently label every day's usage with the newest model.
                if tcol:
                    tq = '"%s"' % tcol.replace('"', '""')
                    day_expr = ("CASE WHEN typeof(%s) IN ('integer','real') "
                                "THEN date(CASE WHEN CAST(%s AS REAL)>40000000000 "
                                "THEN CAST(%s AS REAL)/1000 ELSE CAST(%s AS REAL) END,'unixepoch') "
                                "ELSE substr(CAST(%s AS TEXT),1,10) END" %
                                (tq, tq, tq, tq, tq))
                else:
                    day_expr = "''"
                model_expr = 'COALESCE(CAST("%s" AS TEXT),\'\')' % (mcol or "").replace('"', '""') if mcol else "''"
                sel = [f'SUM("{ci}")', f'SUM("{co}")', "COUNT(*)",
                       f'MIN("{tcol}")' if tcol else "NULL",
                       f'MAX("{tcol}")' if tcol else "NULL", model_expr, day_expr]
                try:
                    rows = con.execute(
                        f'SELECT {",".join(sel)} FROM {quoted_table} GROUP BY {day_expr},{model_expr}'
                    ).fetchall()
                except Exception:
                    continue
                for row in rows:
                    inp, out, cnt, tmin, tmax, model, day = row
                    inp, out = _i(inp), _i(out)
                    if inp + out <= 0:
                        continue
                    ts = to_epoch(tmax or tmin) or int(os.path.getmtime(path))
                    mdl = str(model or self.name)
                    bucket = str(day or "")
                    event_id = stable_event_id(self.id, path, "%s/%s/%s" % (t, bucket, mdl), "",
                                               {"input": inp, "output": out, "requests": _i(cnt)})
                    recs.append(UsageRecord(
                        tool=self.id, ts=ts, model=mdl, project="", session="",
                        input=inp, output=out, requests=max(1, _i(cnt)),
                        cost=(0.0 if self.CATEGORY_LOCAL else estimate_cost(mdl, inp, out)),
                        src=path, event_id=event_id,
                        extra={"table": t, "engine": "sqlite", "quality": "aggregate",
                               "day_bucket": bucket},
                    ))
            con.close()
        except Exception:
            raise
        return recs

    def _scan_text(self, path):
        recs = []
        if is_mostly_binary(path):
            return recs
        try:
            # with 是必需的：直接 io.open(...).read() 会漏掉文件句柄，
            # 扫几百个候选文件时这些句柄要等 GC 才回收。
            with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            raise
        if 'tokens' not in text and 'token_count' not in text:
            return recs
        matches = []
        for style, rx in (("openai", RX_OPENAI), ("anthropic", RX_ANTHRO), ("local-eval", RX_EVAL)):
            for match in rx.finditer(text):
                matches.append((match.start(), match.end(), style, match))
                if len(matches) >= 10000:
                    break
            if len(matches) >= 10000:
                break
        if not matches:
            return recs
        matches.sort(key=lambda item: item[0])
        fallback_ts = int(os.path.getmtime(path))
        # Resolve metadata per usage match rather than taking the first match
        # in the file.  This keeps compact multi-request logs model/day
        # accurate while retaining support for multiline JSON envelopes.
        model_fields = list(RX_MODEL.finditer(text))
        timestamp_fields = list(RX_TS.finditer(text))
        cache_read_fields = list(RX_CACHE_R.finditer(text)) + list(RX_CACHE_R2.finditer(text))
        cache_write_fields = list(RX_CACHE_W.finditer(text))
        for index, (start, end, style, match) in enumerate(matches):
            inp, out = int(match.group(1)), int(match.group(2))
            if inp + out <= 0 or inp > 50_000_000 or out > 50_000_000:
                continue
            # Restrict metadata extraction to the usage object vicinity. A
            # whole-file search paired the first model/cache with every row.
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            if line_end < 0:
                line_end = len(text)
            def nearest(fields, cross_line=False):
                local = [item for item in fields if line_start <= item.start() < line_end]
                if local:
                    return min(local, key=lambda item: abs(item.start() - start))
                if cross_line:
                    candidates = [item for item in fields if abs(item.start() - start) <= 240]
                    return min(candidates, key=lambda item: abs(item.start() - start)) if candidates else None
                return None
            cr = nearest(cache_read_fields)
            cw = nearest(cache_write_fields)
            mm = nearest(model_fields, cross_line=True)
            tm = nearest(timestamp_fields, cross_line=True)
            model = str(mm.group(1) if mm else self.name)
            ts = to_epoch(tm.group(1) if tm else fallback_ts)
            cache_read = int(cr.group(1)) if cr else 0
            cache_write = int(cw.group(1)) if cw else 0
            if style == "openai":
                inp = max(0, inp - cache_read)
            event_id = stable_event_id(self.id, path, os.path.basename(path), "text-%d" % index,
                                       {"start": start, "style": style, "input": inp,
                                        "output": out, "cache_read": cache_read,
                                        "cache_write": cache_write, "model": model})
            recs.append(UsageRecord(
                tool=self.id, ts=ts, model=model, project="", session=os.path.basename(path),
                input=inp, output=out, cache_read=cache_read, cache_write=cache_write,
                cost=(0.0 if self.CATEGORY_LOCAL else estimate_cost(model, inp, out, cache_read, cache_write)),
                src=path, event_id=event_id,
                extra={"engine": "text", "file": os.path.basename(path), "quality": "heuristic",
                       "style": style},
            ))
        return recs

    def parse(self, path):
        low = path.lower()
        if low.endswith((".sqlite", ".db", ".sqlite3", ".vscdb")):
            return iter(self._scan_sqlite(path))
        return iter(self._scan_text(path))

    def probe(self):
        ps = self._paths()
        return bool(ps), (ps[0] if ps else "")

    def side_stats(self):
        st = {"dirs": 0, "size": 0, "last_activity": 0}
        for root in self._paths():
            st["dirs"] += 1
            for cur, dirs, files in walk_readable(root):
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
                for f in files:
                    fp = os.path.join(cur, f)
                    try:
                        st["size"] += os.path.getsize(fp)
                        st["last_activity"] = max(st["last_activity"], int(os.path.getmtime(fp)))
                    except OSError:
                        pass
        return st


def _vendor(cls_id, name, vendor, category, color, glyph, homepage, detail,
            paths, local_cost=False):
    """按参数快速生成一个厂商适配器类。"""
    return type(cls_id.replace("-", "_").title().replace("_", "") + "Adapter",
                (_VendorScanAdapter,), {
                    "id": cls_id, "name": name, "vendor": vendor, "category": category,
                    "color": color, "glyph": glyph, "homepage": homepage,
                    "detail": detail, "PATHS": paths, "CATEGORY_LOCAL": local_cost,
                })


_VENDOR_SPECS = [
    # id, 名称, 厂商, 类别, 颜色, 图形, 官网, 说明, 候选目录
    ("lingma", "通义灵码", "阿里云", "extension", "#8b5cf6", "灵",
     "https://lingma.aliyun.com",
     "在插件缓存与本地日志中搜索带 token 字段的会话记录",
     ("~/.lingma", "%APPDATA%/Lingma", "%LOCALAPPDATA%/Lingma",
      "%APPDATA%/Code/User/globalStorage/alibaba-cloud.tongyi-lingma",
      "%APPDATA%/Cursor/User/globalStorage/alibaba-cloud.tongyi-lingma",
      "%APPDATA%/JetBrains/*/lingma*")),
    ("codebuddy", "CodeBuddy 插件", "腾讯云", "extension", "#3b82f6", "B",
     "https://copilot.tencent.com",
     "扫描 CodeBuddy IDE 插件残留的会话与用量文件",
     ("~/.codebuddy", "%APPDATA%/CodeBuddy", "%APPDATA%/CodeBuddy CN",
      "%LOCALAPPDATA%/CodeBuddyExtension",
      "%APPDATA%/Code/User/globalStorage/tencent-cloud.coding-copilot")),
    ("jetbrains-ai", "JetBrains AI Assistant", "JetBrains", "extension", "#fb7185", "JB",
     "https://www.jetbrains.com/ai/",
     "读取 JetBrains 系 IDE 的 AI 会话统计与日志（只扫配置与日志目录，跳过缓存）",
     ("%APPDATA%/JetBrains/*/options", "%APPDATA%/JetBrains/*/aia",
      "%LOCALAPPDATA%/JetBrains/*/log", "%APPDATA%/JetBrains/*/aiAssistant")),
    ("zed", "Zed", "Zed Industries", "editor", "#a3e635", "Z",
     "https://zed.dev",
     "读取 Zed 的 Agent 线程与助手会话记录",
     ("%APPDATA%/Zed", "%LOCALAPPDATA%/Zed", "~/.local/share/zed", "~/.config/zed")),
    ("jan", "Jan", "Menlo Research", "desktop", "#22d3ee", "J",
     "https://jan.ai",
     "读取 Jan 的本地对话线程（多为本地推理，通常无费用）",
     ("~/jan", "%APPDATA%/Jan", "%LOCALAPPDATA%/Jan"), True),
    ("chatbox", "Chatbox", "Chatbox", "desktop", "#f59e0b", "C",
     "https://chatboxai.app",
     "在 Chatbox 本地库中检索带 token 字段的消息",
     ("%APPDATA%/xyz.chatboxapp.app", "%APPDATA%/Chatbox", "~/.chatbox")),
    ("kiro", "Kiro", "Amazon", "editor", "#f97316", "K",
     "https://kiro.dev",
     "读取 Kiro 的本地会话与用量缓存",
     ("%APPDATA%/Kiro", "%LOCALAPPDATA%/Kiro", "~/.kiro")),
    ("qoder", "Qoder", "阿里云", "editor", "#06b6d4", "Q",
     "https://qoder.com",
     "读取 Qoder 的本地会话缓存（用量可能仅存于云端）",
     ("%APPDATA%/Qoder", "%APPDATA%/Qoder CN", "%LOCALAPPDATA%/Qoder")),
    ("cody", "Sourcegraph Cody", "Sourcegraph", "extension", "#ef4444", "◎",
     "https://sourcegraph.com/cody",
     "读取 Cody 插件的本地对话缓存",
     ("%APPDATA%/Code/User/globalStorage/sourcegraph.cody-ai",
      "%APPDATA%/Cursor/User/globalStorage/sourcegraph.cody-ai")),
    ("tabnine", "Tabnine", "Tabnine", "extension", "#38bdf8", "T",
     "https://www.tabnine.com",
     "读取 Tabnine 本地缓存中的补全与对话统计",
     ("~/.tabnine", "%APPDATA%/TabNine", "%LOCALAPPDATA%/TabNine")),
    ("warp", "Warp", "Warp", "desktop", "#a78bfa", "W",
     "https://www.warp.dev",
     "读取 Warp 终端的 AI 会话记录",
     ("%APPDATA%/Warp", "%LOCALAPPDATA%/Warp", "~/.warp")),
]

VENDOR_ADAPTERS = [
    _vendor(*spec[:9], local_cost=(spec[9] if len(spec) > 9 else False))
    for spec in _VENDOR_SPECS
]


# ==========================================================================
# 注册表
# ==========================================================================

class CopilotCliAdapter(Adapter):
    id = "copilot-cli"
    name = "GitHub Copilot CLI"
    vendor = "GitHub"
    category = "cli"
    color = "#6e40c9"
    glyph = "GH"
    homepage = "https://github.com/github/copilot-cli"
    detail = "读取 ~/.copilot/session-state/*/events.jsonl 的 session.shutdown 模型汇总；未结束会话可能尚无用量，premium request 不是美元费用"
    QUALITY = "aggregate"

    def enumerate(self):
        return glob_files(os.path.join(HOME, ".copilot", "session-state", "*", "events.jsonl"))

    def probe(self):
        path = os.path.join(HOME, ".copilot")
        return os.path.isdir(path), path

    def parse(self, path):
        session = os.path.basename(os.path.dirname(path))
        project, summary, stamp = "", {}, 0
        for line in iter_lines(path):
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("invalid Copilot CLI event")
            data = obj.get("data") or {}
            if obj.get("type") == "session.start":
                session = str(data.get("sessionId") or data.get("session_id") or session)
                context = data.get("context") or {}
                project = norm_project(context.get("cwd") or data.get("cwd") or "")
            if obj.get("type") == "session.shutdown":
                metrics = data.get("modelMetrics")
                if isinstance(metrics, dict):
                    # Resumed sessions may publish another cumulative snapshot.
                    # The most recent shutdown for each model is authoritative.
                    summary.update(metrics)
                    if len(summary) > 512:
                        raise ValueError("Copilot model metrics exceed the supported limit")
                    stamp = to_epoch(obj.get("timestamp") or data.get("endTime"))
        for model, metric in summary.items():
            if not isinstance(metric, dict) or not isinstance(metric.get("usage"), dict):
                raise ValueError("invalid Copilot CLI model metrics")
            usage = metric["usage"]
            inp, out = _i(usage.get("inputTokens")), _i(usage.get("outputTokens"))
            cr, cw = _i(usage.get("cacheReadTokens")), _i(usage.get("cacheWriteTokens"))
            reasoning = _i(usage.get("reasoningTokens"))
            if reasoning > out:
                raise ValueError("Copilot reasoning exceeds output tokens")
            requests = metric.get("requests") or {}
            count = _i(requests.get("count"), 1) if isinstance(requests, dict) else 1
            if inp + out + cr + cw == 0:
                continue
            cost = estimate_cost(str(model), inp, out - reasoning, cr, cw, reasoning)
            yield UsageRecord(tool=self.id, ts=stamp, model=str(model), project=project,
                session=session, input=inp, output=out - reasoning, cache_read=cr, cache_write=cw,
                reasoning=reasoning, requests=count, cost=cost,
                cost_kind="estimated" if cost else "unknown", cost_source="price-table" if cost else "unpriced",
                # 快照身份按（会话, 模型）而不是文件路径：resume 会话会写新的
                # session-state 目录，同一份累计按路径建档会双计。跨文件首见
                # 生效，副本被插入层拒绝。
                src=path, event_id=stable_event_id(self.id, "", session, str(model)),
                extra={"quality": "aggregate", "billing_unit": "premium-request", "request_id": str(model)})


class PiAdapter(Adapter):
    id = "pi"
    name = "Pi Coding Agent"
    vendor = "Earendil"
    category = "cli"
    color = "#f97316"
    glyph = "π"
    homepage = "https://pi.dev"
    detail = "读取 ~/.pi/agent/sessions/**/*.jsonl 中 assistant、usage、compaction、branch_summary 的实报 token 与费用"
    INCREMENTAL_JSONL = True

    def enumerate(self):
        return glob_files(os.path.join(HOME, ".pi", "agent", "sessions", "**", "*.jsonl"))

    def probe(self):
        path = os.path.join(HOME, ".pi", "agent")
        return os.path.isdir(path), path

    def parse(self, path):
        yield from self.parse_lines(path, iter_lines(path), {})

    def parse_lines(self, path, lines, state):
        for line in lines:
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("invalid Pi session event")
            if obj.get("type") == "session":
                state.update(session=str(obj.get("id") or os.path.basename(path)), project=norm_project(obj.get("cwd") or ""))
                continue
            message = obj.get("message") if obj.get("type") == "message" else obj
            if not isinstance(message, dict):
                continue
            if obj.get("type") == "message" and message.get("role") != "assistant":
                continue
            if obj.get("type") not in ("message", "usage", "compaction", "branch_summary"):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            inp, out = _i(usage.get("input")), _i(usage.get("output"))
            cr, cw, reasoning = _i(usage.get("cacheRead")), _i(usage.get("cacheWrite")), _i(usage.get("reasoning"))
            if reasoning > out:
                raise ValueError("Pi reasoning exceeds output tokens")
            if inp + out + cr + cw <= 0:
                continue
            model = str(message.get("model") or obj.get("model") or "")
            cost_obj = usage.get("cost")
            actual = isinstance(cost_obj, dict) and cost_obj.get("total") is not None
            cost = float(cost_obj["total"]) if actual else estimate_cost(model, inp, out - reasoning, cr, cw, reasoning)
            explicit = str(obj.get("id") or "")
            yield UsageRecord(tool=self.id, ts=to_epoch(obj.get("timestamp") or message.get("timestamp")),
                model=model, project=state.get("project", ""), session=state.get("session", os.path.basename(path)),
                input=inp, output=out-reasoning, cache_read=cr, cache_write=cw, reasoning=reasoning,
                cost=cost, cost_kind="actual" if actual else "estimated" if cost else "unknown",
                cost_source="source-report" if actual else "price-table" if cost else "unpriced", src=path,
                event_id=stable_event_id(self.id, path, state.get("session", ""), explicit, obj),
                extra={"provider": str(message.get("provider") or obj.get("provider") or ""),
                       "usage_kind": str(obj.get("kind") or obj.get("type") or ""), "request_id": explicit})


class GooseAdapter(Adapter):
    id = "goose"
    name = "Goose"
    vendor = "Block / AAIF"
    category = "cli"
    color = "#f4b629"
    glyph = "G"
    homepage = "https://block.github.io/goose/"
    detail = "读取 Goose sessions.db 中 usage_ledger 的逐次用量与费用；旧版数据库回退到会话累计值，不与逐次账本重复相加"

    def enumerate(self):
        roots = [os.path.join(HOME, ".local", "share", "goose"),
                 os.path.join(APPDATA, "Block", "goose", "data"),
                 os.path.join(LOCALAPPDATA, "Block", "goose", "data"),
                 os.path.join(HOME, "Library", "Application Support", "Block", "goose")]
        custom = os.environ.get("GOOSE_PATH_ROOT", "")
        if custom and os.path.isabs(custom):
            roots.insert(0, os.path.join(custom, "data"))
        return list(dict.fromkeys(os.path.join(root, "sessions", "sessions.db") for root in roots
                                 if os.path.isfile(os.path.join(root, "sessions", "sessions.db"))))

    def parse(self, path):
        with contextlib.closing(source_connection(path)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sessions" not in tables:
                raise ValueError("Goose session database has no sessions table")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
            required = {"id", "updated_at"}
            if not required.issubset(columns):
                raise ValueError("unsupported Goose session schema")
            def col(name, fallback="NULL"):
                return 's."%s"' % name if name in columns else fallback
            ledger_sessions = set()
            if "usage_ledger" in tables:
                ledger_cols = {row[1] for row in connection.execute("PRAGMA table_info(usage_ledger)")}
                if not {"id", "session_id", "created_timestamp", "input_tokens", "output_tokens", "model"}.issubset(ledger_cols):
                    raise ValueError("unsupported Goose usage ledger schema")
                def ledger_col(name):
                    return 'u."%s"' % name if name in ledger_cols else "NULL"
                query = "SELECT u.id,u.session_id,u.created_timestamp,u.model,u.input_tokens,u.output_tokens,%s AS cache_read,%s AS cache_write,%s AS cost,%s AS cost_source,%s AS project FROM usage_ledger u LEFT JOIN sessions s ON s.id=u.session_id ORDER BY u.id" % (ledger_col("cache_read_tokens"), ledger_col("cache_write_tokens"), ledger_col("cost"), ledger_col("cost_source"), col("working_dir", "''"))
                for row in connection.execute(query):
                    check_parse_cancelled()
                    ledger_sessions.add(str(row["session_id"]))
                    yield self._record(path, dict(row), aggregate=False)
            if not {"accumulated_input_tokens", "accumulated_output_tokens"}.issubset(columns):
                return
            query = "SELECT s.id AS session_id,s.updated_at AS created_timestamp,%s AS model_config,s.accumulated_input_tokens AS input_tokens,s.accumulated_output_tokens AS output_tokens,%s AS cache_read,%s AS cache_write,%s AS cost,%s AS project FROM sessions s" % (col("model_config_json"), col("accumulated_cache_read_tokens"), col("accumulated_cache_write_tokens"), col("accumulated_cost"), col("working_dir", "''"))
            for row in connection.execute(query):
                check_parse_cancelled()
                if str(row["session_id"]) in ledger_sessions:
                    continue
                values = dict(row)
                config = json.loads(values.pop("model_config") or "{}")
                values["model"] = config.get("model_name") or config.get("modelName") or config.get("model") or "goose"
                values["cost_source"] = "estimated"
                if _i(values.get("input_tokens")) + _i(values.get("output_tokens")):
                    yield self._record(path, values, aggregate=True)

    def _record(self, path, row, aggregate):
        raw, out = _i(row.get("input_tokens")), _i(row.get("output_tokens"))
        cr, cw = _i(row.get("cache_read")), _i(row.get("cache_write"))
        if cr + cw > raw:
            raise ValueError("Goose cache subsets exceed total input")
        inp = raw - cr - cw
        actual = row.get("cost") is not None and row.get("cost_source") == "provider_reported"
        cost = float(row["cost"]) if row.get("cost") is not None else estimate_cost(str(row["model"]), inp, out, cr, cw)
        session, request = str(row.get("session_id") or ""), str(row.get("id") or "summary")
        return UsageRecord(tool=self.id, ts=to_epoch(row.get("created_timestamp")), model=str(row["model"]),
            project=norm_project(row.get("project") or ""), session=session, input=inp, output=out,
            cache_read=cr, cache_write=cw, cost=cost, requests=1,
            cost_kind="actual" if actual else "estimated" if cost else "unknown",
            cost_source="source-report" if actual else "source-estimate" if row.get("cost") is not None else "price-table",
            src=path, event_id=stable_event_id(self.id, path, session, request),
            extra={"quality": "aggregate" if aggregate else "exact", "request_id": request})


class ImportedAdapter(Adapter):
    id = "imported"
    name = "导入的用量"
    vendor = "本地文件"
    category = "api"
    color = "#14b8a6"
    glyph = "↓"
    detail = "通过数据源页面导入的 CSV / JSON / JSONL；仅保存用量、模型、时间和来源，不保留提示词或密钥"
    INCREMENTAL_JSONL = True

    def enumerate(self):
        from .source_import import import_directory
        return glob_files(os.path.join(import_directory(self.cfg), "*.jsonl"))

    def parse(self, path):
        from .source_import import imported_records
        yield from imported_records(path)

    def parse_lines(self, path, lines, state):
        for line in lines:
            if line.strip():
                record = UsageRecord(**json.loads(line)).validate()
                if record.tool != self.id or not record.event_id.startswith("import:") or record.ts <= 0:
                    raise ValueError("invalid normalized import record")
                yield record


ADAPTER_CLASSES = [
    # ---- 一、本地可直接解析出 token 的主力来源 ----
    ClaudeCodeAdapter,
    CodexAdapter,
    GeminiCliAdapter,
    DeepSeekHarnessAdapter,
    OpenCodeAdapter,
    WorkBuddyAdapter,
    ZCodeAdapter,
    KimiAdapter,
    ClineFamilyAdapter,
    ContinueAdapter,
    CopilotAdapter,
    CopilotCliAdapter,
    PiAdapter,
    GooseAdapter,
    LMStudioAdapter,
    OllamaAdapter,
    AiderAdapter,
    ClaudeDesktopAdapter,
    QoderWorkAdapter,
    # ---- 二、编辑器 / 桌面端（云端计量或加密可见的，走探测） ----
    CursorAdapter,
    TraeAdapter,
    WindsurfAdapter,
    CherryStudioAdapter,
    # ---- 三、通用厂商扫描（结构不固定的产品，统一用字段探测抽取） ----
] + VENDOR_ADAPTERS + [
    # ---- 四、大模型 API 调用日志（含常见网关默认目录） ----
    ApiLogAdapter,
    ImportedAdapter,
]


def build_adapters(cfg):
    return [cls(cfg) for cls in ADAPTER_CLASSES]


def adapter_meta():
    """给前端「数据源图鉴」用的静态元信息。

    name / vendor / detail 里有中文（例如「通义灵码」「腾讯」「大模型 API
    调用日志」以及 30 条来源说明）。英文界面需要英文版，所以统一在这里查
    对照表下发 *_en 字段，不必去改 30 个适配器类。
    """
    out = []
    for cls in ADAPTER_CLASSES:
        out.append({
            "id": cls.id, "name": cls.name, "vendor": cls.vendor,
            "category": cls.category, "color": cls.color, "glyph": cls.glyph,
            "homepage": cls.homepage, "detail": cls.detail,
            "name_en": ADAPTER_NAME_EN.get(cls.id) or cls.name,
            "vendor_en": VENDOR_EN.get(cls.vendor) or cls.vendor,
            "detail_en": ADAPTER_DETAIL_EN.get(cls.id) or cls.detail,
        })
    return out


# 只有「名字/厂商/说明本身是中文」的才需要登记；英文产品名沿用原名。
ADAPTER_NAME_EN = {
    "imported": "Imported usage",
    "api-log": "LLM API call logs",
    "lingma": "Tongyi Lingma",
    "codebuddy": "CodeBuddy Extension",
}

VENDOR_EN = {
    "本地文件": "Local files",
    "腾讯": "Tencent",
    "腾讯云": "Tencent Cloud",
    "阿里云": "Alibaba Cloud",
    "字节跳动": "ByteDance",
    "智谱 AI": "Zhipu AI",
    "月之暗面": "Moonshot AI",
    "开源插件": "Open source",
    "自定义目录 / 网关": "Custom directories / gateways",
}

ADAPTER_DETAIL_EN = {
    "imported": "User-selected CSV / JSON / JSONL imports. Only accounting fields are retained; prompts and credentials are discarded.",
    "copilot-cli": "Reads per-model session.shutdown totals in ~/.copilot/session-state/*/events.jsonl. Running sessions may not yet contain usage. Premium requests are not USD fees.",
    "pi": "Reads reported usage and cost from assistant, usage, compaction and branch_summary entries in ~/.pi/agent/sessions/**/*.jsonl.",
    "goose": "Reads per-call usage_ledger entries from Goose sessions.db. Older sessions without ledger entries fall back to accumulated totals; the two are never added together.",
    "claude-code": "Reads the usage field of every assistant message in "
                   "~/.claude/projects/**/*.jsonl (including cache read/write)",
    "codex": "Parses token_count events in ~/.codex/sessions/**/rollout-*.jsonl "
             "and accumulates them per turn",
    "gemini-cli": "Parses the tokens field of Gemini messages in ~/.gemini/tmp/*/chats/*.jsonl",
    "deepseek-harness": "Recursively reads tokenUsage from the Harness session projection cache "
                        "(uncached input / output / cache read / cache write). Only sessions whose "
                        "projections still exist locally are counted; cloud history already cleaned "
                        "up or never projected by Harness cannot be recovered from this machine.",
    "opencode": "Reads the SQLite session tables of ~/.local/share/opencode and mimocode "
                "(including cost fields)",
    "workbuddy": "Reads the session_usage table in ~/.workbuddy/workbuddy.db and joins it with "
                 "model and project",
    "zcode": "Reads the model_usage table in ~/.zcode/cli/db/db.sqlite and joins each call "
             "with its session project and title",
    "kimi": "Parses token usage from each session's agents/*/wire.jsonl under ~/.kimi-code "
            "and ~/.kimi (turn-scoped records only, so session-level cumulative totals are "
            "not counted twice)",
    "cline": "Reads tokensIn/tokensOut/cost from each task ui_messages.json under VS Code globalStorage",
    "claude-desktop": "Reads the local agent sessions of Claude Desktop (audit.jsonl and "
                      "usage-ledger under local-agent-mode-sessions), including the official "
                      "costUSD and cache read/write; ledger rows take precedence over the "
                      "overlapping audit rows so nothing is counted twice",
    "qoderwork": "Reads the token usage of each turn.finished event in "
                 "~/.qoderwork/logs/sessions/**/segments/*.jsonl",
    "continue": "Reads usage records from ~/.continue/sessions/*.json",
    "copilot": "Counts token usage in VS Code chat session files; older versions that do not "
               "persist tokens report session counts only",
    "lmstudio": "Reads token counts recorded in local conversation history "
                "(fully offline inference, no API cost)",
    "ollama": "Local inference incurs no API cost; when logs or a proxy record prompt_eval_count "
              "and eval_count, they are collected as local inference volume with zero cost",
    "aider": "Scans .aider.chat.history.md in your projects plus usage records under ~/.aider",
    "cursor": "The local state.vscdb stores only session structure and accepted-completion lines; "
              "tokens are metered by Cursor in the cloud, so they are excluded from the local total",
    "trae": "The session store under ModularData is encrypted and cannot be decoded locally; "
            "refer to official billing for usage",
    "windsurf": "Inspects local caches and logs; tokens are metered by Codeium in the cloud and "
                "never written locally",
    "cherry-studio": "Conversations and usage are stored in Electron IndexedDB (LevelDB); "
                     "parsed read-only on a best-effort basis",
    "lingma": "Searches plugin caches and local logs for session records containing a token field",
    "codebuddy": "Scans session and usage files left behind by the CodeBuddy IDE extension",
    "jetbrains-ai": "Reads AI session statistics and logs from JetBrains IDEs "
                    "(config and log directories only, caches skipped)",
    "zed": "Reads Zed agent threads and assistant session records",
    "jan": "Reads Jan local conversation threads (mostly local inference, usually no cost)",
    "chatbox": "Searches the local Chatbox database for messages containing a token field",
    "kiro": "Reads Kiro local session and usage caches",
    "qoder": "Reads Qoder local session caches (usage may exist only in the cloud)",
    "cody": "Reads the local conversation cache of the Cody extension",
    "tabnine": "Reads completion and conversation statistics from the local Tabnine cache",
    "warp": "Reads AI session records from the Warp terminal",
    "api-log": "Scans the directories you specify plus common gateway defaults, auto-detecting "
               "OpenAI / Anthropic-compatible usage records (JSONL, logs, JSON, CSV)",
}


CATEGORY_LABEL = {
    "cli": "命令行智能体",
    "editor": "代码编辑器",
    "extension": "编辑器插件",
    "desktop": "桌面客户端",
    "api": "API 调用",
}

# 类别名的英文版：界面语言可切换，分类标签是后端算出来的，必须一起给。
CATEGORY_LABEL_EN = {
    "cli": "CLI agents",
    "editor": "Code editors",
    "extension": "Editor extensions",
    "desktop": "Desktop apps",
    "api": "API calls",
}
