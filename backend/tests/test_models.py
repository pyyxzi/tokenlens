"""Unit tests for tokenlens.models pure helpers and UsageRecord validation.

Run from the repository root:

    python -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens.models import (  # noqa: E402
    UsageRecord, _i, day_of, human, to_epoch,
)


class ToEpochTests(unittest.TestCase):
    def test_none_and_garbage_return_zero(self):
        self.assertEqual(to_epoch(None), 0)
        self.assertEqual(to_epoch("not a date"), 0)
        self.assertEqual(to_epoch(""), 0)
        self.assertEqual(to_epoch(float("nan")), 0)
        self.assertEqual(to_epoch(float("inf")), 0)

    def test_epoch_seconds_pass_through(self):
        self.assertEqual(to_epoch(1767000000), 1767000000)
        self.assertEqual(to_epoch("1767000000"), 1767000000)

    def test_milliseconds_and_nanoseconds_auto_downgrade(self):
        self.assertEqual(to_epoch(1767000000000), 1767000000)          # ms
        self.assertEqual(to_epoch(1767000000000000000), 1767000000)    # ns

    def test_iso_zulu_is_deterministic(self):
        expected = int(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp())
        self.assertEqual(to_epoch("2026-01-02T03:04:05+00:00"), expected)

    def test_long_string_falls_back_to_prefix(self):
        self.assertEqual(to_epoch("2026-01-02T03:04:05-bad-suffix"),
                         to_epoch("2026-01-02T03:04:05"))


class DayOfTests(unittest.TestCase):
    def test_zero_is_blank(self):
        self.assertEqual(day_of(0), "")

    def test_matches_local_calendar_day(self):
        ts = 1767000000
        self.assertEqual(day_of(ts), datetime.fromtimestamp(ts).strftime("%Y-%m-%d"))


class IntParseTests(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(_i(5), 5)
        self.assertEqual(_i("5"), 5)
        self.assertEqual(_i("1,234"), 1234)
        self.assertEqual(_i(" 7 "), 7)

    def test_optional_and_invalid_fall_back_to_default(self):
        self.assertEqual(_i(None), 0)
        self.assertEqual(_i(None, default=9), 9)
        self.assertEqual(_i(""), 0)
        # Negative, fractional, boolean and non-numeric values must not be
        # silently coerced to zero-cost-looking rows: they fail closed.
        self.assertEqual(_i(-5), 0)
        self.assertEqual(_i(3.5), 0)
        self.assertEqual(_i(True), 0)
        self.assertEqual(_i("not-a-number"), 0)


class HumanTests(unittest.TestCase):
    def test_plain_numbers(self):
        self.assertEqual(human(0), "0")
        self.assertEqual(human(999), "999")
        self.assertEqual(human("not-a-number"), "0")

    def test_scaling_and_sign(self):
        self.assertEqual(human(1000), "1K")
        self.assertEqual(human(1500), "1.5K")
        self.assertEqual(human(2500000), "2.5M")
        self.assertEqual(human(-2500), "-2.5K")


class UsageRecordValidationTests(unittest.TestCase):
    def test_minimal_record_validates(self):
        record = UsageRecord(tool="claude-code", model="claude-x", input=10, output=5)
        record.validate()

    def test_oversized_text_is_rejected(self):
        record = UsageRecord(tool="claude-code", model="m" * 2000)
        with self.assertRaises(ValueError):
            record.validate()


if __name__ == "__main__":
    unittest.main()
