"""授权与脱敏：没授权一个字不给；有授权也要先脱敏。"""

from datetime import datetime

from family_digest import FamilyDigestService
from family_digest.adapters import MockChannel
from family_digest.consent import redact
from family_digest.models import (
    ChronicleInput, HealthSignalRecord, SignalType, Severity, Tier,
)
from family_digest.cards import build_card, render_plain_text
from family_digest.store import JsonStore


def test_redact_id_card_before_phone():
    text = "身份证 320101199001011234，手机 13812345678，住在 3 栋 502"
    out = redact(text)
    assert "320101199001011234" not in out
    assert "13812345678" not in out
    assert "502" not in out


def test_redact_truncates_long_text():
    assert redact("很" * 900).endswith("……")


def test_no_consent_means_ignored(tmp_path):
    svc = FamilyDigestService(store=JsonStore(str(tmp_path / "s.json")), channel=MockChannel())
    svc.execute("record_signals", {"health_signals": [{
        "event_id": "e1", "elder_id": "e1",
        "occurred_at": "2026-09-28T09:00:00",
        "signal_type": "symptom", "severity": "low"}]})
    res = svc.execute("build_daily_digest", {"elder_id": "e1", "date": "2026-09-28"})
    assert res["status"] == "ignored"
    assert res["meta"]["reason"] == "no_family_digest_consent"


def test_chronicle_without_consent_is_placeholder():
    r = HealthSignalRecord(event_id="e1", elder_id="e1",
                           occurred_at=datetime.fromisoformat("2026-09-28T09:00:00"),
                           signal_type=SignalType.SYMPTOM, severity=Severity.LOW,
                           detail="有点咳嗽")
    card = build_card(card_id="c", elder_id="e1", elder_name="王奶奶",
                      for_date=datetime(2026, 9, 28).date(), tier=Tier.P2,
                      records=[r],
                      chronicle=ChronicleInput(state="ready", narrative="在纺织厂做过挡车工"),
                      granted=["health_summary"], degraded_suspected=False,
                      acquisition=r.acquisition())
    text = render_plain_text(card)
    assert "纺织厂" not in text
    assert "未获得回忆摘要授权" in text


def test_sensitive_detail_is_masked_in_card():
    r = HealthSignalRecord(event_id="e1", elder_id="e1",
                           occurred_at=datetime.fromisoformat("2026-09-28T09:00:00"),
                           signal_type=SignalType.SYMPTOM, severity=Severity.LOW,
                           detail="联系 13812345678")
    from family_digest.models import Tier
    card = build_card(card_id="c", elder_id="e1", elder_name="王奶奶",
                      for_date=datetime(2026, 9, 28).date(), tier=Tier.P2,
                      records=[r], chronicle=ChronicleInput(),
                      granted=["health_summary"], degraded_suspected=False,
                      acquisition=r.acquisition())
    assert card.redacted is True
    assert "13812345678" not in render_plain_text(card)
