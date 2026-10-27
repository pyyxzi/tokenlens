"""Run the real API and frontend with disposable, synthetic local usage only.

    python -B tools/preview_workspace.py
    python -B tools/preview_workspace.py --empty

Stop with Ctrl+C/SIGTERM, or create the STOP_PREVIEW file printed at startup.
No browser is opened. No application credentials or source logs are loaded.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import json
import mimetypes
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
FRONTEND = ROOT / "frontend"
MARKER = "TOKENLENS_ISOLATED_PREVIEW"
PINNED_CONFIG = {
    "scan_on_start": False,
    "auto_scan_minutes": 0,
    "api_log_roots": [],
    "ai_hedge_enabled": False,
    "ai_warmup_enabled": False,
}
# File import/restore, AI, scan, reveal, reset and shutdown routes are deliberately
# absent. These operations only change synthetic rows in the disposable index.
MANAGE_ACTIONS = frozenset({
    "project_save", "price_save", "price_delete",
    "reconcile_preview", "reconcile_detail", "reconcile_delete",
})
PROJECTS = (
    r"Z:\TokenLens-Preview\示例工作区\产品体验与中文长项目名称布局验证",
    r"Z:\TokenLens-Preview\ExampleOrganization\PlatformEngineering\Applications\CustomerExperience\VeryLongWindowsProjectPathForOverflowAndResponsiveLayoutVerification",
    "SyntheticInternationalAnalyticsAndTokenAccountingDashboardWithAnExceptionallyLongASCIIProjectName",
    "仅输出示例_OutputOnly",
    "合成电商平台",
    "Synthetic-Design-System",
    "示例数据仓库",
    "Synthetic-Mobile-App",
    "合成文档中心",
    "Synthetic-Observability",
    "示例模型评测",
    "Synthetic-Research-Workbench",
)
MODELS = (
    "claude-opus-4-6", "claude-sonnet-4-6", "gpt-5.2",
    "gpt-5-mini", "gemini-2.5-pro", "gemini-2.5-flash",
    "deepseek-reasoner", "qwen3-coder", "glm-4.7", "kimi-k2.7",
)
TOOL_IDS = ("claude-code", "codex", "gemini-cli", "opencode", "tokenlens-ai")


def isolate(sandbox: Path) -> None:
    """Run before any tokenlens import; all backend home constants are captured here."""
    if any(name == "tokenlens" or name.startswith("tokenlens.") for name in sys.modules):
        raise RuntimeError("preview isolation must precede all tokenlens imports")
    paths = {
        "HOME": sandbox / "home",
        "USERPROFILE": sandbox / "home",
        "APPDATA": sandbox / "roaming",
        "LOCALAPPDATA": sandbox / "local",
        "TOKENLENS_HOME": sandbox / "app",
        "XDG_CONFIG_HOME": sandbox / "xdg-config",
        "XDG_DATA_HOME": sandbox / "xdg-data",
        "XDG_CACHE_HOME": sandbox / "xdg-cache",
        "XDG_STATE_HOME": sandbox / "xdg-state",
        "CCSWITCH_HOME": sandbox / "ccswitch",
        "DSH_HOME": sandbox / "deepseek",
        "CLAUDE_DESKTOP_HOME": sandbox / "claude-desktop",
        "QODERWORK_HOME": sandbox / "qoderwork",
        "KIMI_CODE_HOME": sandbox / "kimi",
        "GOOSE_PATH_ROOT": sandbox / "goose",
        "CODEX_HOME": sandbox / "codex",
        "CLAUDE_CONFIG_DIR": sandbox / "claude-code",
        "TMPDIR": sandbox / "tmp",
        "TMP": sandbox / "tmp",
        "TEMP": sandbox / "tmp",
    }
    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    drive, tail = os.path.splitdrive(str(paths["USERPROFILE"]))
    os.environ["HOMEDRIVE"], os.environ["HOMEPATH"] = drive, tail
    # Some real API operations create their own TemporaryDirectory snapshots.
    tempfile.tempdir = str(paths["TMP"])
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.chdir(sandbox)
    sys.path.insert(0, str(ROOT / "backend"))


def seed(app) -> dict:
    from tokenlens import adapters, pricing
    from tokenlens.models import SourceReport, Status, UsageRecord

    now = datetime.now().replace(microsecond=0)
    by_tool = defaultdict(list)
    for session_index in range(48):
        project_index = session_index % len(PROJECTS)
        tool = TOOL_IDS[session_index % len(TOOL_IDS)]
        session = "preview-session-%02d-synthetic" % (session_index + 1)
        model = MODELS[session_index % len(MODELS)]
        # Thirty local calendar days; every session stays within one project/tool.
        date = now.date() - timedelta(days=session_index % 30)
        for event_index in range(24):
            ordinal = session_index * 24 + event_index
            ts = int(datetime.combine(date, datetime.min.time()).timestamp())
            ts = min(ts + (8 + session_index % 8) * 3600 + event_index * 61,
                     int(now.timestamp()) - (23 - event_index))
            output_only = project_index == 3
            usage = {
                "input": 0 if output_only else 90000 + ordinal % 19 * 11000,
                "output": 16000 + ordinal % 13 * 2100,
                "cache_read": 0 if output_only else 120000 + ordinal % 17 * 17000,
                "cache_write": 0 if output_only else 5000 + ordinal % 9 * 3000,
                "reasoning": 4000 + ordinal % 7 * 1800,
            }
            internal = tool == "tokenlens-ai"
            extra = {"synthetic": True, "preview_dataset": MARKER}
            if internal:
                # Same event metadata contract as App.record_ai_usage; one event
                # denotes one HTTP attempt, with separated normalized token counts.
                outcome = "cancelled" if ordinal % 29 == 0 else "failed" if ordinal % 31 == 0 else "completed"
                role = ("primary", "retry", "repair", "model-list")[event_index % 4]
                elapsed = 900 + ordinal % 23 * 173
                extra.update({
                    "operation": "assistant" if event_index % 2 else "optimize",
                    "provider": "synthetic.invalid",
                    "request_type": "synthetic_http_attempt",
                    "request_role": role, "purpose": role,
                    "outcome": outcome, "status": outcome,
                    "elapsed_ms": elapsed, "duration_ms": elapsed,
                    "usage_known": True,
                    "http_status": 499 if outcome == "cancelled" else 503 if outcome == "failed" else 200,
                })
            cost = pricing.estimate_cost(model, **{
                "inp": usage["input"], "out": usage["output"],
                "cache_read": usage["cache_read"], "cache_write": usage["cache_write"],
                "reasoning": usage["reasoning"],
            })
            record = UsageRecord(
                tool=tool, ts=ts, model=model, project=PROJECTS[project_index],
                session=session, requests=1, cost=cost,
                cost_kind="estimated", cost_source="price-table",
                price_version=pricing.PRICE_VERSION,
                src="internal://tokenlens-ai" if internal else "preview://synthetic/" + tool,
                event_id="preview-synthetic-%04d" % ordinal, extra=extra, **usage,
            )
            by_tool[tool].append(record)

    meta = {item["id"]: item for item in adapters.adapter_meta()}
    for tool, records in by_tool.items():
        if tool == "tokenlens-ai":
            for record in records:
                app.store.append_usage(record, event_id=record.event_id)
            source = "internal://tokenlens-ai"
        else:
            source = "preview://synthetic/" + tool
            app.store.replace_file(source, tool, 0, now.timestamp(), records,
                                   note="Synthetic preview fixture; no source file exists")
        item = meta.get(tool) or {
            "id": tool, "name": "TokenLens AI", "vendor": "Synthetic provider",
            "category": "api", "color": "#38bdf8", "glyph": "AI", "homepage": "",
        }
        partial = tool == "gemini-cli"
        report = SourceReport(
            **{key: item[key] for key in ("id", "name", "vendor", "category", "color", "glyph", "homepage")},
            status=Status.PARTIAL if partial else Status.OK, paths=[source],
            note="Synthetic usage only; one omitted fixture event" if partial else "Synthetic usage only; no logs scanned",
            error="Synthetic partial example: one malformed usage event was omitted" if partial else "",
            included=True, estimated=True, quality="exact",
            last_activity=max(record.ts for record in records),
            last_scan_at=int(now.timestamp()), last_success_at=int(now.timestamp()),
            raw_records=len(records) + int(partial), accepted_records=len(records),
            skipped_records=int(partial),
        )
        app.store.save_source(report, sum(record.total for record in records),
                              size=0, files=0 if tool == "tokenlens-ai" else 1,
                              records=len(records))
    app.store.set_scan_time(int(now.timestamp()))
    app.clear_dashboard_cache()
    return app.agg.summary("all")


def preview_handler(app):
    from tokenlens.server import RequestError, make_handler

    class PreviewHandler(make_handler(app)):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                return super().do_GET()
            try:
                self.check_origin()  # Keep the product's Host/origin policy intact.
                decoded = unquote(path, errors="strict")
                if not decoded.startswith("/") or "\\" in decoded or "\x00" in decoded:
                    raise RequestError("invalid static path", 400)
                parts = decoded.lstrip("/").split("/")
                if any(part in (".", "..") or ":" in part for part in parts):
                    raise RequestError("invalid static path", 403)
                target = (FRONTEND / (decoded.lstrip("/") or "index.html")).resolve()
                if not target.is_relative_to(FRONTEND.resolve()) or not target.is_file():
                    raise RequestError("static file not found", 404)
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if target.suffix == ".js":
                    content_type = "text/javascript"
                if content_type.startswith("text/"):
                    content_type += "; charset=utf-8"
                self.request_deadline()
                return self._send(200, target.read_bytes(), content_type)
            except (RequestError, ValueError, OSError) as exc:
                return self.json_out({"ok": False, "error": str(exc)}, getattr(exc, "status", 400))

        def api_post(self, path, q, body):
            # do_POST still performs the real origin, X-TokenLens, body-size and
            # schema checks before this preview-only policy can run.
            if path == "/api/config":
                for key, pinned in PINNED_CONFIG.items():
                    if key in body and (body[key] != pinned or type(body[key]) is not type(pinned)):
                        raise RequestError("isolated preview keeps %s fixed" % key, 403)
            elif path == "/api/manage" and body.get("action") in MANAGE_ACTIONS:
                pass
            else:
                raise RequestError("operation disabled in isolated preview", 403)
            return super().api_post(path, q, body)

    return PreviewHandler


def preview_server():
    from tokenlens.server import ReusableServer

    class PreviewServer(ReusableServer):
        # Request snapshots must close before close_app and sandbox removal.
        daemon_threads = False
        block_on_close = True

        def __init__(self, *args, **kwargs):
            self._preview_connections = set()
            self._preview_connections_lock = threading.Lock()
            self._preview_closing = False
            super().__init__(*args, **kwargs)

        def finish_request(self, request, client_address):
            with self._preview_connections_lock:
                # A thread accepted just before shutdown may not have run yet.
                # The base request-thread finally still closes it and frees its slot.
                if self._preview_closing:
                    return
                self._preview_connections.add(request)
            try:
                return super().finish_request(request, client_address)
            finally:
                with self._preview_connections_lock:
                    self._preview_connections.discard(request)

        def server_close(self):
            # ReusableServer stops its input watchdog before joining requests.
            # Interrupt headers, bodies, keep-alives and blocked response writes
            # first; joining then releases each request's SQLite snapshot.
            with self._preview_connections_lock:
                self._preview_closing = True
                connections = tuple(self._preview_connections)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            super().server_close()

    return PreviewServer


def close_app(app) -> None:
    app.stop_event.set()
    app.tasks.cancel_all()
    app.scanner.cancel()
    app._auto_scan_thread.join(timeout=6)
    app.scanner.wait(timeout=5)
    app.backup.close()
    app.assistant.close()
    app.store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--empty", action="store_true", help="start with an empty disposable index")
    args = parser.parse_args()
    if not (FRONTEND / "index.html").is_file():
        parser.error("frontend/index.html is required")
    stopped = threading.Event()

    def interrupt(_signum, _frame):
        stopped.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupt)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupt)

    # Keep all files created by this helper under tools, including its temporary
    # index, config, staging databases and temporary snapshot/backup directories.
    with tempfile.TemporaryDirectory(prefix=".preview-workspace-", dir=TOOLS) as directory:
        sandbox = Path(directory).resolve()
        previous_cwd, previous_tempdir = Path.cwd(), tempfile.tempdir
        isolate(sandbox)
        from tokenlens.config import Config
        from tokenlens.server import App

        app = server = worker = None
        try:
            cfg = Config(str(sandbox / "app" / "config.json"))
            cfg.update({**PINNED_CONFIG, "host": "127.0.0.1", "range": "30d"})
            app = App(cfg)
            summary = app.agg.summary("all") if args.empty else seed(app)
            server = preview_server()(("127.0.0.1", 0), preview_handler(app))
            cfg.set("port", server.server_address[1])
            url = "http://127.0.0.1:%d/" % server.server_address[1]
            stop_file = sandbox / "STOP_PREVIEW"
            worker = threading.Thread(target=server.serve_forever, daemon=True, name="isolated-preview-http")
            worker.start()
            print(json.dumps({
                "marker": MARKER, "isolated": True, "ready": True,
                "url": url, "pid": os.getpid(), "empty": args.empty,
                "sandbox": str(sandbox), "stop_file": str(stop_file),
                "records": summary["records"], "tokens": summary["total"],
                "projects": summary["projects"], "sessions": summary["sessions"],
                "tools": summary["tools"], "models": summary["models"],
                "cost_usd": round(summary["cost"], 4),
            }, ensure_ascii=True), flush=True)
            print("%s URL %s" % (MARKER, url), flush=True)
            print("Stop: Ctrl+C/SIGTERM, or create %s" % stop_file, flush=True)
            while not stopped.wait(0.4):
                if stop_file.exists() or not worker.is_alive():
                    break
        finally:
            if app is not None:
                app.stop_event.set()
            if server is not None:
                if worker is not None and worker.is_alive():
                    server.shutdown()
                    worker.join(timeout=6)
                server.server_close()
            if app is not None:
                close_app(app)
            os.chdir(previous_cwd)
            tempfile.tempdir = previous_tempdir
    print(MARKER + " stopped; disposable workspace removed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
