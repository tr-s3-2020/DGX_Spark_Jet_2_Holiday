"""Health extractor: prompt contract, JSON validation, retry and fallback.

Scope note — with the default ``LLM_PROVIDER=mock`` these tests verify the
*plumbing* (prompt assembly, fence stripping, schema validation, one-shot retry,
fallback) and the documented few-shots.  They do not measure a language model's
accuracy; run ``scripts/run_eval.py`` against a real endpoint for that.
"""

from __future__ import annotations

import json

import pytest

from implicit_health_triage.extractor import (
    EXTRACTION_SYSTEM_PROMPT,
    FEW_SHOT,
    HealthExtractor,
)
from implicit_health_triage.llm.base import LLMProviderError, LLMTimeoutError
from implicit_health_triage.llm.provider import (
    MockLLMProvider,
    strip_json_fence,
    strip_reasoning,
)
from implicit_health_triage.schemas import HealthType
from tests.support import load_cases

EXTRACTION_CASES = load_cases("extraction_cases.jsonl")

IMAGE_FEW_SHOT_TEXT = "今天早上起来腿沉得很，买菜走两步就得歇着。"
IMAGE_FEW_SHOT_EXPECTED = {
    "type": "身体不适",
    "detail": "下肢沉重/乏力",
    "severity": "中等",
}


class _ScriptedProvider(MockLLMProvider):
    """Returns queued raw answers, then falls back to the mock behaviour."""

    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self._answers = list(answers)
        self.json_calls = 0

    async def generate_json(self, *, system_prompt: str, user_prompt: str) -> str:
        self.json_calls += 1
        if self._answers:
            return self._answers.pop(0)
        return await super().generate_json(
            system_prompt=system_prompt, user_prompt=user_prompt
        )


class _RaisingProvider(MockLLMProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def generate_json(self, *, system_prompt: str, user_prompt: str) -> str:
        raise self._error


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


def test_prompt_contains_the_image_few_shot():
    assert IMAGE_FEW_SHOT_TEXT in FEW_SHOT
    assert '"detail":"下肢沉重/乏力"' in FEW_SHOT


def test_system_prompt_forbids_diagnosis_and_advice():
    for phrase in ("疾病诊断", "药物剂量建议", "只输出 JSON"):
        assert phrase in EXTRACTION_SYSTEM_PROMPT


def test_system_prompt_lists_all_health_types():
    for health_type in HealthType:
        assert health_type.value in EXTRACTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Data-driven extraction suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", EXTRACTION_CASES, ids=lambda c: c["text"][:24])
async def test_extraction_cases(case: dict, extractor: HealthExtractor):
    signal = await extractor.extract(case["text"])

    assert signal.type == case["expected_type"]
    if "expected_detail" in case:
        assert signal.detail == case["expected_detail"]
    if "expected_severity" in case:
        assert signal.severity == case["expected_severity"]


async def test_image_few_shot_is_reproduced(extractor: HealthExtractor):
    signal = await extractor.extract(IMAGE_FEW_SHOT_TEXT)

    assert signal.model_dump(mode="json") == IMAGE_FEW_SHOT_EXPECTED


async def test_no_health_content_yields_none(extractor: HealthExtractor):
    signal = await extractor.extract("今天隔壁老李来找我下棋了。")

    assert signal.type == "无"
    assert signal.detail == ""


# ---------------------------------------------------------------------------
# Robustness (section 2.12 / 48.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"type":"身体不适","detail":"下肢沉重/乏力","severity":"中等"}',
        '```json\n{"type":"身体不适","detail":"下肢沉重/乏力","severity":"中等"}\n```',
        '当然，以下是 JSON：\n{"type":"身体不适","detail":"下肢沉重/乏力","severity":"中等"}\n希望有帮助。',
        '{"type":"身体不适","detail":"下肢沉重/乏力","severity":"中等","confidence":0.9}',
        '{"type":"SYMPTOM","detail":"下肢沉重/乏力","severity":"MODERATE"}',
    ],
)
def test_parser_survives_real_world_model_output(raw: str):
    signal = HealthExtractor.parse(raw)

    assert signal is not None
    assert signal.type == "身体不适"
    assert signal.severity == "中等"


@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", "[]", "{ broken", "null"],
)
def test_parser_rejects_garbage(raw: str):
    assert HealthExtractor.parse(raw) is None


async def test_invalid_json_is_retried_exactly_once():
    provider = _ScriptedProvider(["totally not json"])
    outcome = await HealthExtractor(provider, max_retries=1).extract_outcome(
        IMAGE_FEW_SHOT_TEXT
    )

    # First call returns garbage, retry falls through to the mock's real answer.
    assert provider.json_calls == 2
    assert outcome.attempts == 2
    assert outcome.json_valid is True
    assert outcome.signal.type == "身体不适"


async def test_two_failures_fall_back_to_none():
    provider = _ScriptedProvider(["nope", "still nope"])
    outcome = await HealthExtractor(provider, max_retries=1).extract_outcome(
        IMAGE_FEW_SHOT_TEXT
    )

    assert provider.json_calls == 2
    assert outcome.json_valid is False
    assert outcome.signal.type == "无"
    assert outcome.signal.detail == ""
    assert outcome.error


async def test_timeout_falls_back_to_none():
    provider = _RaisingProvider(LLMTimeoutError("too slow"))
    outcome = await HealthExtractor(provider).extract_outcome(IMAGE_FEW_SHOT_TEXT)

    assert outcome.signal.type == "无"
    assert "timeout" in (outcome.error or "")


async def test_backend_error_falls_back_to_none():
    provider = _RaisingProvider(LLMProviderError("connection refused"))
    outcome = await HealthExtractor(provider).extract_outcome(IMAGE_FEW_SHOT_TEXT)

    assert outcome.signal.type == "无"
    assert "backend" in (outcome.error or "")


async def test_empty_text_skips_the_model(extractor: HealthExtractor):
    signal = await extractor.extract("   ")

    assert signal.type == "无"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a":1}', '{"a":1}'),
        ('```json\n{"a":1}\n```', '{"a":1}'),
        ("前缀 {\"a\":1} 后缀", '{"a":1}'),
    ],
)
def test_strip_json_fence(raw: str, expected: str):
    assert strip_json_fence(raw) == expected


def test_unknown_type_is_coerced_to_other():
    signal = HealthExtractor.parse('{"type":"stroke","detail":"x","severity":"需留意"}')

    assert signal is not None
    assert signal.type == "其他体征"


def test_severity_synonyms_are_normalised():
    signal = HealthExtractor.parse('{"type":"疼痛","detail":"膝","severity":"severe"}')

    assert signal is not None
    assert signal.severity == "需留意"


def test_none_type_forces_empty_detail():
    signal = HealthExtractor.parse('{"type":"无","detail":"something","severity":"轻微"}')

    assert signal is not None
    assert signal.detail == ""


def test_dump_is_json_serialisable(extractor: HealthExtractor):
    signal = extractor.parse(json.dumps(IMAGE_FEW_SHOT_EXPECTED))

    assert json.loads(json.dumps(signal.model_dump(mode="json"))) == IMAGE_FEW_SHOT_EXPECTED


# ---------------------------------------------------------------------------
# Reasoning ("thinking") models — Qwen3 and friends
# ---------------------------------------------------------------------------
#
# A thinking model emits its reasoning before the answer, and the reasoning
# text frequently contains braces. Locating the JSON object before stripping
# the reasoning would latch onto the wrong span, so the parser removes the
# reasoning block first.
#
# NOTE — why the markers are built with ``chr(0x3C)`` instead of being written
# as literals: a literal tag-shaped string in a source file does not survive
# every write path intact (it is silently treated as markup and dropped). The
# resulting test then passes for the wrong reason, because a marker that lost
# its opening bracket no longer looks like a marker to the parser. Building the
# string from its code point keeps the test honest.

_LT = chr(0x3C)  # "<"
_GT = chr(0x3E)  # ">"
_THINKING_OPEN = f"{_LT}think{_GT}"
_THINKING_CLOSE = f"{_LT}/think{_GT}"
_THINKING_END = f"{_LT}｜end▁of▁thinking｜{_GT}"
_ANSWER = '{"type":"身体不适","detail":"下肢沉重/乏力","severity":"中等"}'


def test_markers_are_real_brackets():
    """Guard the guard: a mangled marker would make every case below vacuous."""

    assert _THINKING_OPEN == _LT + "think" + _GT
    assert len(_THINKING_OPEN) == 7
    assert _THINKING_OPEN[0] == _LT
    assert _THINKING_END[0] == _LT
    assert _THINKING_CLOSE == _LT + "/think" + _GT


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        (_ANSWER, "plain json"),
        (f"{_THINKING_OPEN}用户说腿沉。{_THINKING_END}{_ANSWER}", "thinking then answer"),
        (f"{_THINKING_OPEN}{{分析}}腿沉归 symptom。{_THINKING_END}{_ANSWER}", "braces in thinking"),
        (
            f"{_THINKING_OPEN}{{\"step\":1}}{{\"step\":2}}{_THINKING_END}{_ANSWER}",
            "json-like thinking",
        ),
        (f"{_THINKING_OPEN}推理{_THINKING_CLOSE}{_ANSWER}", "plain close tag"),
        (f"{_THINKING_OPEN}推理{_LT}/thinking{_GT}{_ANSWER}", "thinking close tag"),
        (f"{_THINKING_OPEN}思考{_THINKING_END}当然，以下是 JSON：\n{_ANSWER}", "prose after thinking"),
        (f"{_THINKING_OPEN}{{推理}}{_THINKING_END}```json\n{_ANSWER}\n```", "fence after thinking"),
        (f"{_THINKING_END}{_ANSWER}", "stray end marker"),
    ],
)
def test_thinking_output_is_stripped_before_parsing(raw: str, label: str):
    signal = HealthExtractor.parse(raw)

    assert signal is not None, label
    assert signal.model_dump(mode="json") == IMAGE_FEW_SHOT_EXPECTED, label


@pytest.mark.parametrize(
    "raw",
    [
        f"{_THINKING_OPEN}我不确定该怎么分类。{_THINKING_END}",
        f"{_THINKING_OPEN}还在思考中",
        "",
    ],
)
def test_thinking_without_an_answer_falls_back_to_retry(raw: str):
    """No usable JSON means the caller retries — never a half-parsed signal."""

    assert HealthExtractor.parse(raw) is None


def test_reasoning_never_leaks_into_detail():
    """The reasoning text must not be mistaken for the extracted detail."""

    raw = f"{_THINKING_OPEN}用户说腿沉得很，我判断是下肢沉重。{_THINKING_END}{_ANSWER}"
    signal = HealthExtractor.parse(raw)

    assert signal is not None
    assert signal.detail == "下肢沉重/乏力"
    assert "判断" not in signal.detail


def test_strip_reasoning_is_idempotent_on_clean_input():
    assert strip_reasoning(_ANSWER) == _ANSWER


# ---------------------------------------------------------------------------
# Asking the endpoint not to think in the first place
# ---------------------------------------------------------------------------


def _provider(disable_thinking: bool):
    from implicit_health_triage.llm.provider import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8001/v1",
        model="qwen3-8b",
        disable_thinking=disable_thinking,
    )


def test_thinking_is_not_disabled_by_default():
    """Endpoints that do not understand the extension must not receive it."""

    payload = _provider(disable_thinking=False).build_payload("sys", "user", 0.0)

    assert "chat_template_kwargs" not in payload
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]


def test_disable_thinking_sends_the_chat_template_kwarg():
    payload = _provider(disable_thinking=True).build_payload("sys", "user", 0.0)

    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_empty_system_prompt_is_omitted():
    payload = _provider(disable_thinking=False).build_payload("", "user", 0.0)

    assert payload["messages"] == [{"role": "user", "content": "user"}]


async def test_aclose_is_safe_to_call_twice():
    provider = _provider(disable_thinking=False)

    await provider.aclose()
    await provider.aclose()
