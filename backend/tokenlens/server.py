# -*- coding: utf-8 -*-
"""TokenLens local HTTP service; optional AI operations use the selected provider."""
from __future__ import annotations

import csv
import datetime as dt
import copy
import io
import inspect
import json
import logging
import os
import re
import math
import socket
import threading
import time
import urllib.parse
import uuid
import tempfile
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import adapters as A
from . import report
from . import ccswitch
from . import connection_models
from .aggregator import Aggregator, range_bounds, parse_custom_range, TOTAL_EXPR
from .config import (APP_NAME, APP_TAGLINE, APP_VERSION, DEFAULTS, Config,
                     data_dir as data_dir_local, db_path, validate_patch)
from .models import human
from .pricing import USD_TO_CNY, ledger
from .promptlab import PromptLab, _sse_event
from .scanner import Scanner
from .store import Store
from .service_tasks import TaskRegistry
from . import diagnostics
from .export_snapshot import export_snapshot
from .finance import Finance, project_cost_basis
from .assistant_service import AssistantService
from .assistant_pricing import PriceAssistant
from .ai_settings import AISettings
from .backup_service import BackupService, MAX_BODY_BYTES
from .credential_safety import sanitize_public, StreamRedactor
from .source_import import SourceImporter, MAX_BODY_BYTES as MAX_IMPORT_BODY

_LOG = logging.getLogger("tokenlens.server")


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def redact_connection_result(value, secret):
    """One public-output policy for every connection source."""
    return sanitize_public(value, secret)


def request_filters(q, default_range="all"):
    """One filter contract for records, summaries and every export format."""
    out = {key: q[key] for key in ("tool", "model", "project", "project_paths", "q", "from", "to", "cost_basis")
           if q.get(key)}
    if q.get("project") == "":
        out["project"] = "(未标注)"
    out["range"] = q.get("range") or default_range
    if any(not isinstance(value, str) or len(value) > 4096 for value in out.values()):
        raise RequestError("invalid query filter")
    if out.get("cost_basis", "total_cost") not in ("total_cost", "historical_cost", "repriced_cost"):
        raise RequestError("invalid cost basis")
    return out


def query_aggregator(store, cfg, filters):
    """Records, totals and exports use the same read-only price projection."""
    agg = Aggregator(store, cfg).filtered(filters)
    project_cost_basis(store, cfg, filters.get("cost_basis", "total_cost"),
                       *agg._where(filters["range"]))
    return agg


def report_aggregator(store, cfg, filters):
    """Project both report periods, with date narrowing applied to current only."""
    rng = filters['range']
    start, end, _, _ = range_bounds(rng)
    if filters.get('from'):
        start = max(start or filters['from'], filters['from'])
    if filters.get('to'):
        end = min(end, filters['to'])
    if not start or start > end:
        return query_aggregator(store, cfg, filters), rng
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise RequestError('invalid report dates')
    span = (last-first).days+1
    prior = dt.date.fromordinal(max(1, first.toordinal()-span))
    scope = {key:value for key,value in filters.items() if key not in ('from','to')}
    agg = Aggregator(store, cfg).filtered(scope)
    project_cost_basis(store, cfg, filters.get('cost_basis','total_cost'),
                       *agg._where('%s~%s' % (prior,end)))
    if filters.get('from') or filters.get('to'):
        rng = '%s~%s' % (start,end)
    return agg, rng


def csv_cell(value):
    # Quoting does not neutralize Excel formulas in imported, untrusted names.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def cost_quality(store, agg, rng):
    """Explain recorded amounts without loading request payloads into Python."""
    where, args = agg._where(rng)
    fields = []
    for kind in ("actual", "estimated", "free", "unknown"):
        fields.append("COALESCE(SUM(CASE WHEN cost_kind='%s' THEN 1 ELSE 0 END),0) %s_records" % (kind, kind))
        if kind in ("actual", "estimated"):
            fields.append("COALESCE(SUM(CASE WHEN cost_kind='%s' THEN cost ELSE 0 END),0) %s_cost" % (kind, kind))
    return store.query_one("SELECT " + ",".join(fields) + " FROM records " + where, args)


# /api/dashboard 响应里回传哪些配置项。
#
# 这份清单**必须**从 DEFAULTS 推导，不能手写：曾经这里是一份硬编码的 11 键
# 列表，漏掉了 language / font / density / accent / radius。后果是「设置里切
# 成英文 → 去别的页面 → 切回设置」时，renderSettings() 从 dashboard 响应回填
# 控件，读到的 language 恒为 undefined，于是下拉框被打回「简体中文」——后端
# 其实已经存对了，只是从来没告诉前端。
# port / host 排除：界面不展示、也不需要回填，且改它们要重启服务才生效。
DASHBOARD_CONFIG_KEYS = tuple(k for k in DEFAULTS if k not in ("port", "host"))


class App:
    def __init__(self, cfg: Config = None):
        self.cfg = cfg or Config()
        self.store = Store(db_path(self.cfg))
        self.scanner = Scanner(self.cfg, self.store)
        self.source_import = SourceImporter(self.cfg, self.store)
        self.agg = Aggregator(self.store, self.cfg)
        self.finance = Finance(self.store, self.cfg)
        self.tasks = TaskRegistry()
        self.prompt = PromptLab(self.cfg, usage_callback=self.record_ai_usage)
        self.ai_settings = AISettings(self)
        # Legacy files are migrated once; versioned database prices are authoritative.
        self.custom_prices_path = os.path.join(data_dir_local(), "custom_prices.json")
        self._migrate_legacy_prices()
        # Existing usage costs are historical snapshots. Loading an override
        # must never rewrite them or overwrite managed price-version history.
        self.boot_ts = int(time.time())
        self.stop_event = threading.Event()
        # 同一份索引在切换时间范围时不会变化。缓存完整 dashboard，避免用户在
        # 7 天 / 30 天等范围之间来回切换时重复跑十余条聚合 SQL。
        self._dashboard_cache = {}
        self._dashboard_cache_lock = threading.RLock()
        self.assistant = AssistantService(self)
        self.assistant_prices = PriceAssistant(self, self.assistant.generate)
        self.backup = BackupService(self)
        self.desktop = None
        # Automatic rescans belong to the service lifecycle, not to a browser
        # tab.  A single lightweight daemon wakes briefly to inspect the
        # setting, so closing the dashboard does not stop collection and no
        # per-page timer can trigger duplicate scans.
        self._auto_scan_thread = threading.Thread(
            target=self._auto_scan_loop, daemon=True, name="tokenlens-auto-scan")
        self._auto_scan_thread.start()

    def _auto_scan_loop(self):
        interval = None
        due = None
        while not self.stop_event.wait(5.0):
            try:
                raw = self.cfg.get("auto_scan_minutes", 0)
                minutes = int(raw or 0)
            except (TypeError, ValueError):
                minutes = 0
            if minutes <= 0:
                interval = None
                due = None
                continue
            if minutes != interval or due is None:
                interval = minutes
                due = time.monotonic() + max(60, minutes * 60)
                continue
            if time.monotonic() < due:
                continue
            # Advance the deadline before starting work.  If a scan is already
            # active, the next interval remains bounded and never queues a
            # second worker behind it.
            due = time.monotonic() + max(60, minutes * 60)
            try:
                self.scanner.start(force=False)
            except Exception:
                _LOG.exception("automatic scan failed to start")

    def record_ai_usage(self, event):
        from .models import UsageRecord
        from . import pricing
        usage = event.get("usage") or {}
        tokens = {key: max(0, int(usage.get(key) or 0)) for key in
                  ("input", "output", "cache_read", "cache_write", "reasoning")}
        known = bool(event.get("usage_known"))
        model = event.get("model") or "unknown"
        cost = 0.0
        kind = "unknown"
        if known and pricing.is_known(model):
            price = pricing.lookup(model)
            cost = (tokens["input"] * price[0] + (tokens["output"] + tokens["reasoning"]) * price[1]
                    + tokens["cache_read"] * price[2] + tokens["cache_write"] * price[3]) / 1_000_000
            kind = "estimated"
        rec = UsageRecord(tool="tokenlens-ai", ts=int(event.get("ts") or time.time()),
                          model=model, project="TokenLens AI", session=event.get("session", ""),
                          src="internal://tokenlens-ai", cost=cost, cost_kind=kind,
                          price_version=pricing.custom_price_version(model) if kind == "estimated" else "",
                          cost_source="price-table" if kind == "estimated" else "usage-unavailable" if not known else "unpriced",
                          event_id=event["event_id"], requests=1, **tokens,
                          extra={**{key: event.get(key) for key in
                                   ("operation", "provider", "request_type", "request_role", "outcome",
                                    "elapsed_ms", "usage_known")},
                                 "http_status": event.get("status"), "status": event.get("outcome"),
                                 "purpose": event.get("request_role", "primary"),
                                 "duration_ms": event.get("elapsed_ms", 0)})
        self.store.append_usage(rec, event_id=event["event_id"])
        if hasattr(self, "_dashboard_cache_lock"):
            self.clear_dashboard_cache()

    # ------------------------------------------------------------------
    # 手动单价：改动后必须清 dashboard 缓存——账单金额在缓存里
    # ------------------------------------------------------------------
    def _migrate_legacy_prices(self):
        from . import pricing as P
        with self.store.locked() as conn, P._lock():
            P.CUSTOM_PRICES.clear()
            marker = "legacy_custom_prices_migrated"
            if conn.execute("SELECT value FROM schema_meta WHERE key=?", (marker,)).fetchone():
                return
            values = {}
            if os.path.exists(self.custom_prices_path):
                with open(self.custom_prices_path, "r", encoding="utf-8") as source:
                    loaded = json.load(source)
                if not isinstance(loaded, dict):
                    raise ValueError("Legacy custom prices must be a JSON object")
                for model, prices in loaded.items():
                    key = P._norm(model)
                    if not key or len(key) > 200:
                        raise ValueError("Invalid legacy model name")
                    values[key] = P.validate_prices(prices)
            with self.store._transaction():
                for model, prices in values.items():
                    if conn.execute("SELECT 1 FROM price_versions WHERE model=?", (model,)).fetchone():
                        continue
                    version = uuid.uuid4().hex
                    effective = time.strftime("%Y-%m-%d")
                    conn.execute("INSERT INTO price_versions(id,model,effective_from,prices,source,created_at) VALUES(?,?,?,?,?,?)",
                                 (version, model, effective, json.dumps(prices), "legacy migration", int(time.time())))
                    conn.execute("INSERT INTO finance_audit(action,before_json,after_json,created_at) VALUES(?,?,?,?)",
                                 ("price_migrate", "{}", json.dumps({"model": model, "prices": prices,
                                  "id": version, "effective_from": effective}), int(time.time())))
                conn.execute("INSERT INTO schema_meta(key,value) VALUES(?,?)", (marker, "1"))

    def effective_prices(self, finance=None):
        today = time.strftime("%Y-%m-%d")
        result = {}
        for row in (finance or self.finance).prices():
            if row["effective_from"] <= today and row["model"] not in result:
                result[row["model"]] = row
        return result

    def _change_custom_price(self, model, prices=None, remove=False):
        """Both price interfaces publish immutable database versions."""
        from . import pricing as P
        key = P._norm(model)
        if not key:
            raise ValueError("model name is required")
        effective = P.builtin_price(key) if remove else P.validate_prices(prices)
        source = ("legacy-override:builtin" if effective is not None else "legacy-override:unpriced") if remove else "legacy override"
        effective = effective if effective is not None else (0.0, 0.0, 0.0, 0.0)
        self.finance.save_price({"model": key, "effective_from": time.strftime("%Y-%m-%d"),
                                 "prices": list(effective), "source": source})
        self.clear_dashboard_cache()
        return True if remove else tuple(effective)

    def set_custom_price(self, model, prices):
        return self._change_custom_price(model, prices)

    def remove_custom_price(self, model):
        return self._change_custom_price(model, remove=True)

    def list_custom_prices(self):
        return [{"model": key, "price": dict(zip(("input", "output", "cache_read", "cache_write"), row["prices"]))}
                for key, row in sorted(self.effective_prices().items())
                if row["source"] not in ("legacy-override:builtin", "legacy-override:unpriced")]

    def clear_dashboard_cache(self):
        with self._dashboard_cache_lock:
            self._dashboard_cache.clear()

    def _dashboard_cache_key(self, rng: str, tool):
        # 配置不会推进 Store.version；把会影响响应内容的配置也纳入 key。
        return (
            rng, tool, self.store.version, time.strftime("%Y-%m-%d"),
            self.cfg.get("theme"), self.cfg.get("usd_to_cny"),
            self.cfg.get("budget_tokens"), self.cfg.get("budget_cost"),
            self.cfg.get("warn_ratio"),
        )

    # ------------------------------------------------------------------
    def usage_ledger(self, store, agg, finance, rng, tool, cny, price_map, summary_cost=None, limit=200, offset=0):
        """Exact ledger totals, bounded Python rows and a pageable disk sort."""
        agg._register_model_key()
        where, args = agg._where(rng,tool)
        sql = ('SELECT tokenlens_model_key(model) model,SUM(input) input,SUM(output) output,'
               'SUM(cache_read) cache_read,SUM(cache_write) cache_write,SUM(reasoning) reasoning,'
               'SUM('+TOTAL_EXPR+') total,SUM(cost) cost,SUM(requests) requests FROM records '+where+' GROUP BY 1')
        segments=finance.price_breakdown(rng,price_map,tool)
        result=ledger([],cny,summary_cost=summary_cost,price_map=price_map)
        totals=result['totals']
        additive=('input','output','cache_read','cache_write','cost','tokens','requests','unpriced','unpriced_tokens')
        count=0
        with tempfile.TemporaryDirectory(prefix='tokenlens-ledger-') as directory:
            staged=sqlite3.connect(os.path.join(directory,'rows.sqlite'))
            try:
                staged.execute('PRAGMA cache_size=-2048')
                staged.execute('PRAGMA temp_store=FILE')
                staged.execute('CREATE TABLE rows(model TEXT,cost REAL,tokens INTEGER,body TEXT)')
                batch=[]
                def publish():
                    nonlocal count
                    part=ledger(batch,cny,price_map=price_map,usage_breakdown=segments)
                    for key in additive:totals[key]+=part['totals'][key]
                    staged.executemany('INSERT INTO rows VALUES(?,?,?,?)',((row['model'],row['subtotal'],row['tokens'],json.dumps(row,ensure_ascii=False,separators=(',',':'))) for row in part['items']))
                    count+=len(part['items']);batch.clear()
                for row in store.iter_query(sql,args):
                    totals['stored'] += float(row.get('cost') or 0)
                    batch.append(row)
                    if len(batch)>=256:publish()
                if batch:publish()
                result['items']=[json.loads(row[0]) for row in staged.execute('SELECT body FROM rows ORDER BY cost DESC,tokens DESC,model ASC LIMIT ? OFFSET ?',(limit,offset))]
            finally:
                staged.close()
        for key in ('input','output','cache_read','cache_write','cost','stored'):totals[key]=round(totals[key],6)
        totals['cost_cny']=round(totals['cost']*cny,4);totals['stored_cny']=round(totals['stored']*cny,4)
        if summary_cost is not None:
            result['reconciliation'].update(itemized=totals['cost'],delta=round(float(summary_cost or 0)-totals['cost'],6),stored_cost=totals['stored'])
        result.update(total=count,limit=limit,offset=offset,paged=count>limit or offset>0)
        return result

    def dashboard(self, rng: str, tool=None):
        key = self._dashboard_cache_key(rng, tool)
        with self._dashboard_cache_lock:
            cached = self._dashboard_cache.pop(key, None)
            if cached is not None:
                self._dashboard_cache[key] = cached
        if cached is not None:
            return copy.deepcopy(cached)

        # A writer can spend seconds publishing a large file atomically. WAL
        # readers keep seeing the last committed data instead of waiting on the
        # writer's Python lock. One response uses one consistent snapshot.
        with export_snapshot(self.store) as snapshot:
            data = self._build_dashboard(rng, tool, snapshot)
        with self._dashboard_cache_lock:
            # 保持一个很小的 LRU，覆盖界面上的全部时间窗口，同时避免长期占用内存。
            if len(self._dashboard_cache) >= 12:
                self._dashboard_cache.pop(next(iter(self._dashboard_cache)))
            self._dashboard_cache[key] = copy.deepcopy(data)
        return data

    def _build_dashboard(self, rng: str, tool=None, snapshot=None):
        store = snapshot or self.store
        base = Aggregator(store, self.cfg) if snapshot else self.agg
        agg = base.filtered({"tool": tool}) if tool else base
        cny = float(self.cfg.get("usd_to_cny") or USD_TO_CNY)
        summary = agg.summary(rng)
        summary.update(cost_quality(store, agg, rng))
        summary["cost_cny"] = round((summary.get("cost") or 0) * cny, 4)
        tools = agg.by_tool(rng)
        cat_map = {m["id"]: m for m in A.adapter_meta()}
        cat_map["tokenlens-ai"] = {"name":"TokenLens AI", "name_en":"TokenLens AI", "color":"#167c64", "glyph":"K", "category":"api"}
        for t in tools:
            meta = cat_map.get(t["tool"], {})
            t.update({"name": meta.get("name", t["tool"]), "color": meta.get("color", "#7c8cf8"),
                      "glyph": meta.get("glyph", "?"), "vendor": meta.get("vendor", ""),
                      "name_en": meta.get("name_en", t["tool"]),
                      "vendor_en": meta.get("vendor_en", ""),
                      "category": meta.get("category", ""),
                      "category_label": A.CATEGORY_LABEL.get(meta.get("category", ""), ""),
                      "category_label_en": A.CATEGORY_LABEL_EN.get(meta.get("category", ""), "")})
        models = agg.by_model(rng)
        for m in models:
            m["cost_cny"] = round((m.get("cost") or 0) * cny, 4)
        # 排行可只预览前 40 个模型；计费明细与合计覆盖全部模型，供前端分页。
        finance = Finance(store, self.cfg, initialize=False) if snapshot else self.finance
        price_map = self.effective_prices(finance)
        bill = App.usage_ledger(self, store, agg, finance, rng, tool, cny, price_map, summary.get("cost"))
        projects = agg.by_project(rng)
        for pj in projects:
            pj["cost_cny"] = round((pj.get("cost") or 0) * cny, 4)
        sources = store.list_sources()
        if tool:
            sources = [source for source in sources if source["id"] == tool]
        side = self.scanner.side_stats()
        # 数据源卡片上的说明文字来自适配器的 detail（部分为中文），
        # 这里补上英文版与英文名，英文界面才有对应的说明可显示。
        for s in sources:
            s["side"] = side.get(s["id"], {})
            s["detail_en"] = A.ADAPTER_DETAIL_EN.get(s["id"]) or s.get("note") or ""
            s["name_en"] = A.ADAPTER_NAME_EN.get(s["id"]) or s.get("name") or ""
        cats = agg.by_category(rng)
        for c in cats:
            c["cost_cny"] = round((c.get("cost") or 0) * cny, 4)
        cmp_ = agg.compare(rng)
        budget = agg.budget(rng, self.cfg)
        rb = range_bounds(rng)
        return {
            "range": rng,
            "tool": tool,
            "range_label": rb[2],
            "range_label_en": rb[3],
            # 供前端区分"单日区间"（要画分时段）和普通区间，并回填日期选择器
            "range_start": rb[0] or "",
            "range_end": rb[1] or "",
            "range_is_custom": bool(parse_custom_range(rng)),
            "range_is_single_day": bool(rb[0] and rb[0] == rb[1]),
            "intraday": agg.intraday(rng),
            "summary": summary,
            "tools": tools,
            "models": models,
            "projects": projects,
            "categories": cats,
            "daily": agg.daily_series(rng),
            "heatmap": agg.heatmap(),
            "sessions": agg.sessions(rng),
            "hourly": agg.hourly(rng),
            "weekday": agg.weekday("90d"),
            "tool_trend": agg.tool_trend(rng),
            "compare": cmp_,
            "budget": budget,
            "ledger": bill,
            "insights": agg.insights(rng),
            "sources": sources,
            "tool_meta": cat_map,
            "span": (store.query_one("SELECT MIN(day) first_day,MAX(day) last_day,COUNT(*) n "
                                           "FROM records WHERE tool=?", (tool,)) if tool else agg.data_span()),
            "data_version": store.version,
            # 白名单来自 DASHBOARD_CONFIG_KEYS（由 DEFAULTS 推导）：手写列表
            # 曾漏掉 language 等 5 个设置，导致设置页从别的页面切回来时回退默认值。
            "config": {k: self.cfg.get(k) for k in DASHBOARD_CONFIG_KEYS},
            "last_scan": store.last_scan_time(),
            "generated": int(time.time()),
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TokenLens/1.0"
        protocol_version = "HTTP/1.1"
        MAX_BODY = 512 * 1024

        HEADER_TIMEOUT = 10.0
        BODY_TIMEOUT = 20.0

        def setup(self):
            super().setup()
            self.connection.settimeout(self.HEADER_TIMEOUT)
            self._response_secrets = []
            self._upload_slot = False

        def request_deadline(self, seconds=None):
            # One server watchdog bounds slow-drip input without a timer thread
            # for every request, and ordinary API work has no input deadline.
            self.server.set_request_deadline(self.connection, seconds)

        def handle_one_request(self):
            self._response_secrets = []
            self._upload_slot = False
            self.request_deadline(self.HEADER_TIMEOUT)
            try:
                return super().handle_one_request()
            except OSError:
                # Deadline expiry or a vanished peer must release its slot
                # quietly, including Windows read-side shutdown resets.
                self.close_connection = True
            finally:
                self.request_deadline()
                if self._upload_slot:
                    self.server.upload_slots.release()
                    self._upload_slot = False

        def finish(self):
            self.request_deadline()
            try:
                super().finish()
            except OSError:
                pass

        def response_secret(self, value):
            if isinstance(value, str) and value and value not in self._response_secrets:
                self._response_secrets.append(value)
            return value

        def safe_stream(self, emit, secret, worker):
            redactor = StreamRedactor(secret)
            def safe_emit(event):
                for safe in redactor.push(event):
                    emit(safe)
            try:
                return sanitize_public(worker(safe_emit), secret)
            finally:
                for safe in redactor.finish():
                    emit(safe)

        def check_origin(self):
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise RequestError("one local Host header is required", 403)
            try:
                parsed = urllib.parse.urlsplit("http://" + hosts[0])
                host, port = parsed.hostname, parsed.port or 80
            except ValueError:
                raise RequestError("invalid Host", 403)
            if (host not in ("localhost", "127.0.0.1", "::1") or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment
                    or port != self.server.server_address[1]):
                raise RequestError("only the local application origin is allowed", 403)
            origin = self.headers.get("Origin")
            if origin:
                try:
                    src = urllib.parse.urlsplit(origin)
                    valid = (src.scheme == "http" and src.hostname == host
                             and (src.port or 80) == port and not src.username
                             and not src.password and not src.path and not src.query and not src.fragment)
                except ValueError:
                    valid = False
                if not valid:
                    raise RequestError("cross-origin request rejected", 403)
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise RequestError("cross-site request rejected", 403)

        # ---------------------------------------------------------- 工具
        def log_message(self, fmt, *args):
            pass  # 静默，避免刷屏

        def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8",
                  extra: dict = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                             "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                             "frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        def json_out(self, obj, code=200):
            obj = sanitize_public(obj, getattr(self, "_response_secrets", ()))
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def read_json(self):
            if self.headers.get("Transfer-Encoding"):
                raise RequestError("chunked request bodies are not supported")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"\d+", lengths[0]):
                raise RequestError("a valid Content-Length is required")
            n = int(lengths[0])
            endpoint = self.path.split("?", 1)[0]
            limit = (MAX_BODY_BYTES if endpoint == "/api/backup/preview" else
                     MAX_IMPORT_BODY if endpoint == "/api/sources/import/preview" else
                     2_200_000 if endpoint == "/api/backup/export" else
                     2_200_000 if endpoint in ("/api/manage", "/api/finance", "/api/finance/action") else self.MAX_BODY)
            if n > limit:
                self._rejected_body_bytes = n
                raise RequestError("request body exceeds the endpoint size limit", 413)
            if n > self.MAX_BODY:
                if not self.server.upload_slots.acquire(blocking=False):
                    raise RequestError("another large upload is in progress; retry shortly", 503)
                self._upload_slot = True
            self.request_deadline(self.BODY_TIMEOUT)
            self.connection.settimeout(self.BODY_TIMEOUT)
            if n == 0:
                return {}
            if self.headers.get_content_type() != "application/json":
                raise RequestError("Content-Type must be application/json", 415)
            try:
                raw = self.rfile.read(n)
                if len(raw) != n:
                    raise ValueError("incomplete body")
                def invalid_constant(value):
                    raise ValueError("non-finite JSON number")
                obj = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
            except (ValueError, UnicodeError, OSError):
                raise RequestError("invalid JSON request body")
            if not isinstance(obj, dict):
                raise RequestError("request body must be a JSON object")
            self.response_secret(obj.get("api_key"))
            for nested in (obj.get("custom"), obj.get("edit")):
                if isinstance(nested, dict):
                    self.response_secret(nested.get("api_key"))
            return obj

        def validate_body(self, body):
            for key in ("model", "base_url", "api_key", "protocol", "profile_id", "profile_name", "ccswitch_id", "ccswitch_protocol",
                        "prompt", "task", "extra", "mode", "thinking", "task_id", "source_id", "source",
                        "connection_id", "action", "range", "scope", "source_url", "preview_id", "effective_from"):
                if key in body and (not isinstance(body[key], str) or len(body[key]) > 20000):
                    raise RequestError("%s must be a string of at most 20000 characters" % key)
            if body.get("ccswitch_protocol", "") not in connection_models.PROTOCOLS | {""}:
                raise RequestError("unsupported CC Switch protocol")
            for key in ("force", "cancel", "new_profile", "quick"):
                if key in body and not isinstance(body[key], bool):
                    raise RequestError("%s must be a boolean" % key)
            for key in ("temperature", "max_tokens"):
                value = body.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value)):
                    raise RequestError("%s must be a finite number" % key)
            return body

        def qs(self):
            parsed = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            return parsed.path, {k: v[0] for k, v in q.items()}

        # ---------------------------------------------------------- 路由
        def do_GET(self):
            try:
                self.check_origin()
                path, q = self.qs()
                self.request_deadline()
                if path.startswith("/api/"):
                    return self.api_get(path, q)
                raise RequestError("unknown endpoint", 404)
            except (RequestError, ValueError) as e:
                return self.json_out({"ok": False, "error": str(e)}, getattr(e, "status", 400))
            except Exception as e:
                _LOG.warning("HTTP request failed (%s)", type(e).__name__)
                return self.json_out({"error": "local service request failed", "code": "internal_error"}, 500)

        def do_POST(self):
            # 简单的 CSRF 防护：本机服务也要挡住其它网页发来的表单 POST。
            # 自定义头会触发 CORS 预检，跨站页面无法伪造。
            try:
                self.check_origin()
                if self.headers.get("X-TokenLens") != "1":
                    raise RequestError("missing X-TokenLens header", 403)
                path, q = self.qs()
                body = self.validate_body(self.read_json())
                self.request_deadline()
                return self.api_post(path, q, body)
            except (RequestError, ValueError, TypeError) as e:
                self.close_connection = True
                self.json_out({"ok": False, "error": str(e)}, getattr(e, "status", 400))
                # Closing a Windows socket with unread upload bytes can reset
                # the connection before the client receives the 413 response.
                # Send FIN first, then drain a bounded amount/time. Oversized
                # uploads never reach JSON parsing or application mutations.
                pending = getattr(self, "_rejected_body_bytes", 0)
                if pending:
                    try:
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_WR)
                        self.connection.settimeout(0.1)
                        deadline = time.monotonic() + 0.25
                        remaining = min(pending, 1024 * 1024)
                        while remaining and time.monotonic() < deadline:
                            chunk = self.rfile.read1(min(65536, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                    except OSError:
                        pass
                return
            except Exception as e:
                _LOG.warning("HTTP request failed (%s)", type(e).__name__)
                return self.json_out({"error": "local service request failed", "code": "internal_error"}, 500)

        # ---------------------------------------------------------- 静态
        def sse_start(self):
            """打开一条 SSE 通道：不缓冲，边生成边推。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._sse_lock = threading.RLock()

        def sse_send(self, obj):
            """写一帧 SSE。返回 False 表示客户端已经断开。"""
            try:
                body = _sse_event(obj)
                with self._sse_lock:
                    self.wfile.write(("%x\r\n" % len(body)).encode("ascii"))
                    self.wfile.write(body)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return False

        def sse_end(self):
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass

        def stream_scan(self, interval=0.2, idle_wait=5.0, max_seconds=1800):
            """把扫描进度当事件推给浏览器，直到扫描结束。

            为什么值得单独做一条流：原先前端用 pollScan 递归 setTimeout
            （正常 620ms、出错 1500ms）拉 /api/scan/status。三万八千个文件要扫
            15.7 秒，等于把一条连续曲线采样成二十几个点，进度条看着是「跳」的；
            而且每个采样点都是一次完整的 HTTP 往返。改成 SSE 之后间隔降到
            200ms，且只有一条长连接。

            只推状态、不负责启动：启动仍然走 POST /api/scan。一个 GET 就能触发
            全盘扫描会让「预取/爬虫点到这个 URL」变成一次真实扫描。

            终态帧带 fresh 标记：phase 在扫描结束后会一直停在 done，
            如果浏览器在「上一轮已扫完」时接上这条流，不加区分就会让前端
            再弹一次「扫描完成」并白刷一遍仪表盘。
            """
            self.sse_start()
            t0 = time.time()
            saw_running = False
            try:
                while True:
                    snap = app.scanner.state.snapshot()
                    if snap["running"]:
                        saw_running = True
                    if not self.sse_send({"type": "progress", **snap}):
                        return                      # 浏览器已经关掉这条流
                    if not snap["running"] and snap["phase"] != "idle":
                        # done / error / cancelled 都是终态：把最后一帧发全就收工
                        fresh = saw_running or (snap.get("started") or 0) >= t0 - 1.0
                        self.sse_send({"type": "done", "fresh": fresh, **snap})
                        return
                    if not snap["running"] and snap["phase"] == "idle":
                        # 前端刚 POST 完 /api/scan 就打开这条流，扫描线程可能还
                        # 没把 running 置位。等一小会儿，别抢在它前面报「就绪」。
                        if time.time() - t0 > idle_wait:
                            self.sse_send({"type": "done", "fresh": False, **snap})
                            return
                    if time.time() - t0 > max_seconds:
                        # 兜底：真出现卡死的扫描，也不能让这条连接永远挂着
                        self.sse_send({"type": "done", "fresh": False, **snap})
                        return
                    time.sleep(interval)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
            finally:
                self.sse_end()

        def sse_run(self, worker, on_done=None, task_id="", kind="ai"):
            """在后台线程跑 worker(emit, cancel)，主线程把事件转发成 SSE。

            这样即使上游 200 秒不吐字，本机服务也一直活着，前端可以
            用自己的计时器显示「已等待 xx 秒」。
            """
            try:
                task_id, cancelled = app.tasks.create(kind, task_id)
            except (RuntimeError, ValueError) as exc:
                self.sse_send({"type": "result", "ok": False, "error": str(exc)})
                self.sse_end()
                return None
            box = {"done": False, "result": None}

            def emit(obj):
                if not cancelled.is_set() and not self.sse_send(obj):
                    cancelled.set()

            def run():
                try:
                    box["result"] = worker(emit, cancelled.is_set)
                except Exception as e:  # 兜底：绝不让前端永远转圈
                    emit({"type": "result", "ok": False,
                          "error": "AI task failed (%s)" % type(e).__name__})
                finally:
                    app.tasks.finish(task_id, not bool((box["result"] or {}).get("ok")))
                    box["done"] = True

            th = threading.Thread(target=run, daemon=True, name="tokenlens-sse")
            emit({"type": "task", "task_id": task_id})
            th.start()
            try:
                heartbeat = time.monotonic()
                while not box["done"]:
                    th.join(timeout=0.25)
                    if cancelled.is_set():
                        break
                    if time.monotonic() - heartbeat >= 1:
                        emit({"type": "heartbeat", "task_id": task_id})
                        heartbeat = time.monotonic()
                if on_done is not None:
                    try:
                        on_done(box["result"])
                    except Exception:
                        pass
                self.sse_send({"type": "done", "task_id": task_id,
                               "cancelled": cancelled.is_set()})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                # 浏览器已经关掉这条流：通知 worker 尽快收工，别再白烧上游额度。
                cancelled.set()
            finally:
                self.sse_end()
            return box["result"]

        # ---------------------------------------------------------- API GET
        def api_get(self, path, q):
            rng = q.get("range", app.cfg.get("range") or "30d")
            if path == "/api/ai-settings":
                return self.json_out(app.ai_settings.public())
            if path == "/api/ai-thinking":
                from .promptlab import reasoning_capabilities, detect_protocol
                model=q.get("model", "")[:256]
                base=q.get("base_url", "")[:2048]
                protocol=q.get("protocol", "auto")
                if protocol == "auto":protocol=detect_protocol(base,model)
                return self.json_out(reasoning_capabilities(protocol,model,base))
            if path == "/api/assistant/state":
                return self.json_out(app.assistant.state())
            if path == "/api/desktop/status":
                desktop = getattr(app, "desktop", None)
                return self.json_out(desktop.status() if desktop else
                                     {"available": False, "visible": False})
            if path == "/api/assistant/snapshot":
                rng=app.assistant._range({"range":q.get("range", "7d")})
                filters = request_filters(q, rng)
                filters.update(app.assistant.request_context({"context": {
                    key: q[key] for key in ("tool", "model", "project", "project_paths", "q", "from", "to", "cost_basis") if key in q}}))
                return self.json_out({"ok":True,"snapshot":app.assistant.snapshot(rng, filters)})
            if path == "/api/assistant/report":
                return self.json_out(app.assistant.report(q.get("id", "")))
            if path == "/api/ccswitch/connections":
                return self.json_out(ccswitch.catalog())
            if path == "/api/backup/export":
                return self.backup_download()
            if path == "/api/bootstrap":
                with export_snapshot(app.store) as snapshot:
                    span = Aggregator(snapshot,app.cfg).data_span()
                    last_scan = snapshot.last_scan_time()
                return self.json_out({
                    "app": APP_NAME, "tagline": APP_TAGLINE, "version": APP_VERSION,
                    "tool_meta": A.adapter_meta(),
                    "categories": A.CATEGORY_LABEL,
                    "ranges": [{"key": k, "label": range_bounds(k)[2]}
                               for k in ("today", "1d", "7d", "30d", "90d", "365d", "ytd", "all")],
                    "config": app.cfg.data,
                    "last_scan": last_scan,
                    "scanning": app.scanner.state.running,
                    "span": span,
                    "pricing_note": "成本为按公开价目估算，仅供参考；订阅制/免费额度/中转价均不会体现在此处。",
                })
            if path == "/api/dashboard":
                return self.json_out(app.dashboard(rng, q.get("tool")))
            if path == "/api/summary":
                filters = request_filters(q, rng)
                with export_snapshot(app.store) as snapshot:
                    agg = query_aggregator(snapshot, app.cfg, filters)
                    summary = agg.summary(filters["range"])
                    summary.update(cost_quality(snapshot, agg, filters["range"]))
                    summary["cost_cny"] = round((summary.get("cost") or 0) *
                                                float(app.cfg.get("usd_to_cny") or USD_TO_CNY), 4)
                    bounds = range_bounds(filters["range"])
                    return self.json_out({"ok": True, "range": filters["range"], "filters": filters,
                                          "range_label": bounds[2], "range_label_en": bounds[3],
                                          "label_en": bounds[3], "summary": summary,
                                          "tools": agg.by_tool(filters["range"])})
            if path == "/api/sources":
                with export_snapshot(app.store) as snapshot:
                    src = snapshot.list_sources()
                side = app.scanner.side_stats()
                for s in src:
                    s["side"] = side.get(s["id"], {})
                return self.json_out({"sources": src, "meta": A.adapter_meta(), "import": app.source_import.catalog()})
            if path == "/api/ledger":
                try:
                    limit=max(1,min(500,int(q.get('limit',20))));offset=int(q.get('offset',0))
                    if offset<0:raise ValueError()
                except (TypeError,ValueError):
                    raise RequestError('invalid ledger page')
                with export_snapshot(app.store) as snapshot:
                    agg=Aggregator(snapshot,app.cfg)
                    where,args=agg._where(rng,q.get('tool'))
                    total_cost=snapshot.query_one('SELECT SUM(cost) cost FROM records '+where,args).get('cost') or 0
                    finance=Finance(snapshot,app.cfg,initialize=False)
                    return self.json_out(app.usage_ledger(snapshot,agg,finance,rng,q.get('tool'),float(app.cfg.get('usd_to_cny') or USD_TO_CNY),app.effective_prices(finance),total_cost,limit,offset))
            if path == "/api/rankings":
                try:
                    limit = max(1, min(500, int(q.get("limit", 20))))
                except (TypeError, ValueError):
                    limit = 20
                try:
                    offset = int(q.get("offset", 0))
                except (TypeError, ValueError):
                    raise RequestError("offset must be a non-negative integer")
                if offset < 0:
                    raise RequestError("offset must be non-negative")
                filters = request_filters(q, rng)
                with export_snapshot(app.store) as snapshot:
                    result = query_aggregator(snapshot, app.cfg, filters).rankings(
                        q.get("kind", ""), filters["range"], limit=limit, offset=offset,
                        order=q.get("order", "total"))
                result["filters"] = filters
                return self.json_out(result)
            if path == "/api/records":
                # 大库保护：limit 由服务端夹住，避免前端（或手改 URL）一次拉几十万条
                try:
                    want = int(q.get("limit", 200))
                except (TypeError, ValueError):
                    want = 200
                try:
                    offset = int(q.get("offset", 0))
                except (TypeError, ValueError):
                    raise RequestError("offset must be a non-negative integer")
                if offset < 0:
                    raise RequestError("offset must be non-negative")
                filters = request_filters(q, rng)
                with export_snapshot(app.store) as snapshot:
                    agg = Aggregator(snapshot, app.cfg)
                    caps = agg.caps()
                    limit = max(1, min(want, caps["records"]))
                    res = query_aggregator(snapshot, app.cfg, filters).records(rng, limit=limit,
                        offset=offset, order=q.get("order", "ts"), clamp_offset=True)
                res["filters"] = filters
                res["big_lib"] = caps["big"]
                res["max_limit"] = caps["records"]
                # 让前端知道实际生效的筛选（非法日期会被后端忽略）
                res["from"] = q.get("from") or ""
                res["to"] = q.get("to") or ""
                return self.json_out(res)
            if path == "/api/session":
                with export_snapshot(app.store) as snapshot:
                    caps = Aggregator(snapshot, app.cfg).caps()
                try:
                    want = int(q.get("limit", 300))
                except (TypeError, ValueError):
                    want = 300
                filters = request_filters(q, "all")
                with export_snapshot(app.store) as snapshot:
                    detail = query_aggregator(snapshot, app.cfg, filters).session_detail(
                        q.get("session", ""), tool=q.get("tool") or None,
                        project=q.get("project"),
                        limit=max(1, min(want, caps["session"])), rng=filters["range"])
                if detail is None:
                    return self.json_out({"error": "session not found"}, 404)
                cny2 = float(app.cfg.get("usd_to_cny") or USD_TO_CNY)
                detail["cost_cny"] = round((detail.get("cost") or 0) * cny2, 4)
                for m in detail.get("models", []):
                    m["cost_cny"] = round((m.get("cost") or 0) * cny2, 4)
                return self.json_out(detail)
            if path == "/api/prompt":
                result=app.prompt.public()
                selection=app.ai_settings.public()['assignments']['prompt']
                result.update(selected_connection=selection, configured=selection['configured'],
                              model=selection.get('model',''), protocol_effective=selection.get('protocol',''))
                return self.json_out(result)
            if path == "/api/tasks":
                return self.json_out({"tasks": app.tasks.public()})
            if path in ("/api/finance", "/api/manage"):
                # 自身 AI 消费表按页读取，避免表格被静默截断后与汇总卡对不上。
                try:
                    internal_offset = max(0, int(q.get("internal_offset", 0)))
                except (TypeError, ValueError):
                    internal_offset = 0
                try:
                    internal_limit = max(1, min(500, int(q.get("internal_limit", 50))))
                except (TypeError, ValueError):
                    internal_limit = 50
                with export_snapshot(app.store) as snapshot:
                    return self.json_out(Finance(snapshot, app.cfg, initialize=False).dashboard(
                        rng, internal_offset=internal_offset, internal_limit=internal_limit))
            if path == "/api/diagnostics":
                return self._send(200, json.dumps(diagnostics.snapshot(app), ensure_ascii=False,
                                                  indent=2).encode("utf-8"),
                                  "application/json; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="tokenlens-diagnostics.json"'})
            if path == "/api/scan/status":
                return self.json_out(app.scanner.state.snapshot())
            if path == "/api/scan/stream":
                # 扫描进度改推模式。原先前端靠 pollScan 递归 setTimeout
                # （正常 620ms、出错 1500ms）拉 /api/scan/status，三万八千个文件
                # 要扫 15.7 秒，等于把连续曲线采样成二十几个点，进度条看着是
                # 「跳」的。这里复用已有的 SSE 通道，间隔降到 200ms。
                return self.stream_scan()
            if path == "/api/export":
                return self.export_stream(q)
            if path == "/api/export-stream":
                return self.export_stream(q)
            if path == "/api/ping":
                return self.json_out({"ok": True, "app": APP_NAME, "version": APP_VERSION,
                                      "ts": int(time.time())})
            if path == "/api/prices/list":
                # GET：前端在打开单价编辑器前拉取当前的自定义价列表
                return self.json_out({"ok": True, "prices": app.list_custom_prices()})
            return self.json_out({"error": "unknown endpoint"}, 404)

        def export(self, q):
            return self.export_stream(q)

        def export_stream(self, q):
            with export_snapshot(app.store) as snapshot:
                return self._export_snapshot(q, snapshot)

        def _export_snapshot(self, q, snapshot):
            """Export using one snapshot and filter set for summaries and batches."""
            filters = request_filters(q)
            rng = filters["range"]
            fmt = q.get("fmt", "csv")
            if fmt not in ("csv", "json", "md", "markdown"):
                raise RequestError("unsupported export format")
            # The filtered aggregator is also used for summary/report rendering.
            agg, report_range = (report_aggregator(snapshot, app.cfg, filters) if fmt in ('md', 'markdown')
                                 else (query_aggregator(snapshot, app.cfg, filters), rng))
            safe_range = re.sub(r"[^a-zA-Z0-9_-]", "_", rng)[:64] or "all"
            if fmt in ("md", "markdown"):
                lang = q.get("lang") or app.cfg.get("language") or "zh-CN"
                md = report.render(agg, report_range, lang, cfg=app.cfg)
                basis = filters.get("cost_basis", "total_cost")
                if lang == "en-US":
                    label = {"total_cost": "Saved cost", "historical_cost": "Prices at call date",
                             "repriced_cost": "Current prices"}[basis]
                    note = "Pricing basis: %s. Base amounts are USD; displayed conversions use the configured rate. Unpriced records are excluded.\n\n" % label
                else:
                    label = {"total_cost": "采集时保存金额", "historical_cost": "调用日价格估算",
                             "repriced_cost": "现价估算"}[basis]
                    note = "计价口径：%s。基准金额为 USD，展示换算使用设置中的汇率；未定价记录不计入金额。\n\n" % label
                md = note + md
                self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="tokenlens-%s.md"' % safe_range})
                return
            size = 25000
            def batches():
                offset = 0
                while True:
                    batch = agg.records(rng, limit=size, offset=offset,
                                        order=q.get("order", "ts"))["rows"]
                    if not batch:
                        return
                    yield batch
                    offset += len(batch)
                    if len(batch) < size:
                        return
            head = {"generated": int(time.time()), "range": rng, "filters": filters,
                    "tool": filters.get("tool"), "summary": agg.summary(rng),
                    "tools": agg.by_tool(rng), "models": agg.by_model(rng, 200),
                    "projects": agg.by_project(rng, 200)} if fmt == "json" else None
            if head is not None:
                head["summary"].update(cost_quality(snapshot, agg, rng))
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8" if fmt == "json"
                             else "text/csv; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Content-Disposition", 'attachment; filename="tokenlens-%s.%s"' % (safe_range, fmt))
            self.end_headers()
            def write(value):
                body = value.encode("utf-8") if isinstance(value, str) else value
                self.wfile.write(("%x\r\n" % len(body)).encode("ascii"))
                self.wfile.write(body)
                self.wfile.write(b"\r\n")
            try:
                if fmt == "json":
                    write(json.dumps(head, ensure_ascii=False)[:-1] + ',"records":[')
                    first = True
                    for batch in batches():
                        for row in batch:
                            if not first:
                                write(b",")
                            write(json.dumps(row, ensure_ascii=False, default=str))
                            first = False
                    write(b"]}")
                else:
                    write("\ufeff")
                    columns = ("day", "ts", "tool", "model", "project", "session", "input", "output",
                               "cache_read", "cache_write", "reasoning", "total", "requests", "cost",
                               "cost_kind", "cost_source", "price_version", "currency", "src")
                    out = io.StringIO()
                    csv.writer(out).writerow(("date", "timestamp", "tool", "model", "project", "session",
                                              "input", "output", "cache_read", "cache_write", "reasoning", "total",
                                              "requests", "cost_usd", "cost_kind", "cost_source", "price_version",
                                              "currency", "source"))
                    write(out.getvalue())
                    for batch in batches():
                        out = io.StringIO()
                        writer = csv.writer(out)
                        for row in batch:
                            writer.writerow([csv_cell(row.get(key, "")) for key in columns])
                        write(out.getvalue())
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True
            except Exception:
                # Headers are already sent. Terminate the chunked response so
                # clients detect an incomplete download instead of appending JSON
                # or a second HTTP response to a seemingly valid export.
                self.close_connection = True

        # ---------------------------------------------------------- API POST
        def backup_download(self, prompt_history=None):
            if prompt_history is not None:
                app.backup.save_history(prompt_history)
            content, filename = app.backup.export()
            return self._send(200, content, "application/zip", extra={
                "Content-Disposition": 'attachment; filename="%s"' % filename})

        def run_ai(self, kind, method, **kwargs):
            """Synchronous compatibility endpoints share restore/concurrency gates."""
            try:
                task_id, cancel = app.tasks.create(kind)
            except RuntimeError as exc:
                raise RequestError(str(exc), 409) from exc
            result = None
            try:
                self.response_secret(kwargs.get("api_key"))
                if "cancel" not in kwargs:
                    try:
                        supports_cancel = "cancel" in inspect.signature(method).parameters
                    except (TypeError, ValueError):
                        supports_cancel = False
                    if supports_cancel:
                        kwargs["cancel"] = cancel.is_set
                result = method(**kwargs)
                return sanitize_public(result, self._response_secrets)
            except Exception as exc:
                secret = kwargs.get("api_key")
                if secret and secret in str(exc):
                    raise RequestError(redact_connection_result(str(exc),secret),500) from None
                raise
            finally:
                app.tasks.finish(task_id, not bool((result or {}).get("ok")))

        def ai_connection(self, body):
            """Resolve credentials before network access and bind saved targets."""
            if body.get("ccswitch_id"):
                resolved = connection_models.resolve_for_test(
                    body["ccswitch_id"], body.get("model", ""), body.get("ccswitch_protocol", ""))
                self.response_secret(resolved["api_key"])
                return {**resolved, "profile_id": ""}
            profile_id = body.get("profile_id", "")
            if profile_id and profile_id not in app.prompt.profiles:
                raise RequestError("saved AI connection no longer exists")
            profile = app.prompt.profiles.get(profile_id, {}) if profile_id else app.prompt.data
            model = body.get("model") or profile.get("model", "")
            base_url = body.get("base_url") or profile.get("base_url", "")
            key = app.prompt._request_key(base_url, body.get("api_key", ""), profile_id, model)
            self.response_secret(key)
            protocol = app.prompt.request_protocol(
                model, base_url, body.get("protocol", "auto"), profile_id)
            return {"model": model, "base_url": base_url, "api_key": key,
                    "profile_id": profile_id, "protocol": protocol}

        def api_post(self, path, q, body):
            if path in ("/api/desktop/show", "/api/desktop/hide"):
                desktop = getattr(app, "desktop", None)
                if desktop is None:
                    return self.json_out({"ok": False, "available": False}, 409)
                if path.endswith("/show"):
                    view = body.get("view")
                    if view not in (None, "", "overview", "settings", "records"):
                        raise RequestError("invalid desktop view")
                    desktop.show(view or None)
                else:
                    desktop.hide()
                return self.json_out({"ok": True, **desktop.status()})
            if path == "/api/ai-settings/check":
                result=self.run_ai("availability",app.ai_settings.check,body=body)
                return self.json_out(result)
            if path == "/api/ai-settings/save":
                return self.json_out(app.ai_settings.save(body))
            if path == "/api/ai-settings/models":
                return self.json_out(self.run_ai("models", app.ai_settings.models, body=body))
            if path == "/api/assistant/models":
                app.assistant.validate(body)
                return self.json_out(self.run_ai("assistant-models", app.assistant.models, body=body))
            if path == "/api/assistant/prices/confirm":
                result = app.assistant_prices.confirm(body)
                app.clear_dashboard_cache()
                return self.json_out(result)
            if path == "/api/assistant/report/delete":
                return self.json_out(app.assistant.delete_report(body.get("id")))
            if path == "/api/assistant/prices/restore":
                result = app.assistant_prices.restore_saved_estimates(body)
                app.clear_dashboard_cache()
                return self.json_out(result)
            if path == "/api/assistant/stream":
                app.assistant.validate(body)
                connection = app.ai_settings.resolve("assistant")
                self.response_secret(connection["api_key"])
                self.sse_start()
                def assistant_worker(emit, cancel):
                    def run(safe_emit):
                        try:
                            return app.assistant.run(body, on_event=safe_emit, cancel=cancel,
                                                     connection=connection)
                        except ValueError as exc:
                            result = {"ok": False, "action": body.get("action"), "error": str(exc)}
                            safe_emit({"type": "result", **result})
                            return result
                    return self.safe_stream(emit, connection["api_key"], run)
                self.sse_run(assistant_worker, task_id=body.get("task_id", ""), kind="assistant")
                return
            if path == "/api/ccswitch/models":
                if not body.get("ccswitch_id"):
                    raise RequestError("ccswitch_id is required")
                # A valid request returns catalog status even when the upstream
                # cannot list models, so the picker can offer manual entry and
                # retain complete pages from a partially failed catalog.
                return self.json_out(connection_models.discover(
                    body["ccswitch_id"], body.get("ccswitch_protocol", "")))
            if path == "/api/backup/export":
                return self.backup_download(body.get("prompt_history"))
            if path == "/api/backup/preview":
                return self.json_out(app.backup.preview(body.get("archive")))
            if path == "/api/backup/restore":
                result = app.backup.restore(body.get("token"))
                app.clear_dashboard_cache()
                return self.json_out(result)
            if path == "/api/sources/import/preview":
                return self.json_out(app.source_import.preview(body))
            if path == "/api/sources/import/discard":
                return self.json_out(app.source_import.discard(body))
            if path == "/api/sources/import/commit":
                # Scan and restore start under this same lock. Publishing a
                # newly imported file midway through a scan could otherwise
                # make its stale file listing prune the just-imported rows.
                with app.scanner.state.lock:
                    if app.scanner.state.running:
                        raise RequestError("请等待当前扫描结束后再确认导入", 409)
                    result = app.source_import.commit(body)
                    app.clear_dashboard_cache()
                return self.json_out(result)
            if path in ("/api/manage", "/api/finance", "/api/finance/action"):
                result = app.finance.action(body)
                app.clear_dashboard_cache()
                return self.json_out(result)
            if path == "/api/tasks/cancel":
                task_id = body.get("task_id", "")
                if not task_id:
                    raise RequestError("task_id is required")
                return self.json_out({"ok": True, "cancelled": app.tasks.cancel(task_id),
                                      "task_id": task_id})
            if path == "/api/scan":
                if bool(body.get("cancel")):
                    return self.json_out({"started": False,
                                          "cancelled": app.scanner.cancel(),
                                          "status": app.scanner.state.snapshot()})
                force = bool(body.get("force"))
                quick = body.get("quick")
                source_id = body.get("source_id") or body.get("source") or None
                started = app.scanner.start(force=force, quick=quick,
                                            source_id=source_id)
                return self.json_out({"started": started,
                                      "status": app.scanner.state.snapshot()})
            if path == "/api/scan/cancel":
                return self.json_out({"cancelled": app.scanner.cancel(),
                                      "status": app.scanner.state.snapshot()})
            if path == "/api/config":
                # 白名单直接从 DEFAULTS 推导，不再手写一份。
                # 手写列表曾经漏掉整整 8 个新增设置（accent / radius /
                # records_page / list_cap / default_range / pl_max_tokens /
                # pl_show_thinking / big_lib_mode）：前端保存时被静默丢弃，
                # 重启后全部回退默认值，而且完全不报错。
                # 现在 validate_patch 是唯一的校验入口，新增配置项自动生效。
                # port / host 仍然排除：它们要重启服务才能生效，不能热改。
                allowed = set(DEFAULTS) - {"port", "host"}
                patch = {k: v for k, v in (body or {}).items() if k in allowed}
                clean, errors = validate_patch(patch)
                if errors:
                    return self.json_out({"ok": False, "error": "配置值无效",
                                          "errors": errors,
                                          "config": app.cfg.data}, 400)
                app.cfg.update(clean)
                app.clear_dashboard_cache()
                return self.json_out({"ok": True, "config": app.cfg.data})
            # ---- 手动单价（自定义模型价格） ----
            # Both editors publish versions; saved historical costs stay unchanged.
            if path == "/api/prices/set":
                b = body or {}
                try:
                    val = app.set_custom_price(b.get("model", ""), b.get("price"))
                except (ValueError, TypeError) as exc:
                    return self.json_out({"ok": False, "error": str(exc)}, 400)
                return self.json_out({"ok": True, "model": str(b.get("model", "")).strip().lower(),
                                      "price": {"input": val[0], "output": val[1],
                                                "cache_read": val[2], "cache_write": val[3]}})
            if path == "/api/prices/remove":
                gone = app.remove_custom_price((body or {}).get("model", ""))
                return self.json_out({"ok": True, "removed": gone})
            if path == "/api/prompt/check":
                b = body or {}
                args = self.ai_connection({**b, "protocol": "auto"})
                # 与 AI 智商检测共用成功缓存：同一配置短时间内重复点「可用性检测」
                # 不再重复请求上游。失败结果不缓存，用户修改配置或重测仍会真实探测。
                res = self.run_ai("availability", app.prompt.availability,
                    model=args["model"], base_url=args["base_url"],
                    api_key=args["api_key"], protocol=args["protocol"],
                    profile_id=args["profile_id"], force=False)
                if res.get("ok"):
                    # 只有检测通过的配置才登记「可保存」
                    verified_key = args["api_key"] or app.prompt.api_key(args.get("profile_id", ""))
                    app.prompt.remember_verified(
                        args["model"], args["base_url"], verified_key,
                        args["protocol"], False, False)
                    res["can_save"] = True
                else:
                    res["can_save"] = False
                return self.json_out(res)

            if path == "/api/prompt/save":
                b = body or {}
                res = app.prompt.save(
                    model=b.get("model", ""), base_url=b.get("base_url", ""),
                    api_key=b.get("api_key", ""), protocol="auto",
                    profile_id=b.get("profile_id", ""), profile_name=b.get("profile_name", ""),
                    new_profile=bool(b.get("new_profile")))
                return self.json_out(res, 200 if res.get("ok") else 400)

            if path == "/api/prompt/select":
                return self.json_out(app.prompt.select_profile((body or {}).get("profile_id", "")))
            if path == "/api/prompt/delete":
                res = app.prompt.delete_profile((body or {}).get("profile_id", ""))
                return self.json_out(res, 200 if res.get("ok") else 400)

            if path == "/api/prompt/optimize":
                b = body or {}
                connection = app.ai_settings.resolve("prompt")
                res = self.run_ai("optimize", app.prompt.optimize,
                    **connection,
                    prompt=b.get("prompt", ""), task=b.get("task", "general"),
                    extra=b.get("extra", ""), mode=b.get("mode", "polish"),
                    temperature=b.get("temperature"), max_tokens=b.get("max_tokens"),
                    profile_id="")
                res = redact_connection_result(res, connection["api_key"])
                return self.json_out(res, 200 if res.get("ok") else 400)

            if path == "/api/ai-test":
                b = body or {}
                if b.get("mode", "q1") not in ("q1", "q2", "pelican"):
                    raise RequestError("unsupported benchmark mode")
                connection = self.ai_connection(b)
                res = self.run_ai("ai-test", app.prompt.ai_test,
                    **connection, thinking=b.get("thinking", "auto"), mode=b.get("mode", "q1"))
                res = redact_connection_result(res, connection["api_key"])
                return self.json_out(res, 200 if res.get("ok") else 400)

            if path == "/api/ai-availability":
                b = body or {}
                connection = self.ai_connection(b)
                res = self.run_ai("availability", app.prompt.availability,
                    **connection, force=bool(b.get("force")))
                res = redact_connection_result(res, connection["api_key"])
                return self.json_out(res, 200 if res.get("ok") else 400)

            if path == "/api/prompt/optimize-stream":
                b = body or {}
                connection = app.ai_settings.resolve("prompt")
                self.response_secret(connection["api_key"])
                self.sse_start()

                def worker(emit, cancel):
                    return self.safe_stream(emit, connection["api_key"], lambda safe_emit:
                        app.prompt.optimize_stream(**connection,
                            prompt=b.get("prompt", ""), task=b.get("task", "general"),
                            extra=b.get("extra", ""), mode=b.get("mode", "polish"),
                            profile_id="", on_event=safe_emit, cancel=cancel))

                self.sse_run(worker, task_id=b.get("task_id", ""), kind="optimize")
                return

            if path == "/api/ai-test-stream":
                b = body or {}
                if b.get("mode", "q1") not in ("q1", "q2", "pelican"):
                    raise RequestError("unsupported benchmark mode")
                connection = self.ai_connection(b)
                self.sse_start()

                def worker(emit, cancel):
                    return self.safe_stream(emit, connection["api_key"], lambda safe_emit:
                        app.prompt.ai_test_stream(**connection,
                            thinking=b.get("thinking", "auto"), mode=b.get("mode", "q1"),
                            on_event=safe_emit, cancel=cancel))

                self.sse_run(worker, task_id=b.get("task_id", ""), kind="ai-test")
                return

            if path == "/api/prompt/clear":
                b = body or {}
                res = app.prompt.delete_profile(b.get("profile_id", "")) if b.get("profile_id") else app.prompt.clear()
                return self.json_out(res, 200 if res.get("ok") else 400)
            if path == "/api/reset":
                app.store.reset()
                return self.json_out({"ok": True})
            if path == "/api/pick-folder":
                return self.json_out({"ok": False, "error": "请在输入框中直接粘贴目录路径"})
            if path == "/api/shutdown":
                app.tasks.cancel_all()
                app.backup.close()
                app.stop_event.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self.json_out({"ok": True})
            return self.json_out({"error": "unknown endpoint"}, 404)

    return Handler


class ReusableServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    MAX_CONNECTIONS = 32
    MAX_LARGE_UPLOADS = 2
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.connection_slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)
        self.upload_slots = threading.BoundedSemaphore(self.MAX_LARGE_UPLOADS)
        self._input_deadlines = {}
        self._rejected_connections = {}
        self._deadline_lock = threading.Lock()
        self._deadline_stop = threading.Event()
        super().__init__(*args, **kwargs)
        self._deadline_thread = threading.Thread(
            target=self._expire_inputs, daemon=True, name="tokenlens-http-input-deadlines")
        self._deadline_thread.start()

    def set_request_deadline(self, connection, seconds=None):
        with self._deadline_lock:
            if seconds is None:
                self._input_deadlines.pop(connection, None)
            else:
                self._input_deadlines[connection] = time.monotonic() + seconds

    def _expire_inputs(self):
        while not self._deadline_stop.wait(0.05):
            now = time.monotonic()
            with self._deadline_lock:
                for connection, deadline in list(self._input_deadlines.items()):
                    if deadline <= now:
                        self._input_deadlines.pop(connection, None)
                        try:
                            connection.shutdown(socket.SHUT_RD)
                        except OSError:
                            pass
                for connection, deadline in list(self._rejected_connections.items()):
                    try:
                        unread = connection.recv(65536)
                    except BlockingIOError:
                        unread = None
                    except OSError:
                        unread = b""
                    if deadline <= now or unread == b"":
                        self._rejected_connections.pop(connection, None)
                        connection.close()

    def server_close(self):
        self._deadline_stop.set()
        self._deadline_thread.join(timeout=1)
        with self._deadline_lock:
            for connection in self._rejected_connections:
                connection.close()
            self._rejected_connections.clear()
        super().server_close()

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Connection: close\r\nContent-Length: 0\r\n\r\n")
                # Closing a Windows socket with unread request bytes may reset
                # the peer before 503 arrives. Send FIN, then let the shared
                # watchdog drain briefly without spawning another worker.
                request.shutdown(socket.SHUT_WR)
                request.setblocking(False)
                with self._deadline_lock:
                    if len(self._rejected_connections) < self.MAX_CONNECTIONS:
                        self._rejected_connections[request] = time.monotonic() + 0.25
                        return
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            return super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

