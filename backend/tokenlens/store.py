# -*- coding: utf-8 -*-
"""
TokenLens · 本地增量索引库

首次扫描会把所有记录写入本地 SQLite；之后按 (路径, 大小, mtime) 判断文件是否变化，
只重解析变化的文件，因此重复扫描非常快。
"""
from __future__ import annotations
import datetime as dt

import os
import json
import math
import sqlite3
import threading
import time
import tempfile
from contextlib import contextmanager
from collections import OrderedDict
from typing import Iterable

from .models import UsageRecord, ROW_COLUMNS, infer_cost_metadata, MAX_USAGE_COUNT, MAX_USAGE_COST

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool        TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    day         TEXT NOT NULL,
    model       TEXT,
    project     TEXT,
    session     TEXT,
    input       INTEGER DEFAULT 0,
    output      INTEGER DEFAULT 0,
    cache_read  INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    reasoning   INTEGER DEFAULT 0,
    requests    INTEGER DEFAULT 1,
    cost        REAL    DEFAULT 0,
    src         TEXT,
    extra       TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,
    tool       TEXT,
    size       INTEGER,
    mtime      REAL,
    records    INTEGER,
    scanned_at INTEGER,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    vendor        TEXT,
    category      TEXT,
    color         TEXT,
    glyph         TEXT,
    status        TEXT,
    paths         TEXT,
    files         INTEGER,
    size          INTEGER,
    records       INTEGER,
    tokens        INTEGER,
    last_activity INTEGER,
    note          TEXT,
    elapsed       REAL,
    homepage      TEXT,
    updated_at    INTEGER,
    last_scan_at  INTEGER DEFAULT 0,
    error         TEXT,
    included      INTEGER DEFAULT 0,
    estimated     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- Native response ids survive copied/exported Codex logs. Keep every
-- validated source even though records stores one canonical event only.
CREATE TABLE IF NOT EXISTS native_event_sources (
    tool     TEXT NOT NULL,
    event_id TEXT NOT NULL,
    src      TEXT NOT NULL,
    PRIMARY KEY(tool, event_id, src)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_native_event_sources_src ON native_event_sources(src);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_records_tool ON records(tool);
CREATE INDEX IF NOT EXISTS idx_records_day ON records(day);
CREATE INDEX IF NOT EXISTS idx_records_day_tool ON records(day, tool);
CREATE INDEX IF NOT EXISTS idx_records_model ON records(model);
CREATE INDEX IF NOT EXISTS idx_records_day_model ON records(day, model);
CREATE INDEX IF NOT EXISTS idx_records_day_session ON records(day, session);
CREATE INDEX IF NOT EXISTS idx_records_session_ts ON records(session, ts DESC);
CREATE INDEX IF NOT EXISTS idx_records_src ON records(src);
CREATE UNIQUE INDEX IF NOT EXISTS idx_records_event ON records(tool, event_id)
    WHERE event_id <> '';
-- 明细流水按 ts 排序 + 按 day 过滤。没有这个索引时，30 万条就要 4 秒：
-- SQLite 得把整表读出来再排序。加上之后走索引有序扫描，翻页也稳定。
CREATE INDEX IF NOT EXISTS idx_records_day_ts_id ON records(day DESC, ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_records_tool_day_ts_id ON records(tool, day DESC, ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records(ts DESC);
CREATE INDEX IF NOT EXISTS idx_records_cost_id_day ON records(cost DESC, id DESC, day);
CREATE INDEX IF NOT EXISTS idx_records_total_id_day ON records(
  (input+output+cache_read+cache_write+reasoning) DESC, id DESC, day);
-- 覆盖索引：把「按天聚合」与「COUNT(DISTINCT ...)」要用的列都放进索引。
--
-- 为什么必须这么宽：summary() 里有一串 COUNT(DISTINCT tool/model/session/project)。
-- 只要索引没覆盖到这些列，SQLite 就会放弃索引、逐行回主表取列——
-- 实测 30 万条：窄覆盖索引 3121ms，宽覆盖索引 337ms（9.3 倍），代价是索引多占约 21MB。
-- 注意 DISTINCT 的列顺序按「基数从低到高」排（tool<model<session<project），
-- 这样前缀压缩效果最好。
CREATE INDEX IF NOT EXISTS idx_records_day_wide ON records(
  day, tool, model, session, project, ts,
  input, output, cache_read, cache_write, reasoning, requests, cost);
"""

SCHEMA_VERSION = 5

# 全文检索索引（需求 10）。
#
# 为什么是「外部内容表 + 批量维护」而不是触发器：
#   records 的写入模式是 replace_file() 的「先删该 src 的全部行，再批量插回」。
#   用 AFTER INSERT 触发器的话索引是**逐行**写的——实测 3700 行要 3121ms
#   （无索引基线只有 231ms，13 倍），扫描会肉眼可见地变慢。
#   换成在 replace_file 里追加两条语句、各自一次扫描：
#       INSERT INTO records_fts(records_fts, rowid, <cols>) SELECT 'delete', id, <cols> ... WHERE src=?
#       INSERT INTO records_fts(rowid, <cols>)               SELECT id, <cols>       ... WHERE src=?
#   实测同样的 3700 行只多花约 25ms，总耗时 303ms vs 无索引 326ms——落在噪声里。
#
# 为什么 tokenize=trigram：
#   unicode61 对中文等于不分词（实测 MATCH '统计' 命中 0 行），把「统计」当
#   一个整词存，中文子串检索完全失效。trigram 按三字符切分，正好匹配
#   「子串搜索」这个语义。
#
# trigram 的硬限制（已实测，不能假装不存在）：
#   它最短只切得出 3 个字符，所以 2 字中文词（统计 / 毕设 / 世界 / 服装）
#   MATCH 恒为 0 行——这是**静默错误**，比慢一点严重得多。
#   因此 search_clause() 会按词长分流：>=3 字符走 FTS，
#   更短的词一律回退 LIKE。宁可慢，不能少。
#
# 为什么只索引 model/project/session/tool 四列、不含 src：
#   这四列正是原来 4 列 LIKE 搜索的目标。src 是完整文件路径，最长且在界面上
#   不可见，把它也塞进索引会让索引膨胀、写入变慢（实测 4 列 3121ms vs
#   5 列 5362ms，触发器口径）。
FTS_COLUMNS = ("model", "project", "session", "tool")

# kv 表里的标记位：索引已经建好并且是全量的。不能靠「表存不存在」判断，
# 因为外部内容表可以在索引为空的情况下存在（见 _setup_fts 的注释）。
FTS_MARK = "fts_ready"

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    model, project, session, tool,
    content='records', content_rowid='id', tokenize=trigram);
"""


def fts_supported(conn) -> bool:
    """当前 SQLite 构建是否带 FTS5。缺了就整体退回 LIKE，不影响其它功能。"""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x, tokenize=trigram)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.Error:
        return False


# trigram 索引最短能切出 3 个字符。短于 3 的词交给 FTS 只会静默漏掉，
# 所以 3 是这个常量存在的唯一理由，改小没有意义。
FTS_MIN_CHARS = 3


def _fts_quote(term: str) -> str:
    """把一个词包成 FTS5 短语，短语内部的 " 要写成 "" 才不会被当成语法。"""
    return '"' + term.replace('"', '""') + '"'


def search_clause(q, fts=True):
    """把用户的搜索串编译成 SQL 片段 + 参数。

    语义是「多关键词取交集（AND），每个词在 4 个列里任意命中即可（OR）」。
    实测 `deepseek flash` 用原来的整串 LIKE 是 0 行（等于查不到），
    按词拆开才有意义。

    trigram 索引只认 >= 3 个字符的词，2 字中文词（统计 / 毕设 / 世界 / 服装）
    在索引里 MATCH 恒为 0 行。这是**静默漏结果**，比慢一点严重得多，
    所以短词一律退回 LIKE 全表扫描：宁可慢，不能少。

    返回 (sql_fragment, params)；没有有效关键词时返回 ("", [])。
    """
    # 保留原有的 % 和 _ 清洗：它们是 LIKE 的通配符，不能当普通字符搜。
    text = str(q or "").replace("%", "").replace("_", "").strip()
    if not text:
        return "", []
    terms = text.split()
    parts, params = [], []
    for term in terms:
        kw = "%%%s%%" % term
        if fts and len(term) >= FTS_MIN_CHARS:
            parts.append(
                "id IN (SELECT rowid FROM records_fts WHERE records_fts MATCH ?)")
            params.append(_fts_quote(term))
        else:
            parts.append("(model LIKE ? OR project LIKE ? OR session LIKE ? OR tool LIKE ?)")
            params.extend([kw, kw, kw, kw])
    if not parts:
        return "", []
    if len(parts) == 1:
        # 单个词不用再套一层括号，套了会变成 ((...))，读 SQL 日志时很干扰
        return parts[0], params
    return "(" + " AND ".join(parts) + ")", params


INSERT_SQL = (
    "INSERT INTO records (" + ",".join(ROW_COLUMNS) + ") VALUES ("
    + ",".join("?" * len(ROW_COLUMNS)) + ")"
)

_COST_COLUMNS = ("cost", "cost_kind", "cost_source", "price_version", "currency")
_USAGE_IDENTITY = ("tool", "ts", "session", "model", "input", "output", "cache_read",
                   "cache_write", "reasoning", "requests", "event_id")
_COST_MATCH_COLUMNS = _USAGE_IDENTITY[:-1]
_COST_MATCH_WHERE = " AND ".join(name + "=?" for name in _COST_MATCH_COLUMNS)
WRITE_BATCH_SIZE = 512
FILE_CURSOR_PREFIX = "file_cursor:"
_EVENT_INSERT_SQL = INSERT_SQL + " ON CONFLICT(tool,event_id) WHERE event_id<>'' DO NOTHING"
_EVENT_COPY_SQL = ("INSERT INTO records (" + ",".join(ROW_COLUMNS) + ") SELECT "
                   + ",".join(ROW_COLUMNS) + " FROM tokenlens_stage.records WHERE rowid>? AND rowid<=?"
                   " ON CONFLICT(tool,event_id) WHERE event_id<>'' DO NOTHING")


class StoreWriteCancelled(RuntimeError):
    """A cooperative cancellation left the previously committed snapshot intact."""


class _PreparedFile:
    """Own a disk-backed parse result without retaining UsageRecord objects."""

    def __init__(self, source_path, directory, seen, skipped):
        self.source_path = source_path
        self.directory = directory
        self.path = os.path.join(directory.name, "rows.sqlite")
        self.seen, self.skipped = seen, skipped

    def close(self):
        directory, self.directory = self.directory, None
        if directory is not None:
            directory.cleanup()

    def __del__(self):
        # Cooperative cancellation also closes queued results explicitly. This
        # fallback covers a caller dropping a prepared parse before publication.
        try:
            self.close()
        except OSError:
            pass


class Store:
    # 数据版本号：任何写库操作都会 +1，聚合层据此失效缓存
    version = 0

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._usage_totals = None
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("tokenlens_model_key", 1,
                                   lambda value: str(value or "").strip().lower(), deterministic=True)
        # 全文检索是否可用。老 SQLite 构建没有 FTS5，此时 fts 保持 False，
        # search_clause() 会整体退回 LIKE，功能不变、只是不再加速。
        self.fts = False
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.executescript(INDEX_SCHEMA)
            # The old mixed-direction index cannot satisfy descending day,
            # timestamp and id together. Keep only the complete stable order.
            self._conn.execute("DROP INDEX IF EXISTS idx_records_day_ts")
            # PRAGMA 必须在建索引之前执行：_setup_fts 会写 kv 标记位，
            # 开了事务之后 journal_mode / synchronous 就改不动了
            # （sqlite3 会报 "Safety level may not be changed inside a transaction"）。
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA temp_store=FILE")
            self._conn.execute("PRAGMA temp.cache_size=-2048")
            # 大库读性能：mmap 免掉聚合扫描时的内核拷贝；连接页缓存默认
            # 只有 2MB，GROUP BY 中间集反复换页是数十万行汇总变慢的主要
            # 来源之一。autocheckpoint 提高一档，整库重建时减少中途刷盘。
            self._conn.execute("PRAGMA mmap_size=268435456")
            self._conn.execute("PRAGMA cache_size=-32768")
            self._conn.execute("PRAGMA wal_autocheckpoint=2000")
            self._setup_fts()
            self._conn.commit()

    def _migrate(self):
        """Run small, idempotent schema migrations for existing indexes."""
        required = {
            "records": {
                "tool": "TEXT", "ts": "INTEGER DEFAULT 0", "day": "TEXT",
                "model": "TEXT", "project": "TEXT", "session": "TEXT",
                "input": "INTEGER DEFAULT 0", "output": "INTEGER DEFAULT 0",
                "cache_read": "INTEGER DEFAULT 0", "cache_write": "INTEGER DEFAULT 0",
                "reasoning": "INTEGER DEFAULT 0", "requests": "INTEGER DEFAULT 1",
                "cost": "REAL DEFAULT 0", "src": "TEXT", "extra": "TEXT",
                "cost_kind": "TEXT DEFAULT ''", "cost_source": "TEXT DEFAULT ''",
                "price_version": "TEXT DEFAULT ''", "currency": "TEXT DEFAULT 'USD'",
                "event_id": "TEXT DEFAULT ''",
            },
            "files": {"note": "TEXT", "fingerprint": "TEXT DEFAULT ''",
                      "raw_records": "INTEGER DEFAULT 0", "skipped_records": "INTEGER DEFAULT 0"},
            "sources": {"elapsed": "REAL DEFAULT 0", "homepage": "TEXT",
                        "updated_at": "INTEGER", "last_scan_at": "INTEGER DEFAULT 0",
                        "error": "TEXT", "included": "INTEGER DEFAULT 0",
                        "estimated": "INTEGER DEFAULT 0", "quality": "TEXT DEFAULT 'exact'",
                        "raw_records": "INTEGER DEFAULT 0", "accepted_records": "INTEGER DEFAULT 0",
                        "skipped_records": "INTEGER DEFAULT 0", "last_success_at": "INTEGER DEFAULT 0"},
        }
        for table, columns in required.items():
            existing = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(%s)" % table).fetchall()}
            for name, definition in columns.items():
                if name not in existing:
                    self._conn.execute("ALTER TABLE %s ADD COLUMN %s %s" %
                                       (table, name, definition))
        self._conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        version = int(row[0]) if row and str(row[0]).isdigit() else 0
        if version < 4:
            rows = self._conn.execute(
                "SELECT id,tool,model,cost,extra FROM records WHERE COALESCE(cost_kind,'')=''"
            )
            for record in rows:
                try:
                    extra = json.loads(record["extra"] or "{}")
                except (ValueError, TypeError):
                    extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                kind, source = infer_cost_metadata(record["tool"], record["model"], record["cost"], extra)
                self._conn.execute(
                    "UPDATE records SET cost_kind=?,cost_source=?,price_version=?,currency='USD',"
                    "cost=CASE WHEN ?='free' THEN 0 ELSE cost END WHERE id=?",
                    (kind, "legacy-inferred:" + source,
                     "legacy-unversioned" if kind == "estimated" else "", kind, record["id"]))
            self._conn.execute("UPDATE files SET raw_records=records")
            self._conn.execute("UPDATE sources SET accepted_records=records,raw_records=records,"
                               "last_success_at=CASE WHEN status='ok' THEN last_scan_at ELSE 0 END")
            self._conn.execute("DELETE FROM kv WHERE k=?", (FTS_MARK,))
        if version < 5:
            self._conn.execute(
                "INSERT OR IGNORE INTO native_event_sources(tool,event_id,src) "
                "SELECT tool,event_id,src FROM records WHERE tool='codex' "
                "AND event_id LIKE 'codex-response-%' AND COALESCE(src,'')<>''")
        # Version 1 records the migration framework. Future columns should be
        # added here with ALTER TABLE and guarded by PRAGMA table_info.
        if version < SCHEMA_VERSION:
            self._conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def maybe_vacuum(self, *, reclaim=False):
        """Checkpoint scans cheaply; full compaction is explicit maintenance.

        Free pages are reused by the next scan. Automatically rewriting a
        multi-gigabyte index (then rebuilding unchanged FTS a second time) held
        every local writer hostage while AI jobs were trying to finish. PASSIVE
        checkpoints also respect active WAL snapshots instead of waiting for
        exports/readers as TRUNCATE did.
        """
        if not self._lock.acquire(blocking=False):
            return
        try:
            try:
                if not reclaim:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    return
                page_count = int(self._conn.execute("PRAGMA page_count").fetchone()[0])
                free = int(self._conn.execute("PRAGMA freelist_count").fetchone()[0])
                if page_count > 1000 and free / max(page_count, 1) >= 0.25:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    self._conn.execute("VACUUM")
                    # INTEGER PRIMARY KEY ids survive VACUUM. External-content
                    # FTS maps those stable ids and needs no expensive rebuild.
            except sqlite3.Error:
                pass
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # 全文检索索引
    # ------------------------------------------------------------------
    def _setup_fts(self):
        """建索引表；如果是老库（索引还是空的）就一次性补齐。

        这里必须同步做完，不能「先标成可用、回头再补」。理由：外部内容表的
        索引是独立存储的，表建好了但没 rebuild 时 MATCH 会**静默返回 0 行**
        ——用户搜「统计」看到「没有结果」，比搜索慢几秒严重得多。
        代价是升级后首次启动要重建一次：实测 38361 行 973ms，之后
        replace_file 会增量维护，不会再触发。
        """
        if not fts_supported(self._conn):
            self.fts = False
            return
        self._conn.execute(FTS_SCHEMA)
        row = self._conn.execute(
            "SELECT v FROM kv WHERE k=?", (FTS_MARK,)).fetchone()
        if row and row["v"] == "1":
            self.fts = True
            return
        self._fts_rebuild()
        self._conn.execute(
            "INSERT INTO kv(k,v) VALUES(?, '1') "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (FTS_MARK,))
        self.fts = True

    def _fts_rebuild(self):
        """按 records 全量重建索引。"""
        self._conn.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")

    def _fts_forget(self, where_sql, params):
        """按 records 里的行集合删掉索引条目。

        外部内容表不会自己感知 records 的删除，必须显式发 'delete' 命令，
        并把**旧值**一起传过去，否则残留在索引里的 rowid 会指向不存在的行。
        """
        if not self.fts:
            return
        cols = ",".join(FTS_COLUMNS)
        self._conn.execute(
            "INSERT INTO records_fts(records_fts, rowid, %s) "
            "SELECT 'delete', id, %s FROM records WHERE %s" % (cols, cols, where_sql),
            params)

    def _fts_add_where(self, where_sql, params):
        """把 records 里符合条件的行加进索引（一次扫描，不是逐行）。"""
        if not self.fts:
            return
        cols = ",".join(FTS_COLUMNS)
        self._conn.execute(
            "INSERT INTO records_fts(rowid, %s) SELECT id, %s FROM records WHERE %s"
            % (cols, cols, where_sql), params)

    # ------------------------------------------------------------------
    def _commit(self):
        """提交并推进数据版本号（聚合缓存据此失效）。"""
        self._conn.commit()
        # _commit is also used after direct administrative SQL updates.
        self._usage_totals = None
        Store.version += 1

    @contextmanager
    def _transaction(self):
        """Called under _lock. FTS, records and file state share one atomic commit."""
        outer_transaction = self._conn.in_transaction
        previous_totals = self._usage_totals
        self._conn.execute("SAVEPOINT tokenlens_write")
        try:
            yield
            self._conn.execute("RELEASE SAVEPOINT tokenlens_write")
        except BaseException:
            self._usage_totals = previous_totals
            # SQLITE_FULL/IOERR can automatically roll back the entire SQLite
            # transaction, removing all savepoints. Cleanup must never replace
            # that original error with "no such savepoint".
            if self._conn.in_transaction:
                try:
                    self._conn.execute("ROLLBACK TO SAVEPOINT tokenlens_write")
                    self._conn.execute("RELEASE SAVEPOINT tokenlens_write")
                except sqlite3.Error:
                    # If the savepoint vanished or cleanup itself failed, avoid
                    # leaving uncommitted writes for a later operation to commit.
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
            raise
        if not outer_transaction:
            Store.version += 1

    def conn(self):
        return self._conn

    def _read_usage_totals(self, where="", params=()):
        # TOTAL does not raise before we can validate a malicious integer SUM.
        # Normal totals below 2**53 are exact and stay entirely inside SQLite.
        # Only an exceptionally large boundary value needs Python integers.
        clause = " WHERE " + where if where else ""
        row = self._conn.execute("SELECT TOTAL(input+output+cache_read+cache_write+reasoning),"
                                 "TOTAL(requests),TOTAL(cost) FROM records" + clause, params).fetchone()
        if not math.isfinite(row[2]) or row[2] > MAX_USAGE_COST:
            raise ValueError("indexed cost total exceeds the supported finite range; previous snapshot retained")
        if all(math.isfinite(value) and value < 2**53 for value in row[:2]):
            return (int(row[0]), int(row[1]), float(row[2]))
        tokens = requests = 0
        cursor = self._conn.execute("SELECT input,output,cache_read,cache_write,reasoning,requests "
                                    "FROM records" + clause, params)
        for item in cursor:
            tokens += sum(int(value or 0) for value in item[:5])
            requests += int(item[5] or 0)
            if tokens > MAX_USAGE_COUNT or requests > MAX_USAGE_COUNT:
                raise ValueError("indexed usage totals exceed the supported integer range; previous snapshot retained")
        return tokens, requests, float(row[2])

    def _adjust_usage_totals(self, added, removed=(0, 0, 0.0)):
        if self._usage_totals is None:
            # Call before changing records to establish the committed baseline.
            self._usage_totals = self._read_usage_totals()
        counts = tuple(before + plus - minus for before, plus, minus in
                       zip(self._usage_totals[:2], added[:2], removed[:2]))
        if any(value < 0 or value > MAX_USAGE_COUNT for value in counts):
            raise ValueError("indexed usage totals exceed the supported integer range; previous snapshot retained")
        try:
            cost = math.fsum((self._usage_totals[2], -removed[2], added[2]))
        except OverflowError as exc:
            raise ValueError("indexed cost total exceeds the supported finite range; previous snapshot retained") from exc
        if not math.isfinite(cost) or cost > MAX_USAGE_COST:
            raise ValueError("indexed cost total exceeds the supported finite range; previous snapshot retained")
        self._usage_totals = (*counts, max(0.0, cost))

    @contextmanager
    def locked(self):
        """所有对 sqlite3 连接的访问都必须走这里：同一连接不能跨线程并发使用。"""
        with self._lock:
            yield self._conn

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params)]

    def iter_query(self, sql, params=()):
        with self._lock:
            cursor = self._conn.execute(sql, params)
            try:
                for row in cursor:
                    yield dict(row)
            finally:
                cursor.close()

    def query_one(self, sql, params=()):
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else {}

    def close(self):
        with self._lock:
            try:
                self._conn.commit()
                # 退出前更新查询计划统计，让下一次启动的大范围汇总直接
                # 用上准确的索引选择（PRAGMA optimize 开销毫秒级）。
                self._conn.execute("PRAGMA optimize")
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 文件新鲜度
    # ------------------------------------------------------------------
    def file_state(self, path: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime, records, fingerprint, raw_records, skipped_records FROM files WHERE path=?", (path,)
            ).fetchone()
            if row is None:
                return None
            cursor = self._conn.execute("SELECT v FROM kv WHERE k=?", (FILE_CURSOR_PREFIX + path,)).fetchone()
            return {**dict(row), "parse_cursor": cursor[0] if cursor else ""}

    def file_states(self, paths):
        """批量读取文件新鲜度，避免扫描时每个文件往返 SQLite 两次。"""
        unique = list(dict.fromkeys(p for p in paths if p))
        out = {}
        if not unique:
            return out
        with self._lock:
            # SQLite 默认参数上限通常是 999，留足余量以兼容不同构建。
            for i in range(0, len(unique), 800):
                chunk = unique[i:i + 800]
                marks = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    "SELECT path, size, mtime, records, fingerprint, raw_records, skipped_records,"
                    "COALESCE((SELECT v FROM kv WHERE k='file_cursor:'||files.path),'') parse_cursor "
                    "FROM files WHERE path IN (" + marks + ")",
                    chunk).fetchall()
                out.update({r["path"]: dict(r) for r in rows})
        return out

    def is_fresh(self, path: str, size: int, mtime: float) -> bool:
        row = self.file_state(path)
        if not row:
            return False
        return int(row["size"]) == int(size) and abs(float(row["mtime"]) - float(mtime)) < 1e-6

    def known_records(self, path: str) -> int:
        row = self.file_state(path)
        return int(row["records"]) if row else 0

    def file_event_exists(self, tool: str, event_id: str, path: str) -> bool:
        """Skip replays in this file without losing provenance in other copies."""
        with self._lock:
            if tool == "codex" and event_id.startswith("codex-response-"):
                row = self._conn.execute(
                    "SELECT 1 FROM native_event_sources WHERE tool=? AND event_id=? AND src=?",
                    (tool, event_id, path)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM records WHERE tool=? AND event_id=? AND event_id<>''",
                    (tool, event_id)).fetchone()
            return row is not None

    def _record_native_sources(self, stage, path, should_cancel):
        pending = stage.execute("SELECT tool,event_id FROM records WHERE tool='codex' "
                                "AND event_id LIKE 'codex-response-%'")
        while True:
            batch = pending.fetchmany(WRITE_BATCH_SIZE)
            if not batch:
                break
            self._check_cancel(should_cancel)
            self._conn.executemany(
                "INSERT OR IGNORE INTO native_event_sources(tool,event_id,src) VALUES(?,?,?)",
                ((row[0], row[1], path) for row in batch))

    def _release_native_sources(self, paths, should_cancel=None):
        """Transfer shared events before an old file snapshot is retired.

        Removing a copy must not remove the single canonical row while another
        validated copy still contains it. IDs, prices and FTS identities remain
        unchanged; the temporary destination set stays on disk for large scans.
        """
        for offset in range(0, len(paths), 800):
            self._check_cancel(should_cancel)
            chunk = paths[offset:offset + 800]
            marks = ",".join("?" for _ in chunk)
            self._conn.execute("DELETE FROM native_event_sources WHERE src IN (" + marks + ")", chunk)
        self._conn.execute("CREATE TEMP TABLE tokenlens_transfer_files(path TEXT PRIMARY KEY) WITHOUT ROWID")
        try:
            for offset in range(0, len(paths), 800):
                self._check_cancel(should_cancel)
                chunk = paths[offset:offset + 800]
                marks = ",".join("?" for _ in chunk)
                destination = ("SELECT MIN(s.src) FROM native_event_sources s "
                               "JOIN files f ON f.path=s.src AND f.tool=s.tool "
                               "WHERE s.tool=records.tool AND s.event_id=records.event_id")
                where = ("src IN (" + marks + ") AND tool='codex' "
                         "AND event_id LIKE 'codex-response-%' AND (" + destination + ") IS NOT NULL")
                self._conn.execute(
                    "INSERT OR IGNORE INTO tokenlens_transfer_files SELECT (" + destination + ") "
                    "FROM records WHERE " + where, chunk)
                self._conn.execute("UPDATE records SET src=(" + destination + ") WHERE " + where, chunk)
            # Copies previously counted as globally skipped now own these rows.
            self._conn.execute(
                "UPDATE files SET records=(SELECT COUNT(*) FROM records WHERE src=files.path),"
                "skipped_records=MAX(0,raw_records-(SELECT COUNT(*) FROM records WHERE src=files.path)) "
                "WHERE path IN (SELECT path FROM tokenlens_transfer_files)")
        finally:
            self._conn.execute("DROP TABLE temp.tokenlens_transfer_files")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    @staticmethod
    def _usage_identity(row):
        # Project display identities can change during parser migrations. Source,
        # timestamp, session and the complete usage breakdown identify the event.
        return tuple((str(row.get(name) or "").strip().lower() if name == "model"
                      else row.get(name) or (0 if name in ("input", "output", "cache_read",
                                                           "cache_write", "reasoning", "requests", "ts") else ""))
                     for name in _USAGE_IDENTITY)

    @staticmethod
    def _check_cancel(should_cancel):
        if should_cancel is not None and should_cancel():
            raise StoreWriteCancelled("file write cancelled; previous snapshot retained")

    def _cost_snapshot(self, path, should_cancel):
        """An indexed SQLite temp table replaces the unbounded Python old-row map."""
        old = self._conn.execute("SELECT id FROM records WHERE src=? ORDER BY id", (path,))
        batch = old.fetchmany(WRITE_BATCH_SIZE)
        if not batch:
            # New files have no historical prices to match. Avoid both the temp
            # table and several otherwise useless lookup queries for every row.
            return False
        self._conn.execute("DROP TABLE IF EXISTS temp.tokenlens_cost_snapshot")
        self._conn.execute("CREATE TEMP TABLE tokenlens_cost_snapshot ("
                           "id INTEGER PRIMARY KEY," + ",".join(_COST_MATCH_COLUMNS) + ","
                           "project TEXT,event_id TEXT,cost REAL,cost_kind TEXT,"
                           "cost_source TEXT,price_version TEXT,currency TEXT)")
        expressions = ["tokenlens_model_key(model)" if name == "model" else
                       "COALESCE(" + name + "," + ("0" if name in ("ts", "input", "output", "cache_read", "cache_write", "reasoning", "requests") else "''") + ")"
                       for name in _COST_MATCH_COLUMNS + ("project", "event_id")]
        copy_sql = "INSERT INTO tokenlens_cost_snapshot SELECT id," + ",".join(expressions + list(_COST_COLUMNS)) + " FROM records WHERE src=? AND id>=? AND id<=?"
        while batch:
            self._check_cancel(should_cancel)
            self._conn.execute(copy_sql, (path, batch[0][0], batch[-1][0]))
            batch = old.fetchmany(WRITE_BATCH_SIZE)
        self._conn.execute("CREATE INDEX temp.tokenlens_cost_match ON tokenlens_cost_snapshot(" + ",".join(_COST_MATCH_COLUMNS) + ",project,event_id,id)")
        return True

    def _price_lookup(self, model, day, cache):
        key = (model, day)
        if key in cache:
            return cache[key]
        if self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_versions'").fetchone():
            version = self._conn.execute("SELECT * FROM price_versions WHERE model=? AND effective_from<=? "
                                         "ORDER BY effective_from DESC,rowid DESC LIMIT 1", key).fetchone()
            exists = version is not None or self._conn.execute("SELECT 1 FROM price_versions WHERE model=? LIMIT 1", (model,)).fetchone() is not None
        else:
            version, exists = None, False
        result = (dict(version) if version else None, exists)
        cache[key] = result
        if len(cache) > 256:
            cache.popitem(last=False)
        return result

    def _priced_rows(self, rows, snapshot=False, price_cache=None):
        """Price a bounded batch; only exact matched events inherit saved costs."""
        cache = price_cache if price_cache is not None else OrderedDict()
        result = []
        for row in rows:
            values = dict(zip(ROW_COLUMNS, row))
            old = None
            if snapshot:
                identity = self._usage_identity(values)[:-1]
                select = "SELECT * FROM tokenlens_cost_snapshot WHERE " + _COST_MATCH_WHERE
                old = self._conn.execute(select + " AND project=? AND event_id=? ORDER BY id LIMIT 1",
                                         identity + (values["project"] or "", values["event_id"] or "")).fetchone()
                if old is None and values["event_id"]:
                    # Parser upgrades introduce stable event ids without changing
                    # the underlying usage; preserve legacy empty-id snapshots.
                    old = self._conn.execute(select + " AND project=? AND event_id='' ORDER BY id LIMIT 1",
                                             identity + (values["project"] or "",)).fetchone()
                if old is None:
                    # Parser upgrades can change even nonempty event ids. An
                    # unchanged usage identity within the same project retains
                    # its saved price, consuming one old row per occurrence.
                    old = self._conn.execute(select + " AND project=? ORDER BY id LIMIT 1",
                                             identity + (values["project"] or "",)).fetchone()
                if old is None:
                    candidates = self._conn.execute(select + " ORDER BY id LIMIT 2", identity).fetchall()
                    if len(candidates) == 1:
                        short = candidates[0]["project"] or ""
                        full = str(values["project"] or "").replace(chr(92), "/").rstrip("/")
                        if short and "/" not in short and chr(92) not in short and full.rsplit("/", 1)[-1].casefold() == short.casefold():
                            old = candidates[0]
            if old is not None:
                self._conn.execute("DELETE FROM tokenlens_cost_snapshot WHERE id=?", (old["id"],))
                for name in _COST_COLUMNS:
                    values[name] = old[name]
            elif values["cost_kind"] in ("estimated", "unknown") and values["cost_source"] != "usage-unavailable":
                model = str(values["model"] or "").strip().lower()
                version, exists = self._price_lookup(model, values["day"], cache)
                usage = (values["input"], values["output"] + values["reasoning"], values["cache_read"], values["cache_write"])
                if version and version["source"] == "legacy-override:unpriced":
                    values.update(cost=0, cost_kind="unknown", cost_source="unpriced", price_version=version["id"], currency="USD")
                elif version:
                    from .pricing import price_usage
                    prices = json.loads(version["prices"])
                    columns = set(version.keys()) if hasattr(version, "keys") else set()
                    rules = json.loads(version["rules"] or "[]") if "rules" in columns else []
                    cost = price_usage(usage, prices, rules, request_time=dt.datetime.fromtimestamp(values["ts"], dt.timezone.utc))
                    if cost is not None and (not math.isfinite(cost) or cost < 0):
                        raise ValueError("versioned usage cost must be finite and non-negative")
                    values.update(cost=cost if cost is not None else 0, cost_kind="estimated" if cost is not None else "unknown",
                                  cost_source="price-version" if cost is not None else "unpriced",
                                  price_version=version["id"], currency="USD")
                elif exists:
                    from . import pricing
                    prices = pricing.builtin_price(model)
                    values.update(cost=(sum(n * p for n, p in zip(usage, prices)) / 1_000_000 if prices else 0),
                                  cost_kind="estimated" if prices else "unknown", cost_source="price-table" if prices else "unpriced",
                                  price_version=pricing.PRICE_VERSION if prices else "", currency="USD")
            result.append(tuple(values[name] for name in ROW_COLUMNS))
        return result

    def prepare_file(self, path, records, should_cancel=None):
        # Parsing and validation happen outside the shared store lock. A private
        # disk-backed staging database bounds Python/SQLite cache memory and lets
        # readers keep using the previous committed snapshot while parsing.
        directory = tempfile.TemporaryDirectory(prefix=".tokenlens-stage-", dir=os.path.dirname(os.path.abspath(self.path)))
        try:
            stage = sqlite3.connect(os.path.join(directory.name, "rows.sqlite"))
            try:
                stage.execute("PRAGMA journal_mode=OFF")
                stage.execute("PRAGMA synchronous=OFF")
                stage.execute("PRAGMA cache_size=-2048")
                stage.execute("CREATE TABLE records (" + ",".join(ROW_COLUMNS) + ")")
                stage.execute("CREATE UNIQUE INDEX stage_events ON records(tool,event_id) WHERE event_id<>''")
                batch, seen, skipped = [], 0, 0
                for record in records:
                    self._check_cancel(should_cancel)
                    seen += 1
                    record.src = path
                    row = record.as_row()
                    if record.ts <= 0:
                        skipped += 1
                        continue
                    batch.append(row)
                    if len(batch) >= WRITE_BATCH_SIZE:
                        before = stage.total_changes
                        stage.executemany(_EVENT_INSERT_SQL, batch)
                        skipped += len(batch) - (stage.total_changes - before)
                        batch.clear()
                if batch:
                    before = stage.total_changes
                    stage.executemany(_EVENT_INSERT_SQL, batch)
                    skipped += len(batch) - (stage.total_changes - before)
                    batch.clear()
                stage.commit()
                self._check_cancel(should_cancel)
                return _PreparedFile(path, directory, seen, skipped)
            finally:
                stage.close()
        except BaseException:
            directory.cleanup()
            raise

    @contextmanager
    def _stage_file(self, path, records, should_cancel):
        prepared = records if isinstance(records, _PreparedFile) else self.prepare_file(path, records, should_cancel)
        if prepared.source_path != path or prepared.directory is None:
            raise ValueError("prepared parse does not belong to this source file")
        stage = None
        try:
            self._check_cancel(should_cancel)
            stage = sqlite3.connect(prepared.path)
            stage.execute("PRAGMA cache_size=-2048")
            yield stage, prepared.seen, prepared.skipped
        finally:
            if stage is not None:
                stage.close()
            prepared.close()

    @contextmanager
    def _attached_stage(self, stage):
        """Publish unmodified staged rows within SQLite without Python copies."""
        if self._conn.in_transaction:
            # Dependent sources publish several files within an outer savepoint.
            # SQLite cannot detach a database used by an active transaction;
            # keep those nested writes on the ordinary bounded batch path.
            yield False
            return
        stage_path = stage.execute("PRAGMA database_list").fetchone()[2]
        self._conn.execute("ATTACH DATABASE ? AS tokenlens_stage", (stage_path,))
        try:
            self._conn.execute("PRAGMA tokenlens_stage.cache_size=-2048")
            yield True
        except BaseException:
            try:
                self._conn.execute("DETACH DATABASE tokenlens_stage")
            except sqlite3.Error:
                # Preserve the publication error. A failed transaction cleanup
                # may still hold the attachment; roll it back and retry detach.
                try:
                    self._conn.rollback()
                    self._conn.execute("DETACH DATABASE tokenlens_stage")
                except sqlite3.Error:
                    pass
            raise
        else:
            self._conn.execute("DETACH DATABASE tokenlens_stage")

    def _write_file(self, path, tool, size, mtime, records, note, fingerprint,
                    raw_records, skipped_records, parse_cursor, should_cancel,
                    append=False, expected_fingerprint=None):
        with self._stage_file(path, records, should_cancel) as (stage, seen, rejected):
            # Parsers finalize this mutable dictionary as their iterator ends.
            cursor_json = json.dumps(parse_cursor, ensure_ascii=False, allow_nan=False) if parse_cursor is not None else None
            accepted = 0
            if append and fingerprint and cursor_json is not None:
                with self._lock:
                    current = self.file_state(path)
                    if (current and current["fingerprint"] == fingerprint
                            and current["size"] == int(size) and current["mtime"] == float(mtime)
                            and current["parse_cursor"] == cursor_json):
                        return 0
            with self._lock, self._attached_stage(stage) as stage_attached, self._transaction():
                if self._usage_totals is None:
                    self._usage_totals = self._read_usage_totals()
                old_state = self._conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
                if expected_fingerprint is not None and (not old_state or old_state["fingerprint"] != expected_fingerprint):
                    raise ValueError("file state changed before append; retry a full scan")
                if append and not old_state:
                    raise ValueError("append requires an existing file snapshot")
                self._check_cancel(should_cancel)
                snapshot = False
                if not append:
                    snapshot = self._cost_snapshot(path, should_cancel)
                    self._release_native_sources([path], should_cancel)
                removed_totals = self._read_usage_totals("src=?", (path,)) if not append else (0, 0, 0.0)
                if not append:
                    self._fts_forget("src=?", (path,))
                    self._conn.execute("DELETE FROM records WHERE src=?", (path,))
                # Only newly inserted ids enter FTS; replayed event ids are ignored.
                first_id = self._conn.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0]
                price_table = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_versions'").fetchone()
                custom_prices = price_table and self._conn.execute("SELECT 1 FROM price_versions LIMIT 1").fetchone()
                if stage_attached and not snapshot and not custom_prices:
                    last_rowid = stage.execute("SELECT COALESCE(MAX(rowid),0) FROM records").fetchone()[0]
                    for offset in range(0, last_rowid, WRITE_BATCH_SIZE):
                        self._check_cancel(should_cancel)
                        end = min(offset + WRITE_BATCH_SIZE, last_rowid)
                        before = self._conn.total_changes
                        self._conn.execute(_EVENT_COPY_SQL, (offset, end))
                        count = self._conn.total_changes - before
                        accepted += count
                        rejected += end - offset - count
                else:
                    pending = stage.execute("SELECT " + ",".join(ROW_COLUMNS) + " FROM records ORDER BY rowid")
                    cache = OrderedDict()
                    while True:
                        batch = pending.fetchmany(WRITE_BATCH_SIZE)
                        if not batch:
                            break
                        self._check_cancel(should_cancel)
                        priced = self._priced_rows(batch, snapshot=snapshot, price_cache=cache)
                        before = self._conn.total_changes
                        self._conn.executemany(_EVENT_INSERT_SQL, priced)
                        count = self._conn.total_changes - before
                        accepted += count
                        rejected += len(priced) - count
                self._record_native_sources(stage, path, should_cancel)
                added_totals = self._read_usage_totals("src=? AND id>?", (path, first_id))
                self._adjust_usage_totals(added_totals, removed_totals)
                self._fts_add_where("src=? AND id>?", (path, first_id))
                self._check_cancel(should_cancel)
                total = (int(old_state["records"] or 0) if append else 0) + accepted
                raw = (int(old_state["raw_records"] or 0) if append else 0) + max(seen, int(raw_records if raw_records is not None else seen))
                skipped = (int(old_state["skipped_records"] or 0) if append else 0) + int(skipped_records) + rejected
                self._conn.execute("INSERT INTO files(path,tool,size,mtime,records,scanned_at,note,fingerprint,raw_records,skipped_records) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET tool=excluded.tool,size=excluded.size,mtime=excluded.mtime,"
                    "records=excluded.records,scanned_at=excluded.scanned_at,note=excluded.note,fingerprint=excluded.fingerprint,"
                    "raw_records=excluded.raw_records,skipped_records=excluded.skipped_records",
                    (path, tool, int(size), float(mtime), total, int(time.time()), note, fingerprint, raw, skipped))
                if cursor_json is None:
                    self._conn.execute("DELETE FROM kv WHERE k=?", (FILE_CURSOR_PREFIX + path,))
                else:
                    self._conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (FILE_CURSOR_PREFIX + path, cursor_json))
                if snapshot:
                    self._conn.execute("DROP TABLE temp.tokenlens_cost_snapshot")
                self._check_cancel(should_cancel)
            return accepted

    def replace_file(self, path: str, tool: str, size: int, mtime: float,
                     records: Iterable[UsageRecord], note: str = "", fingerprint: str = "",
                     raw_records=None, skipped_records=0, *, parse_cursor=None, should_cancel=None,
                     expected_fingerprint=None) -> int:
        """Atomically publish a bounded, staged full replacement; return accepted rows."""
        return self._write_file(path, tool, size, mtime, records, note, fingerprint,
                                raw_records, skipped_records, parse_cursor, should_cancel,
                                expected_fingerprint=expected_fingerprint)

    def append_file(self, path: str, tool: str, size: int, mtime: float,
                    records: Iterable[UsageRecord], note: str = "", fingerprint: str = "",
                    raw_records=None, skipped_records=0, *, parse_cursor=None,
                    expected_fingerprint=None, should_cancel=None) -> int:
        """Atomically append a verified suffix; event_id replays are idempotent.

        raw_records/skipped_records describe this suffix only. Metadata counters
        accumulate; return value is the count of genuinely inserted records.
        expected_fingerprint guards the parser's base snapshot against concurrent writes.
        """
        return self._write_file(path, tool, size, mtime, records, note, fingerprint,
                                raw_records, skipped_records, parse_cursor, should_cancel,
                                append=True, expected_fingerprint=expected_fingerprint)

    def append_usage(self, record: UsageRecord, event_id: str = None) -> int:
        """Append internal usage; an optional event_id makes retries idempotent.

        No files row is created, so filesystem scans cannot remove these events.
        """
        record.tool = record.tool or "tokenlens-ai"
        record.src = ("internal://tokenlens-ai" if record.tool == "tokenlens-ai"
                      else record.src or "internal://" + record.tool)
        record.event_id = str(event_id or record.event_id or "")
        row = record.as_row()
        if record.ts <= 0:
            raise ValueError("internal usage requires a valid timestamp")
        with self._lock, self._transaction():
            if self._usage_totals is None:
                self._usage_totals = self._read_usage_totals()
            if record.event_id:
                old = self._conn.execute("SELECT id FROM records WHERE tool=? AND event_id=?",
                                         (record.tool, record.event_id)).fetchone()
                if old:
                    return int(old[0])
            row = self._priced_rows([row])[0]
            self._adjust_usage_totals((record.total, record.requests, row[ROW_COLUMNS.index("cost")]))
            cur = self._conn.execute(INSERT_SQL, row)
            ident = int(cur.lastrowid)
            self._fts_add_where("id=?", (ident,))
            if record.tool == "tokenlens-ai":
                totals = self._conn.execute(
                    "SELECT COUNT(*),SUM(input+output+cache_read+cache_write+reasoning),MAX(ts),"
                    "SUM(CASE WHEN cost_kind='estimated' THEN 1 ELSE 0 END),"
                    "SUM(CASE WHEN cost_source='usage-unavailable' THEN 1 ELSE 0 END) "
                    "FROM records WHERE tool='tokenlens-ai'").fetchone()
                stamp = int(time.time())
                self._conn.execute(
                    "INSERT INTO sources(id,name,vendor,category,color,glyph,status,paths,files,size,"
                    "records,tokens,last_activity,note,elapsed,homepage,updated_at,last_scan_at,error,"
                    "included,estimated,quality,raw_records,accepted_records,skipped_records,last_success_at) "
                    "VALUES('tokenlens-ai','TokenLens AI','Configured provider','api','#38bdf8','AI',"
                    "'ok','[]',0,0,?,?,?,'Internal model calls; reported usage only',0,'',?,?,"
                    "'',1,?,'exact',?,?,0,?) ON CONFLICT(id) DO UPDATE SET "
                    "records=excluded.records,tokens=excluded.tokens,last_activity=excluded.last_activity,"
                    "updated_at=excluded.updated_at,last_scan_at=excluded.last_scan_at,estimated=excluded.estimated,"
                    "raw_records=excluded.raw_records,accepted_records=excluded.accepted_records,"
                    "last_success_at=excluded.last_success_at,status='ok',included=1,error=''",
                    (totals[0], totals[1], totals[2], stamp, stamp, int(bool(totals[3])),
                     totals[0], totals[0], stamp))
                if totals[4]:
                    self._conn.execute(
                        "UPDATE sources SET status='partial',note=? WHERE id='tokenlens-ai'",
                        ("Internal model calls; %d call(s) did not report token usage" % totals[4],))
        return ident

    # 真实账单来源：这些工具的 cost 是网关实收（读自官方账单/DB/日志），
    # 重算估算成本时必须整工具跳过，否则会把真金白银的数字改掉。
    # 其余来源的 cost 全部是 estimate_cost 写入的快照。
    BILLING_SOURCES = ("claude-desktop", "opencode", "cline")

    def recalc_costs(self, model: str, price, price_version=None, cost_source=None,
                     cost_kind=None) -> int:
        """Revalue estimates only. Actual charges and local/free usage are immutable."""
        from . import pricing
        i, o, cr, cw = (float(v) for v in price)
        if any(not math.isfinite(v) or v < 0 for v in (i, o, cr, cw)):
            raise ValueError("prices must be finite and non-negative")
        kind = cost_kind or ("estimated" if pricing.is_known(model) or any((i, o, cr, cw)) else "unknown")
        if kind not in ("estimated", "unknown"):
            raise ValueError("recalculation only supports estimated/unknown costs")
        version = price_version or getattr(pricing, "PRICE_VERSION", "builtin-2026-09")
        source = cost_source or ("custom-price" if pricing.has_custom_price(model) else "price-table")
        with self._lock, self._transaction():
            cur = self._conn.execute(
                "UPDATE records SET cost = "
                "(input*? + (output+reasoning)*? + cache_read*? + cache_write*?) / 1000000.0,"
                "cost_kind=?,cost_source=?,price_version=? "
                "WHERE lower(trim(model))=? AND cost_kind IN ('estimated','unknown')",
                (i, o, cr, cw, kind, source if kind == "estimated" else "unpriced",
                 version if kind == "estimated" else "", str(model or "").strip().lower()))
            # A finite unit rate can overflow when multiplied by indexed
            # usage. Validate the complete new total before publishing it.
            self._usage_totals = self._read_usage_totals()
            return cur.rowcount

    def touch_file(self, path: str):
        with self._lock:
            self._conn.execute(
                "UPDATE files SET scanned_at=? WHERE path=?", (int(time.time()), path)
            )
            self._commit()

    def drop_missing_files(self, alive_paths: set):
        """清理已经消失的文件对应的记录。"""
        with self._lock, self._transaction():
            self._usage_totals = None
            rows = self._conn.execute("SELECT path FROM files").fetchall()
            dead = [r["path"] for r in rows if r["path"] not in alive_paths]
            if not dead:
                return 0
            self._release_native_sources(dead)
            cur = self._conn.cursor()
            # 索引条目必须在 records 消失前按旧值撤销，所以先分批清索引再删行。
            for i in range(0, len(dead), 800):
                chunk = dead[i:i + 800]
                marks = ",".join("?" for _ in chunk)
                self._fts_forget("src IN (" + marks + ")", chunk)
            for p in dead:
                cur.execute("DELETE FROM records WHERE src=?", (p,))
                cur.execute("DELETE FROM files WHERE path=?", (p,))
                cur.execute("DELETE FROM kv WHERE k=?", (FILE_CURSOR_PREFIX + p,))
            return len(dead)

    def drop_missing_files_for_tool(self, alive_paths: set, tool: str):
        """单个来源重扫时，只清理该来源已经消失的文件。

        全量扫描使用 :meth:`drop_missing_files`，而单源扫描不能把其他适配器
        暂时未枚举到的文件误判为删除；按 ``files.tool`` 限定范围可以保持两者
        的删除语义一致，同时保留其它来源的索引。
        """
        alive = set(alive_paths or ())
        with self._lock, self._transaction():
            self._usage_totals = None
            rows = self._conn.execute(
                "SELECT path FROM files WHERE tool=?", (tool,)).fetchall()
            dead = [r["path"] for r in rows if r["path"] not in alive]
            if not dead:
                return 0
            self._release_native_sources(dead)
            cur = self._conn.cursor()
            for i in range(0, len(dead), 800):
                chunk = dead[i:i + 800]
                marks = ",".join("?" for _ in chunk)
                self._fts_forget("src IN (" + marks + ")", chunk)
            for p in dead:
                cur.execute("DELETE FROM records WHERE src=?", (p,))
                cur.execute("DELETE FROM files WHERE path=? AND tool=?", (p, tool))
                cur.execute("DELETE FROM kv WHERE k=?", (FILE_CURSOR_PREFIX + p,))
            return len(dead)

    def purge_tool(self, tool: str):
        with self._lock, self._transaction():
            self._usage_totals = None
            self._fts_forget("tool=?", (tool,))
            self._conn.execute("DELETE FROM native_event_sources WHERE tool=?", (tool,))
            self._conn.execute("DELETE FROM records WHERE tool=?", (tool,))
            self._conn.execute("DELETE FROM kv WHERE k IN (SELECT 'file_cursor:'||path FROM files WHERE tool=?)", (tool,))
            self._conn.execute("DELETE FROM files WHERE tool=?", (tool,))

    def reset(self):
        """Clear reconstructable file indexes, retaining locally owned AI usage."""
        with self._lock, self._transaction():
            self._usage_totals = None
            indexed = "COALESCE(src,'') <> 'internal://tokenlens-ai'"
            self._fts_forget(indexed, ())
            self._conn.execute("DELETE FROM records WHERE " + indexed)
            self._conn.execute("DELETE FROM native_event_sources")
            self._conn.execute("DELETE FROM files")
            self._conn.execute("DELETE FROM kv WHERE k LIKE 'file_cursor:%'")
            self._conn.execute("DELETE FROM sources WHERE id <> 'tokenlens-ai'")
            self._conn.execute("DELETE FROM kv WHERE k='last_scan'")

    # ------------------------------------------------------------------
    # 数据源报告
    # ------------------------------------------------------------------
    def save_source(self, rep, tokens: int, size: int, files: int, records: int):
        data = rep.as_dict()
        data.update(tokens=int(tokens), size=int(size), files=int(files), records=int(records),
                    paths=json.dumps(rep.paths, ensure_ascii=False), updated_at=int(time.time()),
                    included=int(bool(rep.included)), estimated=int(bool(rep.estimated)))
        columns = list(data)
        with self._lock, self._transaction():
            self._conn.execute(
                "INSERT INTO sources(" + ",".join(columns) + ") VALUES(" +
                ",".join("?" for _ in columns) + ") ON CONFLICT(id) DO UPDATE SET " +
                ",".join(c + "=excluded." + c for c in columns if c != "id"),
                [data[c] for c in columns])

    def list_sources(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM sources ORDER BY tokens DESC, name ASC").fetchall()]

    def last_scan_time(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT v FROM kv WHERE k='last_scan'").fetchone()
        try:
            return int(row["v"]) if row else 0
        except Exception:
            return 0

    def set_scan_time(self, ts: int):
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(k,v) VALUES('last_scan',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(int(ts)),))
            self._commit()
