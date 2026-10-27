"""Derived usage, aggregation and export contracts using only synthetic data.

Run with: python -B -m unittest discover -s backend/tests -p test_usage_metrics.py -v
No Config/App construction, source discovery, real logs or on-disk database is used.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
import unittest
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens import report, server
from tokenlens.aggregator import Aggregator
from tokenlens.models import UsageRecord, ROW_COLUMNS
from tokenlens.store import SCHEMA
from tokenlens.usage_metrics import usage_metrics, with_usage_metrics


METRIC_KEYS = ("input_total", "cache_hit_ratio", "avg_tokens_per_request")
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning", "requests")


class MemoryStore:
    """The small query interface used by Aggregator, backed by :memory: only."""
    fts = False
    version = 1

    def __init__(self, records=()):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        for key in ("cost_kind", "cost_source", "price_version", "currency", "event_id"):
            self.connection.execute("ALTER TABLE records ADD COLUMN " + key + " TEXT")
        self.connection.executemany(
            "INSERT INTO records (" + ",".join(ROW_COLUMNS) + ") VALUES (" +
            ",".join("?" for _ in ROW_COLUMNS) + ")", [record.as_row() for record in records])
        self.connection.commit()

    def conn(self):
        return self.connection

    @contextmanager
    def locked(self):
        yield self.connection

    def query(self, sql, params=()):
        return [dict(row) for row in self.connection.execute(sql, params)]

    def query_one(self, sql, params=()):
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else {}

    def close(self):
        self.connection.close()


def fixture_records():
    return [
        UsageRecord(tool="codex", ts=1767355200, model="Model-A", project="alpha", session="shared",
                    input=100, cache_read=300, cache_write=100, output=25, reasoning=5,
                    requests=2, cost=0.5, cost_kind="actual"),
        UsageRecord(tool="codex", ts=1767441600, model=" model-a ", project="alpha", session="shared",
                    input=900, output=70, requests=8, cost=0.25, cost_kind="actual"),
        UsageRecord(tool="claude-code", ts=1767355200, model="model-b", project="beta", session="shared",
                    cache_write=200, requests=1, cost=0.125, cost_kind="actual"),
        UsageRecord(tool="gemini-cli", ts=1767441600, model="model-c", project="gamma", session="read-only",
                    cache_read=200, requests=3, cost=0, cost_kind="unknown"),
        UsageRecord(tool="gemini-cli", ts=1767441600, model="model-output", project="output-only", session="output-only",
                    output=500, reasoning=50, requests=4, cost=0, cost_kind="unknown"),
        UsageRecord(tool="codex", ts=1767355200, model="model-zero", project="request-only", session="request-only",
                    requests=5, cost=0, cost_kind="unknown"),
        UsageRecord(tool="claude-code", ts=1767441600, model="no-requests", project="no-requests", session="no-requests",
                    input=3, cache_read=7, cache_write=10, output=11, reasoning=2,
                    requests=0, cost=0.0625, cost_kind="actual"),
    ]


class UsageMetricsTests(unittest.TestCase):
    def test_cache_write_is_input_but_output_and_reasoning_are_not(self):
        row = {"input": 100, "cache_read": 300, "cache_write": 100,
               "output": 25, "reasoning": 5, "requests": 2}
        self.assertEqual(usage_metrics(row), {
            "input_total": 500, "cache_hit_ratio": 0.6, "avg_tokens_per_request": 265.0})
        expanded = usage_metrics({**row, "output": 10025, "reasoning": 1005})
        self.assertEqual(expanded["cache_hit_ratio"], 0.6)
        self.assertEqual(expanded["input_total"], 500)
        self.assertEqual(expanded["avg_tokens_per_request"], 5765.0)

    def test_zero_and_unavailable_are_distinct(self):
        cases = [
            ({}, (0, None, None)),
            ({"input": None, "cache_read": None, "cache_write": None, "requests": None}, (0, None, None)),
            ({"input": 10, "requests": 1}, (10, 0.0, 10.0)),
            ({"cache_write": 10, "requests": 1}, (10, 0.0, 10.0)),
            ({"cache_read": 10, "requests": 1}, (10, 1.0, 10.0)),
            ({"output": 8, "reasoning": 2, "requests": 2}, (0, None, 5.0)),
            ({"requests": 4}, (0, None, 0.0)),
            ({"input": 10, "requests": 0}, (10, 0.0, None)),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(tuple(usage_metrics(row)[key] for key in METRIC_KEYS), expected)

    def test_fractional_average_is_not_truncated(self):
        self.assertEqual(usage_metrics({"input": 10, "requests": 3})["avg_tokens_per_request"], 10 / 3)

    def test_response_enrichment_does_not_rewrite_saved_fields(self):
        row = {"input": 10, "total": 14, "requests": 3, "cost": 1.25, "cost_kind": "actual"}
        original = row.copy()
        self.assertIs(with_usage_metrics(row), row)
        self.assertEqual({key: row[key] for key in original}, original)
        self.assertEqual(row["avg_tokens_per_request"], 14 / 3)
        self.assertEqual(set(row) - set(original), set(METRIC_KEYS))


class AggregationMetricsTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(fixture_records())
        self.addCleanup(self.store.close)

    def view(self, filters=None, sql=False):
        agg = Aggregator(self.store).filtered(filters or {})
        if sql:
            agg.MAX_ROLLUP_ROWS = 0
        return agg

    def test_sum_before_division_across_summary_and_all_rankings(self):
        agg = self.view({"project": "alpha"})
        rows = [agg.summary("all"), *agg.by_tool("all"), *agg.by_model("all"),
                *agg.by_project("all"), *agg.sessions("all")]
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(row["input_total"], 1400)
                self.assertAlmostEqual(row["cache_hit_ratio"], 3 / 14)
                self.assertEqual(row["avg_tokens_per_request"], 150.0)
                self.assertEqual(row["cache_write"], 100)
                self.assertEqual(row["reasoning"], 5)
                self.assertEqual(row["cost"], 0.75)
        summary = rows[0]
        self.assertEqual(summary["cache_hit_rate"], 21.4)  # legacy percent, not ratio
        self.assertEqual(summary["avg_per_request"], 150)  # legacy integer

    def test_filtered_values_and_normalized_models(self):
        cases = [
            ({"project": "alpha", "from": "2026-01-02", "to": "2026-01-02"}, (500, 0.6, 265.0)),
            ({"model": "MODEL-A"}, (1400, 3 / 14, 150.0)),
            ({"q": "alpha"}, (1400, 3 / 14, 150.0)),
            ({"project_paths": json.dumps(["alpha"])}, (1400, 3 / 14, 150.0)),
            ({"tool": "claude-code", "project": "beta"}, (200, 0.0, 200.0)),
            ({"project": "output-only"}, (0, None, 137.5)),
            ({"project": "request-only"}, (0, None, 0.0)),
            ({"project": "no-requests"}, (20, 0.35, None)),
            ({"project": "missing"}, (0, None, None)),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters):
                row = self.view(filters).summary("all")
                self.assertEqual(row["input_total"], expected[0])
                if expected[1] is None:
                    self.assertIsNone(row["cache_hit_ratio"])
                else:
                    self.assertAlmostEqual(row["cache_hit_ratio"], expected[1])
                self.assertEqual(row["avg_tokens_per_request"], expected[2])

    def test_empty_library_and_empty_range_preserve_legacy_defaults(self):
        empty = MemoryStore()
        self.addCleanup(empty.close)
        for agg, rng in ((Aggregator(empty), "all"), (self.view(), "2026-01-05~2026-01-05")):
            with self.subTest(rng=rng):
                row = agg.summary(rng)
                self.assertEqual(row["input_total"], 0)
                self.assertIsNone(row["cache_hit_ratio"])
                self.assertIsNone(row["avg_tokens_per_request"])
                self.assertEqual(row["cache_hit_rate"], 0.0)
                self.assertEqual(row["avg_per_request"], 0)
                for kind in ("models", "projects", "sessions"):
                    self.assertEqual(agg.rankings(kind, rng)["rows"], [])

    def test_head_is_full_session_not_limited_turns_and_identity_is_preserved(self):
        agg = self.view()
        head = agg.session_detail("shared", tool="codex", project="alpha", limit=1)
        self.assertEqual(head["records"], 2)
        self.assertEqual(len(head["turns"]), 1)
        self.assertEqual(head["input_total"], 1400)
        self.assertAlmostEqual(head["cache_hit_ratio"], 3 / 14)
        self.assertEqual(head["avg_tokens_per_request"], 150.0)
        self.assertEqual(head["reasoning"], 5)
        self.assertEqual(head["turns"][0]["cache_hit_ratio"], 0.6)
        self.assertEqual(head["turns"][0]["requests"], 2)
        self.assertEqual(head["turns"][0]["avg_tokens_per_request"], 265.0)
        other = agg.session_detail("shared", tool="claude-code", project="beta")
        self.assertEqual(other["input_total"], 200)
        self.assertEqual(other["cache_hit_ratio"], 0)
        narrowed = agg.session_detail("shared", tool="codex", project="alpha", rng="2026-01-02~2026-01-02")
        self.assertEqual(narrowed["input_total"], 500)
        self.assertEqual(narrowed["cache_hit_ratio"], 0.6)
        for row in [head, *head["models"], *head["daily"], *head["turns"]]:
            for key in (*TOKEN_KEYS, *METRIC_KEYS):
                self.assertIn(key, row)
        self.assertIsNone(agg.session_detail("absent"))

    def test_records_have_metrics_and_json_null(self):
        rows = self.view().records("all", limit=50)["rows"]
        by_project = {row["project"]: row for row in rows}
        self.assertEqual(by_project["beta"]["cache_hit_ratio"], 0)
        self.assertEqual(by_project["gamma"]["cache_hit_ratio"], 1)
        self.assertIsNone(by_project["output-only"]["cache_hit_ratio"])
        self.assertIsNone(by_project["no-requests"]["avg_tokens_per_request"])
        encoded = json.loads(json.dumps(by_project["output-only"], allow_nan=False))
        self.assertIsNone(encoded["cache_hit_ratio"])

    def test_sql_fallback_matches_memory_rollup_including_paged_rankings(self):
        scopes = [("all", {}), ("2026-01-02~2026-01-03", {}),
                  ("2026-01-02~2026-01-02", {}), ("2026-01-05~2026-01-05", {}),
                  ("all", {"tool": "codex"}), ("all", {"model": "MODEL-A"}),
                  ("all", {"project": "alpha"}), ("all", {"q": "alpha"}),
                  ("all", {"project": "output-only"})]
        queries = {
            "summary": lambda agg, rng: agg.summary(rng),
            "tools": lambda agg, rng: agg.by_tool(rng),
            "models": lambda agg, rng: agg.by_model(rng),
            "projects": lambda agg, rng: agg.by_project(rng),
            "sessions": lambda agg, rng: agg.sessions(rng),
            "session_cost": lambda agg, rng: agg.sessions(rng, order="cost", limit=2, offset=1),
            "compare": lambda agg, rng: agg.compare(rng),
            "records": lambda agg, rng: agg.records(rng, limit=2, offset=1),
            "head": lambda agg, rng: agg.session_detail("shared", tool="codex", project="alpha", limit=1, rng=rng),
        }
        for kind in ("models", "projects", "sessions"):
            queries["ranking_" + kind] = lambda agg, rng, kind=kind: agg.rankings(kind, rng, limit=2, offset=1)
        for rng, filters in scopes:
            memory, sql = self.view(filters), self.view(filters, sql=True)
            self.assertFalse(memory._uses_sql_rollup())
            self.assertTrue(sql._uses_sql_rollup())
            for name, query in queries.items():
                with self.subTest(rng=rng, filters=filters, query=name):
                    self.assertEqual(query(memory, rng), query(sql, rng))

    def test_rankings_and_head_agree_on_all_usage_metrics(self):
        for sql in (False, True):
            agg = self.view({"project": "alpha"}, sql=sql)
            head = agg.session_detail("shared", tool="codex", project="alpha")
            for kind in ("models", "projects", "sessions"):
                with self.subTest(sql=sql, kind=kind):
                    row = agg.rankings(kind, "all", limit=1)["rows"][0]
                    for key in (*TOKEN_KEYS, *METRIC_KEYS):
                        self.assertEqual(row[key], head[key])

    def test_metrics_reuse_existing_queries_instead_of_scanning_for_ratios(self):
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        agg = self.view()
        agg.summary("all")
        agg.by_tool("all")
        agg.by_model("all")
        agg.by_project("all")
        agg.sessions("all")
        self.assertEqual(len(statements), 1)  # one shared additive rollup
        statements.clear()
        agg.session_detail("shared", tool="codex", project="alpha", limit=1)
        self.assertEqual(len(statements), 4)  # head, models, days, turns
        statements.clear()
        agg.records("all")
        self.assertEqual(len(statements), 2)  # existing count and bounded page

    def test_memo_results_do_not_retain_caller_mutations(self):
        agg = self.view({"project": "alpha"})
        row = agg.summary("all")
        row["cache_hit_ratio"] = 999
        self.assertAlmostEqual(agg.summary("all")["cache_hit_ratio"], 3 / 14)
        rows = agg.by_project("all")
        rows[0]["input_total"] = -1
        self.assertEqual(agg.by_project("all")[0]["input_total"], 1400)


class ReportAndExportMetricsTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(fixture_records())
        self.addCleanup(self.store.close)
        self.cfg = {"range": "all", "usd_to_cny": 7.2, "language": "en-US"}
        self.app = SimpleNamespace(store=self.store, cfg=self.cfg)
        self.handler_type = server.make_handler(self.app)

    def handler(self):
        handler = self.handler_type.__new__(self.handler_type)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: setattr(handler, "captured_status", status)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        return handler

    def api(self, path, query):
        handler = self.handler()
        with patch.object(server, "export_snapshot", side_effect=lambda store: nullcontext(store)):
            handler.api_get(path, query)
        self.assertEqual(handler.captured_status, 200)
        return json.loads(handler.wfile.getvalue().decode("utf-8"))

    def export(self, fmt, **filters):
        handler = self.handler()
        handler._export_snapshot({"range": "all", "fmt": fmt, **filters}, self.store)
        self.assertEqual(handler.captured_status, 200)
        content = handler.wfile.getvalue()
        if fmt in ("md", "markdown"):
            return content.decode("utf-8")
        stream, chunks = io.BytesIO(content), []
        while True:
            size = int(stream.readline().strip(), 16)
            if not size:
                self.assertEqual(stream.read(), b"\r\n")
                break
            chunks.append(stream.read(size))
            self.assertEqual(stream.read(2), b"\r\n")
        return b"".join(chunks).decode("utf-8-sig")

    def test_public_summary_rankings_records_and_session_share_metrics(self):
        query = {"range": "all", "project": "alpha"}
        summary = self.api("/api/summary", query)["summary"]
        head = self.api("/api/session", {**query, "tool": "codex", "session": "shared", "limit": "1"})
        for kind in ("models", "projects", "sessions"):
            row = self.api("/api/rankings", {**query, "kind": kind})["rows"][0]
            for key in METRIC_KEYS:
                self.assertEqual(row[key], summary[key])
                self.assertEqual(row[key], head[key])
        rows = self.api("/api/records", query)["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["input_total"] for row in rows), summary["input_total"])
        self.assertEqual(summary["cache_hit_rate"], 21.4)
        self.assertEqual(head["cost_cny"], 5.4)

    def test_json_export_preserves_raw_fields_and_enriches_all_existing_sections(self):
        body = json.loads(self.export("json", project="alpha"))
        self.assertEqual(body["summary"]["cost"], 0.75)
        self.assertEqual(body["summary"]["cache_hit_rate"], 21.4)
        for row in [body["summary"], *body["tools"], *body["models"], *body["projects"], *body["records"]]:
            for key in (*TOKEN_KEYS, *METRIC_KEYS):
                self.assertIn(key, row)
        self.assertEqual(len(body["records"]), 2)
        self.assertEqual(sum(row["input_total"] for row in body["records"]), 1400)
        output_only = json.loads(self.export("json", project="output-only"))
        self.assertIsNone(output_only["summary"]["cache_hit_ratio"])
        self.assertIsNone(output_only["records"][0]["cache_hit_ratio"])

    def test_csv_only_appends_metrics_and_keeps_null_cells_blank(self):
        rows = list(csv.reader(io.StringIO(self.export("csv"))))
        legacy = ["date", "timestamp", "tool", "model", "project", "session", "input", "output",
                  "cache_read", "cache_write", "reasoning", "total", "requests", "cost_usd",
                  "cost_kind", "cost_source", "price_version", "currency", "source"]
        self.assertEqual(rows[0], legacy + list(METRIC_KEYS))
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows[1:]))
        mapped = [dict(zip(rows[0], row)) for row in rows[1:]]
        by_project = {row["project"]: row for row in mapped}
        self.assertEqual(by_project["output-only"]["cache_hit_ratio"], "")
        self.assertEqual(by_project["no-requests"]["avg_tokens_per_request"], "")
        self.assertEqual(float(by_project["beta"]["cache_hit_ratio"]), 0)
        self.assertEqual(float(by_project["gamma"]["cache_hit_ratio"]), 1)

    def test_markdown_formats_ratio_once_and_explains_token_basis(self):
        agg = Aggregator(self.store).filtered({"project": "alpha", "from": "2026-01-02", "to": "2026-01-02"})
        english = report.render(agg, "all", "en-US")
        self.assertIn("| Input token cache hit rate | 60.0% |", english)
        self.assertIn("| Average tokens per request | 265.00 |", english)
        self.assertIn("not a request hit rate or cost savings percentage", english)
        self.assertNotIn("6000.0%", english)
        chinese = report.render(agg, "all", "zh-CN")
        self.assertIn("| 输入 Token 缓存命中率 | 60.0% |", chinese)
        self.assertIn("| 均次 tokens | 265.00 |", chinese)
        self.assertIn("不代表请求命中率或省费比例", chinese)

    def test_markdown_na_and_zero_are_distinct(self):
        output_only = self.export("md", project="output-only", lang="en-US")
        self.assertIn("| Input token cache hit rate | N/A |", output_only)
        self.assertIn("| Average tokens per request | 137.50 |", output_only)
        zero = self.export("markdown", project="beta", lang="en-US")
        self.assertIn("| Input token cache hit rate | 0.0% |", zero)
        no_requests = self.export("md", project="no-requests", lang="en-US")
        self.assertIn("| Average tokens per request | N/A |", no_requests)

    def test_empty_markdown_reports_unavailable_metrics(self):
        markdown = self.export("md", project="missing", lang="en-US")
        self.assertIn("No usage recorded in this period", markdown)
        self.assertIn("| Input token cache hit rate | N/A |", markdown)
        self.assertIn("| Average tokens per request | N/A |", markdown)
        request_only = self.export("md", project="request-only", lang="en-US")
        self.assertIn("| Input token cache hit rate | N/A |", request_only)
        self.assertIn("| Average tokens per request | 0.00 |", request_only)

    def test_markdown_previous_period_uses_ratio_not_legacy_percent(self):
        agg = Aggregator(self.store).filtered({"project": "alpha"})
        markdown = report.render(agg, "2026-01-03~2026-01-03", "en-US")
        self.assertIn("| Input token cache hit rate | 0.0% | 60.0% |", markdown)
        self.assertIn("| Average tokens per request | 121.25 | 265.00 |", markdown)

    def test_tool_report_compares_the_same_source_in_both_periods(self):
        for sql in (False, True):
            agg = Aggregator(self.store)
            if sql:
                agg.MAX_ROLLUP_ROWS = 0
            with self.subTest(sql=sql):
                markdown = report.render(agg, "2026-01-03~2026-01-03", "en-US", tool="codex")
                self.assertIn("| Input token cache hit rate | 0.0% | 60.0% |", markdown)
                self.assertIn("| Average tokens per request | 121.25 | 75.71 |", markdown)
                self.assertNotIn("model-b", markdown)
                self.assertNotIn("model-c", markdown)

    def test_markdown_date_filters_match_summary_and_preserve_previous_period(self):
        cases = [
            ({"range": "2026-01-02~2026-01-05", "from": "2026-01-04", "to": "2026-01-03"},
             "2026-01-03", "2026-01-04", "60.0%", "265.00"),
            ({"range": "all", "from": "2026-02-30"}, None, None, "N/A", "N/A"),
            ({"range": "2026-01-01~2026-03-01", "to": "2026-02-30"},
             "2026-01-01", "2026-03-01", "N/A", "N/A"),
            ({"range": "2026-01-02~2026-01-03", "from": " 2026-01-03 "},
             "2026-01-03", "2026-01-03", "60.0%", "265.00"),
        ]
        for sql in (False, True):
            for filters, start, end, previous_ratio, previous_average in cases:
                with self.subTest(sql=sql, filters=filters):
                    with patch.object(Aggregator, "MAX_ROLLUP_ROWS", 0 if sql else 8192):
                        query = {**filters, "project": "alpha"}
                        summary = self.api("/api/summary", query)["summary"]
                        markdown = self.export("md", **query, lang="en-US")
                    ratio = "%.1f%%" % (summary["cache_hit_ratio"] * 100)
                    average = format(summary["avg_tokens_per_request"], ",.2f")
                    self.assertIn("| Input token cache hit rate | %s | %s |" % (ratio, previous_ratio), markdown)
                    self.assertIn("| Average tokens per request | %s | %s |" % (average, previous_average), markdown)
                    if start:
                        self.assertIn("*Period: %s → %s" % (start, end), markdown)

    def test_query_and_export_metrics_do_not_add_saved_columns_or_modify_rows(self):
        columns = self.store.query("PRAGMA main.table_info(records)")
        records = self.store.query("SELECT * FROM main.records ORDER BY id")
        self.api("/api/summary", {"range": "all"})
        for kind in ("models", "projects", "sessions"):
            self.api("/api/rankings", {"range": "all", "kind": kind})
        for fmt in ("json", "csv", "md"):
            self.export(fmt)
        self.assertEqual(self.store.query("PRAGMA main.table_info(records)"), columns)
        self.assertEqual(self.store.query("SELECT * FROM main.records ORDER BY id"), records)
        self.assertTrue(set(METRIC_KEYS).isdisjoint(row["name"] for row in columns))


if __name__ == "__main__":
    unittest.main()
