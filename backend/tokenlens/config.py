# -*- coding: utf-8 -*-
"""tokenlens · 运行配置（保存在用户目录，不随程序走）"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import tempfile
import copy

APP_NAME = "TokenLens"
APP_TAGLINE = "LOCAL TOKEN ANALYTICS"
APP_VERSION = "1.2.4"

HOME = os.path.expanduser("~")
_LOG = logging.getLogger("tokenlens.config")


def data_dir() -> str:
    base = os.environ.get("TOKENLENS_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local"), APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base


DEFAULTS = {
    "port": 8799,
    "host": "127.0.0.1",
    # 深色: dark/ocean/neon；浅色: light/mint/solar/ink；auto=跟随系统
    # 默认薄荷（清爽浅色，适合长时间阅读与数据浏览）。
    "theme": "mint",
    "font": "system",             # system / humanist / serif / mono / classic / rounded
    "density": "compact",          # compact / comfortable / spacious
    "language": "zh-CN",            # zh-CN / en-US
    "range": "30d",
    "codex_quick": False,        # True = 只读 thread 汇总表，跳过 rollout 明细（更快）
    "max_file_mb": 400,          # 单文件解析上限
    "workers": 8,                # 并行解析线程：zstd 解压与文件 IO 释放 GIL，8 线程显著快于 4（可在设置中调 1-32）
    "api_log_roots": [],         # 自定义 API 日志目录
    "usd_to_cny": 7.20,
    "display_currency": "auto",  # display only; amounts remain USD in storage
    "exclude_tools": [],
    # ---- 预算、提醒与自动化 ----
    "budget_tokens": 0,          # 0 = 不设预算；否则为「本周期 token 预算」
    "budget_cost": 0,            # 0 = 不设预算；否则为「本周期成本预算（USD）」
    "budget_period": "month",   # fixed calendar period, independent of chart filters
    "warn_ratio": 0.8,           # 达到预算的多少比例开始提醒（0~1）
    "auto_scan_minutes": 0,      # 0 = 关闭；>0 则每隔 N 分钟自动增量扫描
    "scan_on_start": True,
    # ---- 外观扩展 ----
    "accent": "theme",           # theme=跟随主题；否则覆盖 --accent
    "radius": "theme",           # theme / sharp / soft / round
    # ---- 大数据量与性能 ----
    "big_lib_mode": True,        # 记录数很大时自动收紧明细查询规模
    "records_page": 100,         # 明细流水每页条数，减少首次表格布局与绘制
    "list_cap": 40,              # 各类排行/列表最多渲染多少行
    "default_range": "30d",      # 启动时默认时间范围
    # Navigation preferences only; collection and totals remain independent.
    "nav_insights": True,
    "nav_tools": True,
    "nav_models": True,
    "nav_projects": True,
    "nav_sessions": True,
    "nav_sources": True,
    "nav_manage": True,
    "nav_assistant": True,
    # ---- 提示词优化 ----
    "pl_enabled": True,          # 关闭后隐藏「提示词优化」入口与工作区
    "pl_max_tokens": 1024,       # 改写结果的生成上限
    "pl_show_thinking": False,   # 是否在界面上展示模型思考过程
    "ai_hedge_enabled": False,  # extra requests are opt-in and accounted separately
    "ai_warmup_enabled": False,
    # ---- AI 智商检测 ----
    "iq_enabled": True,          # 关闭后隐藏「AI 智商检测」入口与题板
}


# Values accepted by the HTTP settings endpoint.  Keeping this policy next to
# the defaults means loading an old/corrupted config and updating it through
# the API follow the same rules.
ENUMS = {
    "theme": {"dark", "light", "mint", "ocean", "auto", "neon", "solar", "ink"},
    "font": {"system", "humanist", "serif", "mono", "classic", "rounded"},
    "density": {"compact", "comfortable", "spacious"},
    "language": {"zh-CN", "en-US"},
    "range": {"today", "1d", "7d", "30d", "90d", "180d", "365d", "ytd", "all"},
    "accent": {"theme", "indigo", "violet", "cyan", "emerald", "amber", "rose"},
    "radius": {"theme", "sharp", "soft", "round"},
    "default_range": {"today", "1d", "7d", "30d", "90d", "180d", "365d", "ytd", "all"},
    "budget_period": {"month", "week"},
    "display_currency": {"auto", "usd", "cny"},
}


# 主题收敛为「少而精」后，老配置里的主题名映射到最接近的现役主题。
THEME_MIGRATION = {
    "ember": "mint",       # 早期的暖炭主题
    "graphite": "dark",    # 中性灰 -> 深空
    "sakura": "neon",      # 暗紫粉 -> 霓虹
    "forest": "ocean",     # 墨绿青 -> 海蓝
    "paper": "ink",        # 暖白纸感 -> 水墨
    "sunset": "solar",     # 暖橙浅色 -> 日光
}

CONFIG_RESET = {}  # retained for compatibility; legal user preferences are never reset


def _finite_number(value):
    # JSON integers are arbitrary precision. math.isfinite converts them to a
    # float and can raise before normal range validation rejects an input.
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def validate_patch(mapping: dict):
    """Validate and normalize a user supplied config patch.

    The desktop app talks to a local HTTP server, but the endpoint is still an
    input boundary: values are persisted and consumed by scanner threads.  Do
    not let malformed JSON turn into a scanner crash on the next run.
    Returns ``(clean_patch, errors)``; no partial invalid values are returned.
    """
    if not isinstance(mapping, dict):
        return {}, {"config": "must be an object"}
    allowed = set(DEFAULTS)
    clean, errors = {}, {}
    for key, value in (mapping or {}).items():
        if key not in allowed:
            continue
        if key == "port":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_number(value) \
                    or int(value) != value or not 1 <= int(value) <= 65535:
                errors[key] = "must be an integer between 1 and 65535"
            else:
                clean[key] = int(value)
        elif key == "host":
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 255:
                errors[key] = "must be a non-empty hostname"
            else:
                clean[key] = value.strip()
        elif key in ENUMS:
            if not isinstance(value, str) or value not in ENUMS[key]:
                errors[key] = "must be one of: " + ", ".join(sorted(ENUMS[key]))
            else:
                clean[key] = value
        elif key in ("codex_quick", "scan_on_start", "big_lib_mode", "pl_show_thinking",
                     "pl_enabled", "iq_enabled", "ai_hedge_enabled", "ai_warmup_enabled",
                     "nav_insights", "nav_tools", "nav_models", "nav_projects",
                     "nav_sessions", "nav_sources", "nav_manage", "nav_assistant"):
            if not isinstance(value, bool):
                errors[key] = "must be a boolean"
            else:
                clean[key] = value
        elif key in ("workers", "max_file_mb", "budget_tokens",
                     "records_page", "list_cap", "pl_max_tokens"):
            # bool is an int subclass, but accepting it here is surprising.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_number(value):
                errors[key] = "must be an integer"
                continue
            if int(value) != value:
                errors[key] = "must be an integer"
                continue
            iv = int(value)
            limits = {"workers": (1, 32), "max_file_mb": (1, 4096),
                      "budget_tokens": (0, 10 ** 12),
                      "records_page": (50, 2000), "list_cap": (10, 500),
                      "pl_max_tokens": (256, 8192)}
            lo, hi = limits[key]
            if iv < lo or iv > hi:
                errors[key] = "must be between %d and %d" % (lo, hi)
            else:
                clean[key] = iv
        elif key in ("usd_to_cny", "budget_cost", "warn_ratio"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_number(value):
                errors[key] = "must be a finite number"
                continue
            fv = float(value)
            if key == "usd_to_cny" and not 0.01 <= fv <= 1000:
                errors[key] = "must be between 0.01 and 1000"
            elif key == "budget_cost" and not 0 <= fv <= 10 ** 9:
                errors[key] = "must be between 0 and 1000000000"
            elif key == "warn_ratio" and not 0 <= fv <= 1:
                errors[key] = "must be between 0 and 1"
            else:
                clean[key] = fv
        elif key == "auto_scan_minutes":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite_number(value) \
                    or int(value) != value or not 0 <= int(value) <= 1440:
                errors[key] = "must be an integer between 0 and 1440"
            else:
                clean[key] = int(value)
        elif key in ("api_log_roots", "exclude_tools"):
            if not isinstance(value, list) or len(value) > 200 \
                    or any(not isinstance(item, str) or len(item) > 1024 for item in value):
                errors[key] = "must be a list of strings (max 200 items)"
            else:
                clean[key] = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    return clean, errors


class Config:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(data_dir(), "config.json")
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                clean, _errors = validate_patch(saved)
                self.data.update(clean)
                # 已下线的主题平滑迁移到风格最接近的现役主题，避免升级后
                # 用户看到一个自己没选过的默认配色。validate_patch 会丢弃
                # 未知主题，所以这里必须比对磁盘上的原始值。
                legacy = THEME_MIGRATION.get(saved.get("theme"))
                # CONFIG_RESET 处理「由自动化写入、并非用户本意」的值：
                # 只在磁盘上的原值正是它时触发，改完即从映射里删除，
                # 保证用户之后手动选的主题不会再被覆盖。
                changed = False
                for key, mapping in CONFIG_RESET.items():
                    raw = saved.get(key)
                    if raw in mapping:
                        self.data[key] = mapping[raw]
                        mapping.pop(raw)
                        changed = True
                if legacy or changed:
                    if legacy:
                        self.data["theme"] = legacy
                    self.save()
        except Exception as exc:
            _LOG.warning("Could not load config %s: %s", self.path, exc)

    def _write(self, data):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self):
        with self._lock:
            self._write(self.data)

    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        self.update({key: value})

    def update(self, mapping: dict):
        clean, errors = validate_patch(mapping or {})
        if errors:
            raise ValueError("invalid config: " + ", ".join(
                "%s (%s)" % item for item in errors.items()))
        with self._lock:
            proposed = {**self.data, **clean}
            self._write(proposed)
            self.data = proposed

    # 适配器可通过 cfg.get(...) 读取
    def __getitem__(self, key):
        return self.get(key)


def db_path(cfg: Config) -> str:
    return os.path.join(data_dir(), "index.db")
