# -*- coding: utf-8 -*-
"""
TokenLens · 扫描调度器

职责：枚举所有数据源 → 判断文件新鲜度 → 解析变化的部分 → 写索引 → 产出数据源报告。
扫描在后台线程执行，前端通过 /api/scan/status 轮询进度。
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from . import adapters as A
from .models import SourceReport, Status, now_ts
from .incremental_logs import JsonlPlan, supported as incremental_supported


class ScanState:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.phase = "idle"          # idle / enumerating / parsing / done / error
        self.done = 0
        self.total = 0
        self.current = ""
        self.started = 0
        self.finished = 0
        self.parsed_files = 0
        self.reused_files = 0
        self.records = 0
        self.bytes_new = 0
        self.bytes_read = self.parsed_bytes = self.verification_bytes = 0
        self.appended_files = self.appended_records = self.pending_bytes = 0
        self.errors = []
        self.error_count = 0
        self.log = []
        self.cancel_requested = False
        self.target = ""

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running, "phase": self.phase,
                "done": self.done, "total": self.total,
                "percent": round(self.done * 100.0 / self.total, 1) if self.total else 0,
                "current": self.current, "started": self.started,
                "finished": self.finished, "parsed_files": self.parsed_files,
                "reused_files": self.reused_files, "records": self.records,
                "bytes_new": self.bytes_new, "errors": self.errors[-8:], "error_count": self.error_count,
                "bytes_read": self.bytes_read, "parsed_bytes": self.parsed_bytes,
                "verification_bytes": self.verification_bytes,
                "appended_files": self.appended_files, "appended_records": self.appended_records,
                "pending_bytes": self.pending_bytes,
                "log": self.log[-60:],
                "elapsed": round((self.finished or time.time()) - self.started, 1) if self.started else 0,
                "cancel_requested": self.cancel_requested,
                "target": self.target,
            }

    def say(self, text):
        with self.lock:
            self.log.append({"t": int(time.time()), "m": text})
            del self.log[:-120]
            self.current = text


class Scanner:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.state = ScanState()
        self._thread = None
        self._meta = None
        self._meta_ts = 0
        self._side = {}           # 非 token 侧写数据（补全行数等）

    # ------------------------------------------------------------------
    def meta(self):
        if self._meta is None or time.time() - self._meta_ts > 300:
            self._meta = A.adapter_meta()
            self._meta_ts = time.time()
        return self._meta

    def side_stats(self):
        return dict(self._side)

    # ------------------------------------------------------------------
    def start(self, force: bool = False, quick: bool = None, source_id: str = None) -> bool:
        with self.state.lock:
            if self.state.running:
                return False
            # Persist before announcing a running scan. Disk errors must leave
            # subsequent starts available instead of stranding the busy flag.
            if quick is not None:
                self.cfg.set("codex_quick", bool(quick))
            target = str(source_id or "").strip()
            self._begin_state(target)
            try:
                t = threading.Thread(target=self._run, args=(force, target, True), daemon=True,
                                     name="tokenlens-scan")
                self._thread = t
                t.start()
            except BaseException:
                self._thread = None
                self.state.running = False
                self.state.phase = "error"
                self.state.finished = time.time()
                raise
        return True

    def _begin_state(self, source_id=""):
        """Initialize before the worker can run or receive cancellation."""
        st = self.state
        with st.lock:
            st.running = True
            st.phase = "enumerating"
            st.done = st.total = 0
            st.parsed_files = st.reused_files = st.records = st.bytes_new = 0
            st.bytes_read = st.parsed_bytes = st.verification_bytes = 0
            st.appended_files = st.appended_records = st.pending_bytes = 0
            st.started = time.time()
            st.finished = 0
            st.errors, st.log = [], []
            st.error_count = 0
            st.cancel_requested = False
            st.target = source_id or ""

    def cancel(self) -> bool:
        """Request a cooperative cancellation of the current scan."""
        with self.state.lock:
            if not self.state.running:
                return False
            self.state.cancel_requested = True
            self.state.phase = "cancelling"
            self.state.log.append({"t": int(time.time()), "m": "正在取消扫描…"})
        return True

    def wait(self, timeout: float = 600):
        if self._thread:
            self._thread.join(timeout)
        return not self.state.running

    # ------------------------------------------------------------------
    def _run(self, force: bool, source_id: str = "", initialized: bool = False):
        st = self.state
        scan_started_at = now_ts()
        if not initialized:
            self._begin_state(source_id)
        dependent_writes = []
        try:
            if st.cancel_requested:
                raise _ScanCancelled()
            exclude = set(self.cfg.get("exclude_tools") or [])
            self.adapters = [a for a in A.build_adapters(self.cfg)
                             if a.id not in exclude and (not source_id or a.id == source_id)]
            if source_id and not self.adapters:
                raise ValueError("unknown or excluded source: " + source_id)
            previous = {r["id"]: r for r in self.store.list_sources()}
            known_files = self.store.query("SELECT path,tool FROM files")
            reports, alive, complete, candidates = {}, {}, set(), []
            owners = {}
            st.say("正在枚举数据源…")

            def fail(ad, message):
                info = reports[ad.id]
                info["error_count"] = info.get("error_count", 0) + 1
                if len(info["errors"]) < 16:
                    info["errors"].append(message)
                with st.lock:
                    st.errors.append(ad.id + ": " + message)
                    del st.errors[:-64]
                    st.error_count += 1

            for ad in self.adapters:
                if st.cancel_requested:
                    raise _ScanCancelled()
                info = reports[ad.id] = {"files":0, "size":0, "errors":[], "path":"", "detected":False}
                alive[ad.id] = set()
                try:
                    files = list(dict.fromkeys(os.path.abspath(p) for p in ad.enumerate()))
                    # An inaccessible directory can look empty to glob(). Existing
                    # previously-indexed files must not be interpreted as deletion.
                    if not files and any(r["tool"] == ad.id and os.path.exists(r["path"]) for r in known_files):
                        raise OSError("source discovery returned no files although indexed files still exist")
                    if getattr(ad, "enumeration_complete", True):
                        complete.add(ad.id)
                    else:
                        fail(ad, "file limit reached; discovery is incomplete and history was retained")
                    listed = set(files)
                    for known in known_files:
                        if known["tool"] != ad.id or known["path"] in listed:
                            continue
                        try:
                            os.stat(known["path"])
                        except FileNotFoundError:
                            continue
                        except OSError:
                            pass
                        complete.discard(ad.id)
                        fail(ad, "discovery omitted an indexed file; history was retained")
                        break
                    for path in files:
                        alive[ad.id].add(path)
                        if path in owners:
                            fail(ad, "file already belongs to " + owners[path] + ": " + os.path.basename(path))
                            continue
                        owners[path] = ad.id
                        try:
                            stat = os.stat(path)
                            if not os.path.isfile(path):
                                continue
                            deps = ad.cache_dependencies(path) if hasattr(ad, "cache_dependencies") else ()
                            fingerprint = A.file_fingerprint(path, deps)
                            candidates.append((ad, path, stat.st_size, stat.st_mtime, fingerprint))
                            info["files"] += 1
                            info["size"] += stat.st_size
                            info["path"] = info["path"] or path
                        except OSError as exc:
                            complete.discard(ad.id)
                            fail(ad, "file metadata unavailable: " + str(exc))
                except Exception as exc:
                    complete.discard(ad.id)
                    fail(ad, "discovery failed: " + str(exc))
                if not info["files"]:
                    try:
                        info["detected"], info["path"] = ad.probe()
                    except Exception as exc:
                        fail(ad, "probe failed: " + str(exc))
            try:
                max_mb = max(1, min(4096, int(self.cfg.get("max_file_mb") or 400)))
                workers = max(1, min(32, int(self.cfg.get("workers") or 4)))
            except (ValueError, TypeError):
                max_mb, workers = 400, 4
            states = self.store.file_states(p for _, p, *_ in candidates)
            stale = []
            with st.lock:
                st.total = len(candidates)
                st.phase = "parsing"
            for item in candidates:
                ad, path, size, mtime, fingerprint = item
                old = states.get(path)
                fresh = old and old.get("fingerprint") == fingerprint
                if fresh and not force:
                    with st.lock:
                        st.done += 1
                        st.reused_files += 1
                elif size > max_mb * 1024 * 1024:
                    fail(ad, "file exceeds %d MB limit: %s" % (max_mb, os.path.basename(path)))
                    with st.lock:
                        st.done += 1
                else:
                    stale.append(item)
            st.say("需要解析 %d 个文件，复用 %d 个文件" % (len(stale), st.reused_files))
            def save_result(ad, path, size, mtime, fingerprint, records):
                if st.cancel_requested:
                    raise _ScanCancelled()
                plan = records if isinstance(records, JsonlPlan) else None
                method = self.store.append_file if plan and plan.append else self.store.replace_file
                options = {"should_cancel": lambda: st.cancel_requested}
                if plan:
                    options.update(parse_cursor=plan.cursor,
                                   expected_fingerprint=(states.get(path) or {}).get("fingerprint"))
                    # JSONL may grow between discovery and parsing. Publish only
                    # the plan's captured, verified boundary; later bytes must
                    # remain stale so the following scan reads their suffix.
                    size, mtime, fingerprint = plan.stat.st_size, plan.stat.st_mtime, plan.fingerprint
                n = method(path, ad.id, size, mtime, plan.records() if plan else records,
                    note=(ad.detail or "")[:200], fingerprint=fingerprint, **options)
                with st.lock:
                    st.parsed_files += 1
                    st.bytes_new += (size - (states.get(path) or {}).get("size", 0)) if plan and plan.append else size
                    st.bytes_read += plan.bytes_read if plan else size
                    st.parsed_bytes += plan.parsed_bytes if plan else size
                    st.verification_bytes += plan.verification_bytes if plan else 0
                    st.pending_bytes += plan.pending_bytes if plan else 0
                    if plan and plan.append:
                        st.appended_files += 1
                        st.appended_records += n
                    st.records += n
                st.say("%s · %s（%d 条）" % (ad.name, os.path.basename(path), n))

            def work(item):
                ad, path, size, mtime, fingerprint = item
                if incremental_supported(ad, path):
                    return JsonlPlan(ad, path, states.get(path), fingerprint, force=force,
                                     cancelled=lambda: st.cancel_requested,
                                     event_exists=lambda event_id: self.store.file_event_exists(
                                         ad.id, event_id, path))
                # Preflight guarantees a swallowed open error can never masquerade
                # as a successful empty parse. Required schema errors propagate.
                with open(path, "rb") as fh:
                    fh.read(1)
                def validated_records():
                    count = 0
                    # Keep diagnostics active while the parser is consumed;
                    # eagerly listing large DB/JSON sources retained every
                    # UsageRecord for all queued workers at once.
                    with A.parsing_diagnostics(cancelled=lambda: st.cancel_requested) as issues:
                        parser = (ad.scan_db(path) if path.lower().endswith((".db", ".sqlite", ".sqlite3", ".vscdb"))
                                  and hasattr(ad, "scan_db") else ad.parse(path))
                        for record in parser:
                            count += 1
                            yield record
                    if issues:
                        raise ValueError("%d malformed usage record(s); previous snapshot retained" % len(issues))
                    deps = ad.cache_dependencies(path) if hasattr(ad, "cache_dependencies") else ()
                    if A.file_fingerprint(path, deps) != fingerprint:
                        raise OSError("source changed while parsing; retry on the next scan")
                    old = states.get(path)
                    if (not count and size and old and old.get("records")
                            and not (ad.id == "claude-desktop" and os.path.basename(path).lower() == "audit.jsonl")):
                        raise ValueError("nonempty source yielded no usage; previous snapshot retained")
                return self.store.prepare_file(path, validated_records(), should_cancel=lambda: st.cancel_requested)

            # Bound both queued files and retained results. Completed futures are
            # removed immediately instead of retaining an entire scan in memory.
            if stale:
                iterator = iter(stale)
                pool = ThreadPoolExecutor(max_workers=workers)
                pending = {}
                try:
                    def submit_next():
                        try:
                            item = next(iterator)
                        except StopIteration:
                            return False
                        pending[pool.submit(work, item)] = item
                        return True
                    for _ in range(workers * 2):
                        if not submit_next():
                            break
                    while pending:
                        if st.cancel_requested:
                            raise _ScanCancelled()
                        done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                        for fut in done:
                            ad, path, size, mtime, fingerprint = pending.pop(fut)
                            records = None
                            try:
                                records = fut.result()
                                if ad.id == "claude-desktop":
                                    dependent_writes.append((ad, path, size, mtime, fingerprint, records))
                                else:
                                    save_result(ad, path, size, mtime, fingerprint, records)
                            except Exception as exc:
                                if st.cancel_requested or isinstance(exc, _ScanCancelled):
                                    raise _ScanCancelled() from exc
                                fail(ad, "parse failed (%s): %s" % (os.path.basename(path), exc))
                            finally:
                                if records is not None and ad.id != "claude-desktop" and hasattr(records, "close"):
                                    records.close()
                                with st.lock:
                                    st.done += 1
                            submit_next()
                finally:
                    for fut in pending:
                        fut.cancel()
                    pool.shutdown(wait=True, cancel_futures=True)
                    # Completed futures can own staged parses even when the
                    # scan is cancelled before they reach publication.
                    for fut in pending:
                        if not fut.cancelled() and fut.exception() is None:
                            result = fut.result()
                            if hasattr(result, "close"):
                                result.close()
            if st.cancel_requested:
                raise _ScanCancelled()
            if dependent_writes and not reports["claude-desktop"]["errors"]:
                before = st.parsed_files, st.bytes_new, st.records
                try:
                    # A new ledger may retire audit rows. Publish the whole set
                    # atomically so a failed ledger never erases the audit fallback.
                    with self.store.locked(), self.store._transaction():
                        for args in dependent_writes:
                            save_result(*args)
                except Exception as exc:
                    st.parsed_files, st.bytes_new, st.records = before
                    fail(dependent_writes[0][0], "dependent source update failed: " + str(exc))
            for ad in self.adapters:
                info = reports[ad.id]
                if (not info["files"] or getattr(ad, "WANT_SIDE", False)) and hasattr(ad, "side_stats"):
                    try:
                        self._side[ad.id] = ad.side_stats()
                    except Exception as exc:
                        fail(ad, "additional metrics failed: " + str(exc))
                # Imported files are application-owned history. A concurrent
                # import may be published after discovery; never prune it from
                # the index using an older enumeration snapshot.
                if ad.id in complete and not info["errors"] and ad.id != "imported":
                    self.store.drop_missing_files_for_tool(alive[ad.id], ad.id)
            self.store.maybe_vacuum()
            totals = {r["tool"]: r for r in self.store.query(
                "SELECT tool,COUNT(*) records,SUM(input+output+cache_read+cache_write+reasoning) tokens,"
                "MAX(ts) last_activity,SUM(CASE WHEN cost_kind='estimated' THEN 1 ELSE 0 END) estimates "
                "FROM records GROUP BY tool")}
            counts = {r["tool"]: r for r in self.store.query(
                "SELECT tool,SUM(raw_records) raw_records,SUM(skipped_records) skipped_records FROM files GROUP BY tool")}
            for ad in self.adapters:
                info, old = reports[ad.id], previous.get(ad.id, {})
                row = totals.get(ad.id, {})
                rep = ad.report_shell()
                rep.files, rep.size = info["files"], info["size"]
                rep.paths = [info["path"]] if info["path"] else []
                rep.records = int(row.get("records") or 0)
                rep.tokens = int(row.get("tokens") or 0)
                rep.last_activity = int(row.get("last_activity") or 0)
                rep.last_scan_at = scan_started_at
                rep.included = bool(rep.records)
                rep.estimated = bool(row.get("estimates"))
                rep.raw_records = int(counts.get(ad.id, {}).get("raw_records") or 0)
                rep.accepted_records = rep.records
                rep.skipped_records = int(counts.get(ad.id, {}).get("skipped_records") or 0)
                if ad.id == "codex" and self.cfg.get("codex_quick"):
                    rep.quality = "aggregate"
                if ad.id == "deepseek-harness" and A._zstandard is None:
                    rep.quality = "aggregate"
                if rep.records:
                    rep.status = Status.OK if not info["errors"] and rep.quality == "exact" else Status.PARTIAL
                elif info["errors"]:
                    rep.status = Status.ERROR
                elif rep.files:
                    rep.status = Status.PARTIAL
                else:
                    rep.status = Status.DETECTED if info["detected"] else Status.MISSING
                if hasattr(ad, "warnings"):
                    try:
                        warnings = ad.warnings() or []
                        if warnings:
                            rep.note += " · " + "; ".join(warnings)
                            rep.status = Status.PARTIAL
                    except Exception as exc:
                        fail(ad, "quality check failed: " + str(exc))
                rep.error = "; ".join(info["errors"])
                if info.get("error_count", 0) > len(info["errors"]):
                    rep.error += "; %d additional failure(s)" % (info["error_count"] - len(info["errors"]))
                rep.error = rep.error[:1000]
                rep.last_success_at = (int(old.get("last_success_at") or 0) if info["errors"]
                                       else scan_started_at)
                if rep.error and rep.records:
                    rep.status = Status.PARTIAL
                    rep.note += " · 上次成功采集的数据已保留"
                self.store.save_source(rep, rep.tokens, rep.size, rep.files, rep.records)
            self.store.set_scan_time(now_ts())
            with st.lock:
                st.phase = "done"
                st.finished = time.time()
            st.say("扫描完成：新解析 %d 个文件，%d 条用量记录%s" %
                   (st.parsed_files, st.records, "；部分来源需要处理" if st.errors else ""))
        except _ScanCancelled:
            with st.lock:
                st.phase = "cancelled"
                st.finished = time.time()
            st.say("扫描已取消")
        except Exception as exc:
            with st.lock:
                st.phase = "error"
                st.errors.append(str(exc))
                st.finished = time.time()
            st.say("扫描失败：" + str(exc))
            traceback.print_exc()
        finally:
            for args in dependent_writes:
                result = args[-1]
                if hasattr(result, "close"):
                    result.close()
            with st.lock:
                st.running = False
                st.target = source_id or ""


class _ScanCancelled(Exception):
    """Internal control-flow exception for cooperative scan cancellation."""
