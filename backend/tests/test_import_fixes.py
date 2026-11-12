"""Regression tests for the import identity fix (problems.md M-04).

An explicit id is only unique per provider/day. Two different exports (or two
rows in one export) can reuse an id while describing different usage; the old
identity ``[tool, session, id]`` silently dropped the later row.
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens.config import Config  # noqa: E402
from tokenlens.source_import import SourceImporter  # noqa: E402
from tokenlens.store import Store  # noqa: E402


HEADER = "id,model,input,output,ts,session\n"


def csv_bytes(rows):
    return base64.b64encode((HEADER + "".join(rows)).encode("utf-8")).decode()


class ImportIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["TOKENLENS_HOME"] = self._tmp.name
        self.cfg = Config(os.path.join(self._tmp.name, "config.json"))
        self.store = Store(os.path.join(self._tmp.name, "index.db"))
        self.importer = SourceImporter(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _import(self, data, name="usage.csv"):
        preview = self.importer.preview({"filename": name, "format": "csv", "data": data})
        return self.importer.commit({"token": preview["token"]})

    def test_same_id_with_different_usage_is_not_dropped(self):
        first = csv_bytes(["1,gpt-4o,100,10,2026-01-02T00:00:00Z,alpha\n"])
        second = csv_bytes(["1,gpt-4o,200,20,2026-02-02T00:00:00Z,alpha\n"])
        self.assertEqual(self._import(first)["imported"], 1)
        result = self._import(second, name="usage-2.csv")
        self.assertEqual(result["imported"], 1)
        self.assertFalse(result["duplicate"])
        row = self.store.query_one(
            "SELECT COUNT(*) n, COALESCE(SUM(input),0) inp FROM records WHERE tool='imported'")
        self.assertEqual(row["n"], 2)
        self.assertEqual(row["inp"], 300)

    def test_two_rows_in_one_file_sharing_id_are_kept(self):
        data = csv_bytes([
            "1,gpt-4o,100,10,2026-01-02T00:00:00Z,alpha\n",
            "1,gpt-4o,200,20,2026-02-02T00:00:00Z,alpha\n",
        ])
        preview = self.importer.preview({"filename": "usage.csv", "format": "csv", "data": data})
        self.assertEqual(preview["records"], 2)
        self.assertEqual(self.importer.commit({"token": preview["token"]})["imported"], 2)

    def test_identical_row_reimport_stays_idempotent(self):
        data = csv_bytes(["1,gpt-4o,100,10,2026-01-02T00:00:00Z,alpha\n"])
        self.assertEqual(self._import(data)["imported"], 1)
        result = self._import(data, name="again.csv")
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["imported"], 0)
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) n FROM records WHERE tool='imported'")["n"], 1)


if __name__ == "__main__":
    unittest.main()
