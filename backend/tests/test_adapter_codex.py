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
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens.adapters import CodexAdapter  # noqa: E402

try:
    import zstandard as zstd
except Exception:  # pragma: no cover - optional dependency
    zstd = None


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


class CodexCompressedRolloutTests(unittest.TestCase):
    """Current Codex releases compress rollouts to .jsonl.zst.

    Missing them silently dropped the whole history from a scan, so both the
    live sessions tree and archived_sessions/ must be discovered and parsed.
    """

    @staticmethod
    def _write_zst(path, lines):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        with open(path, "wb") as fh:
            with zstd.ZstdCompressor().stream_writer(fh) as writer:
                writer.write(payload)

    @unittest.skipUnless(zstd is not None, "zstandard not installed")
    def test_parse_decompresses_jsonl_zst(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(
                root, "rollout-2026-01-02T03-04-05-abcdef01-2345-6789-abcd-ef0123456789.jsonl.zst")
            self._write_zst(path, [
                json.dumps(SESSION_META),
                mirror(last=usage(100, 40, 20), total=usage(100, 40, 20)),
            ])
            records = list(CodexAdapter(cfg={}).parse(path))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].project, "/home/al/proj")
        self.assertEqual(records[0].session, "thread-owner-1")
        self.assertEqual((records[0].input, records[0].output), (60, 20))

    @unittest.skipUnless(zstd is not None, "zstandard not installed")
    def test_session_id_falls_back_to_compressed_filename(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(
                root, "rollout-2026-01-02T03-04-05-abcdef01-2345-6789-abcd-ef0123456789.jsonl.zst")
            self._write_zst(path, [mirror(last=usage(10, 0, 5), total=usage(10, 0, 5))])
            records = list(CodexAdapter(cfg={}).parse(path))
        self.assertEqual(len(records), 1)
        # .zst must not leak into the derived session identity.
        self.assertEqual(records[0].session, "abcdef01-2345-6789-abcd-ef0123456789")

    @unittest.skipUnless(zstd is not None, "zstandard not installed")
    def test_enumerate_discovers_compressed_sessions_and_archives(self):
        with tempfile.TemporaryDirectory() as root:
            live = os.path.join(root, "sessions", "2026", "01", "02",
                                "rollout-2026-01-02T03-04-05-abcdef01-2345-6789-abcd-ef0123456789.jsonl.zst")
            archived = os.path.join(root, "archived_sessions",
                                    "rollout-2026-01-02T04-05-06-abcdef01-2345-6789-abcd-ef0123456789.jsonl.zst")
            plain = os.path.join(root, "sessions", "2026", "01", "03",
                                 "rollout-2026-01-03T03-04-05-abcdef01-2345-6789-abcd-ef0123456789.jsonl")
            for path in (live, archived, plain):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "wb").close()
            with mock.patch.dict(os.environ, {"CODEX_HOME": root}):
                found = CodexAdapter(cfg={}).enumerate()
        self.assertEqual({os.path.normcase(p) for p in found},
                         {os.path.normcase(p) for p in (live, archived, plain)})


if __name__ == "__main__":
    unittest.main()
