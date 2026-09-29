"""降级不可信：B 失败降级没有独立标志，D 必须自己区分「未获取」和「没事」。"""

from datetime import datetime

from family_digest import (
    Acquisition,
    ChronicleInput,
    HealthSignalRecord,
    SignalType,
    Severity,
    Tier,
)
from family_digest.cards import build_card, render_plain_text
from family_digest.routing import route, summarize_acquisition


def _rec(**kw) -> HealthSignalRecord:
    payload = {
        "event_id": "evt-x",
        "elder_id": "e1",
        "occurred_at": datetime.fromisoformat("2026-09-28T09:00:00"),
    }
    payload.update(kw)
    return HealthSignalRecord(**payload)


def test_not_acquired_when_backend_none():
    r = _rec(signal_type=SignalType.NONE, semantic_backend="none", qwen_called=False)
    assert r.acquisition() is Acquisition.NOT_ACQUIRED


def test_no_signal_when_model_ran_and_found_nothing():
    r = _rec(signal_type=SignalType.NONE, semantic_backend="mock", qwen_called=False)
    assert r.acquisition() is Acquisition.NO_SIGNAL


def test_degraded_suspicion_flag():
    r = _rec(signal_type=SignalType.NONE, semantic_backend="qwen", qwen_called=True)
    assert r.looks_degraded() is True
    d = route([r], [], [r], "e1", datetime(2026, 9, 28).date())
    assert d.degraded_suspected is True


def test_card_never_claims_all_well():
    r = _rec(signal_type=SignalType.NONE, semantic_backend="qwen", qwen_called=True)
    card = build_card(
        card_id="c1", elder_id="e1", elder_name="王奶奶",
        for_date=datetime(2026, 9, 28).date(), tier=Tier.P2,
        records=[r], chronicle=ChronicleInput(state="not_ready"),
        granted=["health_summary"], degraded_suspected=r.looks_degraded(),
        acquisition=summarize_acquisition([r]),
    )
    text = render_plain_text(card)
    assert "良好" not in text
    assert "正常" not in text
    assert any("未能" in w or "不等于" in w for w in card.warnings)


def test_not_acquired_wording():
    r = _rec(signal_type=SignalType.NONE, semantic_backend="none", qwen_called=False)
    card = build_card(
        card_id="c2", elder_id="e1", elder_name="王奶奶",
        for_date=datetime(2026, 9, 28).date(), tier=Tier.P2,
        records=[r], chronicle=ChronicleInput(),
        granted=["health_summary"], degraded_suspected=False,
        acquisition=summarize_acquisition([r]),
    )
    assert card.acquisition is Acquisition.NOT_ACQUIRED
    assert any("未能获取" in w for w in card.warnings)
