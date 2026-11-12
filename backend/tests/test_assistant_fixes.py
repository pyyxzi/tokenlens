"""Regression tests for the assistant / PromptLab audit fixes."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens.config import Config  # noqa: E402
from tokenlens.finance import Finance  # noqa: E402
from tokenlens.store import Store  # noqa: E402


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class PromptLabFingerprintTests(unittest.TestCase):
    def test_save_accepts_check_registered_with_resolved_protocol(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            from tokenlens.promptlab import PromptLab
            lab = PromptLab(Config(os.path.join(work, "config.json")))
            model, url, key = "gpt-4o", "https://api.openai.com/v1", "sk-test-1234"
            # /api/prompt/check resolves the wire protocol before registering.
            lab.remember_verified(model, url, key, "openai", False, False)
            result = lab.save(model, url, key, protocol="auto")
        self.assertNotIn("请先通过", result.get("error") or "")

    def test_availability_probe_cap_is_model_aware(self):
        from tokenlens.promptlab import _is_reasoning_model, _openai_reasoning_model, _probe_token_cap
        self.assertTrue(_is_reasoning_model("gpt-5.1"))
        self.assertTrue(_is_reasoning_model("o3-mini"))
        self.assertFalse(_is_reasoning_model("gpt-4o"))
        self.assertTrue(_openai_reasoning_model("o3"))
        self.assertFalse(_openai_reasoning_model("claude-sonnet-4"))
        self.assertGreater(_probe_token_cap("gpt-5"), _probe_token_cap("gpt-4o"))

    def test_optimize_cache_eviction_never_raises(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            from tokenlens.promptlab import PromptLab
            lab = PromptLab(Config(os.path.join(work, "config.json")))
            for i in range(40):
                lab._opt_cache_put("key-%d" % i, {"prompt": "p%d" % i})
            self.assertLessEqual(len(lab._opt_cache), 32)
            self.assertEqual(lab._opt_cache_get("key-39")[1]["prompt"], "p39")


class AssistantPathPrivacyTests(unittest.TestCase):
    def test_model_context_strips_project_paths(self):
        from tokenlens import assistant_analytics as aa
        snapshot = {
            "schema_version": 1, "range": "30d", "bounds": {}, "current": {}, "previous": {},
            "comparison": {},
            "filters": {"project_paths": '["C:/Users/alice/secret-client/project"]'},
            "limitations": [], "signals": [], "groups": {}, "daily": [], "evidence": [],
            "cost_breakdown": {}, "previous_cost_breakdown": {},
        }
        ctx = aa.model_context(snapshot)
        self.assertNotIn("project_paths", ctx.get("filters") or {})
        self.assertNotIn("secret-client", json.dumps(ctx, ensure_ascii=False))


class PriceEvidenceTests(unittest.TestCase):
    def test_model_version_is_not_price_evidence(self):
        from tokenlens import assistant_pricing as ap
        self.assertFalse(ap._has_number("Claude Sonnet 4.5 pricing: $3 input / $15 output", 4.5))
        self.assertTrue(ap._has_number("input $4.50 per million tokens", 4.5))


class EffectiveToTests(unittest.TestCase):
    def test_expired_price_version_is_not_active(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            cfg = Config(os.path.join(work, "config.json"))
            store = Store(os.path.join(work, "index.db"))
            fin = Finance(store, cfg)
            today = dt.date.today()
            yesterday = (today - dt.timedelta(days=1)).isoformat()
            fin.save_price({"model": "expired-model", "effective_from": "2020-01-01",
                            "effective_to": yesterday, "prices": [1, 2, 3, 4],
                            "source": "test"})
            self.assertNotIn("expired-model", fin.effective_prices())
            fin.save_price({"model": "active-model", "effective_from": "2020-01-01",
                            "effective_to": today.isoformat(), "prices": [1, 2, 3, 4],
                            "source": "test"})
            self.assertIn("active-model", fin.effective_prices())

    def test_end_before_start_is_rejected(self):
        with _tmp() as work:
            os.environ["TOKENLENS_HOME"] = work
            cfg = Config(os.path.join(work, "config.json"))
            store = Store(os.path.join(work, "index.db"))
            fin = Finance(store, cfg)
            with self.assertRaises(ValueError):
                fin.save_price({"model": "m", "effective_from": "2026-05-01",
                                "effective_to": "2026-04-01", "prices": [1, 2, 3, 4],
                                "source": "test"})


if __name__ == "__main__":
    unittest.main()
