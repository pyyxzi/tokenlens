# Adding a data source

TokenLens values one thing above coverage: **honest accounting**. This guide explains what a source needs before it earns a spot in the app. 中文贡献者可对照本指南操作，讨论可用中文进行。

## When a source is supportable

A tool is supportable when its local artifacts contain a **reproducible accounting schema**:

1. **Per-call usage** — input / output tokens, ideally cache-read / cache-write and reasoning splits, with the model name on the record.
2. **A timestamp** — when the call happened.
3. **A stable identity** — a request/session/thread ID so rescans dedupe instead of double-counting.

Sources that only expose aggregate counters, subscription "units", or nothing local at all are recorded in the *Further researched sources* section of [SOURCES.md](../backend/tokenlens/SOURCES.md) instead of being guessed into existence. If you're unsure whether your tool qualifies, open a feature-request issue with the log path and a sanitized sample.

## Anatomy of an adapter

All adapters live in [`backend/tokenlens/adapters.py`](../backend/tokenlens/adapters.py) and subclass `Adapter`:

```python
class MyToolAdapter(Adapter):
    id = "my-tool"              # unique, stable, used as the tool key in records
    name = "My Tool"            # display name
    vendor = "Example Inc."
    category = "cli"            # cli | ide | desktop | local | import | vendor
    color = "#7c8cf8"
    glyph = "M"
    homepage = "https://example.com/my-tool"
    detail = "reads ~/.mytool/usage/*.jsonl per-call usage entries"
    QUALITY = "exact"           # exact | aggregate | estimate | detected
    INCREMENTAL_JSONL = True    # only if replays are safe: see below

    def enumerate(self) -> list[str]:
        """Return the files to scan this round (use glob_files helpers)."""

    def parse(self, path: str) -> Iterator[UsageRecord]:
        """Yield normalized UsageRecords for one file."""
```

The frontend icon goes in `frontend/source-icons/` (see its `NOTICE.md` for licensing rules — only artwork you have the right to redistribute).

## Normalization rules (the part that keeps the ledger honest)

- **Tokens**: populate `input`, `output`, `cache_read`, `cache_write`. If the provider reports cached input inside input tokens, subtract before filling both fields — subsets must never be counted twice.
- **Reasoning**: if the provider bills thought tokens separately from output, put them in `reasoning` (cost paths add it once, at the output rate). If output already includes them, pass `reasoning=0`.
- **Cost**: call `estimate_cost(model, ...)` for a USD estimate. If the log contains the provider's own billed cost, record that and set `cost_kind="actual"`; estimates must say `estimated`. Subscription units are not dollars — never store them as USD.
- **Identity**: build `event_id` with `stable_event_id(tool, path, scope, explicit_id, payload)`. Include the session/thread in `scope`; use the provider's request/message ID as `explicit_id` when one exists. This is what makes incremental rescans idempotent.
- **Failure closes**: use `_i()` for numeric fields (it records a diagnostic instead of silently coercing garbage to zero), skip records without usable usage, and let exceptions propagate — the scanner keeps the previous snapshot for a source whose new file fails to parse.
- **Projects**: normalize working directories with `norm_project()` — it handles Windows/POSIX path and case semantics without touching the filesystem.

## Required paperwork

1. **SOURCES.md**: document the exact format you read, quirks handled (rewinds, checkpoints, compaction, branch copies…), and upstream references (source-code links over marketing pages). Add it under a section for the tool, following the existing entries.
2. **PARSER_VERSION**: if you change the semantics of any existing adapter, bump `PARSER_VERSION` in `adapters.py` so unchanged files are re-scanned.
3. **Tests**: add fixtures under `backend/tests/` using **synthetic** JSONL lines (see `test_adapters.py`'s `ClaudeCodeParserTests` for the pattern). Cover: a normal record, a skipped/empty record, and idempotent replay (`event_id` stability). Real personal logs must never be committed.

## Checklist before opening the PR

- [ ] Synthetic-fixture tests pass: `python -m unittest discover -s backend/tests -v`
- [ ] `id` is unique among adapters (`AdapterRegistryTests` enforces this)
- [ ] SOURCES.md section written with upstream references
- [ ] Icon added or a good reason why not
- [ ] No personal data in fixtures
