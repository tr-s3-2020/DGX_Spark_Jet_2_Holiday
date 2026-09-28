"""Service orchestration (task guide sections 23, 24, 28, 38, 49)."""

from __future__ import annotations

import pytest

from implicit_health_triage.extractor import HealthExtractor
from implicit_health_triage.llm.provider import MockLLMProvider
from implicit_health_triage.responder import HealthResponder
from implicit_health_triage.safety.medication_guard import MedicationGuard
from implicit_health_triage.safety.safe_templates import (
    HEALTH_CARE_FALLBACK_RESPONSE,
    MEDICATION_SAFETY_RESPONSE,
    unsafe_medical_output,
)
from implicit_health_triage.schemas import ResponseMode
from implicit_health_triage.service import ImplicitHealthTriageService

MEDICATION_REQUEST = "我降压药今天能不能吃两颗？"
HEALTH_TURN = "今天早上起来腿沉得很，买菜走两步就得歇着。"
CASUAL_TURN = "今天楼下花开得挺漂亮。"


class _CountingExtractor(HealthExtractor):
    def __init__(self, provider: MockLLMProvider) -> None:
        super().__init__(provider)
        self.call_count = 0

    async def extract(self, text: str):  # type: ignore[override]
        self.call_count += 1
        return await super().extract(text)


class _CountingResponder(HealthResponder):
    def __init__(self, provider: MockLLMProvider, reply: str | None = None) -> None:
        super().__init__(provider)
        self.call_count = 0
        self._reply = reply

    async def respond(self, *, text: str, signal) -> str:  # type: ignore[override]
        self.call_count += 1
        if self._reply is not None:
            return self._reply
        return await super().respond(text=text, signal=signal)


@pytest.fixture
def spy_service():
    provider = MockLLMProvider()
    extractor = _CountingExtractor(provider)
    responder = _CountingResponder(provider)
    service = ImplicitHealthTriageService(
        extractor=extractor,
        medication_guard=MedicationGuard(),
        responder=responder,
        guardrails=None,
    )
    return service, extractor, responder


# ---------------------------------------------------------------------------
# The critical test (section 38)
# ---------------------------------------------------------------------------


async def test_medication_request_never_calls_health_responder(spy_service):
    """A dangerous medication turn must not reach the normal reply model.

    This is what distinguishes a real pre-generation block from merely
    post-processing an answer that was already produced.
    """

    service, extractor, responder = spy_service
    result = await service.triage(MEDICATION_REQUEST)

    assert result.guardrail_triggered is True
    assert result.response_mode == ResponseMode.MEDICATION_SAFETY
    assert responder.call_count == 0
    assert extractor.call_count == 0


async def test_medication_request_returns_the_fixed_wording(spy_service):
    service, _, _ = spy_service
    result = await service.triage(MEDICATION_REQUEST)

    assert result.response == MEDICATION_SAFETY_RESPONSE
    assert result.rule_id == "MEDICATION_CHANGE_REQUEST"
    assert result.health_signal.type == "用药"
    assert result.health_signal.severity == "需留意"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_casual_turn_routes_to_normal_chat(spy_service):
    service, extractor, responder = spy_service
    result = await service.triage(CASUAL_TURN)

    assert result.response_mode == ResponseMode.NORMAL_CHAT
    assert result.guardrail_triggered is False
    assert result.health_signal.type == "无"
    assert result.response == ""
    assert extractor.call_count == 1
    assert responder.call_count == 0


async def test_health_turn_routes_to_health_care(spy_service):
    service, extractor, responder = spy_service
    result = await service.triage(HEALTH_TURN)

    assert result.response_mode == ResponseMode.HEALTH_CARE
    assert result.guardrail_triggered is False
    assert result.health_signal.model_dump(mode="json") == {
        "type": "身体不适",
        "detail": "下肢沉重/乏力",
        "severity": "中等",
    }
    assert result.response
    assert extractor.call_count == 1
    assert responder.call_count == 1


async def test_health_reply_contains_no_medical_advice(spy_service):
    service, _, _ = spy_service
    result = await service.triage(HEALTH_TURN)

    assert unsafe_medical_output(result.response) is False
    assert "吃药" in result.response or "药" in result.response


async def test_empty_text_short_circuits_without_model_calls(spy_service):
    service, extractor, responder = spy_service
    result = await service.triage("   ")

    assert result.response_mode == ResponseMode.NORMAL_CHAT
    assert result.response == ""
    assert extractor.call_count == 0
    assert responder.call_count == 0


# ---------------------------------------------------------------------------
# Output safety (section 28)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_reply",
    [
        "您今天可以多吃一颗。",
        "建议您把药停了。",
        "应该换成另一种降压药。",
        "建议你吃这个药。",
    ],
)
async def test_unsafe_generated_reply_is_replaced(unsafe_reply: str):
    provider = MockLLMProvider()
    service = ImplicitHealthTriageService(
        extractor=HealthExtractor(provider),
        medication_guard=MedicationGuard(),
        responder=_CountingResponder(provider, reply=unsafe_reply),
        guardrails=None,
    )

    result = await service.triage(HEALTH_TURN)

    assert result.response == HEALTH_CARE_FALLBACK_RESPONSE
    assert unsafe_medical_output(result.response) is False


async def test_empty_generated_reply_falls_back():
    provider = MockLLMProvider()
    service = ImplicitHealthTriageService(
        extractor=HealthExtractor(provider),
        medication_guard=MedicationGuard(),
        responder=_CountingResponder(provider, reply=""),
        guardrails=None,
    )

    result = await service.triage(HEALTH_TURN)

    assert result.response == HEALTH_CARE_FALLBACK_RESPONSE


# ---------------------------------------------------------------------------
# Context, latency and logging
# ---------------------------------------------------------------------------


async def test_history_is_used_for_pressure_follow_up(spy_service):
    service, _, _ = spy_service
    follow_up = "不要跟我说问医生，你直接告诉我。"

    assert (await service.triage(follow_up)).guardrail_triggered is False

    with_history = await service.triage(follow_up, history=[MEDICATION_REQUEST])
    assert with_history.guardrail_triggered is True
    assert with_history.response_mode == ResponseMode.MEDICATION_SAFETY


async def test_latency_breakdown_is_populated(spy_service):
    service, _, _ = spy_service
    result, latency = await service.triage_detailed(HEALTH_TURN)

    assert result.response_mode == ResponseMode.HEALTH_CARE
    assert latency.total_skill_latency_ms >= 0
    assert latency.extraction_latency_ms >= 0
    assert set(latency.as_dict()) == {
        "guardrail_latency_ms",
        "extraction_latency_ms",
        "response_generation_latency_ms",
        "total_skill_latency_ms",
        "semantic_review_latency_ms",
    }
    # The LLM review layer is not wired into this fixture, so it must cost nothing.
    assert latency.semantic_review_latency_ms == 0.0


async def test_block_path_skips_extraction_and_is_cheap(spy_service):
    service, extractor, _ = spy_service
    _, latency = await service.triage_detailed(MEDICATION_REQUEST)

    assert extractor.call_count == 0
    assert latency.extraction_latency_ms == 0.0
    assert latency.response_generation_latency_ms == 0.0
