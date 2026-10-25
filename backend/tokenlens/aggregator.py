# -*- coding: utf-8 -*-
"""tokenlens · 聚合分析层：把索引库里的原始记录折叠成各种维度的视图"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys
from collections import OrderedDict
from typing import Optional

from .pricing import lookup, is_known
from .store import search_clause

TOTAL_EXPR = "(input+output+cache_read+cache_write+reasoning)"


def _ndistinct(values):
    """等价于 SQL 的 COUNT(DISTINCT x)：去重且 NULL 不计入。

    summary/by_tool 这些地方原来是交给 SQLite 数的，改从预聚合行折叠之后
    必须自己保持同一口径——否则 model/project/session 存在 NULL 的库会
    多算一两条，数字就对不上了。
    """
    return len({v for v in values if v is not None})


def _session_identity(row):
    return (row["tool"], row["project"] or "", row["session"]) if row["session"] else None

RANGES = {
    "1d": 1, "7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365,
}

# 超过这个记录数就进入「大库模式」：自动收紧明细类查询的上限。
# 阈值取 20 万——本机全量约 3 万条时一切照旧，而几十万条的库会明显受益。
BIG_LIB_ROWS = 200_000

# 自定义区间用 "2026-01-01~2026-03-31" 这种 key 传进来。
# 之所以把它编码进 range 字符串（而不是加一堆并列参数），是因为整个应用
# ——前端状态、dashboard 缓存键、导出、记录分页——都只认一个 range 值，
# 这样自定义区间自动获得与预设区间完全相同的缓存与查询路径。
CUSTOM_SEP = "~"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_day(value):
    """把可能是脏值的日期字符串规范成 YYYY-MM-DD；非法返回 None。

    明细页的时间筛选走 URL 参数，任何字符串都可能传进来。这里一律
    "非法即忽略"，绝不抛异常，也不把原始串拼进 SQL。
    """
    s = str(value or "").strip()
    if not _DATE_RE.match(s):
        return None
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def parse_custom_range(key: str):
    """把 '起始~结束' 解析成 (start, end)；不是自定义区间就返回 None。

    对脏输入一律当作"不是自定义区间"，让调用方回退到默认，绝不抛异常——
    这个函数在每次请求的查询路径上，健壮性比严格性重要。
    """
    if not key or CUSTOM_SEP not in key:
        return None
    a, _, b = key.partition(CUSTOM_SEP)
    a, b = a.strip(), b.strip()
    if not (_DATE_RE.match(a) and _DATE_RE.match(b)):
        return None
    try:
        da, db = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    except ValueError:
        return None
    if da > db:                      # 起止写反了就自动换过来，别报错
        da, db = db, da
    return da.isoformat(), db.isoformat()


def make_custom_range(start, end) -> str:
    """把两个日期拼成自定义 range key；任一侧非法就返回空串。"""
    a, b = str(start or "").strip(), str(end or "").strip()
    try:
        da, db = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    except ValueError:
        return ""
    if da > db:
        da, db = db, da
    return "%s%s%s" % (da.isoformat(), CUSTOM_SEP, db.isoformat())


def range_bounds(key: str):
    """返回 (起始日期, 结束日期, 中文标签, 英文标签)。起始为 None 表示不限。

    标签给两种语言：界面语言由前端切换，而区间标签是后端算出来的，
    只返回中文的话英文界面里就会出现「Last 30 days · by day」混着中文标签。
    """
    today = dt.date.today()
    custom = parse_custom_range(key)
    if custom:
        start, end = custom
        label = start if start == end else "%s ~ %s" % (start, end)
        return start, end, label, label        # 日期本身两种语言写法一致
    if key == "today":
        return today.isoformat(), today.isoformat(), "今天", "Today"
    if key in RANGES:
        days = RANGES[key]
        start = today - dt.timedelta(days=days - 1)
        return (start.isoformat(), today.isoformat(),
                "近 %d 天" % days, "Last %d days" % days)
    if key == "ytd":
        return (dt.date(today.year, 1, 1).isoformat(), today.isoformat(),
                "今年以来", "Year to date")
    return None, today.isoformat(), "全部时间", "All time"


def fmt_i(n) -> str:
    """整数千分位。"""
    try:
        return format(int(n or 0), ",")
    except Exception:
        return "0"


def money(usd) -> str:
    """美元金额的简洁表示。"""
    v = float(usd or 0)
    if v >= 1000:
        return "$%.1fk" % (v / 1000.0)
    if v >= 1:
        return "$%.2f" % v
    return "$%.4f" % v


# ----------------------------------------------------------------------
# 预聚合（rollup）：把整库折叠成「天 × 工具 × 模型 × 会话 × 项目」粒度
#
# 为什么需要：仪表盘一次要跑十余个聚合，原先每个都全表扫一遍，本机 3.7 万条
# 实测合计约 900ms——切时间范围时明显卡顿。而这五列的基数远小于记录数
# （同一份数据 → 402 行），且 idx_records_day_wide 恰好按这个顺序覆盖了这五列
# 与全部参与求和的列，所以是一次纯覆盖索引扫描（EXPLAIN 实测
# "SCAN records USING COVERING INDEX idx_records_day_wide"）。
# 一次扫描（约 33ms）+ Python 折叠，可以替掉那十余次全表扫。
#
# 为什么 project 可以进分组：project 由 session 决定——实测 288 个会话里
# 跨多个 project 的 0 个、跨多个 tool 的 0 个，所以按五列折叠不会拆散会话，
# COUNT(DISTINCT session) 这类去重结果不受影响。
#
# 不从 rollup 恢复的只有三处，因为它们的粒度比 day 更细：
#   hourly / intraday 需要 ts 的时与分 → 继续各自跑 SQL（43ms / 0ms，都不贵）
#   records / session_detail 需要明细行 → 走索引分页，本来就不慢
# ----------------------------------------------------------------------
ROLLUP_GROUPS = ("day", "tool", "model", "session", "project")

ROLLUP_SQL = (
    "SELECT day, tool, model, session, project,"
    " COALESCE(SUM(%s),0) AS total,"
    " COALESCE(SUM(input),0) AS input, COALESCE(SUM(output),0) AS output,"
    " COALESCE(SUM(cache_read),0) AS cache_read,"
    " COALESCE(SUM(cache_write),0) AS cache_write,"
    " COALESCE(SUM(reasoning),0) AS reasoning,"
    " COALESCE(SUM(requests),0) AS requests,"
    " COALESCE(SUM(cost),0) AS cost,"
    " COUNT(*) AS records,"
    " MIN(CASE WHEN ts>0 THEN ts END) AS first_ts, MAX(ts) AS last_ts"
    " FROM records GROUP BY " + ", ".join(ROLLUP_GROUPS)) % TOTAL_EXPR


class Aggregator:
    MAX_MEMO = 48
    MAX_MEMO_BYTES = 4 * 1024 * 1024
    MAX_MEMO_ENTRY_BYTES = 512 * 1024
    MAX_ROLLUP_ROWS = 8192
    # 大库保护的默认开关。真正的值来自 cfg，这里只是 cfg 不可用时的兜底。
    DEFAULT_BIG_LIB = True

    def __init__(self, store, cfg=None):
        self.store = store
        # 同一连接的另一个引用。注意 store.conn 是方法不是属性，忘了调用
        # 拿到的是绑定方法，用起来会以「'method' object is not ...」炸掉。
        self.conn = store.conn()
        self.cfg = cfg
        self._memo_cache = OrderedDict()
        self._memo_sizes = {}
        self._memo_bytes = 0
        self._memo_scope = None
        # rollup 单独缓存：它比 _memo_cache 值钱（要 33ms 才建得起来），
        # 而 _memo_cache 满了会整体 clear，不该把它一起丢掉。
        self._rollup_rows = None
        self._rollup_ver = None
        self._sql_rollup = False
        self._filters = {}

    def filtered(self, filters):
        """A query-scoped view shared by records, summaries and exports."""
        view = Aggregator(self.store, self.cfg)
        allowed = ("tool", "model", "project", "project_paths", "q", "from", "to")
        view._filters = {k: str(filters[k]).strip() for k in allowed if filters.get(k)}
        if view._filters.get("project_paths"):
            paths = json.loads(view._filters["project_paths"])
            if (not isinstance(paths,list) or not 1 <= len(paths) <= 100
                    or any(not isinstance(p,str) or not p or len(p)>1024 for p in paths)):
                raise ValueError("invalid project path filter")
        lo, hi = _clean_day(view._filters.get("from")), _clean_day(view._filters.get("to"))
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        view._filters.update({"from": lo, "to": hi})
        return view

    # ------------------------------------------------------------------
    def _row_count(self):
        """记录总数，带缓存（只随 store.version 变）。"""
        # A failed database read must propagate; caching a synthetic zero made
        # large-library safeguards disappear until the next successful write.
        return self._memo(("__count__",), lambda: int(
            self._one("SELECT COUNT(*) n FROM records").get("n") or 0))

    def is_big(self):
        """库是不是大到需要收紧查询规模。

        为什么需要：明细/会话这类查询在没有上限时，几十万条记录会把
        单个请求拖到十几秒，界面看着就像卡死。这里给一个可关闭的阈值，
        超过后自动限制 LIMIT，并在响应里如实标注（不偷偷改数）。
        """
        try:
            on = bool(self.cfg.get("big_lib_mode")) if self.cfg is not None else self.DEFAULT_BIG_LIB
        except Exception:
            on = self.DEFAULT_BIG_LIB
        return bool(on) and self._row_count() >= BIG_LIB_ROWS

    def caps(self):
        """返回当前应生效的各项上限。"""
        big = self.is_big()
        return {
            "big": big,
            "rows": self._row_count(),
            # /api/records 的单次上限：大库时压到 500，避免一次拉爆内存与带宽
            "records": 500 if big else 5000,
            # 会话下钻的明细条数
            "session": 300 if big else 3000,
        }

    # ------------------------------------------------------------------
    # 记忆化：key 里带 store.version，任何写库都会让缓存自动失效。
    # 返回深拷贝，避免调用方改坏缓存（server 层会往结果里写 cost_cny 等）。
    # ------------------------------------------------------------------
    def _memo_size(self, value):
        """Bound retained response graphs; stop measuring oversized values early."""
        size, seen = 0, set()
        def visit(item):
            nonlocal size
            identity = id(item)
            if identity in seen or size > self.MAX_MEMO_ENTRY_BYTES:
                return
            seen.add(identity)
            size += sys.getsizeof(item)
            if isinstance(item, dict):
                for key, child in item.items():
                    visit(key)
                    visit(child)
                    if size > self.MAX_MEMO_ENTRY_BYTES:
                        break
            elif isinstance(item, (list, tuple, set, frozenset)):
                for child in item:
                    visit(child)
                    if size > self.MAX_MEMO_ENTRY_BYTES:
                        break
        visit(value)
        return size

    def _memo(self, key, fn):
        scope = (getattr(self.store, "version", 0), dt.date.today().isoformat())
        if scope != self._memo_scope:
            # Old data versions can never be reused. Release their graphs at
            # once rather than retaining up to 48 stale responses after scans.
            self.memo_clear()
            self._memo_scope = scope
        hit = self._memo_cache.get(key)
        if hit is not None:
            self._memo_cache.move_to_end(key)
            return copy.deepcopy(hit)
        val = fn()
        size = self._memo_size(val)
        if size > self.MAX_MEMO_ENTRY_BYTES:
            # Large exports/full billing results must not acquire a second,
            # long-lived copy just because ordinary card queries are cached.
            return val
        while self._memo_cache and (len(self._memo_cache) >= self.MAX_MEMO or
                                    self._memo_bytes + size > self.MAX_MEMO_BYTES):
            old_key, _ = self._memo_cache.popitem(last=False)
            self._memo_bytes -= self._memo_sizes.pop(old_key)
        self._memo_cache[key] = copy.deepcopy(val)
        self._memo_sizes[key] = size
        self._memo_bytes += size
        return val

    def memo_stats(self):
        return {"entries": len(self._memo_cache), "bytes": self._memo_bytes,
                "version": getattr(self.store, "version", 0)}

    def memo_clear(self):
        self._memo_cache.clear()
        self._memo_sizes.clear()
        self._memo_bytes = 0


    # ------------------------------------------------------------------
    def _project_clause(self, project):
        paths = [project]
        managed = {}
        if self._one("SELECT 1 present FROM sqlite_master WHERE type='table' AND name='managed_projects'"):
            managed = self._one("SELECT paths FROM managed_projects WHERE id=?", (project,))
        if managed:
            paths = json.loads(managed["paths"])
        elif project != "(未标注)" and ("/" in project or "\\" in project or (len(project) > 1 and project[1] == ":")):
            from .adapters import norm_project
            paths = [norm_project(project)]
        return ("COALESCE(NULLIF(project,''),'(未标注)') IN (" +
                ",".join("?" for _ in paths) + ")", paths)

    @staticmethod
    def _model_clause(model):
        if model == "未知模型":
            return "COALESCE(trim(model),'') = ''", []
        return "lower(trim(model)) = ?", [str(model).strip().lower()]

    def _where(self, rng: str, tool: Optional[str] = None, model: Optional[str] = None):
        clauses, params = [], []
        start, end, _label, _lang = range_bounds(rng)
        if start:
            clauses.append("day >= ?")
            params.append(start)
        clauses.append("day <= ?")
        params.append(end)
        if tool:
            clauses.append("tool = ?")
            params.append(tool)
        if model:
            fragment, values = self._model_clause(model)
            clauses.append(fragment)
            params.extend(values)
        for name in ("tool", "model", "project"):
            value = self._filters.get(name)
            if value:
                if name == "project":
                    fragment, values = self._project_clause(value)
                    clauses.append(fragment)
                    params.extend(values)
                elif name == "model":
                    fragment, values = self._model_clause(value)
                    clauses.append(fragment)
                    params.extend(values)
                else:
                    clauses.append(name + " = ?")
                    params.append(value)
        for key, op in (("from", ">="), ("to", "<=")):
            value = self._filters.get(key)
            if value:
                clauses.append("day " + op + " ?")
                params.append(value)
        if self._filters.get("project_paths"):
            paths = json.loads(self._filters["project_paths"])
            clauses.append("COALESCE(NULLIF(project,''),'(未标注)') IN (" + ",".join("?" for _ in paths) + ")")
            params.extend(paths)
        if self._filters.get("q"):
            fragment, values = search_clause(self._filters["q"], fts=self.store.fts)
            if fragment:
                clauses.append(fragment)
                params.extend(values)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params

    def _q(self, sql, params=()):
        return self.store.query(sql, params)

    def _one(self, sql, params=()):
        return self.store.query_one(sql, params)

    # ------------------------------------------------------------------
    # 预聚合：一次扫描，供下面所有按天/工具/模型/会话/项目的聚合共用
    # ------------------------------------------------------------------
    def _rollup(self):
        """整库折叠成 ROLLUP_GROUPS 粒度的行；按 store.version 缓存。

        单次约 33ms（3.7 万条实测），而它替掉的十余个聚合原本合计约 900ms。
        缓存键用 store.version：任何一次扫描写库都会让它自动重建。
        """
        ver = getattr(self.store, "version", 0)
        if self._rollup_rows is not None and self._rollup_ver == ver:
            return self._rollup_rows
        if self._filters:
            where, params = self._where("all")
            sql = ROLLUP_SQL.replace(" FROM records GROUP BY ",
                                     " FROM records " + where + " GROUP BY ")
        else:
            sql, params = ROLLUP_SQL, []
        # Unique sessions can turn a million input rows into a million Python
        # dictionaries. Probe with a bounded result; perform identity counts and
        # session rankings in SQLite when that representation would grow large.
        rows = self._q(sql + " LIMIT ?", [*params, self.MAX_ROLLUP_ROWS + 1])
        self._sql_rollup = len(rows) > self.MAX_ROLLUP_ROWS
        if self._sql_rollup:
            # Even (day, tool, model) can have one group per record. Retain no
            # fallback rows; each view queries only its own output dimensions.
            rows = []
        self._rollup_rows = rows
        self._rollup_ver = ver
        return rows

    def _uses_sql_rollup(self):
        self._rollup()
        return self._sql_rollup

    @staticmethod
    def _sql_sums():
        return ("COALESCE(SUM(" + TOTAL_EXPR + "),0) total," +
                ",".join("COALESCE(SUM(" + key + "),0) " + key for key in
                         ("input", "output", "cache_read", "cache_write", "reasoning", "requests", "cost")) +
                ",COUNT(*) records,MIN(CASE WHEN ts>0 THEN ts END) first_ts,MAX(ts) last_ts")

    @staticmethod
    def _sql_session_count():
        # Hex uses an alphabet without ':', so separators cannot collide with
        # values (including embedded NULs, for which SQLite length is truncated).
        # NULL tool remains distinct from an empty tool; NULL/empty projects merge.
        return ("COUNT(DISTINCT CASE WHEN session<>'' THEN "
                "CASE WHEN tool IS NULL THEN 'N' ELSE 'S'||hex(tool) END||':'||"
                "hex(COALESCE(project,''))||':'||hex(session) END)")

    def _iter_sql(self, sql, params=()):
        """Stream intermediate groups under Store's connection lock."""
        with self.store.locked() as conn:
            cursor = conn.execute(sql, params)
            try:
                for row in cursor:
                    yield dict(row)
            finally:
                cursor.close()

    def _sql_summary(self, rng, tool):
        where, params = self._where(rng, tool)
        return self._one("SELECT " + self._sql_sums() + ",COUNT(DISTINCT tool) tools,"
            "COUNT(DISTINCT model) models," + self._sql_session_count() + " sessions,"
            "COUNT(DISTINCT project) projects,COUNT(DISTINCT day) days FROM records " + where, params)

    def _sql_group(self, rng, tool, group, select, extras="", limit=None, order="total",
                   offset=0, tie_break=""):
        where, params = self._where(rng, tool)
        sql = "SELECT " + select + "," + self._sql_sums() + extras + " FROM records " + where + " GROUP BY " + group
        sql += " ORDER BY " + order + " DESC"
        if tie_break:
            sql += "," + tie_break
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((max(0, int(limit)), max(0, int(offset))))
        return self._q(sql, params)

    def _register_model_key(self):
        with self.store.locked() as conn:
            conn.create_function("tokenlens_model_key", 1,
                                 lambda value: str(value or "").strip().lower(), deterministic=True)

    def _filtered_token_total(self, rng, tool=None):
        where, params = self._where(rng, tool)
        return self._memo(("range_token_total", where, tuple(params)), lambda: int(
            self._one("SELECT COALESCE(SUM(" + TOTAL_EXPR + "),0) total FROM records " + where,
                      params).get("total") or 0))

    def _rollup_fold(self, rng, tool=None, model=None):
        """取某个时间区间（可选工具/模型过滤）内的预聚合行。

        口径与 _where() 完全一致：day >= start AND day <= end（start 为
        None 时不限下界，与「全部时间」的 SQL 一致）。
        """
        start, end, _label, _lang = range_bounds(rng)
        out = []
        selected_model = "" if model == "未知模型" else str(model or "").strip().lower()
        for r in self._rollup():
            d = r["day"]
            if start and d < start:
                continue
            if d > end:
                continue
            if tool and r["tool"] != tool:
                continue
            if model and str(r["model"] or "").strip().lower() != selected_model:
                continue
            out.append(r)
        return out

    @staticmethod
    def _sum(rows, key):
        return sum((r.get(key) or 0) for r in rows)

    # ------------------------------------------------------------------
    def summary(self, rng="30d", tool=None):
        return self._memo(("summary", rng, tool), lambda: self.__impl_summary(rng, tool))

    def __impl_summary(self, rng="30d", tool=None):
        if self._uses_sql_rollup():
            row = self._sql_summary(rng, tool)
            total = row["total"]
        else:
            row = self._summary_from_rows(rng, tool)
            total = row["total"]
        # Previous-period identity is independent from the displayed range.
        start, end, _, _lang = range_bounds(rng)
        pt = 0
        if start:
            days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
            pend = dt.date.fromisoformat(start) - dt.timedelta(days=1)
            pstart = pend - dt.timedelta(days=days - 1)
            previous = "%s%s%s" % (pstart.isoformat(), CUSTOM_SEP, pend.isoformat())
            pt = (self._sql_summary(previous, tool)["total"] if self._sql_rollup else
                  self._sum(self._rollup_fold(previous, tool=tool), "total"))
        row["prev_total"] = pt
        row["delta"] = (round((total - pt) * 100.0 / pt, 1) if pt else None)
        row["active_days"] = row.get("days") or 0
        row["avg_per_day"] = int(total / row["active_days"]) if row.get("active_days") else 0
        denom = (row.get("input") or 0) + (row.get("cache_read") or 0) + (row.get("cache_write") or 0)
        row["cache_hit_rate"] = round((row.get("cache_read") or 0) * 100.0 / denom, 1) if denom else 0.0
        row["avg_per_request"] = int(total / row["requests"]) if row.get("requests") else 0
        return row

    def _summary_from_rows(self, rng, tool):
        rows = self._rollup_fold(rng, tool=tool)
        total = self._sum(rows, "total")
        row = {
            "total": total,
            "input": self._sum(rows, "input"),
            "output": self._sum(rows, "output"),
            "cache_read": self._sum(rows, "cache_read"),
            "cache_write": self._sum(rows, "cache_write"),
            "reasoning": self._sum(rows, "reasoning"),
            "requests": self._sum(rows, "requests"),
            "cost": self._sum(rows, "cost"),
            "records": self._sum(rows, "records"),
            "tools": _ndistinct(r["tool"] for r in rows),
            "models": _ndistinct(r["model"] for r in rows),
            "sessions": _ndistinct(_session_identity(r) for r in rows),
            "projects": _ndistinct(r["project"] for r in rows),
            "days": _ndistinct(r["day"] for r in rows),
            # MIN(CASE WHEN ts>0 THEN ts END)：全为 0/负时是 NULL，不能退化成 0
            "first_ts": min([r["first_ts"] for r in rows if r.get("first_ts")] or [None]),
            "last_ts": max([r["last_ts"] for r in rows] or [None]),
        }
        return row

    # ------------------------------------------------------------------
    def by_tool(self, rng="30d", tool=None):
        return self._memo(("by_tool", rng, tool), lambda: self.__impl_by_tool(rng, tool))

    def __impl_by_tool(self, rng="30d", tool=None):
        if self._uses_sql_rollup():
            rows = self._sql_group(rng, tool, "tool", "tool", "," + self._sql_session_count() +
                " sessions,COUNT(DISTINCT model) models,COUNT(DISTINCT project) projects")
            grand = sum(r["total"] for r in rows) or 1
            for row in rows:
                row.pop("first_ts", None)
                row["share"] = round(row["total"] * 100.0 / grand, 2)
            return rows
        buckets = {}
        for r in self._rollup_fold(rng, tool=tool):
            b = buckets.get(r["tool"])
            if b is None:
                b = buckets[r["tool"]] = {
                    "tool": r["tool"], "total": 0, "input": 0, "output": 0,
                    "cache_read": 0, "cache_write": 0, "reasoning": 0,
                    "requests": 0, "cost": 0.0, "records": 0,
                    "sessions": set(), "models": set(), "projects": set(),
                    "last_ts": None}
            for k in ("total", "input", "output", "cache_read", "cache_write",
                      "reasoning", "requests", "cost", "records"):
                b[k] += r[k] or 0
            b["sessions"].add(_session_identity(r))
            b["models"].add(r["model"])
            b["projects"].add(r["project"])
            if r.get("last_ts") is not None and (b["last_ts"] is None
                                                 or r["last_ts"] > b["last_ts"]):
                b["last_ts"] = r["last_ts"]
        rows = []
        for b in buckets.values():
            b["sessions"] = _ndistinct(b["sessions"])
            b["models"] = _ndistinct(b["models"])
            b["projects"] = _ndistinct(b["projects"])
            rows.append(b)
        rows.sort(key=lambda x: -x["total"])
        grand = sum(r["total"] for r in rows) or 1
        for r in rows:
            r["share"] = round(r["total"] * 100.0 / grand, 2)
        return rows

    def by_model(self, rng="30d", limit=40, tool=None, offset=0):
        limit, offset = max(0, int(limit)), max(0, int(offset))
        return self._memo(("by_model", rng, limit, tool, offset),
                          lambda: self.__impl_by_model(rng, limit, tool, offset))

    def __impl_by_model(self, rng="30d", limit=40, tool=None, offset=0):
        if self._uses_sql_rollup():
            # Python's strip/lower also handles Unicode and non-space whitespace.
            # SQLite's built-ins would split groups that the small-library path merges.
            self._register_model_key()
            group = "tokenlens_model_key(model)"
            rows = self._sql_group(rng, tool, group, group + " model", ",COUNT(DISTINCT tool) tools," +
                                   self._sql_session_count() + " sessions", limit,
                                   offset=offset, tie_break="model ASC")
            grand = self._filtered_token_total(rng, tool) or 1
            tool_lists = {row["model"]: [] for row in rows}
            if tool_lists:
                where, params = self._where(rng, tool)
                tools = self._iter_sql("SELECT DISTINCT tool," + group + " model FROM records " + where +
                    " AND " + group + " IN (" + ",".join("?" for _ in tool_lists) + ") ORDER BY tool",
                    [*params, *tool_lists])
                for item in tools:
                    tool_lists[item["model"]].append(item["tool"])
            for row in rows:
                model = row["model"]
                row["tool_list"] = tool_lists[model]
                row["model"] = model or "未知模型"
                row["share"] = round(row["total"] * 100.0 / grand, 2)
                row["pricing_known"] = is_known(row["model"])
                for key in ("records", "first_ts", "last_ts"):
                    row.pop(key, None)
            return rows
        buckets = {}
        for r in self._rollup_fold(rng, tool=tool):
            key = str(r["model"] or "").strip().lower()
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {
                    "model": key or "未知模型", "total": 0, "input": 0, "output": 0,
                    "cache_read": 0, "cache_write": 0, "reasoning": 0, "requests": 0, "cost": 0.0,
                    "tools": set(), "sessions": set(), "tl": set()}
            for k in ("total", "input", "output", "cache_read", "cache_write", "reasoning",
                      "requests", "cost"):
                b[k] += r[k] or 0
            b["tools"].add(r["tool"])
            b["sessions"].add(_session_identity(r))
            b["tl"].add(r["tool"])
        rows = []
        for b in buckets.values():
            b["tools"] = _ndistinct(b["tools"])
            b["sessions"] = _ndistinct(b["sessions"])
            # 工具清单显式排序：原写法 GROUP_CONCAT(DISTINCT tool) 的顺序取决于
            # 扫描顺序，换个索引计划就变；排序后同一份数据结果稳定。
            b["tool_list"] = sorted(b.pop("tl"))
            rows.append(b)
        grand = sum(r["total"] for r in rows) or 1
        rows = [buckets[key] for key in sorted(buckets, key=lambda key: (-buckets[key]["total"], key))]
        rows = rows[offset:offset+limit]
        for r in rows:
            r["share"] = round(r["total"] * 100.0 / grand, 2)
            r["pricing_known"] = is_known(r.get("model") or "")
        return rows

    def by_project(self, rng="30d", limit=40, tool=None, offset=0):
        limit, offset = max(0, int(limit)), max(0, int(offset))
        return self._memo(("by_project", rng, limit, tool, offset),
                          lambda: self.__impl_by_project(rng, limit, tool, offset))

    def __impl_by_project(self, rng="30d", limit=40, tool=None, offset=0):
        if self._uses_sql_rollup():
            group = "COALESCE(project,'')"
            rows = self._sql_group(rng, tool, group, group + " project", ",COUNT(DISTINCT tool) tools," +
                                   self._sql_session_count() + " sessions", limit,
                                   offset=offset, tie_break="project ASC")
            grand = self._filtered_token_total(rng, tool) or 1
            tool_lists = {row["project"]: [] for row in rows}
            if tool_lists:
                where, params = self._where(rng, tool)
                # Forty project cards used to run forty full scans because the
                # normalized project expression has no standalone index.
                for item in self._iter_sql("SELECT DISTINCT " + group + " project,tool FROM records " + where +
                        " AND " + group + " IN (" + ",".join("?" for _ in tool_lists) + ") ORDER BY tool",
                        [*params, *tool_lists]):
                    tool_lists[item["project"]].append(item["tool"])
            for row in rows:
                project = row["project"]
                row["tool_list"] = tool_lists[project]
                row["project"] = project or "(未标注)"
                row["share"] = round(row["total"] * 100.0 / grand, 2)
                for key in ("cache_write", "reasoning", "records", "first_ts"):
                    row.pop(key, None)
            return rows
        buckets = {}
        for r in self._rollup_fold(rng, tool=tool):
            key = r["project"] or ""
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {
                    "project": key or "(未标注)", "total": 0, "input": 0, "output": 0,
                    "cache_read": 0, "cost": 0.0, "requests": 0,
                    "tools": set(), "sessions": set(), "tl": set(), "last_ts": None}
            for k in ("total", "input", "output", "cache_read", "cost", "requests"):
                b[k] += r[k] or 0
            b["tools"].add(r["tool"])
            b["sessions"].add(_session_identity(r))
            b["tl"].add(r["tool"])
            if r.get("last_ts") is not None and (b["last_ts"] is None
                                                 or r["last_ts"] > b["last_ts"]):
                b["last_ts"] = r["last_ts"]
        rows = []
        for b in buckets.values():
            b["tools"] = _ndistinct(b["tools"])
            b["sessions"] = _ndistinct(b["sessions"])
            b["tool_list"] = sorted(b.pop("tl"))
            rows.append(b)
        grand = sum(r["total"] for r in rows) or 1
        rows = [buckets[key] for key in sorted(buckets, key=lambda key: (-buckets[key]["total"], key))]
        rows = rows[offset:offset+limit]
        for r in rows:
            r["share"] = round(r["total"] * 100.0 / grand, 2)
        return rows

    def by_category(self, rng="30d"):
        return self._memo(("by_category", rng), lambda: self.__impl_by_category(rng))

    def __impl_by_category(self, rng="30d"):
        """按来源类别（CLI / 编辑器 / 插件 / 桌面端 / API）汇总，供前端雷达式概览用。"""
        from . import adapters as A
        cat_of = {m["id"]: m["category"] for m in A.adapter_meta()}
        buckets = {}
        for row in self.by_tool(rng):
            cat = cat_of.get(row["tool"], "other")
            b = buckets.setdefault(cat, {"category": cat,
                                         "label": A.CATEGORY_LABEL.get(cat, "其他"),
                                         "label_en": A.CATEGORY_LABEL_EN.get(cat, "Other"),
                                         "total": 0, "cost": 0, "tools": 0, "requests": 0})
            b["total"] += row["total"]
            b["cost"] += row["cost"]
            b["requests"] += row["requests"]
            b["tools"] += 1
        return sorted(buckets.values(), key=lambda x: -x["total"])

    # ------------------------------------------------------------------
    def daily(self, rng="30d", tool=None):
        if self._uses_sql_rollup():
            rows = self._sql_group(rng, tool, "day", "day", order="day")
            keys = ("day", "total", "input", "output", "cache_read", "cache_write", "cost", "requests")
            return {r["day"]: {k: r[k] for k in keys} for r in reversed(rows)}
        buckets = {}
        for r in self._rollup_fold(rng, tool):
            b = buckets.get(r["day"])
            if b is None:
                b = buckets[r["day"]] = {"day": r["day"], "total": 0, "input": 0,
                                         "output": 0, "cache_read": 0, "cache_write": 0,
                                         "cost": 0.0, "requests": 0}
            for k in ("total", "input", "output", "cache_read", "cache_write",
                      "cost", "requests"):
                b[k] += r[k] or 0
        return {d: buckets[d] for d in sorted(buckets)}

    def daily_series(self, rng="30d", tool=None):
        return self._memo(("daily_series", rng, tool), lambda: self.__impl_daily_series(rng, tool))

    def __impl_daily_series(self, rng="30d", tool=None):
        """补齐缺失日期，方便前端画连续曲线。"""
        data = self.daily(rng, tool)
        start, end, _, _lang = range_bounds(rng)
        if not start:
            days = sorted(data.keys())
            if not days:
                return []
            start = days[0]
        s = dt.date.fromisoformat(start)
        e = dt.date.fromisoformat(end)
        out, cur = [], s
        while cur <= e:
            key = cur.isoformat()
            r = data.get(key) or {"day": key, "total": 0, "input": 0, "output": 0,
                                  "cache_read": 0, "cache_write": 0, "cost": 0, "requests": 0}
            out.append(r)
            cur += dt.timedelta(days=1)
        return out

    def heatmap(self, weeks=53):
        return self._memo(("heatmap", weeks), lambda: self.__impl_heatmap(weeks))

    def __impl_heatmap(self, weeks=53):
        """近一年每日强度，用于日历热力图。"""
        today = dt.date.today()
        start = today - dt.timedelta(days=today.weekday()) - dt.timedelta(weeks=weeks - 1)
        data = {}
        rows = self._rollup()
        if self._sql_rollup:
            where, params = self._where("all")
            rows = self._q("SELECT day,SUM(" + TOTAL_EXPR + ") total,SUM(cost) cost,"
                           "SUM(requests) requests FROM records " + where +
                           " AND day>=? GROUP BY day", [*params, start.isoformat()])
        for r in rows:
            if r["day"] < start.isoformat():
                continue
            b = data.get(r["day"])
            if b is None:
                b = data[r["day"]] = {"day": r["day"], "total": 0, "cost": 0.0, "requests": 0}
            b["total"] += r["total"] or 0
            b["cost"] += r["cost"] or 0
            b["requests"] += r["requests"] or 0
        days = []
        cur = start
        while cur <= today:
            k = cur.isoformat()
            r = data.get(k) or {"day": k, "total": 0, "cost": 0, "requests": 0}
            days.append(r)
            cur += dt.timedelta(days=1)
        return {"start": start.isoformat(), "end": today.isoformat(), "days": days}

    def hourly(self, rng="30d"):
        return self._memo(("hourly", rng), lambda: self.__impl_hourly(rng))

    def __impl_hourly(self, rng="30d"):
        w, p = self._where(rng)
        return self._q(
            f"SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,"
            f" COALESCE(SUM({TOTAL_EXPR}),0) AS total, COALESCE(SUM(requests),0) AS requests"
            f" FROM records {w} GROUP BY hour ORDER BY hour", p)

    def weekday(self, rng="90d"):
        return self._memo(("weekday", rng), lambda: self.__impl_weekday(rng))

    def __impl_weekday(self, rng="90d"):
        # 星期几由 day 推出来（两者都是本地时区），不必再扫 ts。
        buckets = {}
        for r in self.daily(rng).values():
            try:
                dow = (dt.date.fromisoformat(r["day"]).weekday() + 1) % 7
            except (ValueError, TypeError):
                continue                       # day 有脏值时跳过，不让整页崩掉
            b = buckets.get(dow)
            if b is None:
                b = buckets[dow] = {"dow": dow, "total": 0, "requests": 0}
            b["total"] += r["total"] or 0
            b["requests"] += r["requests"] or 0
        return [buckets[k] for k in sorted(buckets)]

    # ------------------------------------------------------------------
    # 「今天」专用：按小时 / 按半小时切分当天用量。
    # 只选今天时，按天聚合只会得到一根柱子，看不出"几点在忙"，所以要更细的粒度。
    def intraday(self, rng="today", bucket_minutes=30):
        """返回分时段数据。**始终按 30 分钟切**，前端要 60 分钟就两两合并。

        这样前端的"按小时/按半小时"只是显示层切换，不必重新请求，
        切换是瞬时的，也省掉一次往返。
        """
        return self._memo(("intraday", rng), lambda: self.__impl_intraday(rng))

    def __impl_intraday(self, rng="today"):
        start, end, _, _lang = range_bounds(rng)
        if not start or start != end:
            # 不是单日区间：没有分时段可言，前端据此隐藏卡片
            return {"bucket_minutes": 0, "single_day": False, "buckets": []}
        bm = 30
        w, p = self._where(rng)
        rows = self._q(
            f"SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS h,"
            f" CAST(strftime('%M', ts, 'unixepoch', 'localtime') AS INTEGER) AS m,"
            f" COALESCE(SUM({TOTAL_EXPR}),0) AS total,"
            f" COALESCE(SUM(input),0) AS input, COALESCE(SUM(output),0) AS output,"
            f" COALESCE(SUM(cache_read),0) AS cache_read,"
            f" COALESCE(SUM(reasoning),0) AS reasoning,"
            f" COALESCE(SUM(requests),0) AS requests,"
            f" COALESCE(SUM(cost),0) AS cost"
            f" FROM records {w} GROUP BY h, m", p)
        buckets = [[0, 0, 0, 0, 0, 0.0] for _ in range(24 * 60 // bm)]
        for r in rows:
            idx = (int(r["h"]) * 60 + int(r["m"])) // bm
            if 0 <= idx < len(buckets):
                b = buckets[idx]
                b[0] += r["total"] or 0
                b[1] += r["input"] or 0
                b[2] += r["output"] or 0
                b[3] += r["cache_read"] or 0
                b[4] += r["requests"] or 0
                b[5] += r["cost"] or 0
        out = []
        for i, b in enumerate(buckets):
            mins = i * bm
            out.append({"hour": mins // 60, "minute": mins % 60,
                        "label": "%02d:%02d" % (mins // 60, mins % 60),
                        "total": b[0], "input": b[1], "output": b[2],
                        "cache_read": b[3], "requests": b[4], "cost": round(b[5], 6)})
        return {"bucket_minutes": bm, "single_day": True, "day": start, "buckets": out}


    # ------------------------------------------------------------------
    def sessions(self, rng="30d", limit=30, tool=None, order="total", offset=0):
        limit, offset = max(0, int(limit)), max(0, int(offset))
        return self._memo(("sessions", rng, limit, tool, order, offset),
                          lambda: self.__impl_sessions(rng, limit, tool, order, offset))

    def __impl_sessions(self, rng="30d", limit=30, tool=None, order="total", offset=0):
        if self._uses_sql_rollup():
            where, params = self._where(rng, tool)
            order_column = "cost" if order == "cost" else "total"
            rows = self._q("SELECT session,tool,COALESCE(project,'') project," + self._sql_sums() +
                ",MIN(day) start_day,MAX(day) end_day FROM records " + where + " AND session<>'' "
                "GROUP BY tool,COALESCE(project,''),session ORDER BY " + order_column +
                " DESC,tool ASC,project ASC,session ASC LIMIT ? OFFSET ?", [*params, limit, offset])
            all_where, all_params = self._where("all") if self._filters else ("WHERE 1=1", [])
            for row in rows:
                models = self._q("SELECT DISTINCT model FROM records " + all_where +
                    " AND tool IS ? AND COALESCE(project,'')=? AND session=? AND model<>'' ORDER BY model",
                    [*all_params, row["tool"], row["project"], row["session"]])
                row["model_list"] = ",".join(item["model"] for item in models)
                row["project"] = row["project"] or "(未标注)"
                for key in ("cache_write", "reasoning", "records", "first_ts"):
                    row.pop(key, None)
            return rows
        # A session id can recur under another tool or project; keep the full identity.
        buckets = {}
        for r in self._rollup_fold(rng, tool):
            sid = r["session"]
            if not sid:                       # HAVING session <> ''
                continue
            identity = (r["tool"], r["project"] or "", sid)
            b = buckets.get(identity)
            if b is None:
                b = buckets[identity] = {
                    "session": sid, "tool": r["tool"],
                    "project": r["project"] or "(未标注)", "total": 0, "input": 0,
                    "output": 0, "cache_read": 0, "cost": 0.0, "requests": 0,
                    "start_day": r["day"], "end_day": r["day"], "last_ts": None,
                    "models": set()}
            for k in ("total", "input", "output", "cache_read", "cost", "requests"):
                b[k] += r[k] or 0
            if r["day"] < b["start_day"]:
                b["start_day"] = r["day"]
            if r["day"] > b["end_day"]:
                b["end_day"] = r["day"]
            if r.get("last_ts") is not None and (b["last_ts"] is None
                                                 or r["last_ts"] > b["last_ts"]):
                b["last_ts"] = r["last_ts"]
        if not buckets:
            return []
        order_column = "cost" if order == "cost" else "total"
        identities = sorted(buckets, key=lambda identity:
            (-buckets[identity][order_column], *(str(value or "") for value in identity)))
        rows = [buckets[identity] for identity in identities[offset:offset+limit]]
        # model_list 与该会话的日期无关：原实现查的是全库同 session 的记录，
        # 所以这里也从全量 rollup 里取，不能用 rng 过滤后的行。
        wanted = {(r["tool"], r["project"] if r["project"] != "(未标注)" else "", r["session"]) for r in rows}
        all_models = {}
        for r in self._rollup():
            identity = (r["tool"], r["project"] or "", r["session"])
            if identity in wanted and r["model"]:
                all_models.setdefault(identity, set()).add(r["model"])
        for r in rows:
            # 模型清单显式排序（原 GROUP_CONCAT(DISTINCT model) 顺序随扫描计划变）
            identity = (r["tool"], r["project"] if r["project"] != "(未标注)" else "", r["session"])
            r["model_list"] = ",".join(sorted(all_models.get(identity, ())))
            r.pop("models", None)
        return rows

    def rankings(self, kind, rng="30d", limit=20, offset=0, order="total"):
        """Page complete grouped rankings without sending all groups to the UI."""
        if kind not in ("models", "projects", "sessions"):
            raise ValueError("kind must be models, projects or sessions")
        limit, offset = int(limit), int(offset)
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must be non-negative")
        where, params = self._where(rng)
        if kind == "models":
            self._register_model_key()
            group = "tokenlens_model_key(model)"
        elif kind == "projects":
            group = "COALESCE(project,'')"
        else:
            where += " AND session<>''"
            group = "tool,COALESCE(project,''),session"
        total = self._memo(("ranking_count", kind, where, tuple(params)), lambda: int(
            self._one("SELECT COUNT(*) n FROM (SELECT 1 FROM records " + where +
                      " GROUP BY " + group + ")", params).get("n") or 0))
        if offset >= total:
            offset = ((total - 1) // limit) * limit if total else 0
        if kind == "models":
            rows = self.by_model(rng, limit=limit, offset=offset)
        elif kind == "projects":
            rows = self.by_project(rng, limit=limit, offset=offset)
        else:
            rows = self.sessions(rng, limit=limit, offset=offset, order=order)
        return {"rows": rows, "total": total, "limit": limit, "offset": offset}

    def records(self, rng="30d", tool=None, model=None, project=None,
                limit=200, offset=0, order="ts", q=None, day_from=None, day_to=None,
                clamp_offset=False):
        limit, offset = int(limit), int(offset)
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        w, p = self._where(rng, tool, model)
        # 明细页的「时间筛选」：在时间范围之上再收窄到具体某几天。
        # 传进来的日期先做合法性校验，脏值直接忽略而不是让 SQL 报错。
        lo = _clean_day(day_from)
        hi = _clean_day(day_to)
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        if lo:
            w += " AND day >= ?"
            p.append(lo)
        if hi:
            w += " AND day <= ?"
            p.append(hi)
        if project:
            fragment, values = self._project_clause(project)
            w += " AND " + fragment
            p.extend(values)
        if q:
            # 搜索走 FTS5 索引（需求 10）。原来是 4 列 LIKE，30 万条下不管
            # 命中几行都要全表扫描（实测命中数和耗时无关，恒定 ~16-20ms；
            # 零命中的词最贵）。改走索引后零命中 0.1ms、罕见词 1.2ms。
            # store.search_clause 负责按词长分流并处理 trigram 的 2 字下限。
            frag, fparams = search_clause(q, fts=self.store.fts)
            if frag:
                w += " AND " + frag
                p.extend(fparams)
        # Page selection needs the real number of matches. Cache that scalar
        # by the complete SQL filter, not by offset/order, so export batches
        # and page changes do not repeatedly scan the same matching index.
        # _memo includes the data version and date; read-only HTTP snapshots
        # retain their own version and cannot mix old counts with new rows.
        total = self._memo(("records_count", w, tuple(p)), lambda: int(
            self._one(f"SELECT COUNT(*) AS n FROM records {w}", p).get("n") or 0))
        # Only interactive callers clamp a stale page after data shrinks.
        # Export iterators rely on an empty batch at/after the end of the data.
        if clamp_offset and offset >= total:
            offset = ((total - 1) // limit) * limit if total else 0
        # 排序：
        # 「按时间」这一档写成 `day DESC, ts DESC` 而不是单列 `ts DESC`。
        # 原因是 SQLite 只在 ORDER BY 能匹配上索引前缀时才肯用索引排序：
        #   ORDER BY ts DESC        -> 只能走 idx_records_day 过滤，再建临时 B 树排序
        #                              （30 万条实测 2499ms）
        #   ORDER BY day DESC, ts DESC -> 能直接走 idx_records_day_ts 有序扫描
        #                              （同数据实测 70ms，36 倍）
        # 语义完全等价：记录是按时间写入的，day 与 ts 单调同向，两种排法结果一致。
        order_sql = {
            "ts": "day DESC, ts DESC, id DESC",
            "total": TOTAL_EXPR + " DESC, id DESC",
            "cost": "cost DESC, id DESC",
        }.get(order, "day DESC, ts DESC, id DESC")
        rows = self._q(
            f"SELECT id, tool, ts, day, model, project, session, input, output,"
            f" cache_read, cache_write, reasoning, requests, cost, cost_kind, cost_source, price_version, currency, src, extra,"
            f" {TOTAL_EXPR} AS total FROM records {w}"
            f" ORDER BY {order_sql} LIMIT ? OFFSET ?", [*p, limit, offset])
        return {"rows": rows, "total": total, "total_capped": False,
                "offset": offset, "limit": limit}

    # ------------------------------------------------------------------
    # 周期对比：本期 vs 上一等长周期
    # ------------------------------------------------------------------
    def compare(self, rng="30d", tool=None):
        return self._memo(("compare", rng, tool), lambda: self.__impl_compare(rng, tool))

    def __impl_compare(self, rng="30d", tool=None):
        start, end, label, label_en = range_bounds(rng)

        def totals(rows):
            return {
                "total": self._sum(rows, "total"),
                "input": self._sum(rows, "input"),
                "output": self._sum(rows, "output"),
                "cache_read": self._sum(rows, "cache_read"),
                "requests": self._sum(rows, "requests"),
                "cost": self._sum(rows, "cost"),
                "sessions": _ndistinct(_session_identity(r) for r in rows),
                "days": _ndistinct(r["day"] for r in rows),
            }

        sql_mode = self._uses_sql_rollup()
        def period_totals(period):
            if not sql_mode:
                return totals(self._rollup_fold(period, tool))
            result = self._sql_summary(period, tool)
            return {k:result[k] for k in ("total","input","output","cache_read","requests","cost","sessions","days")}
        cur = period_totals(rng)
        if not start:
            return {"label": "全部时间", "label_en": "All time",
                    "has_prev": False, "current": cur, "previous": {},
                    "tools": []}
        d0 = dt.date.fromisoformat(start)
        d1 = dt.date.fromisoformat(end)
        span = (d1 - d0).days + 1
        pend = d0 - dt.timedelta(days=1)
        pstart = pend - dt.timedelta(days=span - 1)
        prng = "%s%s%s" % (pstart.isoformat(), CUSTOM_SEP, pend.isoformat())
        prev = period_totals(prng)

        def per_tool(rows):
            out = {}
            for r in rows:
                b = out.get(r["tool"])
                if b is None:
                    b = out[r["tool"]] = {"total": 0, "requests": 0, "cost": 0.0}
                b["total"] += r["total"] or 0
                b["requests"] += r["requests"] or 0
                b["cost"] += r["cost"] or 0
            return out

        # 工具去重列表显式排序：原写法 GROUP_CONCAT(DISTINCT tool) 的输出顺序
        # 取决于扫描顺序（首次出现的先后），换索引计划就会变；排序后同一份数据
        # 每次刷新都得到相同结果，前端不会莫名抖动。
        if sql_mode:
            ct = per_tool(self._sql_group(rng, tool, "tool", "tool"))
            pt = per_tool(self._sql_group(prng, tool, "tool", "tool"))
        else:
            ct = per_tool(self._rollup_fold(rng, tool))
            pt = per_tool(self._rollup_fold(prng, tool))
        tools = []
        # 平局（current 相同，例如两个来源本期都归零）时按 tool 名再排一次，
        # 否则顺序取决于 set 迭代，同一份数据每次刷新都可能不一样。
        for tid in sorted(set(list(ct.keys()) + list(pt.keys()))):
            c = ct.get(tid, {})
            pv = pt.get(tid, {})
            ctot = c.get("total", 0) or 0
            ptot = pv.get("total", 0) or 0
            tools.append({
                "tool": tid, "current": ctot, "previous": ptot,
                "delta": (round((ctot - ptot) * 100.0 / ptot, 1) if ptot else None),
                "requests": c.get("requests", 0) or 0,
                "cost": c.get("cost", 0.0) or 0.0,
            })
        tools.sort(key=lambda x: -x["current"])
        for k in ("total", "input", "output", "cache_read", "requests", "cost", "sessions", "days"):
            c = cur.get(k) or 0
            pv = prev.get(k) or 0
            cur[k + "_prev"] = pv
            cur[k + "_delta"] = (round((c - pv) * 100.0 / pv, 1) if pv else None)
        return {
            "label": label, "label_en": label_en, "has_prev": True,
            "current": cur, "previous": prev, "tools": tools,
            "period": [start, end], "prev_period": [pstart.isoformat(), pend.isoformat()],
        }

    # ------------------------------------------------------------------
    # 会话详情（下钻抽屉）
    # ------------------------------------------------------------------
    def session_detail(self, session_id, tool=None, limit=300, project=None, rng="all"):
        w, p = self._where(rng)
        w += " AND session = ?"
        p.append(session_id)
        if tool:
            w += " AND tool = ?"
            p.append(tool)
        if project is not None:
            w += " AND COALESCE(project,'') = ?"
            p.append("" if project == "(未标注)" else project)
        head = self._one(
            f"SELECT tool, COALESCE(NULLIF(project,''),'(未标注)') AS project,"
            f" COALESCE(SUM({TOTAL_EXPR}),0) AS total, COALESCE(SUM(input),0) AS input,"
            f" COALESCE(SUM(output),0) AS output, COALESCE(SUM(cache_read),0) AS cache_read,"
            f" COALESCE(SUM(cache_write),0) AS cache_write, COALESCE(SUM(requests),0) AS requests,"
            f" COALESCE(SUM(cost),0) AS cost, MIN(ts) AS first_ts, MAX(ts) AS last_ts,"
            f" COUNT(*) AS records,COALESCE(SUM(CASE WHEN cost_kind='unknown' THEN 1 ELSE 0 END),0) AS unknown_records FROM records {w}", p)
        if not head or not head.get("records"):
            return None
        models = self._q(
            f"SELECT COALESCE(NULLIF(model,''),'未知模型') AS model,"
            f" COALESCE(SUM({TOTAL_EXPR}),0) AS total, COALESCE(SUM(requests),0) AS requests,"
            f" COALESCE(SUM(cost),0) AS cost FROM records {w}"
            f" GROUP BY model ORDER BY total DESC", p)
        daily = self._q(
            f"SELECT day, COALESCE(SUM({TOTAL_EXPR}),0) AS total, COALESCE(SUM(requests),0) AS requests"
            f" FROM records {w} GROUP BY day ORDER BY day ASC", p)
        turns = self._q(
            f"SELECT ts, model, input, output, cache_read, cache_write, reasoning, cost, cost_kind,"
            f" {TOTAL_EXPR} AS total, src FROM records {w} ORDER BY ts ASC LIMIT {int(limit)}", p)
        head["models"] = models
        head["daily"] = daily
        head["turns"] = turns
        head["session"] = session_id
        return head

    # ------------------------------------------------------------------
    # 洞察：把统计结果翻译成"人话"
    # ------------------------------------------------------------------
    def insights(self, rng="30d", tool=None):
        return self._memo(("insights", rng, tool), lambda: self.__impl_insights(rng, tool))

    def __impl_insights(self, rng="30d", tool=None):
        out = []
        s = self.summary(rng, tool)
        if not (s.get("total") or 0):
            return out

        # 1) 峰值日
        peak = None
        by_day = {}
        for r in self.daily(rng, tool).values():
            b = by_day.get(r["day"])
            if b is None:
                b = by_day[r["day"]] = {"day": r["day"], "total": 0, "cost": 0.0, "requests": 0}
            b["total"] += r["total"] or 0
            b["cost"] += r["cost"] or 0
            b["requests"] += r["requests"] or 0
        if by_day:
            peak = max(by_day.values(), key=lambda x: x["total"])
        if peak and peak.get("total"):
            out.append({
                "key": "peak", "glyph": "▲",
                "title": "用量峰值日", "title_en": "Peak usage day",
                "value": peak["day"], "tone": "#a855f7",
                "desc": "当天消耗 %s tokens、%s 次调用，占本周期总量的 %.1f%%"
                        % (fmt_i(peak["total"]), fmt_i(peak.get("requests", 0)),
                           peak["total"] * 100.0 / max(1, s["total"])),
                "desc_en": "Consumed %s tokens and %s calls that day, %.1f%% of the period total"
                           % (fmt_i(peak["total"]), fmt_i(peak.get("requests", 0)),
                              peak["total"] * 100.0 / max(1, s["total"])),
            })

        # 2) 缓存节省（按模型分别套价，算"若未命中要多花多少"）
        by_model = {}
        if self._sql_rollup:
            where, params = self._where(rng, tool)
            model_rows = self._iter_sql("SELECT model,SUM(cache_read) cache_read,SUM(input) input "
                "FROM records " + where + " GROUP BY model", params)
        else:
            model_rows = self._rollup_fold(rng, tool)
        saved = 0.0
        for r in model_rows:
            name = r["model"] or ""
            if self._sql_rollup:
                if is_known(name):
                    pi, _po, pcr, _pcw = lookup(name)
                    saved += max(0.0, (r["cache_read"] or 0) * (pi - pcr)) / 1e6
                continue
            b = by_model.get(name)
            if b is None:
                b = by_model[name] = {"model": name, "cr": 0, "inp": 0}
            b["cr"] += r["cache_read"] or 0
            b["inp"] += r["input"] or 0
        for r in by_model.values():
            if not is_known(r.get("model") or ""):
                continue
            pi, _po, pcr, _pcw = lookup(r["model"] or "")
            saved += max(0.0, (r["cr"] or 0) * (pi - pcr)) / 1e6
        if saved > 0:
            out.append({
                "key": "cache", "glyph": "C",
                "title": "缓存读带来的节省", "title_en": "Savings from cache reads",
                "value": "$%.2f" % saved, "tone": "#22d3ee",
                "desc": "缓存命中 %s tokens。若全部按常规输入价计费，本周期需多花约 $%.2f"
                        % (fmt_i(s.get("cache_read") or 0), saved),
                "desc_en": "%s tokens served from cache. Billing them all at the normal input rate "
                           "would have cost about $%.2f more this period"
                           % (fmt_i(s.get("cache_read") or 0), saved),
            })

        # 3) 增长最快 / 下滑最快的工具
        cmp = self.compare(rng, tool)
        if cmp.get("has_prev") and cmp.get("tools"):
            up = [t for t in cmp["tools"] if t["delta"] is not None and t["previous"] > 0]
            meta = {m["id"]: m for m in self._tool_meta()}
            if up:
                # 主排序按涨幅；涨幅相同时用「上一周期用量」再排一次，避免平局时
                # 结果取决于列表顺序（同一份数据刷两次给出不同结论）。
                top = max(up, key=lambda x: (x["delta"], x["previous"]))
                if top["delta"] > 5:
                    out.append({
                        "key": "up", "glyph": "↑",
                        "title": "增长最快的来源", "title_en": "Fastest-growing source",
                        "value": meta.get(top["tool"], {}).get("name", top["tool"]),
                        "tone": "#fb7185",
                        "desc": "较上一周期增长 %.1f%%（%s → %s tokens）"
                                % (top["delta"], fmt_i(top["previous"]), fmt_i(top["current"])),
                        "desc_en": "Up %.1f%% versus the previous period (%s → %s tokens)"
                                   % (top["delta"], fmt_i(top["previous"]), fmt_i(top["current"])),
                    })
                down = [t for t in up if t["delta"] < -5]
                if down:
                    # 跌幅相同时看绝对减少量（previous - current）谁更大
                    low = min(down, key=lambda x: (x["delta"], -(x["previous"] - x["current"])))
                    out.append({
                        "key": "down", "glyph": "↓",
                        "title": "回落最多的来源", "title_en": "Sharpest decline",
                        "value": meta.get(low["tool"], {}).get("name", low["tool"]),
                        "tone": "#34d399",
                        "desc": "较上一周期下降 %.1f%%（%s → %s tokens）"
                                % (abs(low["delta"]), fmt_i(low["previous"]), fmt_i(low["current"])),
                        "desc_en": "Down %.1f%% versus the previous period (%s → %s tokens)"
                                   % (abs(low["delta"]), fmt_i(low["previous"]), fmt_i(low["current"])),
                    })

        # 4) 主力模型
        # dashboard 已请求过 top 40 和 top 30；从同一个记忆化结果截取，避免
        # 为洞察页面再跑两次相同维度的 GROUP BY。
        m = self.by_model(rng, 40, tool)[:3]
        if m:
            top = m[0]
            out.append({
                "key": "model", "glyph": "◈",
                "title": "主力模型", "title_en": "Primary model",
                "value": top["model"], "tone": "#7c8cf8",
                "desc": "占本周期 %.1f%% 的 token（%s tokens / %s 次调用），估算成本 %s"
                        % (top["share"], fmt_i(top["total"]), fmt_i(top.get("requests", 0)),
                           money(top.get("cost", 0))),
                "desc_en": "%.1f%% of the period's tokens (%s tokens / %s calls), estimated cost %s"
                           % (top["share"], fmt_i(top["total"]), fmt_i(top.get("requests", 0)),
                              money(top.get("cost", 0))),
            })

        # 5) 平均上下文规模
        avg = s.get("avg_per_request") or 0
        if avg:
            out.append({
                "key": "ctx", "glyph": "≡",
                "title": "平均单次调用规模", "title_en": "Average call size",
                "value": fmt_i(avg) + " tokens", "tone": "#34d399",
                "desc": "本周期共 %s 次调用、%s 个会话。单次平均上下文越接近模型窗口上限，"
                        "越容易触发截断与成本飙升" % (fmt_i(s.get("requests") or 0),
                                                   fmt_i(s.get("sessions") or 0)),
                "desc_en": "%s calls across %s sessions this period. The closer the average context "
                           "gets to the model's window limit, the more likely truncation and cost "
                           "spikes become" % (fmt_i(s.get("requests") or 0),
                                              fmt_i(s.get("sessions") or 0)),
            })

        # 6) 最贵会话
        sess = self.sessions(rng, 1, tool, order="cost")
        if sess:
            t = sess[0]
            out.append({
                "key": "session", "glyph": "◉",
                "title": "最贵的一次会话", "title_en": "Most expensive session",
                "value": fmt_i(t["total"]) + " tokens", "tone": "#fbbf24",
                "desc": "会话 %s…（%s · %s），%s 次调用、估算成本 %s"
                        % ((t.get("session") or "")[:12], t.get("project") or "—",
                           t.get("tool") or "—", fmt_i(t.get("requests") or 0),
                           money(t.get("cost") or 0)),
                "desc_en": "Session %s… (%s · %s), %s calls, estimated cost %s"
                           % ((t.get("session") or "")[:12], t.get("project") or "—",
                              t.get("tool") or "—", fmt_i(t.get("requests") or 0),
                              money(t.get("cost") or 0)),
                "action": {"type": "session", "tool": t.get("tool"), "session": t.get("session"),
                           "project": t.get("project")},
            })

        # 7) 连续使用天数
        days = [r["day"] for r in self._q(
            f"SELECT DISTINCT day FROM records {self._where(rng, tool)[0]} ORDER BY day", self._where(rng, tool)[1])]
        if days:
            best = cur = 1
            for i in range(1, len(days)):
                try:
                    gap = (dt.date.fromisoformat(days[i]) - dt.date.fromisoformat(days[i - 1])).days
                except Exception:
                    gap = 9
                cur = cur + 1 if gap == 1 else 1
                best = max(best, cur)
            out.append({
                "key": "streak", "glyph": "◆",
                "title": "连续使用", "title_en": "Usage streak",
                "value": "最长 %d 天" % best, "value_en": "%d days max" % best,
                "tone": "#60a5fa",
                "desc": "本周期有 %d 天产生了模型调用，最长连续 %d 天不间断"
                        % (len(days), best),
                "desc_en": "Model calls were made on %d days this period, with a longest "
                           "unbroken run of %d days" % (len(days), best),
            })
        return out

    @staticmethod
    def _tool_meta():
        from . import adapters as A
        return A.adapter_meta()

    # ------------------------------------------------------------------
    def budget(self, rng, cfg):
        """Calendar budgets are independent of the chart and export filters."""
        today = dt.date.today()
        period = cfg.get("budget_period") or "month"
        if period == "week":
            start = today - dt.timedelta(days=today.weekday())
            end = start + dt.timedelta(days=6)
        else:
            start = today.replace(day=1)
            end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        scope = "%s~%s" % (start, today)
        s = Aggregator(self.store, self.cfg).summary(scope) if self._filters else self.summary(scope)
        elapsed = (today - start).days + 1
        duration = (end - start).days + 1
        def metric(name, used, limit):
            return {"metric": name, "used": used, "limit": limit,
                    "ratio": used / limit if limit else 0.0,
                    "over": bool(limit and used > limit),
                    "warn_ratio": float(cfg.get("warn_ratio", 0.8)),
                    "remaining": max(0, limit - used) if limit else 0,
                    "forecast": round(used / elapsed * duration, 6)}
        tokens = metric("tokens", s.get("total") or 0, float(cfg.get("budget_tokens") or 0))
        cost = metric("cost", s.get("cost") or 0, float(cfg.get("budget_cost") or 0))
        active = [m for m in (tokens, cost) if m["limit"]]
        primary = max(active, key=lambda m: m["ratio"]) if active else metric("none", 0, 0)
        return {**primary, "period": period, "start": str(start), "end": str(end),
                "tokens": tokens, "cost": cost}

    # ------------------------------------------------------------------
    def tool_trend(self, rng="30d", top=8):
        return self._memo(("tool_trend", rng, top), lambda: self.__impl_tool_trend(rng, top))

    def __impl_tool_trend(self, rng="30d", top=8):
        """各工具每日曲线（堆叠面积图用）。"""
        if self._uses_sql_rollup():
            where, params = self._where(rng)
            rows = self._q("SELECT tool FROM records " + where + " GROUP BY tool ORDER BY "
                "SUM(" + TOTAL_EXPR + ") DESC,MIN(day),tool LIMIT ?", [*params, max(0, int(top))])
            tops = [r["tool"] for r in rows]
            series = {}
            for tid in tops:
                points = self._q("SELECT day,SUM(" + TOTAL_EXPR + ") total FROM records " + where +
                                 " AND tool IS ? GROUP BY day", [*params, tid])
                series[tid] = {r["day"]: r["total"] for r in points}
            return {"tools": tops, "series": series}
        rows = self._rollup_fold(rng)
        by_tool = {}
        by_day_tool = {}
        for r in rows:
            tid = r["tool"]
            by_tool[tid] = by_tool.get(tid, 0) + (r["total"] or 0)
            by_day_tool[(r["day"], tid)] = by_day_tool.get((r["day"], tid), 0) + (r["total"] or 0)
        tops = [t for t, _v in sorted(by_tool.items(), key=lambda kv: -kv[1])[:int(top)]]
        series = {t: {} for t in tops}
        for (day, tid), total in by_day_tool.items():
            if tid in series:
                series[tid][day] = total
        return {"tools": tops, "series": series}

    def data_span(self ):
        return self._memo(("data_span",), lambda: self.__impl_data_span())

    def __impl_data_span(self ):
        return self._one(
            "SELECT MIN(day) AS first_day, MAX(day) AS last_day, COUNT(*) AS n FROM records")

    def tool_models(self, rng="30d"):
        if self._uses_sql_rollup():
            where, params = self._where(rng)
            rows = self._iter_sql("SELECT tool,COALESCE(model,'') model,SUM(" + TOTAL_EXPR +
                ") total,SUM(cost) cost FROM records " + where +
                " GROUP BY tool,COALESCE(model,'') ORDER BY total DESC", params)
            out = {}
            for row in rows:
                row["model"] = row["model"] or "未知模型"
                out.setdefault(row["tool"], []).append(row)
            return out
        buckets = {}
        for r in self._rollup_fold(rng):
            key = (r["tool"], (r["model"] or ""))
            b = buckets.get(key)
            if b is None:
                b = buckets[key] = {"tool": r["tool"], "model": r["model"] or "未知模型",
                                    "total": 0, "cost": 0.0}
            b["total"] += r["total"] or 0
            b["cost"] += r["cost"] or 0
        rows = sorted(buckets.values(), key=lambda x: -x["total"])
        out = {}
        for r in rows:
            out.setdefault(r["tool"], []).append(r)
        return out
