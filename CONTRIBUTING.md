# Contributing to TokenLens

Thanks for considering a contribution! Issues, data-source adapters, bug fixes and docs are all welcome.

**中文贡献者**：欢迎用中文提交 issue 和 PR，正文请尽量附上英文简述，方便更多人参与讨论。

## The one rule that matters most

TokenLens is an accounting tool. A wrong number is worse than a missing number. When in doubt, fail closed: skip an unparseable record, mark a source as approximate, keep the previous snapshot — never guess a value into the ledger.

## Good first contributions

- **New data-source adapters** — the single highest-value contribution. Start with [docs/adding-a-source.md](docs/adding-a-source.md).
- **Bug reports with sanitized log samples** — many format quirks only surface on real installations. Redact prompts, keys and personal paths before posting.
- **New tests against synthetic fixtures** — extending coverage of existing adapters.
- **Docs and translations** — the UI ships zh-CN / en-US; help entries live in `frontend/i18n.js`.

## Development setup

You only need Python for backend work:

```bash
pip install -r backend/requirements.txt
python -m unittest discover -s backend/tests -v
```

Full app development (window, tray, packaging) additionally needs Windows 10/11 x64, WebView2, Node.js LTS, Rust MSVC, and Python 3.14 series. Build with:

```powershell
.\build.ps1 -Python 'C:\path\to\python.exe' -WorkDir 'D:\tokenlens-build' -Dev
```

Keep `WorkDir` outside the repository — build artifacts must never enter the source tree.

For frontend changes, check asset references and derived metric behavior:

```bash
python tools/check_frontend_assets.py
node --test tools/test_usage_metrics.cjs
```

To inspect the real frontend and API without using personal usage data, run:

```bash
python -B tools/preview_workspace.py
# Empty-state preview:
python -B tools/preview_workspace.py --empty
```

Open the local URL printed at startup. The helper creates only synthetic usage in a disposable workspace, isolates application and source-log directories, and disables scanning, AI requests, imports and destructive API operations. Stop with Ctrl+C or create the printed `STOP_PREVIEW` file; the helper removes its temporary workspace on exit. Do not use real logs or credentials for UI screenshots.

## Pull requests

1. Keep PRs focused; one adapter or one fix per PR.
2. Add or update tests that would have caught the bug you fixed.
3. Run the backend suite before pushing: `python -m unittest discover -s backend/tests`.
4. Fill in the PR template; call out anything subtle (dedup keys, cache normalization, actual-vs-estimated costs).
5. If a data source's accounting semantics changed, update [backend/tokenlens/SOURCES.md](backend/tokenlens/SOURCES.md) in the same PR.

Never commit: real personal logs, your local database, API keys, or anything under a `WorkDir`.

## CI

GitHub Actions runs on every push and PR:

- backend unit tests (Python),
- frontend asset integrity, JavaScript syntax and derived usage metric tests,
- `cargo check` of the Tauri host (Windows).

Tag pushes (`v*`) build the installer and portable archive and attach them to a GitHub Release.

## Security

Do not open issues containing credentials or raw personal logs. See [SECURITY.md](SECURITY.md) for the reporting process.
