"""确定性卡片渲染。

v0 全部走模板，不把模型自由文本直接面向家属。模型只作为「可选润色」，
默认关闭 —— 家属侧的可预期性比文采重要得多。
"""

from __future__ import annotations

from datetime import date
from typing import List, Sequence

from .consent import check_scope, is_redaction_applied, redact
from .dedup import SIGNAL_LABEL, collapse_repeats
from .models import (
    Acquisition,
    ChronicleInput,
    ConsentScope,
    DigestCard,
    HealthSignalRecord,
    Policy,
    SafetyEventRecord,
    SafetyLevel,
    Tier,
)

TIER_TITLE = {
    Tier.P0: "【需要立即关注】",
    Tier.P1: "【今天需要留意】",
    Tier.P2: "【今日摘要】",
}

ACQUISITION_NOTE = {
    Acquisition.NOT_ACQUIRED: "本次未能获取健康信息，不能据此判断老人没有异常。",
    Acquisition.NO_SIGNAL: "本次对话未识别出明确的健康信号，不等于没有问题。",
    Acquisition.ACQUIRED: "",
}

#: health 段没有任何条目时的兜底文案。
#: 纯语音场景（A 抛 safety event，B 一条 record 都没有）如果还写
#: 「今天没有需要特别说明的健康观察」，等于当着家属的面说反了。
SAFETY_FALLBACK = {
    SafetyLevel.P0: "· 本次通话中出现需要立即关注的表述，已按最高等级通知家属。",
    SafetyLevel.P1: "· 本次通话中出现需要留意的表述，已按今日关注等级高亮。",
}
NO_SIGNAL_LINE = "· 今天没有需要特别说明的健康观察。"
OUT_OF_SCOPE_LINE = "· 有内容超出当前授权范围，本次未展示。"


def _worst_safety(safety: Sequence[SafetyEventRecord]) -> "SafetyLevel | None":
    """今天最高的语音安全分级；没有则返回 None。"""
    if any(s.safety_level is SafetyLevel.P0 for s in safety):
        return SafetyLevel.P0
    if any(s.safety_level is SafetyLevel.P1 for s in safety):
        return SafetyLevel.P1
    return None


def render_health_section(
    records: Sequence[HealthSignalRecord],
    granted: Sequence[str],
    safety: Sequence[SafetyEventRecord] = (),
) -> dict:
    if not check_scope(granted, ConsentScope.HEALTH_SUMMARY):
        return {"heading": "健康观察", "body": "（未获得健康摘要授权，本段不展示）"}

    worst = collapse_repeats([r for r in records])
    lines: List[str] = []
    redacted_flag = False
    for r in worst.values():
        if r.signal_type.value == "none":
            continue
        label = SIGNAL_LABEL.get(r.signal_type, r.signal_type.value)
        detail = redact(r.detail)
        if is_redaction_applied(r.detail):
            redacted_flag = True
        sev = {"low": "轻微", "moderate": "中等", "high": "较明显"}[r.severity.value]
        lines.append(f"· {label}：{detail or '（无补充描述）'}（{sev}）")
    if not lines:
        lines.append(_no_signal_line(safety, granted))
    return {"heading": "健康观察", "body": "\n".join(lines)} | {"_redacted": str(redacted_flag)}


def _no_signal_line(
    safety: Sequence[SafetyEventRecord], granted: Sequence[str]
) -> str:
    """health 段为空时说什么，取决于「为什么为空」。

    三种情形必须分开，否则就是把话说反：
    - 今天语音侧有 P0/P1 → 明说「有需要关注的表述」（有 medication_safety 授权时）；
    - 有 P0/P1 但家属没给 medication_safety 授权 → 只能说「有内容未展示」，
      绝不能退回「今天没有需要特别说明的」那句误导；
    - 真的什么都没有 → 才用原来的兜底句。
    """
    level = _worst_safety(safety)
    if level is None:
        return NO_SIGNAL_LINE
    if not check_scope(granted, ConsentScope.MEDICATION_SAFETY):
        return OUT_OF_SCOPE_LINE
    return SAFETY_FALLBACK[level]


def render_chronicle_section(chronicle: ChronicleInput, granted: Sequence[str]) -> dict:
    head = "近期回忆"
    if not check_scope(granted, ConsentScope.CHRONICLE):
        return {"heading": head, "body": "（未获得回忆摘要授权，本段不展示）"}
    if chronicle.state != "ready":
        return {"heading": head, "body": "（回忆摘要尚未生成，本次不展示）"}
    body = redact(chronicle.narrative).strip() or "（暂无可展示的内容）"
    return {"heading": head, "body": body}


def build_card(
    *,
    card_id: str,
    elder_id: str,
    elder_name: str,
    for_date: date,
    tier: Tier,
    records: Sequence[HealthSignalRecord],
    chronicle: ChronicleInput,
    granted: Sequence[str],
    degraded_suspected: bool,
    acquisition: Acquisition,
    safety: Sequence[SafetyEventRecord] = (),
) -> DigestCard:
    warnings: List[str] = []
    note = ACQUISITION_NOTE.get(acquisition, "")
    if note:
        warnings.append(note)
    if degraded_suspected:
        warnings.append("健康模块本次曾调用语义模型但未稳定返回，不能据此认定没有问题。")

    health = render_health_section(records, granted, safety)
    redacted_flag = health.pop("_redacted", "False") == "True"
    chron = render_chronicle_section(chronicle, granted)

    title = f"{TIER_TITLE[tier]}{elder_name} {for_date.isoformat()}"
    if tier is Tier.P0:
        # P0 有两个来源：B 的用药安全阻断，和 A 的语音 P0。
        # 只认前者的话，纯语音场景会落回通用标题，家属看不出是安全问题。
        blocked = [r for r in records if r.safety_blocked]
        voice_p0 = any(s.safety_level is SafetyLevel.P0 for s in safety)
        if (blocked or voice_p0) and check_scope(granted, ConsentScope.MEDICATION_SAFETY):
            title = f"{TIER_TITLE[tier]}用药/安全风险，建议尽快联系{elder_name}"

    sections = [health, chron]
    if warnings:
        sections.append({"heading": "说明", "body": "\n".join("· " + w for w in warnings)})

    sources: List[str] = []
    for r in records:
        if r.signal_type.value != "none":
            sources.append(f"{r.session_id}:{r.turn_id}")
    for s in safety:
        ref = s.source_ref()
        if ref:
            sources.append(ref)

    return DigestCard(
        card_id=card_id,
        elder_id=elder_id,
        elder_name=elder_name,
        for_date=for_date,
        tier=tier,
        title=title,
        sections=[{"heading": s["heading"], "body": s["body"]} for s in sections],
        warnings=warnings,
        acquisition=acquisition,
        redacted=redacted_flag,
        sources=sorted(set(sources)),
    )


def render_plain_text(card: DigestCard) -> str:
    out = [card.title, ""]
    for s in card.sections:
        out.append(f"[{s['heading']}]")
        out.append(s["body"])
        out.append("")
    return "\n".join(out).strip()
