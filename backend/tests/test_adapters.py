"""Unit tests for tokenlens.adapters parsing helpers and the Claude Code parser."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens import adapters  # noqa: E402
from tokenlens.adapters import (  # noqa: E402
    ClaudeCodeAdapter, dig, first_nested, norm_project, safe_json, stable_event_id,
)


class SafeJsonTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(safe_json('{"a": 1}'), {"a": 1})
        self.assertIsNone(safe_json("not json"))
        self.assertIsNone(safe_json("[1, 2"))


class DigTests(unittest.TestCase):
    def test_nested_dict_path(self):
        self.assertEqual(dig({"a": {"b": {"c": 7}}}, "a.b.c"), 7)

    def test_list_index_path(self):
        self.assertEqual(dig({"l": [10, 20, 30]}, "l.1"), 20)
        self.assertEqual(dig({"l": [10]}, "l.5", default="d"), "d")

    def test_candidate_paths_fall_through(self):
        self.assertEqual(dig({"x": 2}, "a.b", "x"), 2)
        self.assertEqual(dig({}, "a", "b", default="d"), "d")

    def test_empty_values_count_as_missing(self):
        self.assertEqual(dig({"a": None}, "a", default="d"), "d")
        self.assertEqual(dig({"a": ""}, "a", default="d"), "d")


class FirstNestedTests(unittest.TestCase):
    def test_finds_key_at_depth(self):
        self.assertEqual(first_nested({"z": {"cached": 5}}, {"cached"}), 5)

    def test_skips_empty_values(self):
        self.assertIsNone(first_nested({"cached": None, "z": {"other": []}}, {"cached"}))


class NormProjectTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(norm_project(""), "")
        self.assertEqual(norm_project(None), "")

    def test_windows_paths_are_case_insensitive(self):
        self.assertEqual(norm_project("C:\\Users\\Al\\Project"),
                         norm_project("c:/users/al/project"))
        self.assertEqual(norm_project("C:\\Users\\Al\\Project\\"),
                         norm_project("c:/users/al/project"))

    def test_posix_paths_keep_case(self):
        self.assertNotEqual(norm_project("/home/al/Project"),
                            norm_project("/home/al/project"))


class StableEventIdTests(unittest.TestCase):
    def test_idempotent(self):
        a = stable_event_id("claude-code", "logs/a.jsonl", "s1", "req-1")
        b = stable_event_id("claude-code", "logs/a.jsonl", "s1", "req-1")
        self.assertEqual(a, b)

    def test_explicit_id_wins_over_payload(self):
        # A replay that fills in mutable response details must keep the same
        # identity, otherwise it evades deduplication.
        with_cost = stable_event_id("t", "f", "s", "req-1", {"cost": 1.5})
        without_cost = stable_event_id("t", "f", "s", "req-1", {"cost": None})
        self.assertEqual(with_cost, without_cost)

    def test_scope_and_payload_change_identity(self):
        base = stable_event_id("t", "f", "s1", "req-1")
        self.assertNotEqual(base, stable_event_id("t", "f", "s2", "req-1"))
        self.assertNotEqual(stable_event_id("t", "f", "s", "", {"a": 1}),
                            stable_event_id("t", "f", "s", "", {"a": 2}))

    def test_tool_prefixed_and_bounded(self):
        event_id = stable_event_id("claude-code", "f", "s", "r")
        self.assertTrue(event_id.startswith("claude-code:"))
        self.assertLessEqual(len(event_id), len("claude-code:") + 40)


def claude_usage_line(**overrides):
    entry = {
        "type": "assistant",
        "timestamp": "2026-01-02T03:04:05Z",
        "sessionId": "sess-1",
        "uuid": "msg-uuid-1",
        "cwd": "C:\\dev\\demo",
        "message": {
            "id": "msg_1",
            "model": "claude-test-model",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 20,
            },
        },
    }
    entry.update(overrides)
    return json.dumps(entry, ensure_ascii=False)


class ClaudeCodeParserTests(unittest.TestCase):
    def make_adapter(self):
        return ClaudeCodeAdapter(cfg=None)

    def parse(self, lines):
        return list(self.make_adapter().parse_lines("synthetic.jsonl", lines, {}))

    def test_usage_line_normalizes(self):
        records = self.parse([claude_usage_line()])
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.tool, "claude-code")
        self.assertEqual(record.model, "claude-test-model")
        self.assertEqual(record.session, "sess-1")
        self.assertEqual(record.input, 100)
        self.assertEqual(record.output, 50)
        self.assertEqual(record.cache_read, 10)
        self.assertEqual(record.cache_write, 20)
        # Claude reports thinking inside output_tokens; it must not be added.
        self.assertEqual(record.reasoning, 0)
        self.assertTrue(record.event_id.startswith("claude-code:"))
        record.validate()

    def test_non_assistant_and_usageless_lines_skipped(self):
        records = self.parse([
            '{"type": "user", "message": {"usage": {"input_tokens": 5}}}',
            '{"type": "assistant", "message": {"model": "m"}}',  # no usage
            "not json at all",
        ])
        self.assertEqual(records, [])

    def test_synthetic_model_marker_skipped(self):
        line = claude_usage_line()
        entry = json.loads(line)
        entry["message"]["model"] = "<synthetic>"
        records = self.parse([json.dumps(entry)])
        self.assertEqual(records, [])

    def test_replay_is_idempotent_by_event_id(self):
        line = claude_usage_line()
        first = self.parse([line])
        second = self.parse([line, line])
        self.assertEqual(len(second), 2)
        self.assertEqual(first[0].event_id, second[0].event_id)
        self.assertEqual(second[0].event_id, second[1].event_id)


class AdapterRegistryTests(unittest.TestCase):
    def test_every_adapter_declares_identity(self):
        seen_ids = set()
        count = 0
        for name in dir(adapters):
            obj = getattr(adapters, name)
            if (isinstance(obj, type) and issubclass(obj, adapters.Adapter)
                    and obj is not adapters.Adapter):
                self.assertIsInstance(obj.id, str)
                self.assertTrue(obj.id, "%s needs a non-empty id" % name)
                self.assertNotIn(obj.id, seen_ids, "duplicate adapter id %s" % obj.id)
                seen_ids.add(obj.id)
                count += 1
        self.assertGreater(count, 10)


if __name__ == "__main__":
    unittest.main()
