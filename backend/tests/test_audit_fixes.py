"""Regression tests for the audit fixes in adapters, store and scanner.

Each test pins one defect from problems.md so the accounting regressions
cannot come back: Cline cache-write double counting, Gemini snapshot loss,
Kimi mirror over-collapse, Aider timestamps, generic database token columns,
ApiLog bracketed lines, calendar date parsing, Codex cumulative rebounds,
Kimi default pricing, backup restore with sqlite_stat1, cost snapshot
overrides, model normalization, session detail identity, FTS marker trust,
volume availability, cancellation and unknown source validation.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens import adapters as A  # noqa: E402
from tokenlens.config import Config  # noqa: E402
from tokenlens.models import UsageRecord, day_of, to_epoch  # noqa: E402
from tokenlens.scanner import Scanner, _volume_available  # noqa: E402
from tokenlens.store import Store  # noqa: E402
from tokenlens.aggregator import Aggregator  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class AdapterAccountingTests(unittest.TestCase):
    def test_cline_cache_writes_are_not_double_counted(self):
        # Real Cline 3.82: tokensIn already includes cacheWrites + cacheReads.
        payload = {"tokensIn": 12138, "tokensOut": 149, "cacheWrites": 618,
                   "cacheReads": 11520, "cost": 0}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write(os.path.join(tmp, "ui_messages.json"), json.dumps([
                {"say": "api_req_started", "ts": 1767225600000, "text": json.dumps(payload)},
            ]))
            records = list(A.ClineFamilyAdapter(cfg={}).parse(path))
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.input, 0)
        self.assertEqual(r.cache_write, 618)
        self.assertEqual(r.cache_read, 11520)
        self.assertEqual(r.output, 149)
        self.assertEqual(r.total, 12138 + 149 - 618 - 11520 + 618 + 11520)
        # cost 0 from the client is "unknown", never a verified free bill.
        self.assertNotEqual(r.cost_kind, "actual")

    def test_cline_positive_cost_stays_actual(self):
        payload = {"tokensIn": 100, "tokensOut": 10, "cacheWrites": 0,
                   "cacheReads": 0, "cost": 0.42}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write(os.path.join(tmp, "ui_messages.json"), json.dumps([
                {"say": "api_req_started", "ts": 1767225600000, "text": json.dumps(payload)},
            ]))
            r = list(A.ClineFamilyAdapter(cfg={}).parse(path))[0]
        self.assertEqual(r.cost_kind, "actual")
        self.assertAlmostEqual(r.cost, 0.42)

    def test_kimi_growing_unscoped_turns_are_kept(self):
        rows = [
            {"sessionId": "s1", "timestamp": "2026-01-02T03:04:05Z", "model": "kimi-k2",
             "usage": {"input_tokens": 1000, "output_tokens": 200}},
            {"sessionId": "s1", "timestamp": "2026-01-02T03:05:05Z", "model": "kimi-k2",
             "usage": {"input_tokens": 2000, "output_tokens": 300}},
            {"sessionId": "s1", "timestamp": "2026-01-02T03:06:05Z", "model": "kimi-k2",
             "usage": {"input_tokens": 4000, "output_tokens": 400}},
        ]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write(os.path.join(tmp, "wire.jsonl"),
                         "\n".join(json.dumps(r) for r in rows) + "\n")
            adapter = A.KimiAdapter(cfg=None)
            adapter._session_lookup = {}
            records = list(adapter.parse(path))
        self.assertEqual(len(records), 3)
        self.assertEqual(sum(r.total for r in records), 7900)

    def test_aider_uses_heading_timestamps(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write(os.path.join(tmp, ".aider.chat.history.md"),
                         "#### 2026-01-02 10:00 gpt-4o\ntokens: 100 sent, 50 received\n\n"
                         "#### 2026-03-04 10:00 gpt-4o\ntokens: 200 sent, 60 received\n")
            os.utime(path, (1893456000, 1893456000))  # 2030 mtime must lose
            records = list(A.AiderAdapter(cfg=None).parse(path))
        self.assertEqual([day_of(r.ts) for r in records], ["2026-01-02", "2026-03-04"])

    def test_vendor_token_columns_ignore_cache_columns(self):
        adapter = A._VendorScanAdapter(cfg={})
        self.assertEqual(adapter._tok_cols([
            ("id", "INTEGER"), ("cache_read_input_tokens", "INTEGER"),
            ("prompt_tokens", "INTEGER"), ("completion_tokens", "INTEGER")]),
            ("prompt_tokens", "completion_tokens"))
        self.assertEqual(adapter._tok_cols([
            ("cached_input_tokens", "INTEGER"), ("output_tokens", "INTEGER")]), None)

    def test_apilog_bracketed_line_falls_back_to_text(self):
        line = ('[2026-01-02 03:04:05] POST /v1/chat usage: '
                '{"prompt_tokens":1000,"completion_tokens":50}\n')
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = write(os.path.join(tmp, "gateway.log"), line)
            records = list(A.ApiLogAdapter(cfg={}).parse(path))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].total, 1050)

    def test_calendar_dates_are_not_epoch_seconds(self):
        self.assertEqual(day_of(to_epoch("20260102")), "2026-01-02")
        self.assertEqual(day_of(to_epoch(20260102)), "2026-01-02")
        self.assertEqual(day_of(to_epoch("20260102030405")), "2026-01-02")
        self.assertEqual(to_epoch("1767225600"), 1767225600)

    def test_codex_cumulative_rebound_prefers_last_usage(self):
        def mirror(inp, out, total_in, total_out, ts):
            return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": inp, "cached_input_tokens": 0,
                                              "output_tokens": out},
                         "total_token_usage": {"input_tokens": total_in, "cached_input_tokens": 0,
                                               "output_tokens": total_out}}}})
        lines = [
            json.dumps({"type": "session_meta", "payload": {"id": "t", "cwd": "/p", "model": "m"}}),
            mirror(150000, 14992, 164992, 0, "2026-06-03T12:00:00Z"),
            mirror(30000, 334, 142905, 0, "2026-06-03T12:01:00Z"),
            mirror(30000, 334, 195326, 0, "2026-06-03T12:02:00Z"),
        ]
        records = list(A.CodexAdapter(cfg={}).parse_lines("x.jsonl", lines, {}))
        self.assertEqual([(r.input, r.output) for r in records],
                         [(150000, 14992), (30000, 334), (30000, 334)])

    def test_kimi_default_model_is_priced(self):
        from tokenlens.pricing import is_known
        self.assertTrue(is_known("kimi-for-coding"))


class BackupRestoreTests(unittest.TestCase):
    def _app(self, work):
        store = Store(os.path.join(work, "index.db"))
        cfg = Config(os.path.join(work, "config.json"))
        app = SimpleNamespace(cfg=cfg, store=store)
        from tokenlens.backup_service import BackupService
        from tokenlens.finance import Finance
        Finance(store, cfg)  # the real App initializes finance tables on startup
        app.backup = BackupService(app)
        return app

    def test_own_export_restores_after_close_creates_sqlite_stat(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            os.environ["TOKENLENS_HOME"] = work
            app = self._app(work)
            record = UsageRecord(tool="codex", ts=1767225600, model="gpt-5", project="/p",
                                 session="s", input=100, output=50, cost=0.01,
                                 cost_kind="estimated")
            app.store.replace_file("/tmp/x.jsonl", "codex", 100, 1767225600, [record],
                                   note="", fingerprint="f1")
            app.store.query("SELECT COUNT(*) FROM records")
            app.store.close()
            with sqlite3.connect(os.path.join(work, "index.db")) as con:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("sqlite_stat1", tables)  # precondition of the bug
            app.store = Store(os.path.join(work, "index.db"))
            raw, _name = app.backup.export()
            preview = app.backup.preview(base64.b64encode(raw).decode())
        self.assertTrue(preview.get("token"))

    def test_cost_snapshot_does_not_override_new_free_classification(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            os.environ["TOKENLENS_HOME"] = work
            store = Store(os.path.join(work, "index.db"))
            expensive = UsageRecord(tool="codex", ts=1767225600, model="gpt-5", project="/p",
                                    session="cost", input=100, cost=5.0, cost_kind="actual")
            store.replace_file("/e.jsonl", "codex", 1, 1767225600, [expensive], note="", fingerprint="e")
            free = UsageRecord(tool="codex", ts=1767225600, model="gpt-5", project="/p",
                               session="cost", input=100, cost=0.0, cost_kind="free")
            store.replace_file("/e.jsonl", "codex", 1, 1767225600, [free], note="", fingerprint="e2")
            row = store.query_one("SELECT cost, cost_kind FROM records WHERE session='cost'")
        self.assertEqual(row["cost_kind"], "free")
        self.assertEqual(row["cost"], 0)


class ModelNormalizationTests(unittest.TestCase):
    def _store(self, work):
        return Store(os.path.join(work, "index.db"))

    def test_model_filter_matches_grouping(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            os.environ["TOKENLENS_HOME"] = work
            store = self._store(work)
            cfg = Config(os.path.join(work, "config.json"))
            store.replace_file("/a.jsonl", "codex", 1, 1767225600,
                               [UsageRecord(tool="codex", ts=1767225600, model="m", session="s1", input=10)],
                               note="", fingerprint="a")
            store.replace_file("/b.jsonl", "codex", 1, 1767225600,
                               [UsageRecord(tool="codex", ts=1767225600, model="m\t", session="s2", input=100)],
                               note="", fingerprint="b")
            agg = Aggregator(store, cfg)
            total = sum(row["total"] for row in agg.by_model("all"))
            filtered = agg.filtered({"model": "m"}).summary("all")
            unknown = agg.filtered({"model": "未知模型"}).summary("all")
        self.assertEqual(total, 110)
        self.assertEqual(filtered["total"], 110)
        self.assertEqual(unknown["total"], 0)

    def test_session_detail_resolves_one_identity_and_merges_unknown_models(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            os.environ["TOKENLENS_HOME"] = work
            store = self._store(work)
            cfg = Config(os.path.join(work, "config.json"))
            store.replace_file("/a.jsonl", "codex", 1, 1767225600,
                               [UsageRecord(tool="codex", ts=1767225600, model="Model-A",
                                            project="/p1", session="shared", input=100)],
                               note="", fingerprint="a")
            store.replace_file("/b.jsonl", "claude-code", 1, 1767225600,
                               [UsageRecord(tool="claude-code", ts=1767225600, model="model-b",
                                            project="/p2", session="shared", input=7)],
                               note="", fingerprint="b")
            with store.locked() as con, store._transaction():
                for model, inp in (("", 1), (None, 4), ("model-a", 8)):
                    con.execute(
                        "INSERT INTO records(tool,ts,day,model,project,session,input,output,cache_read,"
                        "cache_write,reasoning,requests,cost,src,extra,cost_kind,cost_source,"
                        "price_version,currency,event_id) VALUES(?,?,?,?,?,?,?,0,0,0,0,1,0,'/x','',"
                        "'unknown','unpriced','','USD','')",
                        ("codex", 1767225600, "2026-01-01", model, "/p1", "shared", inp))
            agg = Aggregator(store, cfg)
            detail = agg.session_detail("shared")
        self.assertEqual(detail["tool"], "codex")
        self.assertEqual(detail["project"], "/p1")
        self.assertEqual(detail["total"], 100 + 1 + 4 + 8)
        labels = [m["model"] for m in detail["models"]]
        self.assertEqual(sorted(labels), ["model-a", "未知模型"])
        unknown = [m for m in detail["models"] if m["model"] == "未知模型"][0]
        self.assertEqual(unknown["total"], 5)


class FtsMarkerTests(unittest.TestCase):
    def test_marker_is_dropped_when_fts_unavailable(self):
        import tokenlens.store as S
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            os.environ["TOKENLENS_HOME"] = work
            db = os.path.join(work, "index.db")
            store = Store(db)
            store.replace_file("/a.jsonl", "codex", 1, 1767225600,
                               [UsageRecord(tool="codex", ts=1767225600, model="model-1", input=1)],
                               note="", fingerprint="a")
            store.close()
            real = S.fts_supported
            S.fts_supported = lambda conn: False
            try:
                store = Store(db)
                self.assertFalse(store.fts)
                store.replace_file("/b.jsonl", "codex", 1, 1767225600,
                                   [UsageRecord(tool="codex", ts=1767225600, model="model-2", input=1)],
                                   note="", fingerprint="b")
                store.close()
            finally:
                S.fts_supported = real
            store = Store(db)
            try:
                found = store.query_one(
                    """SELECT COUNT(*) n FROM records_fts WHERE records_fts MATCH '"model-2"'""")
                self.assertEqual(found["n"], 1)
            finally:
                store.close()


class VolumeAvailabilityTests(unittest.TestCase):
    def test_existing_path_is_available(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            self.assertTrue(_volume_available(os.path.join(work, "f.jsonl")))

    def test_missing_drive_is_unavailable(self):
        missing = [letter for letter in "ZYXWV" if not os.path.exists(letter + ":\\")]
        if not missing:
            self.skipTest("no unused drive letter available")
        self.assertFalse(_volume_available(missing[0] + ":\\logs\\rollout.jsonl"))


class ScannerModeTests(unittest.TestCase):
    def _codex_home(self, work):
        home = os.path.join(work, "codex")
        rollout = os.path.join(home, "sessions", "2026", "01", "01",
                               "rollout-2026-01-01T00-00-00-019d0000-0000-7000-8000-000000000001.jsonl")
        meta = {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
                "payload": {"id": "t1", "cwd": "C:\\demo", "model": "gpt-5"}}
        usage = {"timestamp": "2026-01-01T00:00:10Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 110, "cached_input_tokens": 0,
                                      "output_tokens": 0},
                "last_token_usage": {"input_tokens": 110, "cached_input_tokens": 0,
                                     "output_tokens": 0}}}}
        write(rollout, json.dumps(meta) + "\n" + json.dumps(usage) + "\n")
        with sqlite3.connect(os.path.join(home, "state_5.sqlite")) as con:
            con.executescript(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, model TEXT, tokens_used INTEGER,"
                " created_at INTEGER, updated_at INTEGER, title TEXT, source TEXT, cli_version TEXT);"
                "INSERT INTO threads VALUES ('t1','C:\\\\demo','gpt-5',500,1767225600,1767225600,'x','cli','1.0');")
        return home

    def _scan(self, cfg, store, scanner):
        scanner.start(force=False, source_id="codex")
        scanner.wait(timeout=60)
        return store.query_one(
            "SELECT COUNT(*) n, COALESCE(SUM(input+output+cache_read+cache_write+reasoning),0) tok"
            " FROM records WHERE tool='codex'")

    def test_quick_and_full_modes_replace_instead_of_stacking(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            home = self._codex_home(work)
            os.environ["TOKENLENS_HOME"] = work
            os.environ["CODEX_HOME"] = home
            cfg = Config(os.path.join(work, "config.json"))
            store = Store(os.path.join(work, "index.db"))
            scanner = Scanner(cfg, store)
            try:
                self.assertEqual(self._scan(cfg, store, scanner)["tok"], 110)
                cfg.set("codex_quick", True)
                self.assertEqual(self._scan(cfg, store, scanner)["tok"], 500)
                cfg.set("codex_quick", False)
                self.assertEqual(self._scan(cfg, store, scanner)["tok"], 110)
            finally:
                os.environ.pop("CODEX_HOME", None)

    def test_unknown_source_is_rejected_synchronously(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work:
            os.environ["TOKENLENS_HOME"] = work
            cfg = Config(os.path.join(work, "config.json"))
            store = Store(os.path.join(work, "index.db"))
            scanner = Scanner(cfg, store)
            with self.assertRaises(ValueError):
                scanner.start(force=False, source_id="definitely-not-a-source")


if __name__ == "__main__":
    unittest.main()
