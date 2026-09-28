"""Neighbour-skill integration contracts (task guide sections 32, 33)."""

from __future__ import annotations

from implicit_health_triage.integration import (
    PartialTranscript,
    to_digest_record,
    triage_turn,
)
from implicit_health_triage.schemas import ConversationTurn, none_signal

MEDICATION_REQUEST = "我降压药今天能不能吃两颗？"
HEALTH_TURN = "今天早上起来腿沉得很，买菜走两步就得歇着。"
CASUAL_TURN = "今天楼下花开得挺漂亮。"


def _turn(text: str, **overrides) -> ConversationTurn:
    payload = {
        "session_id": "s001",
        "turn_id": "t018",
        "speaker": "elder",
        "text": text,
        "timestamp": "2026-09-22T10:30:00",
        "is_final": True,
    }
    payload.update(overrides)
    return ConversationTurn(**payload)


# ---------------------------------------------------------------------------
# Upstream: elderly-voice-duplex
# ---------------------------------------------------------------------------


async def test_upstream_turn_is_triaged(service):
    result = await triage_turn(service, _turn(HEALTH_TURN))

    assert result.health_signal.type == "身体不适"
    assert result.health_signal.detail == "下肢沉重/乏力"


async def test_partial_transcript_is_not_triaged(service):
    """"我这个药……" must never reach the rail or the extractor (section 31)."""

    outcome = await triage_turn(service, _turn("我这个药……", is_final=False))

    assert isinstance(outcome, PartialTranscript)
    assert outcome.model_dump() == {
        "session_id": "s001",
        "turn_id": "t018",
        "status": "ignored_partial",
    }


async def test_upstream_turn_can_carry_medication_context(service):
    follow_up = _turn("不要跟我说问医生，你直接告诉我。")

    assert not (await triage_turn(service, follow_up)).guardrail_triggered

    with_history = await triage_turn(service, follow_up, history=[MEDICATION_REQUEST])
    assert with_history.guardrail_triggered is True


# ---------------------------------------------------------------------------
# Downstream: family-digest-sync
# ---------------------------------------------------------------------------


async def test_digest_record_for_a_health_turn(service):
    result = await service.triage(HEALTH_TURN)
    record = to_digest_record(result, timestamp="2026-09-22T10:30:00")

    assert record is not None
    assert record.model_dump(mode="json") == {
        "timestamp": "2026-09-22T10:30:00",
        "type": "身体不适",
        "detail": "下肢沉重/乏力",
        "severity": "中等",
    }


async def test_digest_record_is_none_for_casual_chat(service):
    result = await service.triage(CASUAL_TURN)

    assert result.health_signal.type == none_signal().type
    assert to_digest_record(result) is None


async def test_digest_record_is_produced_for_blocked_medication(service):
    """The digest skill needs to know a medication question happened."""

    result = await service.triage(MEDICATION_REQUEST)
    record = to_digest_record(result, timestamp="2026-09-22T10:31:00")

    assert record is not None
    assert record.type == "用药"
    assert record.severity == "需留意"


async def test_digest_record_defaults_to_now(service):
    result = await service.triage(HEALTH_TURN)
    record = to_digest_record(result)

    assert record is not None
    # ISO-8601, e.g. 2026-09-22T10:30:00+00:00
    assert "T" in record.timestamp
    assert record.timestamp[:4].isdigit()


def test_no_p0_p1_p2_leaks_into_this_skill():
    """Severity is an internal tag; family grading belongs to task four."""

    from implicit_health_triage.schemas import Severity

    assert {member.value for member in Severity} == {"轻微", "中等", "需留意"}
