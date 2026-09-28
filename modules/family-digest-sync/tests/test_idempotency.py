"""幂等：上游重放、重试、重复生成日报，都只能产生一份。"""

import json
import os
from datetime import datetime

import pytest

from family_digest import FamilyDigestService
from family_digest.adapters import MockChannel
from family_digest.store import JsonStore

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples")


def load(name):
    with open(os.path.join(EXAMPLES, name), "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def svc(tmp_path):
    return FamilyDigestService(store=JsonStore(str(tmp_path / "s.json")),
                               channel=MockChannel())


def test_replay_same_event_is_skipped(svc):
    sc = load("scenario_p1_trend.json")
    first = svc.execute("record_signals", sc)
    second = svc.execute("record_signals", sc)
    assert first["data"]["accepted"] == len(sc["health_signals"])
    assert second["data"]["accepted"] == 0
    assert len(second["data"]["skipped"]) == len(sc["health_signals"])


def test_build_twice_is_idempotent(svc):
    sc = load("scenario_p1_trend.json")
    svc.execute("set_consent", {"elder_id": sc["elder_id"], "scopes": sc["consents"]})
    svc.execute("record_signals", sc)
    req = {"elder_id": sc["elder_id"], "date": sc["date"], "elder_name": sc["elder_name"],
           "chronicle": sc["chronicle"]}
    a = svc.execute("build_daily_digest", req)
    b = svc.execute("build_daily_digest", req)
    assert a["data"]["card_id"] == b["data"]["card_id"]
    assert b["meta"]["idempotent"] is True


def test_dispatch_twice_keeps_one_card(svc):
    sc = load("scenario_p0_medication.json")
    svc.execute("set_consent", {"elder_id": sc["elder_id"], "scopes": sc["consents"]})
    svc.execute("record_signals", sc)
    card = svc.execute("build_daily_digest", {
        "elder_id": sc["elder_id"], "date": sc["date"],
        "elder_name": sc["elder_name"]})["data"]
    svc.execute("dispatch_digest", {"card_id": card["card_id"]})
    svc.execute("dispatch_digest", {"card_id": card["card_id"]})
    got = svc.execute("get_digest", {"card_id": card["card_id"]})
    assert got["data"]["status"] == "sent"
