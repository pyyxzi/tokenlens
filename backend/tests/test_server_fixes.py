"""Regression tests for the API/server audit fixes."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens.config import Config, db_path  # noqa: E402
from tokenlens.credential_safety import _forms, sanitize_public  # noqa: E402
from tokenlens.finance import Finance  # noqa: E402
from tokenlens.models import UsageRecord, now_ts  # noqa: E402
from tokenlens.server import App, RequestError, report_aggregator, request_filters  # noqa: E402
from tokenlens.store import Store  # noqa: E402
from service import secret_matches  # noqa: E402


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class LegacyPriceStartupTests(unittest.TestCase):
    def test_corrupt_legacy_price_file_does_not_block_startup(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            with open(os.path.join(work, "custom_prices.json"), "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            app = App(Config(os.path.join(work, "config.json")))
            try:
                marker = app.store.query_one(
                    "SELECT value FROM schema_meta WHERE key='legacy_custom_prices_migrated'")
            finally:
                app.backup.close()
                app.store.close()
        self.assertEqual(marker.get("value"), "1")


class CredentialRedactionTests(unittest.TestCase):
    def test_dict_keys_are_never_redacted(self):
        self.assertEqual(sanitize_public({"ok": False, "error": "bad"}, ["k"]),
                         {"ok": False, "error": "bad"})
        self.assertEqual(sanitize_public({"ok": False}, ["ok"]), {"ok": False})

    def test_short_secret_only_matches_whole_value(self):
        self.assertEqual(sanitize_public("model", ["k"]), "model")
        self.assertEqual(sanitize_public("k", ["k"]), "[redacted]")

    def test_long_secret_still_redacts_substrings_and_encodings(self):
        self.assertEqual(sanitize_public("prefix sk-abcdef suffix", ["sk-abcdef"]),
                         "prefix [redacted] suffix")
        encoded = "".join("%%%02X" % byte for byte in b"sk-abcdef")
        self.assertIn("[redacted]", sanitize_public("x" + encoded, ["sk-abcdef"]))

    def test_forms_accept_lone_surrogate(self):
        forms = _forms("\ud800")
        self.assertTrue(forms)
        self.assertEqual(sanitize_public("\ud800", ["\ud800"]), "[redacted]")


class FilterAndPathTests(unittest.TestCase):
    def test_project_paths_allow_large_filter(self):
        paths = ["C:/very/long/project/path/segment/%03d/" % i + "y" * 50 for i in range(100)]
        value = json.dumps(paths)
        self.assertGreater(len(value), 4096)
        self.assertEqual(request_filters({"project_paths": value})["project_paths"], value)

    def test_other_filters_stay_bounded(self):
        with self.assertRaises(RequestError):
            request_filters({"q": "x" * 5000})

    def test_db_path_follows_config_directory(self):
        with _tmp() as work:
            cfg = Config(os.path.join(work, "sub", "config.json"))
            self.assertEqual(db_path(cfg), os.path.join(work, "sub", "index.db"))


class ReportAggregatorTests(unittest.TestCase):
    def _seed(self, store, cfg):
        Finance(store, cfg)
        today = dt.date.today()
        month_start = today.replace(day=1)
        first = int(dt.datetime.combine(month_start, dt.time()).timestamp())
        mid = int(dt.datetime.combine(min(month_start + dt.timedelta(days=1), today),
                                      dt.time()).timestamp())
        for name, ts, tokens in (("m1", first, 100_000), ("m2", mid, 900_000), ("m3", now_ts(), 1_000)):
            store.replace_file("/%s.jsonl" % name, "codex", 1, ts,
                               [UsageRecord(tool="codex", ts=ts, model="gpt-5", session=name,
                                            input=tokens)], note="", fingerprint=name)

    def test_budget_override_uses_unprojected_totals(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            cfg = Config(os.path.join(work, "config.json"))
            cfg.data["budget_tokens"] = 1_000_000_000
            store = Store(os.path.join(work, "index.db"))
            self._seed(store, cfg)
            today = dt.date.today()
            month_total = __import__("tokenlens.aggregator", fromlist=["Aggregator"]) \
                .Aggregator(store, cfg).summary("%s~%s" % (today.replace(day=1), today))["total"]
            agg, _rng = report_aggregator(store, cfg,
                                          {"range": "1d", "cost_basis": "repriced_cost"})
            used = agg._budget_override["tokens"]["used"]
        self.assertEqual(used, month_total)
        self.assertGreater(used, 1_000)

    def test_range_all_with_to_gets_a_bounded_label(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            cfg = Config(os.path.join(work, "config.json"))
            store = Store(os.path.join(work, "index.db"))
            Finance(store, cfg)
            ts = now_ts()
            store.replace_file("/a.jsonl", "codex", 1, ts,
                               [UsageRecord(tool="codex", ts=ts, model="gpt-5", session="s", input=5)],
                               note="", fingerprint="a")
            _agg, rng = report_aggregator(store, cfg, {"range": "all", "to": dt.date.today().isoformat()})
        self.assertNotEqual(rng, "all")
        self.assertIn("~", rng)


class ResetRaceTests(unittest.TestCase):
    class FakeState:
        def __init__(self, running=True):
            self.running = running
            self.lock = threading.RLock()

    class FakeScanner:
        def __init__(self, stop=True):
            self.state = ResetRaceTests.FakeState()
            self.cancelled = False
            self._stop = stop

        def cancel(self):
            self.cancelled = True
            return True

        def wait(self, timeout=0):
            if self._stop:
                self.state.running = False
                return True
            return False

    def test_cancels_running_scan_then_resets(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            store = Store(os.path.join(work, "index.db"))
            scanner = self.FakeScanner(stop=True)
            App.reset_index(SimpleNamespace(scanner=scanner, store=store))
        self.assertTrue(scanner.cancelled)

    def test_refuses_when_scan_cannot_stop(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            store = Store(os.path.join(work, "index.db"))
            scanner = self.FakeScanner(stop=False)
            with self.assertRaises(RequestError):
                App.reset_index(SimpleNamespace(scanner=scanner, store=store))


class SecretHeaderTests(unittest.TestCase):
    def test_non_ascii_secret_is_rejected_without_raising(self):
        self.assertFalse(secret_matches(["\xff\xfe"], "a" * 64))
        self.assertTrue(secret_matches(["a" * 64], "a" * 64))
        self.assertFalse(secret_matches(["a" * 64, "a" * 64], "a" * 64))


if __name__ == "__main__":
    unittest.main()
