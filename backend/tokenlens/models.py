# -*- coding: utf-8 -*-
"""
TokenLens · 统一数据模型

所有数据源适配器最终都要把异构的本地日志归一化成 UsageRecord。
一个 UsageRecord = 一次（或一组）模型调用的 token 消耗。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

RECORD_TEXT_LIMITS = {"tool": 128, "model": 1024, "project": 8192,
                      "session": 4096, "src": 16384, "cost_source": 1024,
                      "price_version": 1024, "event_id": 512}
MAX_RECORD_EXTRA_BYTES = 256 * 1024
# JSON numbers are consumed by JavaScript as IEEE-754 doubles. Enforce the
# same exact-integer contract for a record and the complete index, rather than
# allowing valid SQLite integers to silently lose precision in the interface.
MAX_USAGE_COUNT = 2**53 - 1
MAX_USAGE_COST = 1e24


# --------------------------------------------------------------------------
# 时间工具
# --------------------------------------------------------------------------

def to_epoch(value) -> int:
    """把各种时间表示统一成 epoch 秒（UTC 基准，按本地时区展示）。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        try:
            v = float(value)
        except (ValueError, OverflowError):
            return 0
        if not math.isfinite(v):
            return 0
        # 毫秒 / 微秒 / 纳秒 自动降级
        while v > 4.0e10:
            v /= 1000.0
        return int(v)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        # 纯数字
        if s.isdigit():
            return to_epoch(int(s))
        txt = s.replace("Z", "+00:00")
        for fmt in (None, "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                if fmt is None:
                    dt = datetime.fromisoformat(txt)
                else:
                    dt = datetime.strptime(txt, fmt)
                if dt.tzinfo is None:
                    # Naive timestamp() uses the host's historical local offset,
                    # including DST. Today's fixed offset can shift older logs.
                    return int(dt.timestamp())
                return int(dt.timestamp())
            except Exception:
                continue
        # 兜底：截取前 19 位
        return to_epoch(s[:19]) if len(s) > 19 else 0
    return 0


def day_of(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def now_ts() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# 记录
# --------------------------------------------------------------------------

def _i(v, default=0) -> int:
    """Parse a usage integer without hiding malformed source values.

    Adapters historically converted values such as ``"not-a-number"`` to
    zero.  That made a source look exact even though its usage was incomplete.
    Keep the permissive return value for optional fields, but record a parser
    diagnostic whenever a non-empty value cannot be represented as a
    non-negative integer.  ``Scanner`` turns diagnostics into a partial/error
    source report and retains the previous snapshot.
    """
    def invalid():
        # ``adapters`` owns the per-parse diagnostic context. Import lazily to
        # avoid the models -> adapters import cycle during module startup.
        try:
            from . import adapters as _adapters
            issues = getattr(getattr(_adapters, "_PARSE_CONTEXT", None), "issues", None)
        except Exception:
            issues = None
        if issues is not None:
            issues.append("invalid numeric usage field")

    try:
        if v is None:
            return default
        if isinstance(v, bool):
            invalid()
            return default
        if isinstance(v, (int,)):
            if v < 0:
                invalid()
                return default
            return int(v)
        if isinstance(v, float):
            if not math.isfinite(v) or v < 0 or v != int(v):
                invalid()
                return default
            return int(v)
        s = str(v).strip().replace(",", "")
        if not s:
            return default
        number = float(s)
        if not math.isfinite(number) or number < 0 or number != int(number):
            invalid()
            return default
        return int(number)
    except Exception:
        if v not in (None, ""):
            invalid()
        return default


def _f(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


@dataclass
class UsageRecord:
    """一次模型调用的 token 消耗（已归一化）。"""

    tool: str = ""            # 工具 ID，如 claude-code
    ts: int = 0               # epoch 秒
    model: str = ""           # 模型名
    project: str = ""         # 项目 / 工作目录
    session: str = ""         # 会话 ID
    input: int = 0            # 未命中缓存的输入 token
    output: int = 0           # 输出 token
    cache_read: int = 0       # 缓存读取 token
    cache_write: int = 0      # 缓存写入 token
    reasoning: int = 0        # 思维链 token（部分厂商单列）
    requests: int = 1         # 调用次数
    cost: float = 0.0         # 成本（USD，0 表示未知）
    src: str = ""             # 来源文件
    extra: dict = field(default_factory=dict)
    cost_kind: str = ""       # actual / estimated / free / unknown
    cost_source: str = ""
    price_version: str = ""
    currency: str = "USD"
    event_id: str = ""        # optional stable id; used for idempotent internal calls

    def validate(self):
        """Validate before any database mutation; never coerce bad usage to zero."""
        for name, maximum in RECORD_TEXT_LIMITS.items():
            value = getattr(self, name)
            if value is None:
                value = ""
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError("%s must be bounded text" % name)
            setattr(self, name, value)
        for name in ("input", "output", "cache_read", "cache_write", "reasoning", "requests"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("%s must be a non-negative integer" % name)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("%s must be finite" % name)
            if value < 0 or value != int(value) or value > MAX_USAGE_COUNT:
                raise ValueError("%s is outside the supported integer range" % name)
            setattr(self, name, int(value))
        if self.total > MAX_USAGE_COUNT:
            raise ValueError("total tokens exceed the supported integer range")
        if not isinstance(self.ts, (int, float)) or not 0 <= self.ts <= 253402300799:
            raise ValueError("timestamp is outside the supported range")
        if not math.isfinite(self.ts):
            raise ValueError("timestamp must be finite")
        self.ts = int(self.ts)
        if self.ts > 0:
            try:
                day_of(self.ts)
            except (OSError, ValueError, OverflowError) as exc:
                raise ValueError("timestamp is outside the supported range") from exc
        if (not isinstance(self.cost, (int, float)) or not 0 <= self.cost <= MAX_USAGE_COST
                or not math.isfinite(self.cost)):
            raise ValueError("cost must be finite and non-negative")
        if self.currency != "USD":
            raise ValueError("usage costs must be normalized to USD")
        if not isinstance(self.extra, dict):
            raise ValueError("extra must be an object")
        if len(json.dumps(self.extra, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RECORD_EXTRA_BYTES:
            raise ValueError("record metadata exceeds 256 KiB; previous snapshot retained")
        if not self.cost_kind:
            self.cost_kind, source = infer_cost_metadata(self.tool, self.model, self.cost, self.extra)
            self.cost_source = self.cost_source or source
        if self.cost_kind not in ("actual", "estimated", "free", "unknown"):
            raise ValueError("invalid cost_kind")
        if self.cost_kind in ("free", "unknown") and self.cost != 0:
            raise ValueError("free/unknown records must have zero stored cost")
        if not self.cost_source:
            self.cost_source = {"actual": "source-report", "estimated": "price-table",
                                "free": "local-inference", "unknown": "unpriced"}[self.cost_kind]
        if self.cost_kind == "estimated" and not self.price_version:
            from . import pricing
            self.price_version = getattr(pricing, "PRICE_VERSION", "builtin-2026-09")
        return self

    # -- 派生 ------------------------------------------------------------
    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write + self.reasoning

    @property
    def day(self) -> str:
        return day_of(self.ts)

    def is_empty(self) -> bool:
        return self.total <= 0 and self.requests <= 0

    def key(self) -> tuple:
        return (self.tool, self.ts, self.session, self.model, self.total)

    def as_row(self) -> tuple:
        self.validate()
        return (
            self.tool, self.ts, self.day, self.model, self.project, self.session,
            self.input, self.output, self.cache_read, self.cache_write, self.reasoning,
            self.requests, self.cost, self.src,
            json.dumps(self.extra, ensure_ascii=False, allow_nan=False) if self.extra else "",
            self.cost_kind, self.cost_source, self.price_version, self.currency, self.event_id,
        )


ROW_COLUMNS = (
    "tool", "ts", "day", "model", "project", "session",
    "input", "output", "cache_read", "cache_write", "reasoning",
    "requests", "cost", "src", "extra",
    "cost_kind", "cost_source", "price_version", "currency", "event_id",
)


def infer_cost_metadata(tool, model, cost, extra=None):
    """Compatibility inference. New adapters explicitly label actual and free costs."""
    if tool in ("lmstudio", "ollama", "jan") or (extra or {}).get("local"):
        return "free", "local-inference"
    if cost and tool in ("claude-desktop", "opencode", "cline"):
        return "actual", "legacy-source-report"
    from .pricing import is_known
    if cost or is_known(model):
        return "estimated", "price-table"
    return "unknown", "unpriced"


# --------------------------------------------------------------------------
# 工具（数据源）元信息
# --------------------------------------------------------------------------

@dataclass
class ToolMeta:
    id: str
    name: str
    vendor: str
    category: str          # cli / editor / extension / desktop / api
    color: str             # 主题色
    glyph: str             # 单字母或短符号
    homepage: str = ""
    detail: str = ""       # 数据来源说明


class Status:
    OK = "ok"                  # 已成功采集到 token
    PARTIAL = "partial"        # 采集到部分数据 / 口径受限
    DETECTED = "detected"      # 检测到安装，但本地无 token 落盘
    MISSING = "missing"        # 未安装
    ERROR = "error"


@dataclass
class SourceReport:
    """单个数据源的探测 / 采集报告，用于「数据源」页展示。"""

    id: str
    name: str
    vendor: str
    category: str
    color: str
    glyph: str
    status: str = Status.MISSING
    paths: list = field(default_factory=list)
    files: int = 0
    size: int = 0
    records: int = 0
    tokens: int = 0
    last_activity: int = 0
    note: str = ""
    elapsed: float = 0.0
    homepage: str = ""
    last_scan_at: int = 0
    error: str = ""
    included: bool = False
    estimated: bool = False
    quality: str = "exact"    # exact / aggregate / heuristic / detection
    raw_records: int = 0
    accepted_records: int = 0
    skipped_records: int = 0
    last_success_at: int = 0

    def as_dict(self):
        return asdict(self)


# --------------------------------------------------------------------------
# 数字格式化
# --------------------------------------------------------------------------

_UNITS = ("", "K", "M", "B", "T", "P")


def human(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "0"
    neg = n < 0
    n = abs(n)
    idx = 0
    while n >= 1000 and idx < len(_UNITS) - 1:
        n /= 1000.0
        idx += 1
    if idx == 0:
        s = f"{int(n)}"
    elif n >= 100:
        s = f"{n:.0f}"
    elif n >= 10:
        s = f"{n:.1f}"
    else:
        s = f"{n:.2f}"
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return ("-" if neg else "") + s + _UNITS[idx]
