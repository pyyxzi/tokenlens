"""Resumable JSONL reads with verified prefixes and bounded parser state.

An append reuses parsing state only after SHA-256 verification of every saved
prefix byte. This deliberately still reads old bytes: portable file metadata
cannot prove that a writer did not edit the middle before appending. Old bytes
are hashed in blocks, never decoded or reparsed. Metrics separate this safety
I/O from the genuinely incremental JSON parsing and database insertion.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile

from . import adapters as A

CHUNK_BYTES = 1024 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
CURSOR_VERSION = 1


def _identity(stat):
    return [stat.st_dev, stat.st_ino]


def _dependencies(adapter, path):
    paths = adapter.cache_dependencies(path) if hasattr(adapter, "cache_dependencies") else ()
    entries = []
    for dependency in sorted(set(paths)):
        try:
            stat = os.stat(dependency)
            entries.append([dependency, stat.st_size, stat.st_mtime_ns, A.file_change_time(dependency, stat),
                            stat.st_dev, stat.st_ino])
        except FileNotFoundError:
            entries.append([dependency, None])
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()


def _snapshot_fingerprint(path, stat, dependencies):
    """Match Adapter's metadata fingerprint for the captured JSONL boundary.

    A growing file must keep the captured size in both its file metadata and
    cursor. Using its later, larger fingerprint would incorrectly mark bytes
    which were never parsed as fresh on the next scan.
    """
    parts = [("parser_version", A.PARSER_VERSION)]
    for dependency in [path] + sorted(set(dependencies)):
        try:
            current = stat if dependency == path else os.stat(dependency)
            parts.append((dependency, current.st_size, current.st_mtime_ns,
                          A.file_change_time(dependency, current), current.st_dev, current.st_ino))
        except FileNotFoundError:
            parts.append((dependency, None))
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def supported(adapter, path):
    return bool(getattr(adapter, "INCREMENTAL_JSONL", False) and
                path.lower().endswith((".jsonl", ".ndjson")))


class JsonlPlan:
    """Lazy records consumed by Store's atomic staging API.

    ``cursor`` is serialized only after ``records`` exhausts. No offset becomes
    durable until rows, file state, FTS and this cursor commit together.
    """
    def __init__(self, adapter, path, old, fingerprint, force=False, cancelled=lambda: False,
                 event_exists=lambda event_id: False):
        self.adapter, self.path = adapter, path
        self.old = old or {}
        self.cancelled = cancelled
        self.event_exists = event_exists
        self.cursor = {}
        self.append = False
        self.offset = 0
        self.parser = {"database_dedup": True}
        self.bytes_read = self.parsed_bytes = self.verification_bytes = 0
        self.pending_bytes = 0
        self.final_without_newline = False
        self.fallback = "initial"
        self.stat = os.stat(path)
        self.dependency = _dependencies(adapter, path)
        deps = adapter.cache_dependencies(path) if hasattr(adapter, "cache_dependencies") else ()
        self.fingerprint = _snapshot_fingerprint(path, self.stat, deps)
        self.digest = hashlib.sha256()
        self.snapshot_digest = None
        self.snapshot_bytes = 0
        try:
            prior = json.loads((old or {}).get("parse_cursor") or "null")
        except (ValueError, TypeError):
            prior = None
        if force:
            self.fallback = "forced"
        elif isinstance(prior, dict):
            valid = (prior.get("version") == CURSOR_VERSION and prior.get("parser_version") == A.PARSER_VERSION
                     and prior.get("tool") == adapter.id and prior.get("identity") == _identity(self.stat)
                     and prior.get("dependencies") == self.dependency
                     and not prior.get("final_without_newline")
                     and isinstance(prior.get("offset"), int) and isinstance(prior.get("size"), int)
                     and 0 <= prior["offset"] <= prior["size"] < self.stat.st_size
                     and isinstance(prior.get("parser"), dict)
                     and not prior["parser"].get("unrecorded_events_overflow"))
            self.fallback = "metadata"
            if valid:
                self._check_cancel()
                with open(path, "rb", buffering=CHUNK_BYTES) as source:
                    remaining = prior["offset"]
                    while remaining:
                        self._check_cancel()
                        chunk = source.read(min(CHUNK_BYTES, remaining))
                        if not chunk:
                            raise OSError("source truncated during prefix verification")
                        self.bytes_read += len(chunk)
                        self.verification_bytes += len(chunk)
                        self.digest.update(chunk)
                        remaining -= len(chunk)
                if self.digest.hexdigest() == prior.get("sha256"):
                    self.append, self.offset = True, prior["offset"]
                    self.parser = copy.deepcopy(prior["parser"])
                    self.parser["database_dedup"] = True
                    self.fallback = ""
                else:
                    self.digest = hashlib.sha256()
                    self.fallback = "prefix_changed"

    def _check_cancel(self):
        if self.cancelled():
            raise InterruptedError("scan cancelled while reading JSONL")

    def _lines(self):
        with open(self.path, "rb", buffering=CHUNK_BYTES) as source:
            opened = os.fstat(source.fileno())
            if _identity(opened) != _identity(self.stat) or opened.st_size < self.stat.st_size:
                raise OSError("source replaced or truncated before parsing; previous snapshot retained")
            source.seek(self.offset)
            self.snapshot_digest = self.digest.copy()
            self.snapshot_bytes = self.offset
            while source.tell() < self.stat.st_size:
                self._check_cancel()
                start = source.tell()
                line = source.readline(min(MAX_LINE_BYTES + 1, self.stat.st_size - start))
                if not line:
                    raise OSError("source truncated while reading JSONL; previous snapshot retained")
                self.bytes_read += len(line)
                self.parsed_bytes += len(line)
                self.snapshot_digest.update(line)
                self.snapshot_bytes += len(line)
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError("JSONL line exceeds 16 MiB; previous snapshot retained")
                if not line.endswith(b"\n"):
                    # A complete JSON value at EOF remains a supported format.
                    # If more bytes arrive later, reparse the file so extending
                    # that unterminated line cannot duplicate an accepted event.
                    try:
                        text = line.decode("utf-8-sig" if start == 0 else "utf-8", errors="strict")
                        json.loads(text)
                    except (UnicodeError, ValueError):
                        self.pending_bytes = len(line)
                        break
                    self.final_without_newline = True
                self.digest.update(line)
                self.offset = source.tell()
                text = line.decode("utf-8-sig" if start == 0 else "utf-8", errors="strict")
                if text.strip():
                    yield text

    def _verify_snapshot(self):
        """Accept appends only when every byte in the read snapshot survives.

        File metadata alone cannot distinguish a normal append from a rewrite
        followed by an append. Rehash the captured prefix in bounded blocks,
        including an incomplete last line, and leave later bytes for next scan.
        """
        if self.snapshot_bytes != self.stat.st_size or self.snapshot_digest is None:
            raise OSError("source truncated while parsing; previous snapshot retained")
        if _dependencies(self.adapter, self.path) != self.dependency:
            raise OSError("source dependencies changed while parsing; previous snapshot retained")
        digest = hashlib.sha256()
        with open(self.path, "rb", buffering=CHUNK_BYTES) as source:
            opened = os.fstat(source.fileno())
            if _identity(opened) != _identity(self.stat) or opened.st_size < self.stat.st_size:
                raise OSError("source replaced or truncated while parsing; previous snapshot retained")
            remaining = self.stat.st_size
            while remaining:
                self._check_cancel()
                chunk = source.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise OSError("source truncated during snapshot verification; previous snapshot retained")
                self.bytes_read += len(chunk)
                self.verification_bytes += len(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.digest() != self.snapshot_digest.digest():
                raise OSError("source prefix changed while parsing; previous snapshot retained")
            final = os.fstat(source.fileno())
            current = os.stat(self.path)
            if (_identity(final) != _identity(self.stat) or _identity(current) != _identity(self.stat)
                    or min(final.st_size, current.st_size) < self.stat.st_size):
                raise OSError("source replaced or truncated during verification; previous snapshot retained")
        if _dependencies(self.adapter, self.path) != self.dependency:
            raise OSError("source dependencies changed during verification; previous snapshot retained")

    def records(self):
        count = 0
        # Exact deduplication of cumulative replays needs a seen-event index
        # before changing parser state. Keep that index on disk, not in the
        # JSON cursor or a Python set proportional to the log's history.
        with tempfile.TemporaryDirectory(prefix="tokenlens-jsonl-events-") as directory:
            events = None
            try:
                if self.adapter.id == "codex":
                    events = sqlite3.connect(os.path.join(directory, "events.sqlite"))
                    events.execute("PRAGMA journal_mode=OFF")
                    events.execute("PRAGMA synchronous=OFF")
                    events.execute("PRAGMA cache_size=-1024")
                    events.execute("CREATE TABLE events(id TEXT PRIMARY KEY) WITHOUT ROWID")
                    unrecorded = set(self.parser.get("unrecorded_event_ids", ()))
                    registered = 0
                    def register(event_id):
                        nonlocal registered
                        if self.append and (event_id in unrecorded or self.event_exists(event_id)):
                            return False
                        added = bool(events.execute("INSERT OR IGNORE INTO events VALUES(?)", (event_id,)).rowcount)
                        registered += 1
                        if registered % 1024 == 0:
                            events.commit()
                        return added
                    self.parser["register_cumulative_event"] = register
                with A.parsing_diagnostics() as issues:
                    for record in self.adapter.parse_lines(self.path, self._lines(), self.parser):
                        count += 1
                        yield record
            finally:
                self.parser.pop("register_cumulative_event", None)
                if events is not None:
                    events.close()
        if issues:
            raise ValueError("%d malformed usage record(s); previous snapshot retained" % len(issues))
        # New forks can contain only copied parent usage before their first
        # response. The owned-usage parser explicitly identifies this case so
        # a verified empty replacement can retire old, inflated child rows.
        inherited_only = (self.adapter.id == "codex"
                          and self.parser.get("inherited_usage_skipped") is True)
        if (not self.append and not count and self.stat.st_size and self.old.get("records")
                and not inherited_only):
            raise ValueError("nonempty source yielded no usage; previous snapshot retained")
        self._check_cancel()
        self._verify_snapshot()
        self.cursor.update(version=CURSOR_VERSION, parser_version=A.PARSER_VERSION,
                           tool=self.adapter.id, identity=_identity(self.stat),
                           dependencies=self.dependency, size=self.stat.st_size,
                           offset=self.offset, sha256=self.digest.hexdigest(), parser=self.parser,
                           final_without_newline=self.final_without_newline)
        if len(json.dumps(self.cursor, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
            raise ValueError("JSONL parser state exceeds 256 KiB; previous snapshot retained")
