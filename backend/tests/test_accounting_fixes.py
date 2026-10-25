"""Regression tests for the 1.2.2 accounting hardening fixes.

Each test pins one audited defect: Kimi cumulative mirrors counted as turns,
CherryStudio LevelDB cross-file duplicates, Aider model names polluted by
heading timestamps, ApiLog flat reasoning double-billed on OpenRouter-style
logs, LMStudio aggregate wrappers counted alongside their children,
Copilot CLI resumed-session snapshots double-counted across files, and
DeepSeek Harness projections double-counted when names mismatch.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens import adapters as A  # noqa: E402


def write_lines(directory, name, objects):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        for o in objects:
            f.write((o if isinstance(o, str) else json.dumps(o)) + "\n")
    return path


class KimiMirrorTests(unittest.TestCase):
    def make(self, tmp, records):
        adapter = A.KimiAdapter(cfg=None)
        adapter._session_lookup = {}   # isolate from any real machine index
        path = write_lines(tmp, "wire.jsonl", records)
        return list(adapter.parse(path))

    def test_cumulative_mirrors_are_dropped_when_turns_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self.make(tmp, [
                # three real turns (scope detectable via turnId)
                {"sessionId": "s1", "turnId": "t1", "timestamp": "2026-01-02T03:04:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"sessionId": "s1", "turnId": "t2", "timestamp": "2026-01-02T03:05:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 200, "output_tokens": 100}},
                {"sessionId": "s1", "turnId": "t3", "timestamp": "2026-01-02T03:06:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 150, "output_tokens": 100}},
                # unscoped cumulative mirrors: monotonic, doubling, covering
                # the turn totals (150, 450, 700) — must be dropped, not summed
                {"sessionId": "s1", "timestamp": "2026-01-02T03:06:06Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 120, "output_tokens": 30}},
                {"sessionId": "s1", "timestamp": "2026-01-02T03:06:07Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 350, "output_tokens": 100}},
                {"sessionId": "s1", "timestamp": "2026-01-02T03:06:08Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 550, "output_tokens": 150}},
            ])
        self.assertEqual(len(records), 3)
        self.assertEqual(sum(r.input for r in records), 450)

    def test_non_cumulative_unknown_records_stay_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self.make(tmp, [
                {"sessionId": "s1", "turnId": "t1", "timestamp": "2026-01-02T03:04:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 100, "output_tokens": 50}},
                # unscoped but fluctuating (not monotonic) — real turns
                {"sessionId": "s1", "timestamp": "2026-01-02T03:05:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 500, "output_tokens": 300}},
                {"sessionId": "s1", "timestamp": "2026-01-02T03:06:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 90, "output_tokens": 40}},
                {"sessionId": "s1", "timestamp": "2026-01-02T03:07:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 600, "output_tokens": 350}},
            ])
        self.assertEqual(len(records), 4)

    def test_session_level_kept_per_model_for_sessions_without_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = self.make(tmp, [
                # session A has turn detail
                {"sessionId": "A", "turnId": "t1", "timestamp": "2026-01-02T03:04:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 100, "output_tokens": 50}},
                # session B has only session-level cumulative snapshots, two models
                {"sessionId": "B", "scope": "session", "timestamp": "2026-01-02T03:04:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"sessionId": "B", "scope": "session", "timestamp": "2026-01-02T03:05:05Z",
                 "model": "kimi-k2", "usage": {"input_tokens": 300, "output_tokens": 150}},
                {"sessionId": "B", "scope": "session", "timestamp": "2026-01-02T03:06:05Z",
                 "model": "kimi-k1.5", "usage": {"input_tokens": 40, "output_tokens": 20}},
            ])
        # A keeps its turn; B keeps the max snapshot per model (k2: 300/150, k1.5: 40/20)
        self.assertEqual(len(records), 3)
        b_k2 = [r for r in records if r.session == "B" and r.model == "kimi-k2"]
        self.assertEqual(len(b_k2), 1)
        self.assertEqual(b_k2[0].input, 300)


class CherryStudioIdentityTests(unittest.TestCase):
    def parse(self, tmp, name, payload):
        path = os.path.join(tmp, name)
        with open(path, "wb") as f:
            f.write(payload)
        return list(A.CherryStudioAdapter(cfg=None).parse(path))

    def test_same_triple_in_two_files_shares_identity(self):
        blob = b'{"prompt_tokens":500,"completion_tokens":300,"total_tokens":800}'
        with tempfile.TemporaryDirectory() as tmp:
            a = self.parse(tmp, "000003.log", blob)
            b = self.parse(tmp, "000003.ldb", blob)
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].event_id, b[0].event_id)
        self.assertTrue(a[0].event_id.startswith("cherry-studio:"))

    def test_different_triples_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.parse(tmp, "a.log", b'{"prompt_tokens":500,"completion_tokens":300,"total_tokens":800}')
            b = self.parse(tmp, "b.ldb", b'{"prompt_tokens":700,"completion_tokens":300,"total_tokens":1000}')
        self.assertNotEqual(a[0].event_id, b[0].event_id)


class AiderModelNameTests(unittest.TestCase):
    def test_timestamp_prefix_is_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".aider.chat.history.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("#### 2024-05-01 10:00 gpt-4o\n"
                        "tokens: 1,234 sent, 567 received\n")
            records = list(A.AiderAdapter(cfg=None).parse(path))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].model, "gpt-4o")

    def test_plain_model_heading_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".aider.chat.history.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("#### deepseek-chat\n"
                        "tokens: 100 sent, 50 received\n")
            records = list(A.AiderAdapter(cfg=None).parse(path))
        self.assertEqual(records[0].model, "deepseek-chat")


class ApiLogReasoningTests(unittest.TestCase):
    def test_openrouter_style_flat_reasoning_is_included(self):
        adapter = A.ApiLogAdapter(cfg=None)
        records = list(adapter._from_object({
            "provider": "openai",
            "usage": {"prompt_tokens": 100, "completion_tokens": 200,
                      "reasoning_tokens": 80, "total_tokens": 300},
        }, "x.jsonl"))
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.output, 120)   # 200 - 80 included reasoning
        self.assertEqual(r.reasoning, 80)  # billed once, at output rate

    def test_plain_gateway_flat_reasoning_stays_separate(self):
        adapter = A.ApiLogAdapter(cfg=None)
        records = list(adapter._from_object({
            "usage": {"prompt_tokens": 100, "completion_tokens": 200,
                      "reasoning_tokens": 80, "total_tokens": 300},
        }, "x.jsonl"))
        self.assertEqual(records[0].output, 200)
        self.assertEqual(records[0].reasoning, 80)


class LMStudioWrapperTests(unittest.TestCase):
    def test_aggregate_wrapper_equal_to_children_sum_is_skipped(self):
        doc = {
            "model": "qwen2.5-7b",
            "history": {"promptTokens": 300, "completionTokens": 150,
                        "requests": [
                            {"promptTokens": 100, "completionTokens": 50},
                            {"promptTokens": 200, "completionTokens": 100},
                        ]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            records = list(A.LMStudioAdapter(cfg=None).parse(path))
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(r.input for r in records), 300)

    def test_disagreeing_totals_are_both_kept(self):
        doc = {
            "model": "qwen2.5-7b",
            "history": {"promptTokens": 999, "completionTokens": 500,
                        "requests": [
                            {"promptTokens": 100, "completionTokens": 50},
                        ]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            records = list(A.LMStudioAdapter(cfg=None).parse(path))
        self.assertEqual(len(records), 2)


class CopilotCliSnapshotIdentityTests(unittest.TestCase):
    EVENTS = [
        {"type": "session.start", "data": {"sessionId": "sess-77",
            "context": {"cwd": "C:\\dev\\demo"}}},
        {"type": "session.shutdown", "timestamp": "2026-01-02T03:04:05Z",
         "data": {"modelMetrics": {"gpt-5.2": {"usage": {
             "inputTokens": 100, "outputTokens": 50}, "requests": {"count": 2}}}}},
    ]

    def parse(self, tmp, dirname):
        d = os.path.join(tmp, dirname)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "events.jsonl")
        write_lines(d, "events.jsonl", self.EVENTS)
        return list(A.CopilotCliAdapter(cfg=None).parse(path))

    def test_resumed_session_snapshot_identity_is_path_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.parse(tmp, "state-1")
            b = self.parse(tmp, "state-2")
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].event_id, b[0].event_id)


class DeepSeekHarnessProjectionTests(unittest.TestCase):
    PROJECTION = {"record": {"id": "session-abc", "rows": {"tokenUsage": {"val": {
        "totals": {"inputTokens": 500, "outputTokens": 200}}}}}}

    def test_projection_of_parsed_session_is_skipped_by_content(self):
        adapter = A.DeepSeekHarnessAdapter(cfg=None)
        adapter._event_sessions = {"session-abc"}
        real, saved = A._zstandard, None
        A._zstandard = object()   # pretend zstd present: event streams win
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "weirdly-named.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.PROJECTION, f)
                records = list(adapter.parse(path))
        finally:
            A._zstandard = real
        self.assertEqual(records, [])

    def test_unrelated_projection_still_parsed(self):
        adapter = A.DeepSeekHarnessAdapter(cfg=None)
        adapter._event_sessions = {"session-other"}
        real = A._zstandard
        A._zstandard = object()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "session-abc.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.PROJECTION, f)
                records = list(adapter.parse(path))
        finally:
            A._zstandard = real
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].input, 500)


if __name__ == "__main__":
    unittest.main()
