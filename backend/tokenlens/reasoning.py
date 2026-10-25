"""Documented reasoning controls shared by the IQ API and its model selector.

These are protocol capabilities, not proof that a third-party gateway implements
them. A gateway rejection remains an error; an explicit effort is never retried
with a weaker/default effort. Reviewed against official provider docs 2026-10-12:
https://developers.openai.com/api/docs/guides/reasoning
https://platform.claude.com/docs/en/build-with-claude/effort
https://platform.claude.com/docs/en/build-with-claude/extended-thinking
https://ai.google.dev/gemini-api/docs/generate-content/thinking
"""
from __future__ import annotations

import re


LEVELS = ("auto", "none", "minimal", "low", "medium", "high", "xhigh", "max")


def _family(model, pattern):
    return bool(re.search(r"(?:^|[/ :])(?:" + pattern + r")(?:[-/: ]|$)", model))


def reasoning_capabilities(protocol, model, base_url=""):
    """Return credential-free metadata and the exact levels a model can receive.

    ``auto`` means omit the effort/budget, preserving the provider's default. The
    summary request is independent of effort and is sent even in auto mode.
    Unknown model IDs offer only auto instead of claiming unsupported controls.
    """
    proto = str(protocol or "").strip().lower()
    name = str(model or "").strip().lower()
    url = str(base_url or "").lower()
    result = {"protocol": proto, "levels": ["auto"], "control": "",
              "summary": "provider", "note": "未确认该模型支持思考强度设置，自动模式使用服务商默认值。"}

    def supported(levels, control, summary="provider", note=""):
        result.update(levels=["auto", *levels], control=control, summary=summary,
                      note=note or "按所选协议发送原生思考参数；第三方服务商是否实现该参数以实际响应为准。")
        return result

    if proto in ("responses", "openai"):
        effort = "reasoning.effort" if proto == "responses" else "reasoning_effort"
        summary = "responses_summary" if proto == "responses" else "provider"
        if _family(name, r"gpt-6(?:\.\d+)?-astra") or _family(name, r"gpt-6\.[1-9]\d*-sol"):
            return supported(("low", "medium", "high", "xhigh", "max"), effort, summary)
        if _family(name, r"gpt-6-sol"):
            return supported(("none", "low", "medium", "high", "xhigh", "max"), effort, summary)
        if _family(name, r"gpt-5\.6(?:-sol)?"):
            return supported(("none", "low", "medium", "high", "xhigh", "max"), effort, summary)
        if _family(name, r"gpt-5(?:\.\d+)?-pro"):
            levels = ("high",) if _family(name, r"gpt-5-pro") else ("medium", "high", "xhigh")
            return supported(levels, effort, summary)
        if "codex" in name and _family(name, r"gpt-5(?:\.\d+)?"):
            # Codex has no none/minimal effort. xhigh was added to Codex-Max
            # and the 5.2/5.3 generation.
            extended = "codex-max" in name or bool(re.search(r"gpt-5\.[2-9]\d*", name))
            levels = ("low", "medium", "high", "xhigh") if extended else ("low", "medium", "high")
            return supported(levels, effort, summary)
        if _family(name, r"gpt-5\.1"):
            return supported(("none", "low", "medium", "high"), effort, summary)
        if _family(name, r"gpt-5\.[2-9]\d*"):
            return supported(("none", "low", "medium", "high", "xhigh"), effort, summary)
        if _family(name, r"gpt-5"):
            return supported(("minimal", "low", "medium", "high"), effort, summary)
        if _family(name, r"o1-pro"):
            return supported(("high",), effort, summary)
        if _family(name, r"o1(?:-preview|-mini)"):
            return result
        if _family(name, r"o[134]"):
            return supported(("low", "medium", "high"), effort, summary)
        # These compatible APIs expose a thinking switch, not low/medium/high
        # effort. Show only off/on instead of presenting three identical modes.
        if proto == "openai" and "siliconflow" in url:
            return supported(("none", "high"), "enable_thinking", note="该接入支持关闭或启用思考；启用时使用模型默认思考预算。")
        if proto == "openai" and "deepseek" in name + " " + url:
            return supported(("none", "high"), "thinking.type", note="该接入使用原生思考开关，启用时使用模型默认预算。")
        if proto == "openai" and any(x in name for x in ("glm-4.5", "glm-4.6", "qwen3")):
            return supported(("none", "high"), "chat_template_kwargs.enable_thinking",
                             note="此模型使用兼容服务的思考开关；是否支持由服务商确认。")
        return result

    if proto == "anthropic":
        # Claude 4.6+ uses adaptive thinking; manual budget_tokens is rejected
        # by 4.7+. Opus 4.5 supports effort with manual extended thinking.
        version = re.search(r"claude-(opus|sonnet|haiku)-(\d+)[.-](\d+)", name)
        if version and (int(version[2]), int(version[3])) >= (4, 6):
            family, major, minor = version[1], int(version[2]), int(version[3])
            levels = ("none", "low", "medium", "high", "max")
            if family == "opus" and (major, minor) >= (4, 7) or (major, minor) >= (5, 0):
                levels = ("none", "low", "medium", "high", "xhigh", "max")
            if family == "haiku" and (major, minor) < (5, 5):
                return result
            return supported(levels, "anthropic_adaptive", "anthropic_summary",
                             "自动使用服务商默认值；其他档位使用自适应思考与 output_config.effort。思考摘要由模型决定是否返回。")
        if "claude-opus-4-5" in name or "claude-opus-4.5" in name:
            return supported(("none", "low", "medium", "high"), "anthropic_effort_legacy", "anthropic_summary")
        if _family(name, r"claude-mythos-preview"):
            return supported(("none", "low", "medium", "high", "max"), "anthropic_adaptive", "anthropic_summary")
        if _family(name, r"claude-(?:opus|sonnet|haiku)-[5-9]") or _family(name, r"claude-(?:fable|mythos)-5"):
            return supported(("none", "low", "medium", "high", "xhigh", "max"), "anthropic_adaptive", "anthropic_summary")
        if any(x in name for x in ("claude-3-7-sonnet", "claude-sonnet-4", "claude-opus-4", "claude-haiku-4-5")):
            return supported(("none", "low", "medium", "high"), "anthropic_budget", "anthropic_summary",
                             "低、中、高分别设置 1024、4096、8192 个思考 token 的预算；公开摘要由服务商返回。")
        return result

    if proto == "gemini":
        if re.search(r"gemini-[3-9](?:\.\d+)?-", name):
            levels = ("minimal", "low", "medium", "high") if "flash" in name else ("low", "medium", "high")
            # Gemini 3.0 Pro documented only low/high; 3.1 Pro adds medium.
            if re.search(r"gemini-3(?:\.0)?-pro", name):
                levels = ("low", "high")
            return supported(levels, "gemini_level", "gemini_summary",
                             "使用原生 thinkingLevel 并请求公开思考摘要；minimal 不保证完全关闭思考。")
        if "gemini-2.5" in name:
            levels = ("low", "medium", "high") if "pro" in name else ("none", "low", "medium", "high")
            return supported(levels, "gemini_budget", "gemini_summary",
                             "使用原生 thinkingBudget；低、中、高分别为 1024、4096、8192 token。Pro 不能关闭思考。")
    return result


def economical_reasoning(protocol, model):
    """Smallest documented control for availability/rewrites, not IQ effort."""
    capability = reasoning_capabilities(protocol, model)
    levels = capability["levels"][1:]
    if not levels:
        return {}
    if protocol == "responses":
        return {"reasoning": {"effort": levels[0]}}
    if capability["control"] == "gemini_level":
        return {"thinkingLevel": levels[0]}
    if capability["control"] == "gemini_budget":
        return {"thinkingBudget": 128 if "pro" in str(model).lower() else 0}
    return {}
