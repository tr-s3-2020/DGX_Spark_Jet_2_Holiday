"""路由：P0 只做映射不自创；趋势只升到 P1。"""

from datetime import datetime

from family_digest import (
    HealthSignalRecord,
    Policy,
    SafetyEventRecord,
    SafetyLevel,
    Severity,
    SignalType,
    Tier,
)
from family_digest.routing import route


def hr(day: str, st=SignalType.SYMPTOM, sev=Severity.LOW, **kw) -> HealthSignalRecord:
    payload = {
        "event_id": f"evt-{day}-{st.value}-{sev.value}",
        "elder_id": "e1",
        "occurred_at": datetime.fromisoformat(f"2026-09-{day}T09:00:00"),
        "signal_type": st,
        "severity": sev,
    }
    payload.update(kw)
    return HealthSignalRecord(**payload)


def test_medication_block_maps_to_p0():
    recs = [hr("28", SignalType.MEDICATION, Severity.HIGH, safety_blocked=True,
               rule_id="MEDICATION_DOSE_INCREASE")]
    d = route(recs, [], recs, "e1", datetime(2026, 9, 28).date())
    assert d.tier is Tier.P0
    assert any("medication_safety_blocked" in r for r in d.reasons)


def test_voice_p0_maps_to_p0():
    safety = [SafetyEventRecord(event_id="a1", elder_id="e1",
                                occurred_at=datetime.fromisoformat("2026-09-28T09:00:00"),
                                safety_level=SafetyLevel.P0)]
    d = route([], safety, [], "e1", datetime(2026, 9, 28).date())
    assert d.tier is Tier.P0
    assert "voice_safety_p0" in d.reasons


def test_single_high_is_p1():
    recs = [hr("28", SignalType.PAIN, Severity.HIGH)]
    d = route(recs, [], recs, "e1", datetime(2026, 9, 28).date())
    assert d.tier is Tier.P1
    assert "single_day_high" in d.reasons


def test_three_day_trend_is_p1():
    recs = [hr(d, SignalType.MOBILITY, Severity.MODERATE) for d in ("26", "27", "28")]
    d = route([recs[-1]], [], recs, "e1", datetime(2026, 9, 28).date(), Policy())
    assert d.tier is Tier.P1
    assert "trend:bodily>=3d" in d.reasons  # mobility 归入 bodily 组


def test_two_day_trend_stays_p2():
    recs = [hr(d, SignalType.MOBILITY, Severity.MODERATE) for d in ("27", "28")]
    d = route([recs[-1]], [], recs, "e1", datetime(2026, 9, 28).date(), Policy())
    assert d.tier is Tier.P2


def test_trend_never_escalates_to_p0():
    """边界纪律：趋势只能到 P1，D 不自创 P0。"""
    recs = [hr(d, SignalType.MOBILITY, Severity.HIGH) for d in ("26", "27", "28")]
    d = route([recs[-1]], [], recs, "e1", datetime(2026, 9, 28).date(), Policy())
    assert d.tier is not Tier.P0


def test_routine_is_p2():
    recs = [hr("28", SignalType.APPETITE, Severity.LOW)]
    d = route(recs, [], recs, "e1", datetime(2026, 9, 28).date())
    assert d.tier is Tier.P2
    assert d.reasons == ["routine"]


def test_shallow_hierarchy_counts_as_one_trend():
    """B 的 type 是浅层次结构：疼痛/睡眠/食欲/行动能力 都是「身体不适」的子类。

    连续三天分别是 pain / symptom / mobility —— 精确 type 相等会判成互不连续，
    按趋势分组才是真趋势。这是 B 的 HealthType 文档明确点名的坑。
    """
    recs = [
        hr("26", SignalType.PAIN, Severity.MODERATE),
        hr("27", SignalType.SYMPTOM, Severity.MODERATE),
        hr("28", SignalType.MOBILITY, Severity.MODERATE),
    ]
    d = route([recs[-1]], [], recs, "e1", datetime(2026, 9, 28).date(), Policy())
    assert d.tier is Tier.P1
    assert "trend:bodily>=3d" in d.reasons


def test_medication_is_its_own_group():
    """用药不并入 bodily —— 用药阻断另走 P0 路径，不能被身体不适趋势稀释。"""
    recs = [hr(d, SignalType.MEDICATION, Severity.MODERATE) for d in ("26", "27", "28")]
    d = route([recs[-1]], [], recs, "e1", datetime(2026, 9, 28).date(), Policy())
    assert d.tier is Tier.P1
    assert "trend:medication>=3d" in d.reasons
