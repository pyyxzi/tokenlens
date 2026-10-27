"""Derived metrics for normalized usage rows, computed after additive rollups."""
from __future__ import annotations

from typing import Mapping


def usage_metrics(row: Mapping) -> dict:
    """Return token-weighted metrics without changing saved usage or cost.

    ``input`` is uncached input; cache reads and writes are separate input
    categories. Outputs (including reasoning) never enter the cache denominator.
    A missing denominator is unavailable, whereas zero cache reads with known
    input is a real zero. Ratios stay in 0..1; callers format percentages.
    """
    input_total = sum(row.get(key) or 0 for key in ("input", "cache_read", "cache_write"))
    total = row.get("total")
    if total is None:
        total = input_total + (row.get("output") or 0) + (row.get("reasoning") or 0)
    requests = row.get("requests") or 0
    return {
        "input_total": input_total,
        "cache_hit_ratio": (row.get("cache_read") or 0) / input_total if input_total else None,
        "avg_tokens_per_request": total / requests if requests else None,
    }


def with_usage_metrics(row: dict) -> dict:
    """Enrich one response row in place; only call after summing raw counts."""
    row.update(usage_metrics(row))
    return row
