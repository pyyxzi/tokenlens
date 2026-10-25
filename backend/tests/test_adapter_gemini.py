"""Synthetic-fixture tests for the Gemini CLI adapter.

Gemini sessions are replayed message stores: JSONL append, $set snapshots,
$rewindTo and $patch mutations all update the same logical message map before
usage is totaled. These tests pin the accounting semantics from SOURCES.md:
cached is a subset of input, thoughts are separate reasoning output, and
rewound or patched-away messages must not be counted.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens.adapters import GeminiCliAdapter  # noqa: E402


def gemini_message(mid, inp, out, cached=0, thoughts=0, ts="2026-01-02T03:04:05Z"):
    return {
        "id": mid,
        "type": "gemini",
        "model": "gemini-test-model",
        "timestamp": ts,
        "tokens": {"input": inp, "output": out, "cached": cached, "thoughts": thoughts},
    }


class GeminiSessionDir:
    """Creates the tmp/<hash>/chats layout the adapter enumerates."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chats = os.path.join(self.tmp.name, "abc123", "chats")
        os.makedirs(self.chats, exist_ok=True)

    def write(self, name, lines):
        path = os.path.join(self.chats, name)
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return path

    def write_json(self, name, doc):
        path = os.path.join(self.chats, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return path

    def set_project_root(self, project):
        with open(os.path.join(self.tmp.name, "abc123", ".project_root"),
                  "w", encoding="utf-8") as f:
            f.write(project)

    def close(self):
        self.tmp.cleanup()


class GeminiCliAdapterTests(unittest.TestCase):
    def setUp(self):
        self.dir = GeminiSessionDir()
        self.adapter = GeminiCliAdapter(cfg=None)

    def tearDown(self):
        self.dir.close()

    def parse(self, path):
        return list(self.adapter.parse(path))

    def test_basic_usage_normalization(self):
        self.dir.set_project_root("C:\\code\\demo")
        path = self.dir.write("session.jsonl", [gemini_message("m1", 100, 30, cached=40, thoughts=10)])
        records = self.parse(path)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.tool, "gemini-cli")
        self.assertEqual(r.model, "gemini-test-model")
        self.assertEqual(r.input, 60)
        self.assertEqual(r.output, 30)
        self.assertEqual(r.cache_read, 40)
        self.assertEqual(r.reasoning, 10)
        self.assertTrue(r.event_id.startswith("gemini-cli:"))
        r.validate()

    def test_rewind_to_earlier_message_drops_later_usage(self):
        # $rewindTo removes the target message and everything after it.
        path = self.dir.write("session.jsonl", [
            gemini_message("m1", 100, 20),
            gemini_message("m2", 500, 80),
            {"$rewindTo": "m2"},
        ])
        records = self.parse(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].session, "session")
        self.assertEqual(records[0].input, 100)

    def test_rewind_to_unknown_id_clears_session(self):
        path = self.dir.write("session.jsonl", [
            gemini_message("m1", 100, 20),
            {"$rewindTo": "missing"},
        ])
        self.assertEqual(self.parse(path), [])

    def test_patch_remove_ids_drops_usage(self):
        path = self.dir.write("session.jsonl", [
            gemini_message("m1", 100, 20),
            gemini_message("m2", 500, 80),
            {"$patch": {"removeIds": ["m2"]}},
        ])
        records = self.parse(path)
        self.assertEqual([r.input for r in records], [100])

    def test_set_snapshot_replaces_messages(self):
        path = self.dir.write("session.jsonl", [
            gemini_message("m1", 100, 20),
            {"$set": {"sessionId": "live-session",
                      "messages": [gemini_message("m2", 700, 90)]}},
            gemini_message("m3", 50, 10),
        ])
        records = self.parse(path)
        self.assertEqual(sorted(r.input for r in records), [50, 700])
        self.assertTrue(all(r.session == "live-session" for r in records))

    def test_duplicate_message_id_replayed_keeps_last(self):
        path = self.dir.write("session.jsonl", [
            gemini_message("m1", 100, 20),
            gemini_message("m1", 900, 60),
        ])
        records = self.parse(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].output, 60)

    def test_legacy_json_session(self):
        path = self.dir.write_json("legacy.json", {
            "sessionId": "legacy-1",
            "messages": [gemini_message("m1", 100, 20),
                         {"id": "m2", "type": "user", "tokens": {"input": 999}},],
        })
        records = self.parse(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].session, "legacy-1")
        self.assertEqual(records[0].input, 100)

    def test_cached_exceeding_input_is_rejected(self):
        path = self.dir.write("session.jsonl", [gemini_message("m1", 10, 5, cached=40)])
        with self.assertRaises(ValueError):
            self.parse(path)

    def test_non_accounting_messages_ignored(self):
        path = self.dir.write("session.jsonl", [
            {"id": "u1", "type": "user", "tokens": {"input": 999, "output": 99}},
            {"id": "i1", "type": "info"},
            gemini_message("g1", 100, 20),
        ])
        records = self.parse(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].input, 100)


if __name__ == "__main__":
    unittest.main()
