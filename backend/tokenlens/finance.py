"""Local project management, price versions and reproducible bill reconciliation."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import time
import uuid
from functools import lru_cache

from .aggregator import Aggregator, TOTAL_EXPR, range_bounds


def amount(value):
    if isinstance(value, bool):
        raise ValueError("金额必须是非负有限数值")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1e12:
        raise ValueError("金额必须是非负有限数值")
    return number


def day(value):
    text = str(value or "").strip()
    parsed = dt.date.fromisoformat(text)
    if parsed.isoformat() != text:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    return text


def project_cost_basis(store, cfg, basis, where, args):
    """Price one bounded analysis scope once, using connection-local rows.

    The main database is opened read-only by export_snapshot. The temporary
    table avoids repeating Python price calculations for every evidence query
    and never copies request extras, prompts, or the rest of the database.
    """
    if basis == 'total_cost':
        return
    if basis not in ('historical_cost', 'repriced_cost'):
        raise ValueError('计价方式无效。')
    from .pricing import price_usage
    versions = {}
    for row in Finance(store, cfg, initialize=False).prices():
        versions.setdefault(row['model'], []).append(row)
    today = str(dt.date.today())

    @lru_cache(maxsize=512)
    def priced(day, model, ts, kind, source, saved, input, output, reasoning, read, write):
        if kind in ('actual', 'free'):
            return saved or 0, kind, None, None
        if source == 'usage-unavailable':
            return saved or 0, 'unknown', None, None
        at = today if basis == 'repriced_cost' else day
        match = next((row for row in versions.get((model or '').strip().lower(), []) if row['effective_from'] <= at), None)
        if not match:
            return saved or 0, kind, None, None
        provenance = 'analysis-current-prices' if basis == 'repriced_cost' else 'analysis-call-date-prices'
        if match['source'] == 'legacy-override:unpriced':
            return 0, 'unknown', provenance, match['id']
        value = price_usage((input or 0, (output or 0)+(reasoning or 0), read or 0, write or 0),
                            match['prices'], match.get('rules'),
                            prompt_tokens=(input or 0)+(read or 0)+(write or 0), request_time=ts)
        return ((saved or 0, 'unknown', provenance, match['id']) if value is None else
                (value, 'estimated', provenance, match['id']))

    conn = store.conn()
    fields = 'day,model,ts,cost_kind,cost_source,cost,input,output,reasoning,cache_read,cache_write'
    conn.create_function('tl_analysis_cost', 11, lambda *values: priced(*values)[0])
    conn.create_function('tl_analysis_kind', 11, lambda *values: priced(*values)[1])
    conn.create_function('tl_analysis_source', 11, lambda *values: priced(*values)[2])
    conn.create_function('tl_analysis_version', 11, lambda *values: priced(*values)[3])
    # URI mode=ro still protects main; temporarily allow only our TEMP schema.
    conn.execute('PRAGMA query_only=OFF')
    try:
        conn.execute('CREATE TEMP TABLE tl_analysis_prices AS SELECT id,'
                     'tl_analysis_cost('+fields+') cost,tl_analysis_kind('+fields+') cost_kind,'
                     'tl_analysis_source('+fields+') cost_source,tl_analysis_version('+fields+') price_version '
                     'FROM main.records '+where, args)
        conn.execute('CREATE UNIQUE INDEX temp.analysis_record_id ON tl_analysis_prices(id)')
        # Only IDs and projected prices are materialized. Full page rows,
        # including extras, stay in main and are fetched through this view.
        columns = [row['name'] for row in store.query('PRAGMA main.table_info(records)')]
        selection = []
        for name in columns:
            quoted = '"'+name.replace('"', '""')+'"'
            if name in ('cost','cost_kind'):
                selection.append('p.'+quoted+' '+quoted)
            elif name in ('cost_source','price_version'):
                selection.append('COALESCE(p.'+quoted+',r.'+quoted+') '+quoted)
            else:
                selection.append('r.'+quoted+' '+quoted)
        conn.execute('CREATE TEMP VIEW records AS SELECT '+','.join(selection)+',r.cost saved_cost '
                     'FROM main.records r JOIN tl_analysis_prices p ON p.id=r.id')
    finally:
        conn.execute('PRAGMA query_only=ON')
        conn.create_function('tl_analysis_cost', 11, None)
        conn.create_function('tl_analysis_kind', 11, None)
        conn.create_function('tl_analysis_source', 11, None)
        conn.create_function('tl_analysis_version', 11, None)


class Finance:
    def __init__(self, store, cfg, *, initialize=True):
        self.store, self.cfg = store, cfg
        if not initialize:
            return
        with store.locked() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS managed_projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, paths TEXT NOT NULL,
                    tags TEXT NOT NULL, budget REAL NOT NULL DEFAULT 0, updated_at INTEGER);
                CREATE TABLE IF NOT EXISTS price_versions (
                    id TEXT PRIMARY KEY, model TEXT NOT NULL, effective_from TEXT NOT NULL,
                    prices TEXT NOT NULL, source TEXT NOT NULL, created_at INTEGER);
                CREATE TABLE IF NOT EXISTS finance_audit (
                    id INTEGER PRIMARY KEY, action TEXT, before_json TEXT, after_json TEXT, created_at INTEGER);
                CREATE TABLE IF NOT EXISTS reconciliations (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, name TEXT, created_at INTEGER,
                    report TEXT NOT NULL);
            """)
            # Old databases allowed only one price per model/day. Preserve every
            # existing id while removing that constraint for immutable revisions.
            indexes = conn.execute("PRAGMA index_list(price_versions)").fetchall()
            legacy_unique = any(index["unique"] and
                [r["name"] for r in conn.execute('PRAGMA index_info("' + index["name"].replace('"', '""') + '")')] == ["model", "effective_from"]
                for index in indexes)
            if legacy_unique:
                with store._transaction():
                    conn.execute("CREATE TABLE price_versions_immutable ("
                                 "id TEXT PRIMARY KEY, model TEXT NOT NULL, effective_from TEXT NOT NULL,"
                                 "prices TEXT NOT NULL, source TEXT NOT NULL, created_at INTEGER)")
                    conn.execute("INSERT INTO price_versions_immutable SELECT id,model,effective_from,prices,source,created_at "
                                 "FROM price_versions ORDER BY rowid")
                    conn.execute("DROP TABLE price_versions")
                    conn.execute("ALTER TABLE price_versions_immutable RENAME TO price_versions")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(price_versions)")}
            if "rules" not in columns:
                conn.execute("ALTER TABLE price_versions ADD COLUMN rules TEXT NOT NULL DEFAULT '[]'")
                conn.commit()

    def _write(self, sql, params=(), audit=None):
        with self.store.locked() as conn:
            conn.execute("SAVEPOINT finance_write")
            try:
                conn.execute(sql, params)
                if audit:
                    conn.execute("INSERT INTO finance_audit(action,before_json,after_json,created_at) VALUES(?,?,?,?)",
                                 (audit[0], json.dumps(audit[1], ensure_ascii=False),
                                  json.dumps(audit[2], ensure_ascii=False), int(time.time())))
                conn.execute("RELEASE finance_write")
                self.store._commit()
            except Exception:
                conn.execute("ROLLBACK TO finance_write")
                conn.execute("RELEASE finance_write")
                raise

    def prices(self):
        values = self.store.query("SELECT * FROM price_versions ORDER BY effective_from DESC,rowid DESC")
        for row in values:
            row["prices"] = json.loads(row["prices"])
            row["rules"] = json.loads(row.get("rules") or "[]")
        return values

    def save_price(self, body):
        return self._save_price(body)

    def save_reference_price(self, body):
        """Publish verified public rates, preserving unpublished rates as null."""
        if not str(body.get("source") or "").startswith(("assistant-reference:","assistant-official:")):
            raise ValueError("参考价须包含服务器核验来源")
        return self._save_price(body, partial=True)

    def _save_price(self, body, partial=False):
        model = str(body.get("model") or "").strip().lower()
        if not model or len(model) > 200:
            raise ValueError("请填写模型名称（不超过200字）")
        effective = day(body.get("effective_from"))
        raw = body.get("prices")
        if not isinstance(raw, list) or len(raw) != 4:
            raise ValueError("请填写输入、输出、缓存读、缓存写四项单价")
        prices = [None if partial and v is None else amount(v) for v in raw]
        from .pricing import validate_context_rules
        rules = validate_context_rules(body.get("rules") or [])
        if rules and not partial:
            raise ValueError("条件价格须通过已核验参考价发布")
        if partial and any(prices[index] is None for index in (0, 1)):
            raise ValueError("参考价必须至少包含输入和输出单价")
        source = str(body.get("source") or "用户录入").strip()[:500]
        existing = self.store.query_one("SELECT * FROM price_versions WHERE model=? AND effective_from=? ORDER BY rowid DESC LIMIT 1", (model, effective))
        row = {"id": uuid.uuid4().hex, "model": model,
               "effective_from": effective, "prices": prices, "rules": rules, "source": source, "created_at": int(time.time())}
        self._write("INSERT INTO price_versions(id,model,effective_from,prices,source,created_at,rules) VALUES(?,?,?,?,?,?,?)",
                    (row["id"], model, effective, json.dumps(prices), source, row["created_at"], json.dumps(rules)),
                    ("price_save", existing, row))
        return {"ok": True, "price": row}

    def reestimate_unknown(self, model, version, prices, source, rules=None):
        """Apply a confirmed current reference to unpriced usage only.

        Original actual/free/estimated charges remain immutable. Missing rates
        block only records that consumed that token category. The audit entry
        records that earlier unpriced usage was valued with a current rate.
        """
        key = str(model or "").strip().lower()
        values = [None if value is None else amount(value) for value in prices]
        if len(values) != 4:
            raise ValueError("需要四项参考单价")
        from .pricing import RATE_FIELDS, validate_context_rules, price_rule_sql
        rules = validate_context_rules(rules or [])
        columns = ("COALESCE(input,0)", "(COALESCE(output,0)+COALESCE(reasoning,0))",
                   "COALESCE(cache_read,0)", "COALESCE(cache_write,0)")
        prompt = "(COALESCE(input,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0))"
        rate_sql = []
        for field, value in zip(RATE_FIELDS, values):
            base = "NULL" if value is None else repr(value)
            matches = ["WHEN " + price_rule_sql(rule, prompt) + " THEN " + repr(rule["rates"][field])
                       for rule in reversed(rules) if field in rule["rates"]]
            expression = "(CASE " + " ".join(matches) + " ELSE " + base + " END)" if matches else base
            rate_sql.append(expression)
        missing = ["(" + column + "=0 OR " + expression + " IS NOT NULL)" for column, expression in zip(columns, rate_sql)]
        where = "lower(trim(model))=? AND cost_kind='unknown'"
        eligible = where + " AND COALESCE(cost_source,'')<>'usage-unavailable'" + (" AND " + " AND ".join(missing) if missing else "")
        expression = "+".join(column + "*COALESCE(" + rate + ",0)" for column, rate in zip(columns, rate_sql))
        with self.store.locked() as conn, self.store._transaction():
            self.store._usage_totals = None
            matched = conn.execute("SELECT COUNT(*) FROM records WHERE " + where, (key,)).fetchone()[0]
            cursor = conn.execute("UPDATE records SET cost=(" + expression + ")/1000000.0,"
                "cost_kind='estimated',cost_source=?,price_version=?,currency='USD' WHERE " + eligible,
                (source, version, key))
            updated = cursor.rowcount
            self.store._usage_totals = self.store._read_usage_totals()
            if updated:
                conn.execute("INSERT INTO finance_audit(action,before_json,after_json,created_at) VALUES(?,?,?,?)",
                    ("price_apply_unknown", json.dumps({"model": key, "cost_kind": "unknown", "records": updated}),
                     json.dumps({"model": key, "price_version": version, "source": source, "prices": values,
                                 "rules": rules, "records": updated, "basis": "confirmed_current_reference"}), int(time.time())))
        return {"repriced_records": updated, "remaining_unknown_records": matched - updated}

    def price_breakdown(self, rng, price_map, tool=None):
        """Aggregate current-price ledger rows by request context tier."""
        from .pricing import RATE_FIELDS, price_rule_sql
        where, args = self._where(rng)
        if tool:
            where += " AND tool=?"
            args = (*args, tool)
        result = {}
        prompt = "(COALESCE(input,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0))"
        # Tiered prices include many models absent from a user's library.
        # Discover applicable models in bounded batches instead of scanning
        # every record once for each entry in the built-in price catalog.
        candidates = [model for model, version in price_map.items() if version.get("rules")]
        present = set()
        for offset in range(0, len(candidates), 400):
            batch = candidates[offset:offset + 400]
            rows = self.store.query("SELECT DISTINCT lower(trim(model)) model FROM records WHERE " + where +
                " AND lower(trim(model)) IN (" + ",".join("?" for _ in batch) + ")", (*args, *batch))
            present.update(row["model"] for row in rows)
        for model, version in price_map.items():
            rules = version.get("rules") or []
            if not rules or model not in present:
                continue
            thresholds = sorted({rule["prompt_tokens_gt"] for rule in rules if "prompt_tokens_gt" in rule})
            bucket = "(" + "+".join("CASE WHEN " + price_rule_sql(rule, prompt) + " THEN " + str(2**index) + " ELSE 0 END"
                                     for index, rule in enumerate(rules)) + ")"
            rows = self.store.query("SELECT " + bucket + " bucket,MIN(" + prompt + ") prompt_min,"
                "COUNT(*) records,SUM(requests) requests,SUM(input) input,SUM(output+reasoning) output,"
                "SUM(cache_read) cache_read,SUM(cache_write) cache_write FROM records WHERE " + where +
                " AND lower(trim(model))=? GROUP BY bucket ORDER BY bucket", (*args, model))
            segments = []
            for row in rows:
                rates = list(version["prices"])
                active = []
                for index, rule in enumerate(rules):
                    if row["bucket"] & 2**index:
                        active.append(rule)
                        for field, value in rule["rates"].items():
                            rates[RATE_FIELDS.index(field)] = value
                only_context = all(set(rule) <= {"prompt_tokens_gt", "rates"} for rule in rules)
                matched_thresholds = [rule["prompt_tokens_gt"] for rule in active if "prompt_tokens_gt" in rule]
                lower = max(matched_thresholds) + 1 if matched_thresholds else 0
                upper = next((value for value in thresholds if value >= lower), None) if only_context else None
                segments.append({"context_min": lower if only_context else None, "context_max": upper,
                    "conditions": active, "tokens_by": {field: row[field] or 0 for field in RATE_FIELDS},
                    "prices": rates, "requests": row["requests"] or 0, "records": row["records"]})
            result[model] = segments
        return result

    def save_project(self, body):
        # Ownership validation and publication must observe the same state.
        # HTTP requests otherwise could both claim a path and count it twice.
        with self.store.locked():
            return self._save_project(body)

    def _save_project(self, body):
        selected = str(body.get("project") or "").strip()
        if not selected:
            raise ValueError("请选择项目")
        existing = self.store.query_one("SELECT * FROM managed_projects WHERE id=?", (selected,))
        paths = body.get("paths") or (json.loads(existing["paths"]) if existing else [selected])
        if not isinstance(paths, list) or not paths or len(paths) > 100 or any(not isinstance(p, str) for p in paths):
            raise ValueError("项目路径必须为非空列表，最多100项")
        paths = list(dict.fromkeys(paths))
        known = {r["project"] or "(未标注)" for r in self.store.query("SELECT DISTINCT project FROM records")}
        managed = self.store.query("SELECT * FROM managed_projects")
        for item in managed:
            owned = set(json.loads(item["paths"]))
            known.update(owned)
            if item["id"] != existing.get("id") and owned.intersection(paths):
                raise ValueError("部分路径已属于另一个项目，请先解除原项目关联")
        if any(p not in known for p in paths):
            raise ValueError("只能关联当前已识别的项目路径")
        name = str(body.get("name") or selected).strip()
        if not name or len(name) > 200:
            raise ValueError("项目名称应为1至200字")
        tags = body.get("tags") or []
        if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) or len(t) > 40 for t in tags):
            raise ValueError("标签最多20项，每项不超过40字")
        row = {"id": existing.get("id") or hashlib.sha256(selected.encode()).hexdigest()[:24],
               "name": name, "paths": paths, "tags": list(dict.fromkeys(t.strip() for t in tags if t.strip())),
               "budget": amount(body.get("budget") or 0), "updated_at": int(time.time())}
        self._write("INSERT INTO managed_projects(id,name,paths,tags,budget,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,paths=excluded.paths,tags=excluded.tags,budget=excluded.budget,updated_at=excluded.updated_at",
                    (row["id"], name, json.dumps(paths, ensure_ascii=False), json.dumps(row["tags"], ensure_ascii=False), row["budget"], row["updated_at"]),
                    ("project_save", existing, row))
        return {"ok": True, "project": row}

    def _where(self, rng):
        start, end, _, _ = range_bounds(rng)
        return ("day>=? AND day<=?", (start or "0001-01-01", end))

    def _project_paths(self, value):
        """Resolve a managed id or a normalized source path without guessing aliases."""
        value = str(value or "").strip()
        managed = self.store.query_one("SELECT paths FROM managed_projects WHERE id=?", (value,))
        if managed:
            return json.loads(managed["paths"])
        if value == "(未标注)":
            return [value]
        if "\\" in value or "/" in value or (len(value) > 1 and value[1] == ":"):
            from .adapters import norm_project
            return [norm_project(value)]
        return [value]

    def _project_rows(self, rng):
        where, args = self._where(rng)
        rows = self.store.query("SELECT COALESCE(NULLIF(project,''),'(未标注)') project, SUM(cost) cost, "
                                "SUM(" + TOTAL_EXPR + ") tokens,SUM(requests) requests,"
                                "COUNT(DISTINCT CASE WHEN session<>'' THEN tool||char(31)||session END) sessions FROM records WHERE " + where + " GROUP BY 1", args)
        today = dt.date.today()
        monthly = {r["project"]: r["cost"] for r in self.store.query(
            "SELECT COALESCE(NULLIF(project,''),'(未标注)') project,SUM(cost) cost FROM records WHERE day>=? AND day<=? GROUP BY 1",
            (str(today.replace(day=1)), str(today)))}
        raw = {r["project"]: r for r in rows}
        managed = self.store.query("SELECT * FROM managed_projects ORDER BY name")
        result, owned = [], set()
        for item in managed:
            paths = json.loads(item["paths"])
            owned.update(paths)
            sessions = self.store.query_one("SELECT COUNT(*) n FROM (SELECT DISTINCT tool,session FROM records WHERE " +
                where + " AND session<>'' AND COALESCE(NULLIF(project,''),'(未标注)') IN (" +
                ",".join("?" for _ in paths) + "))", [*args,*paths])
            result.append({**item, "paths": paths, "tags": json.loads(item["tags"]),
                           **{k: sum(raw.get(p, {}).get(k) or 0 for p in paths) for k in ("cost", "tokens", "requests")},
                           "sessions": int(sessions.get("n") or 0),
                           "period_cost": sum(monthly.get(p) or 0 for p in paths)})
        for path, row in raw.items():
            if path not in owned:
                result.append({**row, "id": path, "name": path, "paths": [path], "tags": [],
                               "budget": 0, "period_cost": monthly.get(path) or 0})
        for row in result:
            row["ratio"] = row["period_cost"] / row["budget"] if row["budget"] else 0
        return sorted(result, key=lambda r: -r["cost"])

    def _costs(self, rng, prices):
        where, args = self._where(rng)
        tiered = sorted({p["model"] for p in prices if p.get("rules")})
        timed = sorted({p["model"] for p in prices if any("utc_days" in rule or "utc_start" in rule for rule in p.get("rules") or [])})
        prompt = ("CASE WHEN lower(trim(model)) IN (" + ",".join("?" for _ in tiered) +
                  ") THEN input+cache_read+cache_write ELSE 0 END") if tiered else "0"
        instant = ("CASE WHEN lower(trim(model)) IN (" + ",".join("?" for _ in timed) +
                   ") THEN CAST(strftime('%w',ts,'unixepoch') AS INTEGER)*10000+CAST(strftime('%H%M',ts,'unixepoch') AS INTEGER) ELSE 0 END") if timed else "0"
        rows = self.store.query("SELECT day,model,cost_kind,cost_source," + prompt + " request_prompt_tokens," + instant + " request_utc,MIN(ts) request_timestamp,COUNT(*) records,SUM(" + TOTAL_EXPR + ") tokens,"
                                "SUM(input) input,SUM(output) output,SUM(reasoning) reasoning,SUM(cache_read) cache_read,SUM(cache_write) cache_write,"
                                "SUM(cost) cost FROM records WHERE " + where + " GROUP BY day,model,cost_kind,cost_source,request_prompt_tokens,request_utc", (*tiered, *timed, *args))
        total = {"actual": 0, "estimated": 0, "free_records": 0, "unknown_records": 0,
                 "total_cost": 0, "repriced_cost": 0, "historical_cost": 0, "tokens": 0, "priced_tokens": 0,
                 "repriced_unknown_records": 0, "historical_unknown_records": 0}
        today = str(dt.date.today())
        for row in rows:
            kind, cost = row["cost_kind"], row["cost"] or 0
            total["total_cost"] += cost
            total["tokens"] += row["tokens"] or 0
            if kind in ("actual", "estimated"):
                total[kind] += cost
            if kind in ("free", "unknown"):
                total[kind + "_records"] += row["records"]
            if kind != "unknown":
                total["priced_tokens"] += row["tokens"] or 0
            candidates = [p for p in prices if p["model"] == (row["model"] or "").strip().lower()]
            def repriced(at, unknown_key):
                if row["cost_source"] == "usage-unavailable":
                    total[unknown_key] += row["records"]
                    return cost
                match = next((p for p in candidates if p["effective_from"] <= at), None)
                if kind in ("actual", "free"):
                    return cost
                if not match:
                    if kind == "unknown":
                        total[unknown_key] += row["records"]
                    return cost
                if match["source"] == "legacy-override:unpriced":
                    total[unknown_key] += row["records"]
                    return 0
                from .pricing import price_usage
                value = price_usage((row["input"] or 0, (row["output"] or 0)+(row["reasoning"] or 0),
                                     row["cache_read"] or 0,row["cache_write"] or 0), match["prices"], match.get("rules"),
                                     prompt_tokens=row["request_prompt_tokens"], request_time=row["request_timestamp"])
                if value is None:
                    total[unknown_key] += row["records"]
                    return cost
                return value
            total["repriced_cost"] += repriced(today, "repriced_unknown_records")
            total["historical_cost"] += repriced(row["day"], "historical_unknown_records")
        total["pricing_coverage"] = total["priced_tokens"] / total["tokens"] if total["tokens"] else (0.0 if total["unknown_records"] else 1.0)
        return total

    def reconcile(self, body, save=False):
        source = body.get("csv")
        if not isinstance(source, str) or len(source.encode("utf-8")) > 2_000_000:
            raise ValueError("请提供不超过2MB的CSV文本")
        reader = csv.DictReader(io.StringIO(source.lstrip("\ufeff")))
        if not reader.fieldnames or not {"date", "model", "cost_usd"}.issubset(reader.fieldnames):
            raise ValueError("CSV需要date,model,cost_usd列，可选tool,project；日期为YYYY-MM-DD，金额为USD")
        grouped, errors, project_labels = {}, [], {}
        for number, row in enumerate(reader, 2):
            if number > 10001:
                raise ValueError("CSV最多10000行")
            try:
                date = day(row.get("date"))
                model = str(row.get("model") or "").strip().lower()
                if not model or len(model) > 200 or None in row:
                    raise ValueError("模型为空、过长或列数不正确")
                project = (row.get("project") or "").strip()
                if project and not self.store.query_one("SELECT id FROM managed_projects WHERE id=?", (project,)):
                    project = self._project_paths(project)[0]
                paths = tuple(sorted(self._project_paths(project))) if project else ()
                key = (date, model, (row.get("tool") or "").strip(), paths)
                project_labels[key] = project
                grouped[key] = grouped.get(key, 0) + amount(row.get("cost_usd"))
            except (TypeError, ValueError) as exc:
                errors.append({"line": number, "error": str(exc)})
        if not grouped and not errors:
            errors.append({"line": 2, "error": "CSV没有数据行"})
        groups = {}
        for key in grouped:
            group = groups.setdefault(key[:2], [])
            if any((not a[2] or not key[2] or a[2] == key[2]) and
                   (not a[3] or not key[3] or set(a[3]).intersection(key[3])) for a in group):
                errors.append({"line": 0, "error": "%s %s存在范围重叠的账单行，请统一工具和项目粒度" % key[:2]})
            group.append(key)
        results = []
        for key, billed in sorted(grouped.items()):
            date, model, tool, paths = key
            project = project_labels[key]
            sql, args = "day=? AND lower(trim(model))=?", [date, model]
            if tool:
                sql += " AND tool=?"
                args.append(tool)
            if paths:
                sql += " AND COALESCE(NULLIF(project,''),'(未标注)') IN (" + ",".join("?" for _ in paths) + ")"
                args.extend(paths)
            matched = self.store.query_one("SELECT COALESCE(SUM(cost),0) cost,COUNT(*) records,"
                                           "SUM(CASE WHEN cost_kind='unknown' THEN 1 ELSE 0 END) unknown FROM records WHERE " + sql, args)
            recorded = matched.get("cost") or 0
            results.append({"date": date, "model": model, "tool": tool, "project": project, "project_paths": list(paths),
                            "billed": billed, "recorded": recorded, "difference": round(billed - recorded, 8),
                            "matched_records": matched.get("records") or 0, "unknown_records": matched.get("unknown") or 0})
        totals = {k: round(sum(r[k] for r in results), 8) for k in ("billed", "recorded", "difference")}
        result = {"ok": True, "rows": results, "totals": totals, "errors": errors, "importable": bool(results and not errors),
                  "data_version": self.store.version, "created_at": int(time.time()),
                  "name": str(body.get("name") or "账单对账").strip()[:200]}
        if save:
            if not result["importable"]:
                raise ValueError("请先修正CSV错误，未导入任何数据")
            fingerprint = hashlib.sha256(json.dumps(sorted(grouped.items()), ensure_ascii=False).encode()).hexdigest()
            old = self.store.query_one("SELECT id FROM reconciliations WHERE fingerprint=?", (fingerprint,))
            if old:
                return {**self.reconciliation(old["id"]), "duplicate": True}
            result["id"] = uuid.uuid4().hex
            self._write("INSERT INTO reconciliations(id,fingerprint,name,created_at,report) VALUES(?,?,?,?,?)",
                        (result["id"], fingerprint, result["name"], result["created_at"], json.dumps(result, ensure_ascii=False)))
        return result

    def reconciliation(self, report_id):
        row = self.store.query_one("SELECT report FROM reconciliations WHERE id=?", (report_id,))
        if not row:
            raise ValueError("对账记录不存在")
        return json.loads(row["report"])

    def anomalies(self, rng):
        start, end, _, _ = range_bounds(rng)
        if not start:
            return []
        days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
        previous_end = dt.date.fromisoformat(start) - dt.timedelta(days=1)
        previous_start = previous_end - dt.timedelta(days=days - 1)
        result = []
        for kind in ("model", "project", "tool"):
            group = {"project": "COALESCE(NULLIF(project,''),'(未标注)')",
                     "model": "COALESCE(NULLIF(lower(trim(model)),''),'未知模型')"}.get(kind, kind)
            rows = self.store.query("SELECT " + group + " name,"
                "SUM(CASE WHEN day>=? THEN cost ELSE 0 END) current_cost,"
                "SUM(CASE WHEN day<? THEN cost ELSE 0 END) previous_cost FROM records "
                "WHERE day>=? AND day<=? GROUP BY " + group, (start, start, str(previous_start), end))
            for row in rows:
                current, previous = row["current_cost"] or 0, row["previous_cost"] or 0
                if current > previous and current > 0:
                    result.append({"kind": kind, "title": row["name"] or "(未标注)",
                        "detail": "本期与上一等长周期的费用增量；点击核对调用明细",
                        "current_cost": current, "previous_cost": previous, "delta": current - previous,
                        kind: row["name"], "range": rng})
        return sorted(result, key=lambda r: -r["delta"])[:12]

    def dashboard(self, rng="30d", internal_offset=0, internal_limit=50):
        where, args = self._where(rng)
        prices = self.prices()
        quality = self.store.query_one("SELECT COUNT(*) records,COALESCE(SUM(" + TOTAL_EXPR + "),0) tokens FROM records WHERE " + where, args)
        quality["sources"] = self.store.list_sources()
        quality["last_scan"] = self.store.last_scan_time()
        quality["unknown_models"] = self.store.query("SELECT COALESCE(NULLIF(lower(trim(model)),''),'未知模型') model,COUNT(*) records,SUM(" + TOTAL_EXPR + ") tokens "
            "FROM records WHERE " + where + " AND cost_kind='unknown' GROUP BY 1 ORDER BY tokens DESC", args)
        # 汇总卡永远覆盖整个范围，表格按页读取：只截断表格而不同步分页，
        # 会让「请求记录 69」与表内 50 行看起来互相矛盾。
        offset = max(0, int(internal_offset or 0))
        limit = max(1, min(500, int(internal_limit or 50)))
        # Aggregate metadata inside SQLite, so changing a page never transfers
        # and decodes the entire AI request history in Python.
        meta = "CASE WHEN json_valid(extra) THEN extra ELSE '{}' END"
        status = "COALESCE(NULLIF(json_extract(" + meta + ",'$.outcome'),''),json_extract(" + meta + ",'$.status'))"
        internal = self.store.query_one("SELECT COUNT(*) records,COALESCE(SUM(cost),0) cost,COALESCE(SUM(" + TOTAL_EXPR +
            "),0) tokens,COALESCE(SUM(CASE WHEN cost_kind='unknown' THEN 1 ELSE 0 END),0) unknown,"
            "COALESCE(SUM(CASE WHEN " + status + " IN ('failed','error') THEN 1 ELSE 0 END),0) failures,"
            "COALESCE(SUM(CASE WHEN " + status + " IN ('cancelled','canceled') THEN 1 ELSE 0 END),0) cancelled,"
            "COALESCE(SUM(CASE WHEN COALESCE(json_extract(" + meta + ",'$.usage_known'),"
            "cost_source<>'usage-unavailable' AND (cost_kind<>'unknown' OR (" + TOTAL_EXPR + ")>0))=0 THEN 1 ELSE 0 END),0) usage_unknown,"
            "COALESCE(SUM(CASE WHEN cost_kind='actual' THEN cost ELSE 0 END),0) actual,"
            "COALESCE(SUM(CASE WHEN cost_kind='estimated' THEN cost ELSE 0 END),0) estimated "
            "FROM records WHERE " + where + " AND tool='tokenlens-ai'", args)
        pages = max(1, -(-int(internal["records"] or 0) // limit))
        offset = min(offset, (pages - 1) * limit)
        internal_rows = self.store.query("SELECT ts,model,requests,cost,cost_kind,cost_source,extra," + TOTAL_EXPR +
            " tokens FROM records WHERE " + where + " AND tool='tokenlens-ai' ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?",
            tuple(args) + (limit, offset))
        def usage_metadata(raw):
            try:
                value = json.loads(raw or "{}")
            except (ValueError, TypeError):
                return {}
            return value if isinstance(value, dict) else {}

        for record in internal_rows:
            metadata = usage_metadata(record.pop("extra"))
            record.update({k: metadata.get(k) for k in ("status", "operation", "purpose", "duration_ms", "provider")})
            record["status"] = metadata.get("outcome") or record["status"]
            record["purpose"] = metadata.get("request_role") or record["purpose"]
            if record["duration_ms"] is None:
                record["duration_ms"] = metadata.get("elapsed_ms")
            record["usage_known"] = bool(metadata.get("usage_known", record["cost_source"] != "usage-unavailable" and (record["cost_kind"] != "unknown" or record["tokens"] > 0)))
        internal["recent"] = internal_rows
        internal["offset"] = offset
        internal["limit"] = limit
        internal["pages"] = pages
        internal["page"] = min(internal["pages"], offset // limit + 1)
        internal["shown"] = len(internal_rows)
        # List lightweight summaries, so pagination can reach every saved bill
        # without loading each reconciliation's potentially large rows into Python.
        reports = self.store.query("SELECT id,name,created_at,json_array_length(report,'$.rows') rows,"
            "json_extract(report,'$.totals.billed') billed,json_extract(report,'$.totals.recorded') recorded,"
            "json_extract(report,'$.totals.difference') difference FROM reconciliations ORDER BY created_at DESC,rowid DESC")
        return {"ok": True, "range": rng, "summary": self._costs(rng, prices), "projects": self._project_rows(rng),
                "quality": quality, "prices": prices, "reconciliations": reports,
                "anomalies": self.anomalies(rng), "internal": internal,
                "price_audit": self.store.query("SELECT action,before_json,after_json,created_at FROM finance_audit WHERE action LIKE 'price_%' ORDER BY id DESC LIMIT 50")}

    def action(self, body):
        action = body.get("action")
        if action == "project_save":
            return self.save_project(body)
        if action == "price_save":
            return self.save_price(body)
        if action in ("reconcile_preview", "reconcile_import"):
            return self.reconcile(body, save=action == "reconcile_import")
        if action == "reconcile_detail":
            return self.reconciliation(str(body.get("id") or ""))
        if action in ("price_delete", "reconcile_delete"):
            table = "price_versions" if action == "price_delete" else "reconciliations"
            identifier = str(body.get("id") or "")
            old = self.store.query_one("SELECT * FROM " + table + " WHERE id=?", (identifier,))
            if not old:
                raise ValueError("记录不存在")
            self._write("DELETE FROM " + table + " WHERE id=?", (identifier,), (action, old, {}))
            return {"ok": True}
        raise ValueError("未知管理操作")
