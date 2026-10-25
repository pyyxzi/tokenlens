"""Bounded, local-only usage imports with preview, atomic publication and replay dedup.

Only accounting fields are retained. Uploaded prompts, response bodies, credentials,
and arbitrary object metadata never enter the application's persistent import files.
"""
from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .models import UsageRecord, SourceReport, Status, to_epoch, MAX_USAGE_COUNT
from . import adapters as A
from .pricing import estimate_cost

MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_BODY_BYTES = ((MAX_IMPORT_BYTES + 2) // 3) * 4 + 4096
MAX_ROWS = 100_000
MAX_PENDING_BYTES = 64 * 1024 * 1024
PREVIEW_SECONDS = 15 * 60
FORMATS = ("auto", "csv", "api", "claude-code", "codex", "gemini-cli", "copilot-cli", "pi")
_SAFE_ID = re.compile(r"^[a-f0-9]{48}$")


def import_directory(cfg):
    return os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "imports")


def imported_records(path):
    """Read a sanitized ledger. Unknown fields are deliberately rejected."""
    with open(path, encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if number > MAX_ROWS or len(line) > 64 * 1024:
                raise ValueError("import ledger exceeds the supported size")
            obj = json.loads(line)
            record = UsageRecord(**obj).validate()
            if record.tool != "imported" or not record.event_id.startswith("import:") or record.ts <= 0:
                raise ValueError("invalid normalized import record")
            yield record


def _integer(value, name, default=0):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(name + " must be a non-negative integer")
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[0-9]+(?:\.0+)?", text):
        raise ValueError(name + " must be a non-negative integer")
    result = int(text.split(".")[0])
    if result > MAX_USAGE_COUNT:
        raise ValueError(name + " exceeds the supported range")
    return result


class SourceImporter:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.directory = import_directory(cfg)
        self.pending = os.path.join(self.directory, ".pending")
        os.makedirs(self.pending, exist_ok=True)
        self.lock = threading.RLock()

    def catalog(self):
        return {"max_bytes": MAX_IMPORT_BYTES, "max_rows": MAX_ROWS,
                "formats": list(FORMATS), "local_only": True,
                "retains": "usage fields only; prompts and credentials are discarded"}

    def _cleanup(self):
        now, total, count = time.time(), 0, 0
        for item in Path(self.pending).glob("*.jsonl"):
            stat = item.stat()
            if now - stat.st_mtime > PREVIEW_SECONDS:
                item.unlink(missing_ok=True)
            else:
                total += stat.st_size
                count += 1
        return total, count

    @staticmethod
    def _format(text, filename, chosen):
        if chosen != "auto":
            return chosen
        if filename.lower().endswith(".csv"):
            return "csv"
        start = text.lstrip()
        if not start.startswith(("{", "[")):
            raise ValueError("请选择 CSV、JSON 或 JSONL 用量文件")
        try:
            obj = json.loads(text)
            samples = obj[:64] if isinstance(obj, list) else [obj]
        except json.JSONDecodeError:
            samples = []
            for line in io.StringIO(text):
                if line.strip():
                    samples.append(json.loads(line))
                    if len(samples) >= 64:
                        break
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError("用量数据必须是 JSON 对象或对象数组")
            kind = sample.get("type", "")
            if not isinstance(kind, str):
                raise ValueError("日志 type 字段必须是文本")
            if kind.startswith("session.") or kind == "assistant.usage":
                return "copilot-cli"
            if sample.get("version") and kind == "session" or kind == "message" and isinstance(sample.get("message"), dict) and sample["message"].get("role"):
                return "pi"
            if kind in ("session_meta", "event_msg", "response_item"):
                return "codex"
            if kind in ("assistant", "user") and isinstance(sample.get("message"), dict):
                return "claude-code"
            if "messages" in sample and "sessionId" in sample or "$set" in sample or kind == "gemini" or "projectHash" in sample:
                return "gemini-cli"
        return "api"

    def _csv(self, text, path):
        try:
            dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
        names = {str(key).strip().lower(): key for key in reader.fieldnames or []}
        if len(names) != len(reader.fieldnames or []) or None in (reader.fieldnames or []):
            raise ValueError("CSV 表头缺失或重复")
        def value(row, *keys, default=""):
            return next((row[names[key]] for key in keys if key in names and row.get(names[key]) not in (None, "")), default)
        if not any(key in names for key in ("input", "input_tokens", "prompt_tokens", "output", "output_tokens", "completion_tokens")):
            raise ValueError("CSV 缺少 input/output 或 prompt_tokens/completion_tokens 列")
        for number, row in enumerate(reader, 2):
            if number > MAX_ROWS + 1:
                raise ValueError("文件最多支持 100,000 条记录")
            if None in row or any(value is None for value in row.values()):
                raise ValueError("CSV 第 %d 行列数与表头不一致" % number)
            model = value(row, "model", "model_name")
            inp = _integer(value(row, "input", "input_tokens", "prompt_tokens"), "input")
            out = _integer(value(row, "output", "output_tokens", "completion_tokens"), "output")
            cr = _integer(value(row, "cache_read", "cache_read_tokens", "cached_tokens"), "cache_read")
            cw = _integer(value(row, "cache_write", "cache_write_tokens", "cache_creation_input_tokens"), "cache_write")
            reasoning = _integer(value(row, "reasoning", "reasoning_tokens"), "reasoning")
            if "prompt_tokens" in names and "input" not in names and "input_tokens" not in names:
                if cr > inp:
                    raise ValueError("CSV 缓存读取不能超过 prompt_tokens")
                inp -= cr
            # OpenAI completion_tokens already includes reasoning_tokens;
            # normalized output/output_tokens columns keep reasoning separate.
            if "completion_tokens" in names and "output" not in names and "output_tokens" not in names:
                if reasoning > out:
                    raise ValueError("CSV 推理 Token 不能超过 completion_tokens")
                out -= reasoning
            currency = value(row, "currency", default="USD").strip().upper()
            cost_text = value(row, "cost_usd", "cost")
            if currency != "USD" and cost_text:
                raise ValueError("费用必须转换为 USD 后导入")
            cost = float(cost_text) if cost_text else estimate_cost(model, inp, out, cr, cw, reasoning)
            kind = value(row, "cost_kind") if cost_text else "estimated" if cost else "unknown"
            yield UsageRecord(tool=value(row, "tool", "source", default="csv"),
                ts=to_epoch(value(row, "ts", "timestamp", "created", "created_at", "time", "date")),
                model=model, project=value(row, "project", "cwd"), session=value(row, "session", "session_id"),
                input=inp, output=out, cache_read=cr, cache_write=cw, reasoning=reasoning,
                requests=_integer(value(row, "requests", default="1"), "requests"), cost=cost,
                cost_kind=kind or ("actual" if cost_text else "unknown"),
                cost_source="import-source-report" if cost_text else "price-table", src=path,
                event_id=value(row, "event_id", "request_id", "id"))

    def _api(self, text, path, filename):
        adapter = A.ApiLogAdapter(self.cfg)
        def parse(obj, inherited_model="", inherited_ts=0):
            if isinstance(obj, list):
                for child in obj:
                    yield from parse(child, inherited_model, inherited_ts)
                return
            if not isinstance(obj, dict):
                raise ValueError("JSON 用量记录必须为对象")
            model = str(A.dig(obj, "model", "model_name", default=inherited_model) or "")
            stamp = A.dig(obj, "timestamp", "created", "created_at", "time", "ts", "date", default=inherited_ts)
            is_usage = isinstance(obj.get("usage"), dict) or any(key in obj for key in ("prompt_tokens", "input_tokens", "prompt_eval_count"))
            if is_usage:
                if not model or to_epoch(stamp) <= 0:
                    raise ValueError("每条 API 用量记录必须包含有效时间和模型")
                records = list(adapter._from_object(obj, path, model, to_epoch(stamp)))
                if not records:
                    raise ValueError("用量记录缺少有效的 token 字段")
                for record in records:
                    # The adapter's identity contains its temporary path. Use
                    # the original provider ID (or canonical usage below).
                    record.event_id = str(A.dig(obj, "request_id", "requestId", "id", "uuid", default="") or "")
                    yield record
                return
            for key in ("response", "data", "result", "records", "events", "responses"):
                if isinstance(obj.get(key), (dict, list)):
                    yield from parse(obj[key], model, stamp)
                    return
            raise ValueError("JSON 中存在无法识别的用量记录")
        if filename.lower().endswith((".jsonl", ".ndjson")):
            for number, line in enumerate(io.StringIO(text), 1):
                if line.strip():
                    try:
                        yield from parse(json.loads(line))
                    except (ValueError, TypeError) as exc:
                        raise ValueError("第 %d 行：%s" % (number, exc)) from exc
        else:
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                for number, line in enumerate(io.StringIO(text), 1):
                    if line.strip():
                        yield from parse(json.loads(line))
            else:
                yield from parse(obj)

    def _records(self, text, filename, chosen):
        suffix = ".json" if filename.lower().endswith(".json") else ".jsonl"
        with tempfile.TemporaryDirectory(prefix="tokenlens-import-") as temporary:
            path = os.path.join(temporary, "upload" + suffix)
            with open(path, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            if chosen == "csv":
                yield from self._csv(text, path)
            elif chosen == "api":
                yield from self._api(text, path, filename)
            else:
                if suffix == ".jsonl":
                    # Validate syntax even in native metadata lines that an
                    # adapter does not otherwise need for token accounting.
                    for line in io.StringIO(text):
                        if line.strip() and not isinstance(json.loads(line), dict):
                            raise ValueError("原生日志的每一行必须是 JSON 对象")
                cls = {"claude-code": A.ClaudeCodeAdapter, "codex": A.CodexAdapter,
                       "gemini-cli": A.GeminiCliAdapter, "copilot-cli": A.CopilotCliAdapter,
                       "pi": A.PiAdapter}[chosen]
                yield from cls(self.cfg).parse(path)

    def preview(self, body):
        if not isinstance(body, dict) or set(body) - {"filename", "format", "data"}:
            raise ValueError("导入参数无效")
        filename, chosen, encoded = body.get("filename", "usage.jsonl"), body.get("format", "auto"), body.get("data")
        if not isinstance(filename, str) or not filename or len(filename) > 255:
            raise ValueError("文件名无效")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if chosen not in FORMATS or not isinstance(encoded, str) or len(encoded) > MAX_BODY_BYTES:
            raise ValueError("导入格式或文件大小无效")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("文件编码无效") from exc
        if not raw or len(raw) > MAX_IMPORT_BYTES:
            raise ValueError("文件大小须在 1 B 至 32 MB 之间")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("请将文件保存为 UTF-8 编码后导入") from exc
        del raw
        chosen = self._format(text, filename, chosen)
        with self.lock:
            pending_size, pending_count = self._cleanup()
            if pending_size >= MAX_PENDING_BYTES or pending_count >= 4:
                raise ValueError("待确认导入过多，请先完成或取消现有预览")
            token = secrets.token_hex(24)
            target = os.path.join(self.pending, token + ".jsonl")
            count = tokens = size = duplicate = 0
            occurrences, seen_ids = Counter(), set()
            models, tools, sample = Counter(), Counter(), []
            first, last, cost = None, 0, 0.0
            try:
                with open(target, "x", encoding="utf-8", newline="\n") as output, A.parsing_diagnostics() as issues:
                    for record in self._records(text, filename, chosen):
                        count += 1
                        if count > MAX_ROWS:
                            raise ValueError("文件最多支持 100,000 条记录")
                        record.validate()
                        if record.ts <= 0 or not record.model:
                            raise ValueError("每条用量记录必须包含有效时间和模型")
                        original_tool = record.tool
                        # Native IDs include a temporary source path; normalize
                        # them to canonical accounting fields for replay safety.
                        explicit = record.event_id if chosen in ("csv", "api") else str(record.extra.get("request_id", "") or "")
                        identity = [original_tool, record.session, explicit] if explicit else [original_tool, record.ts, record.model, record.project, record.session,
                            record.input, record.output, record.cache_read, record.cache_write, record.reasoning, record.requests]
                        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                        if explicit:
                            key = "import:" + digest
                        else:
                            occurrences[digest] += 1
                            key = "import:%s:%d" % (digest, occurrences[digest])
                        if key in seen_ids:
                            duplicate += 1
                            continue
                        seen_ids.add(key)
                        record.tool, record.src, record.event_id = "imported", "", key
                        record.extra = {"original_tool": original_tool, "import_format": chosen,
                                        "source_file": filename}
                        record.cost_source = record.cost_source or "import-source-report"
                        line = json.dumps(asdict(record), ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
                        size += len(line.encode("utf-8"))
                        if pending_size + size > MAX_PENDING_BYTES:
                            raise ValueError("预览数据超过 64 MB 限额，请拆分文件")
                        output.write(line)
                        tokens += record.total
                        cost += record.cost
                        if tokens > MAX_USAGE_COUNT or not math.isfinite(cost):
                            raise ValueError("文件汇总超出支持的精确数值范围，请拆分文件")
                        models[record.model] += 1
                        tools[original_tool] += 1
                        first, last = min(first or record.ts, record.ts), max(last, record.ts)
                        if len(sample) < 5:
                            sample.append({key: getattr(record, key) for key in ("ts", "model", "input", "output", "cache_read", "cache_write", "reasoning", "cost", "cost_kind")})
                    if issues:
                        raise ValueError("文件包含 %d 条格式错误的用量数据，未导入任何记录" % len(issues))
                if not seen_ids:
                    raise ValueError("未找到可导入的用量数据；会话内容不能代替 token 计量")
                return {"token": token, "format": chosen, "filename": filename, "records": len(seen_ids),
                        "duplicate_rows": duplicate, "tokens": tokens, "cost_usd": cost,
                        "models": dict(models.most_common(10)), "tools": dict(tools), "first": first, "last": last,
                        "sample": sample, "expires_in": PREVIEW_SECONDS}
            except BaseException:
                Path(target).unlink(missing_ok=True)
                raise

    def discard(self, body):
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not _SAFE_ID.fullmatch(token):
            raise ValueError("预览标识无效")
        with self.lock:
            Path(self.pending, token + ".jsonl").unlink(missing_ok=True)
        return {"discarded": True}

    def commit(self, body):
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not _SAFE_ID.fullmatch(token):
            raise ValueError("预览标识无效")
        with self.lock:
            self._cleanup()
            preview = Path(self.pending, token + ".jsonl")
            if not preview.is_file():
                raise ValueError("预览已过期或已导入，请重新选择文件")
            digest_hash = hashlib.sha256()
            with preview.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_hash.update(chunk)
            digest = digest_hash.hexdigest()
            target = Path(self.directory, digest + ".jsonl")
            if target.is_file() and self.store.file_state(str(target)):
                preview.unlink()
                return {"imported": 0, "duplicate": True, "records": self.store.known_records(str(target))}
            os.replace(preview, target)
            try:
                stat = target.stat()
                # Publish accounting rows and their source summary together.
                # A metadata write error must not report a failed import after
                # records have already committed and consumed the preview.
                with self.store.locked(), self.store._transaction():
                    count = self.store.replace_file(str(target), "imported", stat.st_size, stat.st_mtime,
                        imported_records(str(target)), note="User-selected import; accounting fields only",
                        fingerprint=A.file_fingerprint(str(target)))
                    self._report()
            except BaseException:
                os.replace(target, preview)
                raise
            return {"imported": count, "duplicate": count == 0, "records": self.store.known_records(str(target))}

    def _report(self):
        row = self.store.query_one("SELECT COUNT(*) records,COALESCE(SUM(input+output+cache_read+cache_write+reasoning),0) tokens,MAX(ts) last_activity FROM records WHERE tool='imported'") or {}
        files = self.store.query_one("SELECT COUNT(*) files,COALESCE(SUM(size),0) size FROM files WHERE tool='imported'") or {}
        report = A.ImportedAdapter(self.cfg).report_shell()
        report.status = Status.OK
        report.included = bool(row.get("records"))
        report.last_activity = row.get("last_activity") or 0
        report.last_scan_at = int(time.time())
        report.raw_records = report.accepted_records = row.get("records") or 0
        report.last_success_at = int(time.time())
        estimated = self.store.query_one("SELECT COUNT(*) n FROM records WHERE tool='imported' AND cost_kind='estimated'") or {}
        report.estimated = bool(estimated.get("n"))
        report.paths = [self.directory]
        self.store.save_source(report, row.get("tokens") or 0, files.get("size") or 0,
                               files.get("files") or 0, row.get("records") or 0)
