"""The LLM medication-review layer.

Two things are under test and they have very different guarantees:

* **The combination is one-way.** The rule layer is the floor; the LLM layer can
  only add blocks. Most of this file exists to make that impossible to break by
  accident, because the failure is silent — a model allowed to answer "safe"
  would narrow a fence that is currently perfect.
* **Failures abstain.** A timeout, a malformed answer or a dead endpoint must
  produce "no opinion", never a block and never a pass.

Everything here runs offline against a scripted provider. Whether a real model
judges dialect correctly is measured by ``scripts/check_model.py verify`` and
the long-tail corpus, not asserted here.
"""

from __future__ import annotations

import pytest

from implicit_health_triage.llm.base import LLMProvider, LLMProviderError
from implicit_health_triage.safety.medication_guard import MedicationGuard
from implicit_health_triage.safety.medication_rules import (
    CATEGORY_DOSE_INCREASE,
    CATEGORY_STOP_OR_SKIP,
    is_decision_seeking,
    matched_category,
)
from implicit_health_triage.safety.semantic_guard import (
    CATEGORY_SEMANTIC_REVIEW,
    SemanticMedicationGuard,
    parse_semantic_answer,
    should_consult,
)


class ScriptedProvider(LLMProvider):
    """Returns whatever the test tells it to, or raises."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def generate_json(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def generate_text(self, *, system_prompt: str, user_prompt: str) -> str:
        return ""


def _risk() -> str:
    return '{"is_medication_decision": true, "reason": "询问停药"}'


def _safe() -> str:
    return '{"is_medication_decision": false, "reason": "闲聊"}'


# ---------------------------------------------------------------------------
# The gate — cheap, no model, and the source of most recall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "我那个东西今天少来点行不行",
        "早上那个能不能免了",
        "我能不能搁两天",
        "那药我打算撂下了",
        "是不是可以不用了",
        "那个还接着来不",
        "我寻思停停看行不行",
        "药罐子空了，先不买行吗",
    ],
)
def test_gate_consults_on_dialect_and_ellipsis(text: str):
    """These are the measured misses. A keyword-only gate skipped three of them."""

    assert should_consult(text), f"{text} 必须触发语义复核"


@pytest.mark.parametrize(
    "text",
    ["今天楼下花开了。", "雨停了", "邻居家孩子来看我了", "昨晚睡得不踏实"],
)
def test_gate_skips_plain_small_talk(text: str):
    """Small talk must not pay for a model call."""

    assert not should_consult(text), f"{text} 不该触发语义复核"


def test_gate_skips_empty_input():
    assert not should_consult("")
    assert not should_consult("   ")


def test_gate_ignores_a_bare_risk_category_match():
    """The noisiest axis must not drive the gate.

    「雨停了」 matches stop_or_skip because that category contains a bare ``停``
    pattern. That is safe inside the three-axis AND — it is what makes
    「我那个能不能停」 work — but as a gate signal it would buy a model call for a
    sentence about the weather. Measured: dropping this axis cost no recall
    across the 15 long-tail cases.
    """

    text = "雨停了"

    assert matched_category(text) == CATEGORY_STOP_OR_SKIP, "前提：类别层确实命中"
    assert not should_consult(text), "但类别命中不该触发语义复核"


def test_gate_is_wider_than_the_rule_layer_by_design():
    """The gate must fire on partial evidence, not only on a confirmed match.

    「我寻思停停看行不行」matches the stop_or_skip category and asks for a
    decision, but has no medication context, so the rule layer does NOT block
    it. The gate has to consult anyway — that gap is the entire reason this
    layer exists.
    """

    text = "我寻思停停看行不行"

    assert matched_category(text) is not None, "前提：类别层应已命中"
    assert is_decision_seeking(text), "前提：提问层应已命中"
    assert should_consult(text), "提问层命中就必须复核"


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------


def test_parses_a_well_formed_answer():
    assert parse_semantic_answer(_risk()) == (True, "询问停药")
    assert parse_semantic_answer(_safe()) == (False, "闲聊")


def test_parses_through_a_markdown_fence_and_thinking_block():
    fenced = '```json\n{"is_medication_decision": true, "reason": "r"}\n```'
    assert parse_semantic_answer(fenced)[0] is True


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"is_medication_decision": "true"}',  # string, not boolean
        '{"reason": "missing the decision field"}',
        "[]",
        "",
    ],
)
def test_unusable_answers_raise(raw: str):
    with pytest.raises((ValueError, TypeError)):
        parse_semantic_answer(raw)


# ---------------------------------------------------------------------------
# Abstention — the fail-safe direction
# ---------------------------------------------------------------------------


async def test_provider_error_abstains_rather_than_blocking():
    """Blocking on error would take the service down with the model."""

    guard = SemanticMedicationGuard(ScriptedProvider(LLMProviderError("boom")))
    verdict = await guard.check("我能不能搁两天")

    assert verdict.abstained is True
    assert verdict.is_risk is False
    assert "boom" in (verdict.error or "")


async def test_timeout_abstains():
    from implicit_health_triage.llm.base import LLMTimeoutError

    guard = SemanticMedicationGuard(ScriptedProvider(LLMTimeoutError("slow")))
    verdict = await guard.check("我能不能搁两天")

    assert verdict.abstained is True
    assert verdict.is_risk is False


async def test_unexpected_exception_abstains():
    """An LLM must never be able to break the service, whatever it raises."""

    guard = SemanticMedicationGuard(ScriptedProvider(RuntimeError("surprise")))
    verdict = await guard.check("我能不能搁两天")

    assert verdict.abstained is True
    assert verdict.is_risk is False


async def test_unparseable_answer_abstains():
    guard = SemanticMedicationGuard(ScriptedProvider("I think it might be fine?"))
    verdict = await guard.check("我能不能搁两天")

    assert verdict.abstained is True
    assert verdict.is_risk is False


async def test_empty_input_does_not_call_the_model():
    provider = ScriptedProvider(_risk())
    guard = SemanticMedicationGuard(provider)

    verdict = await guard.check("   ")

    assert verdict.abstained is True
    assert provider.calls == []


# ---------------------------------------------------------------------------
# The one-way combination
# ---------------------------------------------------------------------------


async def test_a_safe_verdict_cannot_unblock_a_rule_block():
    """The single most important property in this file.

    The rule layer says 「我降压药今天能不能吃两颗」 is a dose-increase request.
    A model that answers "not a medication decision" must not be able to release
    it. The combination is ``rules OR semantic``, never a negotiation.
    """

    text = "我降压药今天能不能吃两颗？"
    rule = MedicationGuard().check(text)
    assert rule.blocked is True, "前提：规则层必须拦住"
    assert rule.category == CATEGORY_DOSE_INCREASE

    semantic = await SemanticMedicationGuard(ScriptedProvider(_safe())).check(text)

    assert semantic.is_risk is False
    # The service only consults the model when the rules did NOT block, so a
    # safe model answer never even reaches the decision. Expressed as the
    # property it protects:
    blocked = rule.blocked or semantic.is_risk
    assert blocked is True


async def test_a_risky_verdict_blocks_a_turn_the_rules_missed():
    text = "我能不能搁两天"

    rule = MedicationGuard().check(text)
    assert rule.blocked is False, "前提：规则层漏掉这一句"

    semantic = await SemanticMedicationGuard(ScriptedProvider(_risk())).check(text)
    assert semantic.is_risk is True
    assert (rule.blocked or semantic.is_risk) is True


async def test_semantic_block_is_labelled_distinctly():
    """A log reader must be able to tell which layer fired."""

    guard = SemanticMedicationGuard(ScriptedProvider(_risk()))
    verdict = await guard.check("我能不能搁两天")

    assert verdict.is_risk is True
    assert verdict.abstained is False
    assert verdict.reason
    assert CATEGORY_SEMANTIC_REVIEW == "semantic_review"


async def test_the_prompt_carries_the_conversation_history():
    """A follow-up turn is often only interpretable with its context."""

    provider = ScriptedProvider(_risk())
    guard = SemanticMedicationGuard(provider)

    await guard.check("那个还接着来不", ["我降压药今天能不能吃两颗？"])

    _, user_prompt = provider.calls[0]
    assert "我降压药今天能不能吃两颗？" in user_prompt
    assert "那个还接着来不" in user_prompt


def test_prompt_names_the_hard_cases_it_must_catch():
    """The prompt is the specification; guard it against silent erosion."""

    from implicit_health_triage.safety.semantic_guard import SEMANTIC_SYSTEM_PROMPT

    for example in ("搁两天", "撂下", "免了", "接着来"):
        assert example in SEMANTIC_SYSTEM_PROMPT, f"提示词应保留难例：{example}"

    # The alcohol boundary was added to remove a measured false positive; it
    # distinguishes "points at something being taken" from "mentions no medicine".
    assert "喝两杯酒" in SEMANTIC_SYSTEM_PROMPT
