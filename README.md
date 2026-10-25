# TokenLens

**Local-first AI usage & cost workbench for Windows.** TokenLens scans the logs your AI coding tools already write to disk and turns them into one unified ledger: tokens, estimated cost, models, projects and per-session detail — without uploading anything.

[![CI](https://github.com/pyyxzi/tokenlens/actions/workflows/ci.yml/badge.svg)](https://github.com/pyyxzi/tokenlens/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078D4)](https://aka.ms/windows)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[简体中文](README.zh-CN.md) | English

---

![Overview dashboard](docs/screenshots/overview.png)

<details>
<summary><b>More screenshots</b></summary>

| Usage by tool | Data sources |
| :---: | :---: |
| ![Usage by tool](docs/screenshots/by-tool.png) | ![Data sources](docs/screenshots/sources.png) |

</details>

## Why TokenLens

If you use several AI tools — Claude Code, Codex CLI, Gemini CLI, Cursor, Copilot, … — every one of them keeps its own usage log in its own format. Answering *"what did I actually burn this month, on which model, in which project?"* means reverse-engineering a dozen log formats by hand.

TokenLens does that once, properly:

- **One unified ledger.** 25+ data sources normalized into a single schema: input / output / cache-read / cache-write / reasoning tokens, per call, with stable event IDs so rescans stay idempotent.
- **Cost you can reason about.** Public per-model price tables plus your own manual prices produce USD estimates; provider-reported actuals are kept when the source exposes them. Subscription-only sources are labeled honestly instead of being mislabeled as dollars.
- **Local-first privacy.** Everything runs on your machine and lands in a local SQLite database. API keys for the optional AI assistant are protected with Windows DPAPI. No telemetry, no cloud dashboard.
- **Built for big histories.** Incremental scanning (unchanged files are skipped by fingerprint), paginated drill-downs, background aggregation — validated against a 300k-record, ~260 MiB database.
- **Import & export.** CSV / JSON / JSONL import with full pre-validation, snapshot backup & restore, CSV export.
- **Optional AI analysis.** A built-in assistant can analyze usage, draft reports and optimize prompts — it only ever calls the provider *you* configure, and only when you ask.

## Supported data sources

CLI agents: Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI, OpenCode, Aider, Goose, Pi, Kimi, ZCode, WorkBuddy, DeepSeek Harness, generic API logs
IDE & editors: Cursor, Windsurf, Trae, Qoder, Continue, Cline-family, JetBrains AI (via Cline family), Claude Desktop
Local runtimes: Ollama, LM Studio, Cherry Studio
Plus user imports: CSV / JSON / JSONL

Each adapter documents the exact log format it reads, with upstream references, in [backend/tokenlens/SOURCES.md](backend/tokenlens/SOURCES.md). Sources whose logs can only give approximate totals are marked as such in the UI — TokenLens never presents a guess as an exact bill.

## Getting started

Download the latest installer from [**Releases**](../../releases) (Windows 10/11 x64, NSIS installer), or build from source:

**Requirements:** Windows 10/11 x64, WebView2, Node.js LTS, Rust (MSVC toolchain), Python 3.14 series (3.14.8+ recommended).

```powershell
git clone https://github.com/pyyxzi/tokenlens.git
cd REPO
.\build.ps1 -Python 'C:\path\to\python.exe' -WorkDir 'D:\tokenlens-build'
```

`WorkDir` holds the virtual environment, dependencies and build caches and should live **outside** the repository; the installer ends up in `<WorkDir>\target\...\bundle`. Append `-Dev` for a development build, or `-UseExistingPython` if you already have an isolated environment.

For day-to-day backend work you don't need the full toolchain — Python alone is enough to run the tests:

```bash
python -m unittest discover -s backend/tests -v
```

## How it works

```text
┌──────────────┐   spawns    ┌──────────────────┐   reads    ┌─────────────┐
│  src-tauri/  │ ──────────▶ │    backend/      │ ─────────▶ │ tool logs   │
│  Rust host:  │  sidecar    │  Python service: │            │ (25+ tools) │
│  window,tray │ ◀────────── │  scan · parse ·  │            └─────────────┘
│  IPC bridge  │  localhost  │  aggregate ·     │
└──────┬───────┘  + secret   │  price · serve   │   writes   ┌─────────────┐
       │ loads     token     └──────────────────┘ ─────────▶ │ SQLite DB   │
       ▼                                                      └─────────────┘
┌──────────────┐
│  frontend/   │  vanilla JS/CSS workbench: dashboard, rankings,
│  UI bundle   │  drill-downs, themes, zh-CN / en-US
└──────────────┘
```

- **`src-tauri/`** — native window, tray, single-instance, and the Python sidecar lifecycle (job objects, crash recovery, generation-based reconnection).
- **`backend/`** — the analytics engine: adapters for every data source, incremental scanner, aggregator, price tables, AI assistant services, import/backup.
- **`frontend/`** — the workbench UI, no framework, no build step.

## Data & privacy

- Statistics are computed locally; the database lives in the system app-data directory.
- The analytics service binds to `127.0.0.1` on a random port, authenticated with a per-launch secret; the frontend talks to it only through the native bridge.
- The optional AI assistant sends requests **only** to the provider you configure, only when you trigger it. Credential handling is described in [SECURITY.md](SECURITY.md).

## Documentation

- [项目结构 / Project structure](项目结构.md) — reading map for the codebase
- [Data-source accounting reference](backend/tokenlens/SOURCES.md) — every supported log format, with upstream links
- [Validation record](VALIDATION.md) — functional, robustness and performance test results
- [Adding a data source](docs/adding-a-source.md) — how to write a new adapter

## Contributing

Issues and pull requests are welcome! New data-source adapters are the single most valuable contribution — see the [adapter guide](docs/adding-a-source.md) and [CONTRIBUTING.md](CONTRIBUTING.md) to start. Good first issues are labeled `good first issue`.

## License

[MIT](LICENSE) — third-party notices in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [`licenses/`](licenses/).
