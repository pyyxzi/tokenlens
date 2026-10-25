Data-source accounting reference
===============================

Sources were checked on 2026-10-15. These references establish local formats;
installed versions may expose fewer fields. Detected installations without
readable usage remain excluded from totals. An empty log is not a zero bill.

Gemini CLI
----------
Official session management and recording implementation:
https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md
https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingService.ts
https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingTypes.ts
https://ai.google.dev/api/generate-content#UsageMetadata

Legacy JSON and current JSONL sessions (including nested subagents) are supported.
JSONL metadata/checkpoints, repeated message IDs, patches and rewinds are replayed
before usage is totaled. Cached tokens are a subset of the reported input;
thought tokens are separate from candidate output. Migrated JSON/JSONL siblings
are not counted twice. Only accounting fields are retained in replay state.

GitHub Copilot CLI
------------------
https://github.com/github/copilot-cli/issues/2000
https://github.com/github/copilot-sdk/blob/main/docs/features/streaming-events.md
https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing
https://github.com/github/copilot-cli/issues/3994

The adapter reads persisted session.shutdown per-model cumulative snapshots in
~/.copilot/session-state/*/events.jsonl, using the latest available snapshot.
Reasoning is a subset of output. Copilot premium-request and AIU multipliers
are not dollars; no native billing unit is mislabeled as a USD charge. Public
model prices are estimates. Live sessions and CLI versions that omit shutdown
metrics may have incomplete local totals, so this source is marked aggregate.

Pi Coding Agent
---------------
https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md
https://github.com/earendil-works/pi/blob/main/packages/ai/src/models.ts
https://github.com/earendil-works/pi/blob/main/packages/ai/src/types.ts

Reads assistant messages and dedicated usage, compaction and branch_summary
entries under ~/.pi/agent/sessions/**/*.jsonl. Input and cache buckets are
separate. Reasoning is a subset of output. Reported usage.cost.total is retained
as actual; missing costs use model-price estimates or remain unknown.

Goose
-----
https://github.com/aaif-goose/goose/blob/main/crates/goose/src/config/paths.rs
https://github.com/aaif-goose/goose/blob/main/crates/goose/src/session/session_manager.rs
https://github.com/aaif-goose/goose/blob/main/crates/goose-provider-types/src/conversation/token_usage.rs

Discovers sessions/sessions.db in platform data directories and an absolute
GOOSE_PATH_ROOT/data override. Reads the authoritative usage_ledger first;
older sessions without ledger entries use accumulated session totals, never
both. Goose input includes cache reads/writes, so those subsets are separated
before totals and costs. provider_reported cost is actual; estimated and older
ambiguous costs remain estimates. Read-only SQLite, query time limits and source
fingerprints protect the previous committed index on failure.

Imports
-------
User-selected CSV / JSON / JSONL and supported native CLI logs are validated in
full before publication. Limit: 32 MiB input, 100,000 accounting records, four
pending previews, 64 MiB total preview storage and 15-minute preview lifetime.
Every accounting row needs a model, valid timestamp and nonnegative finite usage.
One malformed row rejects the entire file. Prompt text, response content, keys
and arbitrary metadata are discarded before saving normalized ledgers.

Retained import ledgers live in the app data directory and survive restart and
rescanning. Reimports merge by source/session/provider ID; anonymous rows use
canonical accounting fields plus an occurrence number so legitimate identical
calls within a file are not collapsed. A file selection upload grants no API
permission to read arbitrary filesystem paths. Only normalized USD costs are
accepted as charges; subscription units cannot substitute for actual fees.

Further researched sources
--------------------------
Amp: https://ampcode.com/api/external documents authenticated per-thread cloud
usage. No stable authoritative local usage format was established, so no guessed
token extraction or automatic cloud request is added. Existing broad adapters
continue to report unmeasured sources honestly. New adapters should be added only
with a reproducible accounting schema, identity and cache normalization.

