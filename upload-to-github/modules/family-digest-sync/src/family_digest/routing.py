"""三档路由：把 A/B 的判定结果映射成 D 的通知档位。

边界纪律：D **不自创告警**。P0 的唯一来源是 A 的 safety_level=P0 与 B 的
safety.blocked=true；趋势只升到 P1，绝不自动升 P0。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Sequence

from .models import (
    Acquisition,
    HealthSignalRecord,
    Policy,
    SafetyEventRecord,
    SafetyLevel,
    Severity,
    SEVERITY_ORDER,
    Tier,
)
from . import trend


@dataclass
class RoutingDecision:
    tier: Tier
    reasons: List[str] = field(default_factory=list)
    acquisition: Acquisition = Acquisition.NOT_ACQUIRED
    degraded_suspected: bool = False

    def as_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "reasons": self.reasons,
            "acquisition": self.acquisition.value,
            "degraded_suspected": self.degraded_suspected,
        }


def summarize_acquisition(
    records: Sequence[HealthSignalRecord],
    safety: Sequence[SafetyEventRecord] = (),
) -> Acquisition:
    """判定"今天到底采集到没有"。

    ⚠️ 两条腿都要看，只看任何一条都会得出方向相反的结论：

    - 只看 B 的 health record：纯语音场景（A 抛 safety event，B 一条 record
      都没有）会被判成 ``not_acquired`` —— 可真相是「聊得很好，而且出了 P0」；
    - 只看 A 的 safety event：B 正常跑完却无信号的日子会被漏判。

    A 只要给出了明确分级（P0/P1），就说明**老人开口说了被识别出的风险句**，
    这本身就是采集成功。
    """
    if any(s.has_signal() for s in safety):
        return Acquisition.ACQUIRED
    if not records:
        return Acquisition.NOT_ACQUIRED
    if any(r.acquisition() is Acquisition.ACQUIRED for r in records):
        return Acquisition.ACQUIRED
    if any(r.acquisition() is Acquisition.NO_SIGNAL for r in records):
        return Acquisition.NO_SIGNAL
    return Acquisition.NOT_ACQUIRED


def route(
    today_records: Sequence[HealthSignalRecord],
    today_safety: Sequence[SafetyEventRecord],
    history: Sequence[HealthSignalRecord],
    elder_id: str,
    for_date: date,
    policy: Policy = Policy(),
) -> RoutingDecision:
    reasons: List[str] = []

    # ---- P0：只做映射，来源严格限于 A 的 P0 与 B 的用药安全阻断 ----
    for r in today_records:
        if r.safety_blocked:
            reasons.append(f"medication_safety_blocked:{r.rule_id or 'RULE'}")
    for s in today_safety:
        if s.safety_level is SafetyLevel.P0:
            reasons.append("voice_safety_p0")
    if reasons:
        return RoutingDecision(
            tier=Tier.P0,
            reasons=sorted(set(reasons)),
            acquisition=summarize_acquisition(today_records, today_safety),
            degraded_suspected=any(r.looks_degraded() for r in today_records),
        )

    # ---- P1：单日 high ----
    if any(
        r.signal_type.value != "none" and r.severity is Severity.HIGH
        for r in today_records
    ):
        reasons.append("single_day_high")

    # ---- P1：语音侧 P1 映射（不自创，来源就是 A 的分级）----
    # 不写这一条的话，A 明确判定「需要留意」的话会在 D 里被静默丢掉，
    # 家属侧完全感知不到——这比误报更糟。
    if any(s.safety_level is SafetyLevel.P1 for s in today_safety):
        reasons.append("voice_safety_p1")

    # ---- P1：跨日趋势 ----
    for st in trend.trending_types(history, elder_id, for_date, policy):
        reasons.append(f"trend:{st}>={policy.trend_days}d")

    # ---- P1：单日聚集 ----
    today_count = sum(
        1
        for r in today_records
        if r.signal_type.value != "none"
        and SEVERITY_ORDER[r.severity] >= SEVERITY_ORDER[policy.trend_min_severity]
    )
    if today_count >= policy.sameday_count_threshold:
        reasons.append(f"sameday_count:{today_count}")

    # ---- P1：偏离基线 ----
    base = trend.baseline_mean(history, elder_id, for_date, policy)
    if base > 0 and today_count >= 2 and today_count > base * (1 + policy.baseline_deviation):
        reasons.append(f"baseline_deviation:{today_count}vs{base:.2f}")

    if reasons:
        return RoutingDecision(
            tier=Tier.P1,
            reasons=sorted(set(reasons)),
            acquisition=summarize_acquisition(today_records, today_safety),
            degraded_suspected=any(r.looks_degraded() for r in today_records),
        )

    return RoutingDecision(
        tier=Tier.P2,
        reasons=["routine"],
        acquisition=summarize_acquisition(today_records, today_safety),
        degraded_suspected=any(r.looks_degraded() for r in today_records),
    )
