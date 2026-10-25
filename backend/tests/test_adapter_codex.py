"""Synthetic-fixture tests for the Codex CLI adapter.

Covers the accounting rules from SOURCES.md and the 1.1.1 fix: per-response
ledgers (token_usage_record) are authoritative, forked/inherited threads are
skipped, and the cumulative token_count mirror is ignored once a ledger entry
was seen, so replayed histories never double count.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens.adapters import CodexAdapter  # noqa: E402


SESSION_META = {
    "timestamp": "2026-01-02T03:04:05Z",
    "type": "session_meta",
    "payload": {
        "id": "thread-owner-1",
        "session_id": "root-1",
        "cwd": "/home/al/proj",
        "model": "gpt-test-model",
        "model_provider": "openai",
        "cli_version": "0.5.0",
    },
}


def ledger(thread, response_id, usage, timestamp="2026-01-02T03:04:06Z"):
    return json.dumps({
        "timestamp": timestamp,
        "type": "token_usage_record",
        "payload": {"thread_id": thread, "response_id": response_id,
                    "turn_id": "t-" + response_id, "usage": usage},
    })


def mirror(last, total, timestamp="2026-01-02T03:04:07Z"):
    return json.dumps({
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "token_count",
                    "info": {"last_token_usage": last, "total_token_usage": total}},
    })


def usage(inp, cached, out, cw=0, reason=0):
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
            "cache_write_input_tokens": cw, "reasoning_output_tokens": reason}


class CodexLedgerTests(unittest.TestCase):
    def parse(self, lines):
        state = {}
        records = list(CodexAdapter(cfg=None).parse_lines(
            "rollout-2026-01-02T03-04-05-abcdef.jsonl", lines, state))
        return records, state

    def test_ledger_record_normalizes_and_subtracts_cached(self):
        records, state = self.parse([
            json.dumps(SESSION_META),
            ledger("thread-owner-1", "resp-1", usage(100, 40, 50, cw=10, reason=5)),
        ])
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.tool, "codex")
        self.assertEqual(r.session, "thread-owner-1")
        self.assertEqual(r.model, "gpt-test-model")
        self.assertEqual(r.project, "/home/al/proj")
        # Codex input_tokens already includes the cached subset.
        self.assertEqual(r.input, 60)
        self.assertEqual(r.cache_read, 40)
        self.assertEqual(r.output, 50)
        self.assertEqual(r.cache_write, 10)
        self.assertEqual(r.reasoning, 0)
        self.assertTrue(r.event_id.startswith("codex-response-"))
        self.assertEqual(r.extra["usage_source"], "token_usage_record")
        self.assertTrue(state.get("response_ledger"))
        r.validate()

    def test_inherited_thread_ledger_is_skipped(self):
        records, state = self.parse([
            json.dumps(SESSION_META),
            ledger("thread-other-2", "resp-x", usage(10, 0, 5)),
        ])
        self.assertEqual(records, [])
        self.assertTrue(state.get("inherited_usage_skipped"))

    def test_token_count_mirror_is_ignored_after_ledger(self):
        # The 1.1.1 regression: accepting the cumulative mirror after the
        # authoritative ledger double counts every native response.
        records, _ = self.parse([
            json.dumps(SESSION_META),
            ledger("thread-owner-1", "resp-1", usage(100, 0, 20)),
            mirror(last=usage(100, 0, 20), total=usage(100, 0, 20)),
        ])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].output, 20)

    def test_duplicate_ledger_response_replay_is_idempotent(self):
        lines = [
            json.dumps(SESSION_META),
            ledger("thread-owner-1", "resp-1", usage(80, 0, 30)),
            ledger("thread-owner-1", "resp-1", usage(80, 0, 30)),
        ]
        records, _ = self.parse(lines)
        self.assertEqual(len(records), 1)
        # A second pass over the same lines produces no new events either.
        records2, _ = self.parse(lines)
        self.assertEqual(records2[0].event_id, records[0].event_id)

    def test_malformed_ledger_usage_recorded_as_diagnostic(self):
        from tokenlens import adapters as _adapters
        records = []
        with _adapters.parsing_diagnostics() as issues:
            parsed, _state = self.parse([
                json.dumps(SESSION_META),
                ledger("thread-owner-1", "resp-bad", {"unknown_field": 1}),
            ])
            records.extend(parsed)
        self.assertEqual(records, [])
        self.assertTrue(issues)


class CodexLegacyTokenCountTests(unittest.TestCase):
    def parse(self, lines):
        state = {}
        return list(CodexAdapter(cfg=None).parse_lines(
            "rollout-2026-01-02T03-04-05-abcdef.jsonl", lines, state))

    def test_cumulative_deltas(self):
        records = self.parse([
            json.dumps(SESSION_META),
            mirror(last=usage(100, 0, 20), total=usage(100, 0, 20),
                   timestamp="2026-01-02T03:04:10Z"),
            mirror(last=usage(30, 0, 10), total=usage(130, 0, 30),
                   timestamp="2026-01-02T03:04:20Z"),
        ])
        self.assertEqual(len(records), 2)
        self.assertEqual((records[0].input, records[0].output), (100, 20))
        self.assertEqual((records[1].input, records[1].output), (30, 10))

    def test_replayed_cumulative_counter_is_not_recounted(self):
        records = self.parse([
            json.dumps(SESSION_META),
            mirror(last=usage(100, 0, 20), total=usage(100, 0, 20),
                   timestamp="2026-01-02T03:04:10Z"),
            # Same cumulative totals replayed (rewind or restart).
            mirror(last=usage(100, 0, 20), total=usage(100, 0, 20),
                   timestamp="2026-01-02T03:04:11Z"),
        ])
        self.assertEqual(len(records), 1)

    def test_input_includes_cached_no_double_count(self):
        records = self.parse([
            json.dumps(SESSION_META),
            mirror(last=usage(200, 150, 10), total=usage(200, 150, 10)),
        ])
        self.assertEqual(records[0].input, 50)
        self.assertEqual(records[0].cache_read, 150)


if __name__ == "__main__":
    unittest.main()
