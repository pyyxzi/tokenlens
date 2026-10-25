"""Unit tests for tokenlens.pricing: validation, lookup and cost estimation."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tokenlens import pricing  # noqa: E402
from tokenlens.pricing import (  # noqa: E402
    cost_breakdown, estimate_cost, has_custom_price, is_known, lookup,
    remove_custom_price, set_custom_price, validate_prices,
)


class ValidatePricesTests(unittest.TestCase):
    def test_valid_quadruple(self):
        self.assertEqual(validate_prices([1, 2, 3, 4]), (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(validate_prices((0, 0, 0, 0)), (0.0, 0.0, 0.0, 0.0))

    def test_rejects_wrong_shape_and_values(self):
        for bad in ([1, 2, 3], "1234", [1, 2, 3, -1], [True, 1, 1, 1],
                    [1, 2, 3, float("nan")], [1, 2, 3, float("inf")]):
            with self.assertRaises(ValueError):
                validate_prices(bad)


class LookupTests(unittest.TestCase):
    def test_returns_four_rates(self):
        rates = lookup("definitely-unknown-model-xyz")
        self.assertEqual(len(rates), 4)
        self.assertTrue(all(r >= 0 for r in rates))


class CustomPriceTests(unittest.TestCase):
    MODEL = "test-custom-price-model"

    def tearDown(self):
        remove_custom_price(self.MODEL)

    def test_round_trip(self):
        self.assertFalse(has_custom_price(self.MODEL))
        set_custom_price(self.MODEL, (10, 20, 0, 0))
        self.assertTrue(has_custom_price(self.MODEL))
        # A manual price must make an otherwise unknown model priceable
        # (regression: is_known used to check only the builtin table).
        self.assertTrue(is_known(self.MODEL))
        self.assertEqual(lookup(self.MODEL), (10.0, 20.0, 0.0, 0.0))
        self.assertEqual(estimate_cost(self.MODEL, 1_000_000, 1_000_000), 30.0)
        breakdown = cost_breakdown(self.MODEL, 1_000_000, 1_000_000, 0, 0)
        self.assertTrue(breakdown["known"])
        self.assertAlmostEqual(breakdown["input"], 10.0)
        self.assertAlmostEqual(breakdown["output"], 20.0)
        remove_custom_price(self.MODEL)
        self.assertFalse(has_custom_price(self.MODEL))
        self.assertFalse(is_known(self.MODEL))

    def test_model_name_required(self):
        with self.assertRaises(ValueError):
            set_custom_price("  ", (1, 1, 1, 1))


class UnknownModelCostTests(unittest.TestCase):
    MODEL = "definitely-unknown-model-xyz"

    def test_unknown_models_are_not_priced(self):
        self.assertFalse(is_known(self.MODEL))
        self.assertEqual(estimate_cost(self.MODEL, 1000, 1000), 0.0)
        breakdown = cost_breakdown(self.MODEL, 1000, 1000, 100, 100)
        self.assertFalse(breakdown["known"])
        self.assertEqual(breakdown["input"], 0.0)
        self.assertFalse(is_known(""))
        self.assertFalse(is_known(None))


class ReasoningCostTests(unittest.TestCase):
    MODEL = "test-reasoning-price-model"

    def tearDown(self):
        remove_custom_price(self.MODEL)

    def test_reasoning_billed_once_at_output_rate(self):
        set_custom_price(self.MODEL, (1, 2, 0, 0))
        base = estimate_cost(self.MODEL, 1_000_000, 1_000_000)
        with_reasoning = estimate_cost(self.MODEL, 1_000_000, 500_000, reasoning=500_000)
        self.assertAlmostEqual(base, 3.0)
        self.assertAlmostEqual(with_reasoning, base)


if __name__ == "__main__":
    unittest.main()
