"""Consistent read-only WAL snapshots for HTTP queries and streaming exports."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def export_snapshot(store):
    connection = sqlite3.connect(Path(store.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-4096")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA temp.cache_size=-2048")
        connection.execute("BEGIN")
        # Establish the snapshot before any aggregates or batches are requested.
        connection.execute("SELECT id FROM records LIMIT 1").fetchone()
    except BaseException:
        connection.close()
        raise
    class Snapshot:
        fts = store.fts
        version = store.version

        def conn(self):
            return connection

        @contextmanager
        def locked(self):
            # This connection belongs to one request; no shared writer lock is
            # needed for streaming rows or connection-local SQL functions.
            yield connection

        def query(self, sql, params=()):
            return [dict(row) for row in connection.execute(sql, params)]

        def iter_query(self, sql, params=()):
            for row in connection.execute(sql, params):
                yield dict(row)

        def query_one(self, sql, params=()):
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row is not None else {}

        def list_sources(self):
            return self.query("SELECT * FROM sources ORDER BY tokens DESC,name ASC")

        def last_scan_time(self):
            row = self.query_one("SELECT v FROM kv WHERE k='last_scan'")
            return int(row.get("v") or 0)
    try:
        yield Snapshot()
    finally:
        connection.close()
