"""Privacy-preserving diagnostic snapshots; no paths, prompts or credentials."""
from __future__ import annotations

import platform
import sqlite3
import time

from .config import APP_NAME, APP_VERSION


def snapshot(app):
    sources = []
    for source in app.store.list_sources():
        # Explicit allowlist: raw parser errors often contain filenames or content.
        sources.append({k: source.get(k) for k in
                        ("id", "status", "files", "records", "tokens", "last_scan_at",
                         "included", "estimated", "skipped", "parse_errors") if k in source})
        sources[-1]["has_error"] = bool(source.get("error"))
    scan = app.scanner.state.snapshot()
    return {
        "app": APP_NAME, "version": APP_VERSION, "generated": int(time.time()),
        "runtime": {"python": platform.python_version(), "system": platform.system(),
                    "release": platform.release(), "sqlite": sqlite3.sqlite_version},
        "uptime_seconds": max(0, int(time.time()) - app.boot_ts),
        "scan": {k: scan.get(k) for k in
                 ("running", "phase", "files_done", "files_total", "records", "cancelled")
                 if k in scan},
        "sources": sources, "data_version": app.store.version,
        "tasks": app.tasks.public(),
        "privacy": "No API keys, endpoint URLs, prompts, project names, file paths or raw errors are included.",
    }
