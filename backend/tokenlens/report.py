# -*- coding: utf-8 -*-
"""tokenlens · Markdown 报告：把聚合结果写成能直接贴进周报 / issue 的文本

为什么要有它：CSV / JSON 是给机器看的，导出来还得自己再整理一遍才能写进
周报。这里直接产出「一句话摘要 + 关键表格 + 洞察条目」的 Markdown，粘贴
即用，让这个工具从看板变成可交付物。

设计取舍：
  · 纯字符串拼接，不引第三方模板（本工程 stdlib-only，且 Markdown 表格
    本身就没有复杂的转义需求）。
  · 所有数字直接复用 Aggregator 的既有聚合入口，不另算一套口径——否则
    报告和界面上的数字会对不上，那种不一致比没有报告更糟。
  · 表格列数固定为 4~5 列。周报是要贴进 issue / 邮件里的，列多了在窄容器
    里会横向滚动，反而不如少给几列。
"""
from __future__ import annotations

import datetime as dt

from .aggregator import fmt_i, money, range_bounds


def _range_label(rng, lang="zh-CN") -> str:
    """区间的人类可读名字。复用聚合层的 range_bounds，保证与界面同一个口径
    （未知 key 同样按「全部时间」处理）。"""
    try:
        _start, _end, zh, en = range_bounds(rng)
    except Exception:
        return "全部时间" if lang != "en-US" else "All time"
    return (en or zh) if lang == "en-US" else (zh or en)


def _esc(value) -> str:
    """把任意值转成能安全放进 Markdown 表格单元格的文本。

    `|` 会截断单元格、换行会截断整张表，两者都必须转义。项目名和会话名
    来自用户自己的目录名，出现这两种字符完全可能。
    """
    s = str(value if value is not None else "")
    s = s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return s.strip() or "—"


def _delta_text(pct, lang="zh-CN") -> str:
    """涨跌幅的文字表示；None（没有上期可比）时给出「新出现」而不是 0%。"""
    if pct is None:
        return "new" if lang == "en-US" else "新增"
    sign = "+" if pct >= 0 else ""
    return "%s%.1f%%" % (sign, pct)


# 聚合层给「没有项目名 / 没有模型名」的记录发了中文占位符（(未标注) / 未知模型）。
# 那些是界面文案，不是用户数据，英文报告里必须翻掉；而项目名、会话名是用户
# 自己的目录名（可能是任何语言），一个字都不能动。
_PLACEHOLDERS = {
    "(未标注)": "(unlabeled)",
    "未知模型": "Unknown model",
}


def _label(value, lang="zh-CN"):
    """只翻译聚合层造的占位符，用户数据原样透出。

    占位符会**嵌在句子里**（洞察的会话描述是「((未标注) · t)」这种拼好的串），
    所以这里做的是子串替换而不是整串查表。用户真的把自己的项目命名成
    「(未标注)」的概率可以忽略，而漏翻的代价是英文报告里冒出一行中文。
    """
    s = str(value if value is not None else "")
    if lang != "en-US":
        return s
    for zh, en in _PLACEHOLDERS.items():
        if zh in s:
            s = s.replace(zh, en)
    return s


def _table(headers, rows) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join([" --- "] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_esc(c) for c in r) + " |")
    return "\n".join(out)


def _money_cell(usd, cny_rate=None) -> str:
    if cny_rate:
        return "%s (¥%.2f)" % (money(usd), float(usd or 0) * float(cny_rate))
    return money(usd)


def _cache_hit_cell(ratio) -> str:
    """The canonical value is a fraction; format it as percent exactly once."""
    return "N/A" if ratio is None else "%.1f%%" % (ratio * 100)


def _average_cell(value) -> str:
    return "N/A" if value is None else format(value, ",.2f")


# ----------------------------------------------------------------------
# 各段落
# ----------------------------------------------------------------------

def _headline(agg, rng, s, lang, tool=None) -> str:
    """一句话摘要：总量 + 环比 + 主力工具。这是整份报告里最该被读到的一行。"""
    label = _range_label(rng, lang)
    total = s.get("total") or 0
    delta = s.get("delta")
    cost = s.get("cost") or 0
    tools = agg.by_tool(rng, tool)
    top_tool = tools[0] if tools else None
    meta = {m["id"]: m for m in agg._tool_meta()}
    name = "—"
    if top_tool:
        # 适配器图鉴里查不到就退回原始 id：显示 "mytool" 也比显示 "—" 有用，
        # 至少读者知道该去找哪个来源（自定义 API 日志目录会走这条路）。
        name = (meta.get(top_tool["tool"], {}).get("name")
                or top_tool["tool"] or "—")

    if lang == "en-US":
        parts = ["**%s** used **%s tokens** across %s calls and %s sessions"
                 % (label, fmt_i(total), fmt_i(s.get("requests") or 0),
                    fmt_i(s.get("sessions") or 0))]
        if delta is not None:
            parts.append("that is **%s%.1f%%** versus the previous period"
                         % ("+" if delta >= 0 else "", delta))
        parts.append("estimated cost **%s**" % money(cost))
        if top_tool and not tool:
            parts.append("the largest source was **%s** (%.1f%%)" % (name, top_tool["share"]))
        return ". ".join(parts) + "."

    parts = ["**%s** 共消耗 **%s tokens**，%s 次调用、%s 个会话"
             % (label, fmt_i(total), fmt_i(s.get("requests") or 0),
                fmt_i(s.get("sessions") or 0))]
    if delta is not None:
        parts.append("较上一周期**%s%.1f%%**" % ("+" if delta >= 0 else "", delta))
    parts.append("记录费用 **%s**" % money(cost))
    if top_tool and not tool:
        parts.append("用量最大的来源是 **%s**（占 %.1f%%）" % (name, top_tool["share"]))
    return "，".join(parts) + "。"


def _summary_table(agg, rng, s, lang, tool=None) -> str:
    if lang == "en-US":
        headers = ["Metric", "Value", "Previous", "Change"]
        metric_names = ["Total tokens", "Input", "Output", "Cache read",
                        "Cache write", "Calls", "Recorded cost",
                        "Input token cache hit rate", "Average tokens per request"]
    else:
        headers = ["指标", "本期", "上期", "变化"]
        metric_names = ["总 tokens", "输入", "输出", "缓存读",
                        "缓存写", "调用次数", "记录费用",
                        "输入 Token 缓存命中率", "均次 tokens"]

    # Both periods must keep the same source and query-scoped filters.
    cmp_data = agg.compare(rng, tool)
    cur = cmp_data.get("current") or {}
    prev = cmp_data.get("previous") or {}

    def d(pct):
        return _delta_text(pct, lang) if cmp_data.get("has_prev") else "—"

    rows = [
        [metric_names[0], fmt_i(s.get("total")), fmt_i(cur.get("total_prev")),
         d(cur.get("total_delta"))],
        [metric_names[1], fmt_i(s.get("input")), fmt_i(cur.get("input_prev")),
         d(cur.get("input_delta"))],
        [metric_names[2], fmt_i(s.get("output")), fmt_i(cur.get("output_prev")),
         d(cur.get("output_delta"))],
        [metric_names[3], fmt_i(s.get("cache_read")), fmt_i(cur.get("cache_read_prev")),
         d(cur.get("cache_read_delta"))],
        [metric_names[4], fmt_i(s.get("cache_write")), "—", "—"],
        [metric_names[5], fmt_i(s.get("requests")), fmt_i(cur.get("requests_prev")),
         d(cur.get("requests_delta"))],
        [metric_names[6], _money_cell(s.get("cost")), _money_cell(prev.get("cost")),
         d(cur.get("cost_delta"))],
        [metric_names[7], _cache_hit_cell(s.get("cache_hit_ratio")),
         _cache_hit_cell(prev.get("cache_hit_ratio")), "—"],
        [metric_names[8], _average_cell(s.get("avg_tokens_per_request")),
         _average_cell(prev.get("avg_tokens_per_request")), "—"],
    ]
    return _table(headers, rows)


def _tool_table(agg, rng, lang, top=8, cny_rate=None, tool=None) -> str:
    rows = agg.by_tool(rng, tool)[:top]
    if not rows:
        return ""
    meta = {m["id"]: m for m in agg._tool_meta()}
    if lang == "en-US":
        headers = ["Source", "Tokens", "Calls", "Cost", "Share"]
    else:
        headers = ["来源", "tokens", "调用", "成本", "占比"]
    body = []
    for r in rows:
        name = meta.get(r["tool"], {}).get("name") or r["tool"]
        body.append([name, fmt_i(r["total"]), fmt_i(r.get("requests") or 0),
                     _money_cell(r.get("cost"), cny_rate), "%.1f%%" % r["share"]])
    return _table(headers, body)


def _model_table(agg, rng, lang, top=8, tool=None) -> str:
    rows = agg.by_model(rng, top, tool)[:top]
    if not rows:
        return ""
    if lang == "en-US":
        headers = ["Model", "Tokens", "Calls", "Cost", "Share"]
    else:
        headers = ["模型", "tokens", "调用", "成本", "占比"]
    body = [[_label(r["model"], lang), fmt_i(r["total"]), fmt_i(r.get("requests") or 0),
             money(r.get("cost")), "%.1f%%" % r["share"]] for r in rows]
    return _table(headers, body)


def _project_table(agg, rng, lang, top=8, tool=None) -> str:
    rows = agg.by_project(rng, top, tool)[:top]
    if not rows:
        return ""
    if lang == "en-US":
        headers = ["Project", "Tokens", "Calls", "Cost"]
    else:
        headers = ["项目", "tokens", "调用", "成本"]
    body = [[_label(r["project"], lang), fmt_i(r["total"]), fmt_i(r.get("requests") or 0),
             money(r.get("cost"))] for r in rows]
    return _table(headers, body)


def _daily_table(agg, rng, lang, top=7, tool=None) -> str:
    """每日明细。周报取前 7 行即可，全量列表在 CSV 里。"""
    series = agg.daily_series(rng, tool)
    if not series:
        return ""
    rows = sorted(series, key=lambda x: -(x.get("total") or 0))[:top]
    rows.sort(key=lambda x: x["day"])
    if lang == "en-US":
        headers = ["Date", "Tokens", "Calls", "Cost"]
    else:
        headers = ["日期", "tokens", "调用", "成本"]
    body = [[r["day"], fmt_i(r.get("total")), fmt_i(r.get("requests") or 0),
             money(r.get("cost"))] for r in rows]
    return _table(headers, body)


def _insights(agg, rng, lang, tool=None) -> str:
    items = agg.insights(rng, tool)
    if not items:
        return ""
    out = []
    for it in items:
        if lang == "en-US":
            title = it.get("title_en") or it.get("title") or ""
            value = it.get("value_en") or it.get("value") or ""
            desc = it.get("desc_en") or it.get("desc") or ""
        else:
            title = it.get("title") or ""
            value = it.get("value") or ""
            desc = it.get("desc") or ""
        # 洞察正文里会嵌项目名/会话名（聚合层拼好的），占位符一并翻掉
        title, value, desc = (_label(title, lang), _label(value, lang),
                              _label(desc, lang))
        line = "- **%s**：%s" % (title, value)
        if lang == "en-US":
            line = "- **%s**: %s" % (title, value)
        if desc:
            line += (" — %s" if lang == "en-US" else " —— %s") % desc
        out.append(line)
    return "\n".join(out)


def _budget(agg, rng, cfg, lang) -> str:
    try:
        b = agg.budget(rng, cfg)
    except Exception:
        return ""
    if (b.get("metric") or "none") == "none" or not b.get("limit"):
        return ""
    if lang == "en-US":
        return ("**Budget**: %s of %s used (%.1f%%)%s"
                % (fmt_i(b["used"]) if b["metric"] == "tokens" else money(b["used"]),
                   fmt_i(b["limit"]) if b["metric"] == "tokens" else money(b["limit"]),
                   (b["used"] * 100.0 / b["limit"]) if b["limit"] else 0,
                   " — **over budget**" if b.get("over") else ""))
    return ("**预算**：已用 %s / %s（%.1f%%）%s"
            % (fmt_i(b["used"]) if b["metric"] == "tokens" else money(b["used"]),
               fmt_i(b["limit"]) if b["metric"] == "tokens" else money(b["limit"]),
               (b["used"] * 100.0 / b["limit"]) if b["limit"] else 0,
               " —— **已超预算**" if b.get("over") else ""))


def _privacy_note(agg, rng, lang, tool=None) -> str:
    """口径说明。报告一旦贴出去就脱离了界面，必须自带这段，否则读者会
    把估算值当成账单。"""
    s = agg.summary(rng, tool)
    unpriced = agg.by_model(rng, 200, tool)
    unknown = [r for r in unpriced if not _is_known_model(r["model"])]
    if lang == "en-US":
        txt = ("Costs include recorded charges and price estimates; unpriced records are excluded. "
               "Estimates from public list prices are for reference only; "
               "subscriptions, free tiers and gateway discounts are not reflected. "
               "Totals count input + output + cache read + cache write + reasoning. "
               "Input token cache hit rate is cache read / (uncached input + cache read + cache write), "
               "not a request hit rate or cost savings percentage. Average tokens per request is total tokens / calls. "
               "N/A means no input tokens or no calls, respectively.")
        if unknown:
            txt += (" %d model(s) are absent from the default price list; recorded charges or saved rates may still apply: %s."
                    % (len(unknown),
                       ", ".join(_label(_esc(r["model"]), lang) for r in unknown[:5])))
        return txt
    txt = ("费用包含采集到的实付金额与价格估算，未定价记录不计入金额。公开价目估算仅供参考；订阅制、免费额度与中转折扣不在估算中体现。"
           "总量口径为 输入 + 输出 + 缓存读 + 缓存写 + 思考。"
           "输入 Token 缓存命中率 = 缓存读 /（未缓存输入 + 缓存读 + 缓存写），不代表请求命中率或省费比例。"
           "均次 tokens = 总 tokens / 调用次数；两项指标的 N/A 分别表示没有输入 Token 或没有调用次数。")
    if unknown:
        txt += "其中 %d 个模型未收录默认单价；仍可使用采集金额或已保存价格：%s。" % (
            len(unknown), "、".join(_esc(r["model"]) for r in unknown[:5]))
    return txt


def _is_known_model(name) -> bool:
    from .pricing import is_known
    return is_known(name)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def render(agg, rng="30d", lang="zh-CN", tool=None, cfg=None, title=None) -> str:
    """把当前区间渲染成一份 Markdown 报告。

    rng 支持的取值与界面一致：today / 1d / 7d / 30d / 90d / 180d / 365d /
    ytd / all / 自定义 "起始~结束"。未知 key 与聚合层一样按「全部时间」处理
    （保持前后端同一套语义，不在这里另立规矩）。
    """
    en = (lang or "zh-CN").lower().startswith("en")
    lang = "en-US" if en else "zh-CN"
    s = agg.summary(rng, tool)
    label = _range_label(rng, lang)
    start, end, _zh, _en = range_bounds(rng)
    today = dt.date.today().isoformat()

    cny_rate = None
    if cfg is not None:
        try:
            rate = float(cfg.get("usd_to_cny") or 0)
            cny_rate = rate if rate > 0 else None
        except (TypeError, ValueError):
            cny_rate = None

    if not title:
        title = ("TokenLens usage report · %s" % label) if en else ("TokenLens 用量报告 · %s" % label)

    # 按来源筛选时，报告必须在标题下说清自己只覆盖哪一个来源——这份文本
    # 贴出去之后没人能看到界面上的筛选器，只看到数字会以为这是全部用量。
    scope = ""
    if tool:
        meta = {m["id"]: m for m in agg._tool_meta()}
        name = meta.get(tool, {}).get("name") or tool
        scope = ("Source: **%s**" % name) if en else ("数据来源：**%s**" % name)

    L = []
    L.append("# %s" % title)
    L.append("")
    span = ("%s → %s" % (start, end)) if start else ("— → %s" % end)
    if en:
        L.append("*Period: %s · generated %s*" % (span, today))
    else:
        L.append("*统计区间：%s · 生成于 %s*" % (span, today))
    if scope:
        L.append("")
        L.append("*%s*" % scope)
    L.append("")

    if not (s.get("total") or 0):
        L.append("_No usage recorded in this period._" if en
                 else "_该区间没有记录到任何用量。_")
        L.append("")
        # Even an empty period distinguishes unavailable input from a real 0%.
        # Use the existing summary; no comparison queries are needed here.
        L.append(_table(["Metric", "Value"] if en else ["指标", "本期"], [
            ["Input token cache hit rate" if en else "输入 Token 缓存命中率",
             _cache_hit_cell(s.get("cache_hit_ratio"))],
            ["Average tokens per request" if en else "均次 tokens",
             _average_cell(s.get("avg_tokens_per_request"))],
        ]))
        return "\n".join(L) + "\n"

    # 一句话摘要
    L.append("## " + ("Summary" if en else "一句话摘要"))
    L.append("")
    L.append(_headline(agg, rng, s, lang, tool))
    L.append("")
    b = _budget(agg, rng, cfg, lang)
    if b:
        L.append(b)
        L.append("")

    L.append("## " + ("Key numbers" if en else "关键指标"))
    L.append("")
    L.append(_summary_table(agg, rng, s, lang, tool))
    L.append("")

    # 已经按来源收窄时，「按数据来源」这张表只会剩一行，没有信息量
    t = "" if tool else _tool_table(agg, rng, lang, cny_rate=cny_rate)
    if t:
        L.append("## " + ("By data source" if en else "按数据来源"))
        L.append("")
        L.append(t)
        L.append("")

    m = _model_table(agg, rng, lang, tool=tool)
    if m:
        L.append("## " + ("By model" if en else "按模型"))
        L.append("")
        L.append(m)
        L.append("")

    p = _project_table(agg, rng, lang, tool=tool)
    if p:
        L.append("## " + ("By project" if en else "按项目"))
        L.append("")
        L.append(p)
        L.append("")

    d = _daily_table(agg, rng, lang, tool=tool)
    if d:
        L.append("## " + ("Busiest days" if en else "用量最高的几天"))
        L.append("")
        L.append(d)
        L.append("")

    ins = _insights(agg, rng, lang, tool=tool)
    if ins:
        L.append("## " + ("Insights" if en else "洞察"))
        L.append("")
        L.append(ins)
        L.append("")

    L.append("---")
    L.append("")
    L.append(_privacy_note(agg, rng, lang, tool))
    L.append("")
    return "\n".join(L)
