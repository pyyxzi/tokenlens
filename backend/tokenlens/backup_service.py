"""Validated local data backups; credentials and machine settings never restore.

HTTP contract: ``export() -> (zip_bytes, filename)``, ``preview(base64_text)``
and ``restore(token)`` return JSON dictionaries. Browser prompt history may be
synced with ``save_history(list)`` before export; restore also returns the list.
The caller must map BackupError.status to its HTTP response status.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile

from .config import APP_VERSION
from .finance import Finance
from .store import SCHEMA_VERSION, Store


MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_HISTORY_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = ((MAX_ARCHIVE_BYTES + 2) // 3) * 4 + 4096
FORMAT = "tokenlens-backup"
FORMAT_VERSION = 1
PREVIEW_TTL = 15 * 60
MAX_PREVIEWS = 3
ENTRY_LIMITS = {"manifest.json": 64 * 1024, "database.sqlite": MAX_DATABASE_BYTES,
                "settings.json": 32 * 1024, "prompt_history.json": MAX_HISTORY_BYTES}
DATA_TABLES = ("records", "files", "sources", "kv", "schema_meta", "managed_projects",
               "price_versions", "finance_audit", "reconciliations", "native_event_sources")
SUPPORTED_SCHEMAS = frozenset((4, SCHEMA_VERSION))
FTS_TABLES = {"records_fts", "records_fts_data", "records_fts_idx",
              "records_fts_docsize", "records_fts_config"}
SAFE_SETTINGS = frozenset(("theme", "font", "density", "language", "range", "usd_to_cny",
                           "budget_tokens", "budget_cost", "budget_period", "warn_ratio",
                           "accent", "radius", "records_page", "list_cap", "default_range"))
HISTORY_FIELDS = frozenset(("t", "input", "optimized", "model", "mode", "elapsed_ms", "basis"))


class BackupError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _json_read(raw):
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BackupError("备份包含无效 JSON") from exc


def _atomic_write(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tokenlens-backup-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_history(value):
    """Copy only the browser's documented history fields, never profile objects."""
    if not isinstance(value, list) or len(value) > 20:
        raise BackupError("提示词历史须为最多 20 条的列表")
    clean = []
    for item in value:
        if not isinstance(item, dict) or set(item) - HISTORY_FIELDS:
            raise BackupError("提示词历史包含不支持的字段")
        row = {}
        for key, maximum in (("input", 4000), ("optimized", 20000), ("model", 500)):
            text = item.get(key, "")
            if not isinstance(text, str) or len(text) > maximum:
                raise BackupError("提示词历史文本超出长度限制")
            row[key] = text
        mode = item.get("mode", "polish")
        if mode not in ("polish", "structured"):
            raise BackupError("提示词历史模式无效")
        row["mode"] = mode
        basis = item.get("basis")
        if basis is not None:
            if not isinstance(basis, dict) or set(basis) - {"title", "summary", "principles", "references"}:
                raise BackupError("提示词历史依据字段无效")
            for field in ("title", "summary"):
                if not isinstance(basis.get(field, ""), str) or len(basis.get(field, "")) > 2000:
                    raise BackupError("提示词历史依据文本无效")
            principles, references = basis.get("principles", []), basis.get("references", [])
            if not isinstance(principles, list) or len(principles) > 20 or any(not isinstance(p, str) or len(p) > 2000 for p in principles):
                raise BackupError("提示词历史依据原则无效")
            if not isinstance(references, list) or len(references) > 20:
                raise BackupError("提示词历史依据链接无效")
            for reference in references:
                if not isinstance(reference, dict) or set(reference) != {"name", "url"} or any(not isinstance(v, str) or len(v) > 2000 for v in reference.values()):
                    raise BackupError("提示词历史依据链接无效")
                if not reference["url"].startswith("https://"):
                    raise BackupError("提示词历史依据仅支持 HTTPS 链接")
            row["basis"] = basis
        for key in ("t", "elapsed_ms"):
            number = item.get(key, 0)
            if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or not 0 <= number <= 1e16:
                raise BackupError("提示词历史时间无效")
            row[key] = number
        clean.append(row)
    if len(_json_bytes(clean)) > MAX_HISTORY_BYTES:
        raise BackupError("提示词历史超过 2 MiB")
    return clean


class BackupService:
    def __init__(self, app):
        self.app = app
        self.directory = os.path.dirname(os.path.abspath(app.store.path))
        self.history_path = os.path.join(self.directory, "prompt_history.json")
        self.backup_directory = os.path.join(self.directory, "backups")
        self._lock = threading.RLock()
        self._previews = {}

    def close(self):
        with self._lock:
            for pending in self._previews.values():
                pending["directory"].cleanup()
            self._previews.clear()

    def history(self):
        with self._lock:
            if not os.path.exists(self.history_path):
                return []
            if os.path.getsize(self.history_path) > MAX_HISTORY_BYTES:
                raise BackupError("本机提示词历史超过 2 MiB")
            return validate_history(_json_read(Path(self.history_path).read_bytes()))

    def save_history(self, records):
        with self._lock:
            clean = validate_history(records)
            _atomic_write(self.history_path, _json_bytes(clean))
            return {"ok": True, "prompt_history": clean, "count": len(clean)}

    def _settings(self):
        return {key: self.app.cfg.get(key) for key in sorted(SAFE_SETTINGS)}

    @staticmethod
    def _backup(source, target):
        """One stable SQLite backup, including committed pages still in WAL."""
        source.backup(target, pages=256)

    def _snapshot(self, path):
        with self.app.store.locked() as connection:
            if connection.in_transaction:
                raise BackupError("数据库正在写入，请稍后再备份", 409)
            size = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
            if size > MAX_DATABASE_BYTES:
                raise BackupError("数据库超过当前备份上限 128 MiB", 413)
            target = sqlite3.connect(path)
            try:
                self._backup(connection, target)
            finally:
                target.close()

    def _archive(self, db_path, history):
        assets = {"database.sqlite": Path(db_path).read_bytes(), "settings.json": _json_bytes(self._settings()),
                  "prompt_history.json": _json_bytes(history)}
        manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "app_version": APP_VERSION,
                    "schema_version": SCHEMA_VERSION, "created_at": int(time.time()),
                    "credentials_included": False,
                    "assets": {name: {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                               for name, raw in assets.items()}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", _json_bytes(manifest))
            for name, raw in assets.items():
                if len(raw) > ENTRY_LIMITS[name]:
                    raise BackupError("备份数据超过大小限制", 413)
                archive.writestr(name, raw)
        raw = buffer.getvalue()
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise BackupError("压缩备份超过当前上限 32 MiB", 413)
        return raw

    def export(self):
        with self._lock, tempfile.TemporaryDirectory(prefix="tokenlens-export-", dir=self.directory) as directory:
            path = os.path.join(directory, "snapshot.sqlite")
            self._snapshot(path)
            raw = self._archive(path, self.history())
            return raw, "tokenlens-backup-%s.zip" % dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    def _read_archive(self, encoded):
        if not isinstance(encoded, str) or len(encoded) > MAX_BODY_BYTES - 4096:
            raise BackupError("备份文件超过 32 MiB，或上传格式无效", 413)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BackupError("备份须为有效的 Base64 ZIP 文件") from exc
        if not raw or len(raw) > MAX_ARCHIVE_BYTES:
            raise BackupError("备份文件为空或超过 32 MiB", 413)
        try:
            with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
                entries = archive.infolist()
                if len(entries) != len(ENTRY_LIMITS) or {i.filename for i in entries} != set(ENTRY_LIMITS):
                    raise BackupError("备份文件列表无效；只允许指定数据文件")
                assets = {}
                for entry in entries:
                    mode = (entry.external_attr >> 16) & 0xFFFF
                    if (entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1 or
                            entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                        raise BackupError("备份包含不支持的 ZIP 条目")
                    if not 0 <= entry.file_size <= ENTRY_LIMITS[entry.filename]:
                        raise BackupError("备份解压后超过大小限制", 413)
                    with archive.open(entry, "r") as handle:
                        content = handle.read(ENTRY_LIMITS[entry.filename] + 1)
                    if len(content) != entry.file_size:
                        raise BackupError("备份条目长度不符")
                    assets[entry.filename] = content
        except (zipfile.BadZipFile, RuntimeError, EOFError, OSError) as exc:
            raise BackupError("无法读取备份 ZIP 文件") from exc
        manifest = _json_read(assets.pop("manifest.json"))
        if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or
                type(manifest.get("format_version")) is not int or manifest["format_version"] != FORMAT_VERSION or
                type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in SUPPORTED_SCHEMAS or
                manifest.get("credentials_included") is not False or
                not isinstance(manifest.get("assets"), dict) or set(manifest["assets"]) != set(assets)):
            raise BackupError("备份格式或数据库版本不兼容")
        for name, content in assets.items():
            metadata = manifest["assets"].get(name)
            if (not isinstance(metadata, dict) or metadata.get("size") != len(content) or
                    metadata.get("sha256") != hashlib.sha256(content).hexdigest()):
                raise BackupError("备份完整性校验失败")
        settings = _json_read(assets["settings.json"])
        if not isinstance(settings, dict) or set(settings) - SAFE_SETTINGS:
            raise BackupError("备份设置包含非允许字段；不能导入扫描路径或凭据")
        history = validate_history(_json_read(assets["prompt_history.json"]))
        if not assets["database.sqlite"].startswith(b"SQLite format 3\x00"):
            raise BackupError("备份数据库格式无效")
        return manifest, assets["database.sqlite"], history

    @staticmethod
    def _columns(connection, table):
        return [tuple(row)[1:] for row in connection.execute('PRAGMA table_xinfo("%s")' % table)]

    def _stage(self, raw, directory, schema_version=SCHEMA_VERSION):
        source_path = os.path.join(directory, "untrusted.sqlite")
        stage_path = os.path.join(directory, "staged.sqlite")
        Path(source_path).write_bytes(raw)
        stage = Store(stage_path)
        source = None
        try:
            Finance(stage, self.app.cfg)
            source = sqlite3.connect(Path(source_path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            source.execute("PRAGMA trusted_schema=OFF")
            source.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 60
            source.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            if hasattr(source, "setlimit"):
                source.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * 1024 * 1024)
                source.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
            # A hostile schema is never copied. Only known ordinary tables and
            # their values are imported into a freshly created, trusted schema.
            if schema_version not in SUPPORTED_SCHEMAS:
                raise BackupError("备份数据库 schema 版本不兼容")
            data_tables = tuple(table for table in DATA_TABLES
                                if schema_version >= 5 or table != "native_event_sources")
            objects = list(source.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema"))
            trusted_fts = {row[0]: " ".join((row[1] or "").lower().split()) for row in
                           stage._conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'")
                           if row[0] in FTS_TABLES}
            for kind, name, table, sql in objects:
                if kind in ("trigger", "view"):
                    raise BackupError("备份数据库包含不允许的触发器或视图")
                if kind == "table" and name not in set(data_tables) | FTS_TABLES | {"sqlite_sequence"}:
                    raise BackupError("备份数据库包含不允许的数据表")
                if kind == "table" and name in data_tables and "VIRTUAL" in (sql or "").upper():
                    raise BackupError("备份普通数据表不能为虚拟表")
                if kind == "table" and name in FTS_TABLES and trusted_fts and " ".join((sql or "").lower().split()) != trusted_fts.get(name):
                    raise BackupError("备份全文索引结构不兼容")
                if kind == "index" and table not in data_tables:
                    raise BackupError("备份数据库包含不允许的索引")
            names = {name for kind, name, _, _ in objects if kind == "table"}
            if not set(data_tables).issubset(names):
                raise BackupError("备份数据库缺少必要数据表")
            for table in data_tables:
                before, after = self._columns(source, table), self._columns(stage._conn, table)
                compatible_legacy_prices = table == "price_versions" and before == [column for column in after if column[0] != "rules"]
                if before != after and not compatible_legacy_prices:
                    raise BackupError("备份数据库表结构不兼容: " + table)
            version = source.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if not version or str(version[0]) != str(schema_version):
                raise BackupError("备份数据库 schema 版本不兼容")
            if [row[0] for row in source.execute("PRAGMA quick_check")] != ["ok"]:
                raise BackupError("备份数据库完整性检查失败")
            with stage.locked() as destination, destination:
                for table in data_tables:
                    destination.execute('DELETE FROM "%s"' % table)
                    columns = [row[1] for row in destination.execute('PRAGMA table_info("%s")' % table)]
                    source_columns = {row[1] for row in source.execute('PRAGMA table_info("%s")' % table)}
                    select_columns = ["'[]' AS rules" if table == "price_versions" and name == "rules" and name not in source_columns
                                      else '"%s"' % name for name in columns]
                    select = 'SELECT %s FROM "%s"' % (",".join(select_columns), table)
                    insert = 'INSERT INTO "%s" VALUES(%s)' % (table, ",".join("?" for _ in columns))
                    cursor = source.execute(select)
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        destination.executemany(insert, rows)
                if schema_version < 5:
                    # Run the same trusted migration as opening an existing
                    # index. A version-4 archive has no provenance table; its
                    # canonical native rows provide the initial source claims.
                    stage._migrate()
                # Imported path metadata is descriptive only. No config/profile
                # files enter the archive and no adapter is started by restore.
                destination.execute("DELETE FROM kv WHERE k NOT IN ('fts_ready','last_scan')")
                if stage.fts:
                    stage._fts_rebuild()
                    destination.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('fts_ready','1')")
                else:
                    destination.execute("DELETE FROM kv WHERE k='fts_ready'")
            if [row[0] for row in stage._conn.execute("PRAGMA quick_check")] != ["ok"]:
                raise BackupError("恢复暂存数据库完整性检查失败")
            self._validate_data(stage._conn)
            summary = {table: stage.query_one('SELECT COUNT(*) n FROM "%s"' % table)["n"]
                       for table in ("records", "managed_projects", "price_versions", "reconciliations", "finance_audit")}
            summary.update(stage.query_one("SELECT MIN(day) first_day,MAX(day) last_day,COALESCE(SUM(cost),0) cost_usd FROM records"))
            summary["internal_usage_records"] = stage.query_one("SELECT COUNT(*) n FROM records WHERE tool='tokenlens-ai'")["n"]
            fts = stage.fts
        except BackupError:
            raise
        except (sqlite3.Error, ValueError, OverflowError) as exc:
            raise BackupError("备份数据库校验失败，未更改本机数据") from exc
        finally:
            if source is not None:
                source.close()
            stage.close()
        os.unlink(source_path)
        return stage_path, fts, summary

    @staticmethod
    def _validate_data(connection):
        # SQLite's structural check cannot catch valid files whose business
        # values would break the dashboard or silently corrupt accounting.
        numeric = ("input", "output", "cache_read", "cache_write", "reasoning", "requests", "ts")
        invalid = " OR ".join('typeof("%s")<>\'integer\' OR "%s"<0' % (key, key) for key in numeric)
        invalid += (" OR typeof(cost) NOT IN ('integer','real') OR cost<0 OR cost>1.7976931348623157e308"
                    " OR currency IS NULL OR currency<>'USD' OR cost_kind IS NULL OR cost_kind NOT IN ('actual','estimated','free','unknown')"
                    " OR (cost_kind IN ('free','unknown') AND cost<>0)")
        if connection.execute("SELECT 1 FROM records WHERE " + invalid + " LIMIT 1").fetchone():
            raise BackupError("备份记录包含无效 token 数量或成本")
        # Mirror the live record contract, including calendar dates and JSON.
        # PRAGMA quick_check only proves SQLite structure; it cannot validate
        # application data or stop integer SUM overflow after restoration.
        from .models import (UsageRecord, ROW_COLUMNS, day_of, RECORD_TEXT_LIMITS,
                             MAX_RECORD_EXTRA_BYTES, MAX_USAGE_COUNT, MAX_USAGE_COST)
        native_prefix = "codex-response-"
        for tool, event_id, src in connection.execute(
                "SELECT tool,event_id,src FROM native_event_sources"):
            if (tool != "codex" or not isinstance(event_id, str)
                    or not event_id.startswith(native_prefix)
                    or len(event_id) != len(native_prefix) + 64
                    or any(char not in "0123456789abcdef" for char in event_id[len(native_prefix):])
                    or not isinstance(src, str) or not src or len(src) > RECORD_TEXT_LIMITS["src"]):
                raise BackupError("备份原生用量来源关系无效")
        if connection.execute(
                "SELECT 1 FROM native_event_sources s WHERE "
                "NOT EXISTS(SELECT 1 FROM files f WHERE f.path=s.src AND f.tool=s.tool) OR "
                "NOT EXISTS(SELECT 1 FROM records r WHERE r.tool=s.tool AND r.event_id=s.event_id) LIMIT 1"
                ).fetchone():
            raise BackupError("备份原生用量来源关系缺少对应记录或文件")
        if connection.execute(
                "SELECT 1 FROM records r WHERE r.tool='codex' AND r.event_id LIKE 'codex-response-%' "
                "AND NOT EXISTS(SELECT 1 FROM native_event_sources s "
                "WHERE s.tool=r.tool AND s.event_id=r.event_id AND s.src=r.src) LIMIT 1"
                ).fetchone():
            raise BackupError("备份原生用量记录缺少来源关系")
        token_total = request_total = 0
        cost_total = 0.0
        fields = [name for name in ROW_COLUMNS if name != "day"]
        for values in connection.execute("SELECT " + ",".join(ROW_COLUMNS) + " FROM records"):
            stored = dict(zip(ROW_COLUMNS, values))
            for name, limit in RECORD_TEXT_LIMITS.items():
                value = stored[name]
                if value is not None and (not isinstance(value, str) or len(value) > limit):
                    raise BackupError("备份记录文本字段无效或超出长度限制")
                stored[name] = value or ""
            raw_extra = stored["extra"]
            if not isinstance(raw_extra, str) or len(raw_extra.encode("utf-8")) > MAX_RECORD_EXTRA_BYTES:
                raise BackupError("备份记录元数据无效或过大")
            stored["extra"] = _json_read(raw_extra.encode("utf-8")) if raw_extra else {}
            try:
                record = UsageRecord(**{name: stored[name] for name in fields}).validate()
                imported_day = dt.date.fromisoformat(stored["day"])
                local_day = dt.date.fromisoformat(day_of(record.ts))
                # Backups can come from another local time zone. Their valid
                # calendar day can differ by one day at midnight boundaries.
                if (record.ts <= 0 or imported_day.isoformat() != stored["day"] or
                        abs((imported_day - local_day).days) > 1):
                    raise ValueError("record day does not match timestamp")
            except (ValueError, TypeError, OverflowError, OSError) as exc:
                raise BackupError("备份记录日期或元数据无效") from exc
            token_total += record.total
            request_total += record.requests
            cost_total += record.cost
            if (token_total > MAX_USAGE_COUNT or request_total > MAX_USAGE_COUNT
                    or not math.isfinite(cost_total) or cost_total > MAX_USAGE_COST):
                raise BackupError("备份汇总数量或成本超出支持范围")
        from .pricing import validate_context_rules
        from .finance import day, amount
        for prices, source, rules, effective in connection.execute("SELECT prices,source,rules,effective_from FROM price_versions"):
            try:
                day(effective)
            except (ValueError, TypeError):
                raise BackupError("备份价格版本日期无效")
            value = _json_read(prices.encode("utf-8")) if isinstance(prices, str) else None
            partial_reference = bool(isinstance(source,str) and source.startswith(("assistant-reference:","assistant-official:")))
            if not isinstance(value, list) or len(value) != 4 or any(
                    not (partial_reference and index >= 2 and number is None) and (
                    isinstance(number, bool) or not isinstance(number, (int, float)) or
                    not math.isfinite(number) or not 0 <= number <= 1e12) for index, number in enumerate(value)):
                raise BackupError("备份价格版本数据无效")
            try:
                parsed_rules = _json_read(rules.encode("utf-8")) if isinstance(rules, str) else None
                validate_context_rules(parsed_rules)
                if parsed_rules and not partial_reference:
                    raise ValueError("unverified rule source")
            except (ValueError, TypeError):
                raise BackupError("备份条件价格规则无效")
        owned = set()
        for paths, tags, budget in connection.execute("SELECT paths,tags,budget FROM managed_projects"):
            try:
                amount(budget)
            except (ValueError, TypeError, OverflowError):
                raise BackupError("备份项目预算无效")
            for index, value in enumerate((paths, tags)):
                parsed = _json_read(value.encode("utf-8")) if isinstance(value, str) else None
                maximum = 100 if index == 0 else 20
                length = 8192 if index == 0 else 40
                if (not isinstance(parsed, list) or len(parsed) > maximum or
                        any(not isinstance(item, str) or len(item) > length for item in parsed)):
                    raise BackupError("备份项目数据无效")
                if index == 0:
                    if not parsed or len(set(parsed)) != len(parsed) or owned.intersection(parsed):
                        raise BackupError("备份项目路径重复或归属冲突")
                    owned.update(parsed)
        for report, in connection.execute("SELECT report FROM reconciliations"):
            if not isinstance(report, str) or not isinstance(_json_read(report.encode("utf-8")), dict):
                raise BackupError("备份对账报告数据无效")
        # Administrative metadata is also returned as JSON and summed by the
        # scanner. A backup must not bypass the exact-integer usage boundary
        # through file/source counters even when records themselves are valid.
        for table, names in (("files", ("size", "records", "raw_records", "skipped_records")),
                             ("sources", ("size", "files", "records", "tokens", "raw_records",
                                          "accepted_records", "skipped_records"))):
            totals = [0] * len(names)
            for row in connection.execute("SELECT " + ",".join(names) + " FROM " + table):
                for index, value in enumerate(row):
                    value = 0 if value is None else value
                    if (isinstance(value, bool) or not isinstance(value, int)
                            or not 0 <= value <= MAX_USAGE_COUNT):
                        raise BackupError("备份来源统计数量无效")
                    totals[index] += value
                    if totals[index] > MAX_USAGE_COUNT:
                        raise BackupError("备份来源统计汇总数量超出支持范围")

    def _expire(self):
        now = time.monotonic()
        for token, pending in list(self._previews.items()):
            if now - pending["created"] > PREVIEW_TTL:
                pending["directory"].cleanup()
                del self._previews[token]

    def preview(self, archive):
        with self._lock:
            self._expire()
            if len(self._previews) >= MAX_PREVIEWS:
                raise BackupError("待恢复预览过多，请完成恢复或等待 15 分钟后重试", 409)
            manifest, database, history = self._read_archive(archive)
            directory = tempfile.TemporaryDirectory(prefix="tokenlens-restore-", dir=self.directory)
            try:
                path, fts, summary = self._stage(database, directory.name, manifest["schema_version"])
            except Exception:
                directory.cleanup()
                raise
            token = secrets.token_urlsafe(32)
            summary.update({"prompt_history": len(history), "created_at": manifest.get("created_at"),
                            "app_version": manifest.get("app_version"), "settings_restored": False,
                            "credentials_restored": False, "scan_paths_restored": False})
            self._previews[token] = {"directory": directory, "path": path, "fts": fts,
                                     "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                                     "history": history, "summary": summary, "created": time.monotonic()}
            return {"ok": True, "token": token, "expires_in": PREVIEW_TTL, "summary": summary}

    def _idle(self):
        state = self.app.scanner.state
        running = getattr(state, "running", None)
        if running is None:
            running = state.snapshot().get("running", False)
        if running or any(not task.get("ended_at") for task in self.app.tasks.public()):
            raise BackupError("请先等待扫描和 AI 任务结束，再执行恢复", 409)

    def _invalidate(self):
        self.app.store._usage_totals = None
        agg = getattr(self.app, "agg", None)
        if agg is not None:
            agg._memo_cache.clear()
            agg._rollup_rows = None
            agg._rollup_ver = None
        clear = getattr(self.app, "clear_dashboard_cache", None)
        if clear:
            clear()
        scanner = self.app.scanner
        if hasattr(scanner, "_side"):
            scanner._side = {}
        if hasattr(scanner, "_meta"):
            scanner._meta = None

    def restore(self, token):
        if not isinstance(token, str) or not token or len(token) > 100:
            raise BackupError("恢复预览凭据无效")
        with self._lock, ExitStack() as locks:
            self._expire()
            pending = self._previews.get(token)
            if not pending:
                raise BackupError("恢复预览已过期或已使用，请重新预览", 409)
            if hashlib.sha256(Path(pending["path"]).read_bytes()).hexdigest() != pending["sha256"]:
                raise BackupError("恢复暂存数据已改变，请重新预览", 409)
            # Holding the same locks used to start scans/tasks closes the race
            # between idle checking and applying the database. Keep this order.
            state_lock = getattr(self.app.scanner.state, "lock", None)
            if state_lock is not None:
                locks.enter_context(state_lock)
            locks.enter_context(self.app.tasks._lock)
            self._idle()
            store = self.app.store
            connection = locks.enter_context(store.locked())
            if connection.in_transaction:
                raise BackupError("数据库正在写入，请稍后恢复", 409)
            previous_path = os.path.join(pending["directory"].name, "previous.sqlite")
            self._snapshot(previous_path)
            previous_history = Path(self.history_path).read_bytes() if os.path.exists(self.history_path) else None
            recovery_name = "before-restore-%s-%s.zip" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), secrets.token_hex(4))
            recovery_path = os.path.join(self.backup_directory, recovery_name)
            _atomic_write(recovery_path, self._archive(previous_path, self.history()))
            original_fts = store.fts
            stage = sqlite3.connect(pending["path"])
            try:
                self._backup(stage, connection)
                _atomic_write(self.history_path, _json_bytes(pending["history"]))
                store.fts = pending["fts"]
                Store.version += 1
                self._invalidate()
            except Exception as exc:
                previous = sqlite3.connect(previous_path)
                try:
                    # Use SQLite directly so an injected apply/callback failure
                    # cannot also bypass the recovery copy through that hook.
                    previous.backup(connection)
                    if previous_history is None:
                        if os.path.exists(self.history_path):
                            os.unlink(self.history_path)
                    else:
                        _atomic_write(self.history_path, previous_history)
                    store.fts = original_fts
                    Store.version += 1
                    self._invalidate()
                except Exception as rollback_error:
                    raise BackupError("恢复失败，自动回滚未完成；恢复前备份已保留: " + recovery_name, 500) from rollback_error
                finally:
                    previous.close()
                raise BackupError("恢复失败，已自动回滚；恢复前备份已保留: " + recovery_name, 500) from exc
            finally:
                stage.close()
            result = {"ok": True, "restored": True, "summary": pending["summary"],
                      "prompt_history": pending["history"], "recovery_backup": recovery_name,
                      "settings_preserved": True, "credentials_preserved": True}
            del self._previews[token]
            pending["directory"].cleanup()
            return result
