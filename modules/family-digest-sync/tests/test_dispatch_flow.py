"""发送：重试到上限进死信，且死信可查 —— 家属侧漏发比迟到严重。"""

import json
import os
from datetime import datetime

import pytest

from family_digest import FamilyDigestService, Policy
from family_digest.adapters import FlakyChannel, MockChannel
from family_digest.cards import build_card, render_plain_text
from family_digest.models import (
    Acquisition, CardStatus, ChronicleInput, HealthSignalRecord, SignalType, Severity, Tier,
)
from family_digest.state import CardStateMachine, InvalidTransition
from family_digest.store import JsonStore

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")


def load(name):
    with open(os.path.join(EXAMPLES, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _card(tmp_path, channel, policy=Policy()):
    svc = FamilyDigestService(store=JsonStore(str(tmp_path / "s.json")),
                              channel=channel, policy=policy)
    r = HealthSignalRecord(event_id="e1", elder_id="e1",
                           occurred_at=datetime.fromisoformat("2026-09-28T09:00:00"),
                           signal_type=SignalType.SYMPTOM, severity=Severity.MODERATE,
                           detail="腿沉")
    card = build_card(card_id="c1", elder_id="e1", elder_name="王奶奶",
                      for_date=datetime(2026, 9, 28).date(), tier=Tier.P1,
                      records=[r], chronicle=ChronicleInput(),
                      granted=["health_summary"], degraded_suspected=False,
                      acquisition=Acquisition.ACQUIRED)
    svc.store.put_card(card.model_dump(mode="json"))
    return svc, card


def test_send_success(tmp_path):
    svc, card = _card(tmp_path, MockChannel())
    res = svc.execute("dispatch_digest", {"card_id": "c1"})
    assert res["status"] == "ok"
    assert res["data"]["status"] == "sent"
    assert svc.execute("get_digest", {"card_id": "c1"})["data"]["status"] == "sent"


def test_retry_then_succeed(tmp_path):
    channel = FlakyChannel(fail_times=2)
    svc, _ = _card(tmp_path, channel)
    res = svc.execute("dispatch_digest", {"card_id": "c1"})
    assert res["data"]["status"] == "sent"
    assert channel.calls == 3


def test_exhausted_retry_goes_dead_letter(tmp_path):
    channel = FlakyChannel(fail_times=9)
    svc, _ = _card(tmp_path, channel, Policy(retry_limit=3))
    res = svc.execute("dispatch_digest", {"card_id": "c1"})
    assert res["status"] == "degraded"
    assert res["data"]["status"] == "dead_letter"
    assert svc.execute("get_digest", {"card_id": "c1"})["data"]["status"] == "dead_letter"


def test_state_machine_rejects_illegal_transition():
    fsm = CardStateMachine("c1", CardStatus.DRAFT)
    with pytest.raises(InvalidTransition):
        fsm.transition(CardStatus.SENT)


def test_end_to_end_p0_scenario(tmp_path):
    sc = load("scenario_p0_medication.json")
    svc = FamilyDigestService(store=JsonStore(str(tmp_path / "s.json")), channel=MockChannel())
    svc.execute("set_consent", {"elder_id": sc["elder_id"], "scopes": sc["consents"]})
    svc.execute("record_signals", sc)
    built = svc.execute("build_daily_digest", {
        "elder_id": sc["elder_id"], "date": sc["date"],
        "elder_name": sc["elder_name"], "chronicle": sc["chronicle"]})
    assert built["data"]["tier"] == "P0"
    disp = svc.execute("dispatch_digest", {"card_id": built["data"]["card_id"]})
    assert disp["data"]["status"] == "sent"
    body = svc.channel.sent[-1]["body"]
    assert "用药" in body or "安全" in body
