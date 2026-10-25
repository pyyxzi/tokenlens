# -*- coding: utf-8 -*-
"""
TokenLens · 模型计价表

价格为「美元 / 百万 token」。内置表是版本化估算快照，不实时同步厂商价目，
用于**成本估算**，并非账单凭证；第三方中转、订阅套餐、免费额度都会让
实际花费不同。

计费口径与各家官网一致：
  · 成本 = 未命中缓存的输入 × 输入价 + 输出 × 输出价
          + 缓存命中 × 缓存读价 + 缓存写入 × 缓存写价
  · 缓存写入和存储收费依具体服务/模型而定。历史表中的 0 是估算参数，
    不表示已核实免费；需要按服务实际价目建立价格版本。四项 token 单价
    不涵盖所有缓存存储、地区、上下文档位和额外服务费用。
  · DeepSeek / Grok / Gemini 按时段或上下文长度分档，这里取**高峰档 /
    短上下文档**（最常用、也是更保守的估计）。
"""
from __future__ import annotations
import datetime
import re
import threading

PRICE_VERSION = "builtin-2026-09"

USD_TO_CNY = 7.20  # 汇率，可在设置中调整

from .prices_data import PRICING    # 数据文件：模型价目表（可社区维护）

_BUILTIN_PRICES = {row[0]: row[1:] for row in PRICING}

# 只展开历史快照已明确说明的同价别名；未知档位必须保持未计价，不能
# 因为共享名称前缀就继承基础模型的价格。这里不引入新的单价。
_BUILTIN_ALIASES = {
    "gpt-5.2-codex": "gpt-5.2",
    "gpt-5.1-codex": "gpt-5.1",
    "gpt-5.1-codex-max": "gpt-5.1",
    "gemini-3-pro": "gemini-3",
    "grok-code-fast": "grok-code",
    "claude-opus-4.5": "claude-opus-4",
    "claude-opus-4.6": "claude-opus-4",
    "claude-opus-4.7": "claude-opus-4",
    "claude-opus-4.8": "claude-opus-4",
    "claude-opus-4-5": "claude-opus-4",
    "claude-opus-4-6": "claude-opus-4",
    "claude-opus-4-7": "claude-opus-4",
    "claude-opus-4-8": "claude-opus-4",
    "claude-sonnet-4.5": "claude-sonnet-4",
    "claude-sonnet-4.6": "claude-sonnet-4",
    "claude-sonnet-4.7": "claude-sonnet-4",
    "claude-sonnet-4-5": "claude-sonnet-4",
    "claude-sonnet-4-6": "claude-sonnet-4",
    "claude-sonnet-4-7": "claude-sonnet-4",
    "claude-haiku-4.5": "claude-haiku-4",
    "claude-haiku-4-5": "claude-haiku-4",
}
_DATED_MODEL = re.compile(r"(.+)[-:](20\d{2}-\d{2}-\d{2}|20\d{6})")

DEFAULT_PRICE = (1.00, 3.00, 0.10, 0.0)   # 旧接口兜底；ledger 对未知模型保留 unknown。

# ---------------------------------------------------------------------------
# 手动单价覆盖（用户自定义）
# ---------------------------------------------------------------------------
# 价目表不可能永远跟得上厂商调价，中转站的价格也和官牌不同。这里提供
# 「按模型名精确覆盖」：键为完整模型名（不区分大小写、去除首尾空格），
# 值为 (输入, 输出, 缓存读, 缓存写)，单位同样是美元/百万 token。
# 覆盖值优先于内置价目表；被覆盖的模型在账单上会标注「自定义价」。
#
# 持久化在 data_dir()/custom_prices.json，由 config.py 侧的加载逻辑在
# 启动时调用 load_custom_prices() 注入；HTTP API 可直接调用
# set_custom_price()/remove_custom_price()。
CUSTOM_PRICES: dict[str, tuple[float, float, float, float]] = {}
_CUSTOM_LOCK = threading.RLock()


def _lock():
    return _CUSTOM_LOCK


def _norm(model: str) -> str:
    return str(model or "").strip().lower()


def load_custom_prices(path: str) -> int:
    """从 JSON 文件读入手动单价；返回条数。文件损坏时忽略而不是崩溃。"""
    import json
    import os
    with _lock():
        CUSTOM_PRICES.clear()
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return 0
        if not isinstance(data, dict):
            return 0
        n = 0
        for name, prices in data.items():
            if not isinstance(name, str) or not isinstance(prices, (list, tuple)):
                continue
            if len(prices) != 4:
                continue
            try:
                vals = tuple(max(0.0, float(v)) for v in prices)
            except (TypeError, ValueError):
                continue
            import math
            if any(not math.isfinite(v) for v in vals):
                continue
            CUSTOM_PRICES[_norm(name)] = vals
            n += 1
        return n


def save_custom_prices(path: str) -> bool:
    import json
    import os
    import tempfile
    with _lock():
        temporary = None
        try:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".prices-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({k: list(v) for k, v in sorted(CUSTOM_PRICES.items())},
                          fh, ensure_ascii=False, indent=2, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
            return True
        except Exception:
            return False
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def custom_price_version(model: str, prices=None) -> str:
    """Content-addressed version; the same explicit prices survive restarts."""
    import hashlib
    import json
    with _lock():
        values = tuple(prices) if prices is not None else CUSTOM_PRICES.get(_norm(model))
        if values is None:
            return PRICE_VERSION
        encoded = json.dumps([_norm(model), list(values)], separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    return "custom-" + hashlib.sha256(encoded).hexdigest()[:16]


def restore_custom_prices_file(path: str, content) -> bool:
    """Restore exact pre-transaction bytes if the later database commit fails."""
    import os
    import tempfile
    temporary = None
    try:
        if content is None:
            if os.path.exists(path):
                os.unlink(path)
            return True
        fd, temporary = tempfile.mkstemp(prefix=".prices-rollback-", suffix=".tmp",
                                         dir=os.path.dirname(os.path.abspath(path)))
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def validate_prices(prices) -> tuple[float, float, float, float]:
    if not isinstance(prices, (list, tuple)) or len(prices) != 4:
        raise ValueError("prices must be [input, output, cache_read, cache_write]")
    vals = []
    for v in prices:
        if isinstance(v, bool):
            raise ValueError("prices must be finite non-negative numbers")
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")) or not 0 <= f <= 1e12:
            raise ValueError("prices must be finite non-negative numbers")
        vals.append(f)
    return tuple(vals)


def set_custom_price(model: str, prices) -> tuple[float, float, float, float]:
    """Legacy standalone helper; application writes use Finance price versions."""
    if not str(model or "").strip():
        raise ValueError("model name is required")
    vals = validate_prices(prices)
    key = _norm(model)
    with _lock():
        CUSTOM_PRICES[key] = tuple(vals)
    return CUSTOM_PRICES[key]


def remove_custom_price(model: str) -> bool:
    with _lock():
        return CUSTOM_PRICES.pop(_norm(model), None) is not None


def has_custom_price(model: str) -> bool:
    with _lock():
        return _norm(model) in CUSTOM_PRICES


def _builtin_key(model: str):
    """Return the explicitly supported price key for ``model``.

    Routing namespaces (``provider/model``) do not change a model's identity.
    After removing that namespace, accept only a complete ID, an explicit
    alias, or either followed by a valid calendar date.  A new tier such as
    ``gpt-4.1-nano`` must never inherit ``gpt-4.1`` pricing merely because
    it shares the prefix; even adding a date does not make that tier known.
    """
    normalized = _norm(model)
    if not normalized:
        return None
    candidate = normalized.rsplit("/", 1)[-1]
    if candidate in _BUILTIN_PRICES:
        return candidate
    if candidate in _BUILTIN_ALIASES:
        return _BUILTIN_ALIASES[candidate]
    dated = _DATED_MODEL.fullmatch(candidate)
    if dated is None:
        return None
    base, version = dated.groups()
    try:
        digits = version.replace("-", "")
        datetime.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None
    if base in _BUILTIN_PRICES:
        return base
    return _BUILTIN_ALIASES.get(base)


def builtin_price(model: str):
    """Return a public-table price or None, ignoring legacy custom overrides."""
    key = _builtin_key(model)
    if key is None:
        return None
    return _BUILTIN_PRICES[key]


def lookup(model: str):
    """返回 (输入价, 输出价, 缓存读价, 缓存写价)，单位 美元/百万token。

    手动单价优先：完整模型名与自定义条目一致时直接返回；内置表只认
    确切模型 ID、显式别名和日期版本，不把未知档位套用基础模型价格。
    """
    m = _norm(model)
    if not m:
        return DEFAULT_PRICE
    with _lock():
        hit = CUSTOM_PRICES.get(m)
    if hit is not None:
        return hit
    return builtin_price(m) or DEFAULT_PRICE


def is_known(model: str) -> bool:
    """该模型能不能计价：内置价目表命中，或用户设了手动单价。

    这个函数是 estimate_cost / cost_breakdown / ledger 三处的总门槛。
    2026-09-25 修过一个真实事故：它原先只查内置关键字表，用户给一个
    内置表查不到的模型（gpt-6-luna）设了手动单价——CUSTOM_PRICES 里
    明明有、lookup() 也认，但这里先把它拦成 False，于是 estimate_cost
    返回 0、账单整行显示「未计价」，用户改了价却看不到任何变化。
    手动单价的本意就是「关键字匹配不到 / 官牌不对，我自己填」，所以
    设了手动单价就必须视为 known。
    """
    if not model:
        return False
    m = _norm(model)
    with _lock():
        if m in CUSTOM_PRICES:
            return True
    return _builtin_key(m) is not None


def estimate_cost(model: str, inp: int, out: int, cache_read: int = 0,
                  cache_write: int = 0, reasoning: int = 0) -> float:
    """估算成本（USD）。

    Providers that expose reasoning/thought tokens separately (for example
    Gemini CLI) report them in ``reasoning``.  They are billed at the output
    rate, so every cost path must add them exactly once.  Providers whose
    output already includes reasoning pass zero (the normalized adapters do
    this for Claude/Codex).
    """
    if not is_known(model):
        return 0.0
    i, o, cr, cw = lookup(model)
    return (
        inp * i + (out + reasoning) * o + cache_read * cr + cache_write * cw
    ) / 1_000_000.0


def cost_breakdown(model: str, inp: int, out: int, cache_read: int,
                   cache_write: int, reasoning: int = 0) -> dict:
    if not is_known(model):
        return {"input": 0.0, "output": 0.0, "cache_read": 0.0,
                "cache_write": 0.0, "currency": "USD", "known": False}
    i, o, cr, cw = lookup(model)
    return {
        "input": inp * i / 1e6,
        "output": (out + reasoning) * o / 1e6,
        "cache_read": cache_read * cr / 1e6,
        "cache_write": cache_write * cw / 1e6,
        "currency": "USD",
        "known": True,
    }


RATE_FIELDS = ("input", "output", "cache_read", "cache_write")


def validate_context_rules(rules):
    """Canonical request-level context overrides in their published order."""
    import math
    if not isinstance(rules, list) or len(rules) > 40:
        raise ValueError("invalid context price rules")
    result = []
    for rule in rules:
        allowed = {"prompt_tokens_gt", "utc_days", "utc_start", "utc_end", "rates"}
        if not isinstance(rule, dict) or set(rule) - allowed or "rates" not in rule or not set(rule) - {"rates"}:
            raise ValueError("unsupported context price rule")
        threshold, rates = rule.get("prompt_tokens_gt"), rule["rates"]
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 10**12):
            raise ValueError("invalid context price threshold")
        if not isinstance(rates, dict) or not rates or set(rates) - set(RATE_FIELDS):
            raise ValueError("invalid context override rates")
        clean = {}
        for key, value in rates.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1e12:
                raise ValueError("invalid context override rate")
            clean[key] = float(value)
        item = {"rates": clean}
        if threshold is not None:
            item["prompt_tokens_gt"] = threshold
        if "utc_days" in rule:
            days = rule["utc_days"]
            if not isinstance(days, list) or not days or len(days) > 7 or any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in days) or len(set(days)) != len(days):
                raise ValueError("invalid UTC weekdays")
            item["utc_days"] = sorted(set(days))
        if ("utc_start" in rule) != ("utc_end" in rule):
            raise ValueError("UTC price windows need start and end")
        if "utc_start" in rule:
            for key in ("utc_start", "utc_end"):
                value = rule[key]
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2359 or value % 100 >= 60:
                    raise ValueError("invalid UTC time")
                item[key] = value
            if item["utc_start"] == item["utc_end"]:
                raise ValueError("ambiguous equal UTC endpoints")
        result.append(item)
    return result


def rule_matches(rule, prompt_tokens, request_time=None):
    if "prompt_tokens_gt" in rule and prompt_tokens <= rule["prompt_tokens_gt"]:
        return False
    if any(key.startswith("utc_") for key in rule):
        if request_time is None:
            return None
        instant = (datetime.datetime.fromtimestamp(request_time, datetime.timezone.utc)
                   if isinstance(request_time, (int, float)) else request_time.astimezone(datetime.timezone.utc))
        if "utc_days" in rule and instant.weekday() not in rule["utc_days"]:
            return False
        if "utc_start" in rule:
            hhmm = instant.hour * 100 + instant.minute
            start, end = rule["utc_start"], rule["utc_end"]
            if not (start <= hhmm < end if start < end else hhmm >= start or hhmm < end):
                return False
    return True


def price_rule_sql(rule, prompt, timestamp="ts"):
    """SQL equivalent of rule_matches for validated persisted rule values."""
    parts = []
    if "prompt_tokens_gt" in rule:
        parts.append(prompt + ">" + str(rule["prompt_tokens_gt"]))
    if "utc_days" in rule:
        weekday = "((CAST(strftime('%w'," + timestamp + ",'unixepoch') AS INTEGER)+6)%7)"
        parts.append(weekday + " IN (" + ",".join(str(value) for value in rule["utc_days"]) + ")")
    if "utc_start" in rule:
        hhmm = "(CAST(strftime('%H'," + timestamp + ",'unixepoch') AS INTEGER)*100+CAST(strftime('%M'," + timestamp + ",'unixepoch') AS INTEGER))"
        start, end = str(rule["utc_start"]), str(rule["utc_end"])
        parts.append("(" + hhmm + ">=" + start + (" AND " if rule["utc_start"] < rule["utc_end"] else " OR ") + hhmm + "<" + end + ")")
    return "(" + " AND ".join(parts) + ")"


def usage_prices(usage, prices, rules=None, *, prompt_tokens=None, request_time=None):
    """Select rates by the request's full input, including cached input.

    OpenRouter specifies a strict greater-than condition; later matching
    overrides win per price key and omitted keys retain the preceding rate.
    A model/day aggregate must supply the original request prompt length.
    """
    if len(usage) != 4 or len(prices) != 4:
        raise ValueError("usage and prices require four token categories")
    selected = list(prices)
    prompt = usage[0] + usage[2] + usage[3] if prompt_tokens is None else prompt_tokens
    for rule in rules or []:
        matched = rule_matches(rule, prompt, request_time)
        if matched is None:
            return [None] * 4
        if matched:
            for key, value in rule["rates"].items():
                selected[RATE_FIELDS.index(key)] = value
    return selected


def price_usage(usage, prices, rules=None, *, prompt_tokens=None, request_time=None):
    """Return a cost only when all token categories actually used are priced."""
    if len(usage) != 4 or len(prices) != 4:
        raise ValueError("usage and prices require four token categories")
    prices = usage_prices(usage, prices, rules, prompt_tokens=prompt_tokens, request_time=request_time)
    if any(tokens and rate is None for tokens, rate in zip(usage, prices)):
        return None
    return sum(tokens * rate for tokens, rate in zip(usage, prices) if rate is not None) / 1_000_000


def ledger(models, usd_to_cny: float = USD_TO_CNY, summary_cost=None, price_map=None, usage_breakdown=None) -> dict:
    """把模型维度的聚合结果整理成「计费明细」。

    每条给出：单价（USD / 百万 token）、各计费项的 token 数与金额、小计，
    以及折算人民币。这样用户能一眼看出钱花在哪个计费项上，而不是只看到
    一个估算总额。

    关于「为什么还要对账」：`records.cost` 有两个来源——能读到网关账单的
    日志会直接写入**真实花费**，读不到的才用本价目表估算。于是
    SUM(cost) 与本表重算出来的金额天然可能不等（真实花费 vs 估算）。
    这里把两者都报出来，让界面能如实显示差额，而不是让明细的合计
    和总览的"估算成本"对不上、看起来像算错了。

    summary_cost 传入总览口径的成本时，会附带 reconciliation 字段。
    """
    items, totals = [], {
        "input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0,
        "cost": 0.0, "stored": 0.0, "tokens": 0, "requests": 0,
        "unpriced": 0, "unpriced_tokens": 0,
    }
    for m in (models or []):
        name = m.get("model") or ""
        inp = int(m.get("input") or 0)
        out = int(m.get("output") or 0) + int(m.get("reasoning") or 0)
        cr = int(m.get("cache_read") or 0)
        cw = int(m.get("cache_write") or 0)
        tokens = int(m.get("total") or (inp + out + cr + cw))
        stored = float(m.get("cost") or 0)
        version = (price_map or {}).get(_norm(name))
        if version:
            known = version["source"] != "legacy-override:unpriced"
            custom = version["source"] not in ("legacy-override:unpriced", "legacy-override:builtin")
            current = version["prices"]
        else:
            current = builtin_price(name) if price_map is not None else lookup(name)
            known = current is not None if price_map is not None else is_known(name)
            custom = False if price_map is not None else has_custom_price(name)
        partial_price = bool(known and any(value is None for value in current))
        # 版本里登记的基准单价。分档/条件价模型的表内单价稍后会被换成
        # 「按本区间用量加权」的值（否则 单价 × token 对不上金额），
        # 单价编辑器要写回的必须是最初的基准价，不能拿加权值当目录价。
        base_rates = list(current) if known else None
        segments = (usage_breakdown or {}).get(_norm(name)) or []
        if known:
            pi, po, pcr, pcw = current
            if version and version.get("rules") and not segments:
                # Aggregated input cannot tell which requests crossed a tier.
                # Never silently apply the high-context tier to the whole model.
                priced_usage = None
                bd = dict.fromkeys(RATE_FIELDS, None)
            elif segments:
                bd = dict.fromkeys(RATE_FIELDS, 0.0)
                priced_usage = 0.0
                for segment in segments:
                    usage = [segment["tokens_by"][key] for key in RATE_FIELDS]
                    rates = segment["prices"]
                    cost = price_usage(usage, rates)
                    if cost is None:
                        priced_usage = None
                    for key, used, rate in zip(RATE_FIELDS, usage, rates):
                        if used and rate is None:
                            bd[key] = None
                        elif bd[key] is not None and rate is not None:
                            bd[key] += used * rate / 1e6
                # The main row uses exact weighted rates; expanded segments
                # carry published rates and request counts for each tier.
                current = [bd[key] * 1e6 / used if used and bd[key] is not None
                           else None if used else rate
                           for key, used, rate in zip(RATE_FIELDS, (inp, out, cr, cw), current)]
                pi, po, pcr, pcw = current
            else:
                priced_usage = price_usage((inp, out, cr, cw), current)
                bd = dict(zip(RATE_FIELDS,
                              (used * rate / 1e6 if rate is not None else 0 if not used else None
                               for used, rate in zip((inp, out, cr, cw), current))))
            prices = {"input": pi, "output": po, "cache_read": pcr, "cache_write": pcw}
            amounts = {field: round(value, 6) if value is not None else None for field, value in bd.items()}
            subtotal = round(sum(value for value in amounts.values() if value is not None), 6)
            known = priced_usage is not None
            if not known:
                totals["unpriced"] += 1
                totals["unpriced_tokens"] += sum(tokens for tokens, rate in zip((inp,out,cr,cw),current) if rate is None)
        else:
            # 价目表没收录：不编造单价，也不按 0 元参与合计。
            # 单价与金额一律给 None，前端显示"—"，避免出现
            # "$1.00 × 500,000 = $0.00" 这种算术上明显错误的行。
            prices = {k: None for k in ("input", "output", "cache_read", "cache_write")}
            amounts = {k: None for k in ("input", "output", "cache_read", "cache_write")}
            subtotal = 0.0
            totals["unpriced"] += 1
            totals["unpriced_tokens"] += tokens
        items.append({
            "model": name or "未知模型",
            "requests": int(m.get("requests") or 0),
            "tokens": tokens,
            "price": prices,
            # 版本登记的单价（分档模型下与上面的加权单价不同）。
            # 前端「编辑模型单价」用它做预填，手工覆盖时必须基于它，
            # 否则会把加权后的用量均价写进价目表。
            "base_price": (dict(zip(RATE_FIELDS, base_rates)) if base_rates is not None else None),
            "tokens_by": {"input": inp, "output": out, "cache_read": cr, "cache_write": cw},
            "amount": amounts,
            "subtotal": subtotal,
            "subtotal_cny": round(subtotal * usd_to_cny, 4),
            # 账单里记录的实际花费（网关日志有则非 0）
            "stored": round(stored, 6),
            "priced": known,
            "partial_price": partial_price,
            "tiered_price": bool(version and version.get("rules")),
            "pricing_segments": segments,
            "price_source": version["source"] if version else "",
            # 该模型使用了手动单价（覆盖内置价目表）
            "custom_price": custom,
            "price_version": version["id"] if version else PRICE_VERSION if known else "",
        })
        if known or partial_price:
            for k in ("input", "output", "cache_read", "cache_write"):
                totals[k] += amounts[k] or 0
        totals["cost"] += subtotal
        totals["stored"] += stored
        totals["tokens"] += tokens
        totals["requests"] += items[-1]["requests"]
    items.sort(key=lambda x: (-x["subtotal"], -x["tokens"]))
    totals["cost"] = round(totals["cost"], 6)
    totals["stored"] = round(totals["stored"], 6)
    totals["cost_cny"] = round(totals["cost"] * usd_to_cny, 4)
    totals["stored_cny"] = round(totals["stored"] * usd_to_cny, 4)
    for k in ("input", "output", "cache_read", "cache_write"):
        totals[k] = round(totals[k], 6)
    totals["rate"] = usd_to_cny
    totals["currency"] = "USD"

    if price_map is not None:
        custom_list = [
            {"model": key, "price": dict(zip(("input", "output", "cache_read", "cache_write"), row["prices"]))}
            for key, row in sorted(price_map.items())
            if row["source"] not in ("legacy-override:builtin", "legacy-override:unpriced")]
    else:
        with _lock():
            custom_list = [
                {"model": k, "price": {"input": v[0], "output": v[1],
                                       "cache_read": v[2], "cache_write": v[3]}}
                for k, v in sorted(CUSTOM_PRICES.items())
            ]
    out = {"items": items, "totals": totals,
           "custom_prices": custom_list,
           "note": "内置单价是历史价格快照，单位为「美元 / 百万 token」，用于成本估算而非账单凭证。"
                   "缓存写入及其他收费依具体服务与模型而定，表中 0 值不代表已核实免费；请按有效价目保存价格版本。"
                   + ("另有 %d 个模型使用手动单价（标注「自定义价」）。" % len(custom_list) if custom_list else ""),
           "note_en": "Built-in unit prices are historical snapshots in USD per million tokens, "
                      "used for estimation rather than billing records. Cache writes and other "
                      "charges depend on the service and model; zero does not establish that "
                      "a service is free. Save a price version using the applicable price list."
                      + (" %d model(s) use manually set prices (marked \"custom\")." % len(custom_list) if custom_list else ""),
           "sources": "DeepSeek api-docs.deepseek.com/quick_start/pricing · OpenAI "
                      "developers.openai.com/api/docs/pricing · Anthropic "
                      "platform.claude.com/docs/en/about-claude/pricing · Gemini "
                      "ai.google.dev/gemini-api/docs/pricing · Grok docs.x.ai/developers/pricing "
                      "· Kimi platform.kimi.ai/docs/pricing/chat · Qwen "
                      "alibabacloud.com/help/en/model-studio/model-pricing · GLM docs.z.ai/guides/overview/pricing"}
    if summary_cost is not None:
        # 以总览的估算成本为基准做对账：明细能解释的部分 + 差额
        base = round(float(summary_cost or 0), 6)
        out["reconciliation"] = {
            "summary_cost": base,
            "itemized": totals["cost"],
            "delta": round(base - totals["cost"], 6),
            "stored_cost": totals["stored"],
        }
    return out
