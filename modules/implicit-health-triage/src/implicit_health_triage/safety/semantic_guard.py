"""LLM-judged medication safety — the *widening* layer.

Why this exists
---------------
:mod:`medication_rules` is a deterministic rule layer and it is good at what it
does: 103/103 recall and 43/43 precision on the test corpus, zero model calls,
sub-millisecond.  But it matches *phrasings*, and elderly speech escapes
phrasings constantly.  Measured on 15 hand-written dialect/ellipsis utterances
that are all genuine medication questions, the rule layer catches **1**.

These are not vocabulary gaps that more regex can close::

    早上那个能不能免了            「免了」= 省略
    我能不能搁两天                「搁」= 停，方言
    那药我打算撂下了              「撂下」= 停，方言
    我瞅着好利索了，那玩意儿还吃不  三重省略
    是不是可以不用了              无主语、无药名、无剂量

The safety direction
--------------------
**This layer can only ever ADD blocks.**  The caller combines the two as::

    blocked = deterministic_verdict OR semantic_verdict

Never the other way round.  A model that is allowed to declare a sentence *safe*
would be able to narrow a fence that is currently perfect, and it would do so
silently — which is exactly the failure this module exists to prevent.  Keeping
the rule layer as an unconditional floor also means an LLM outage degrades
recall, never safety.

Fail-safe on error
------------------
A timeout, a malformed answer or an unreachable endpoint produces an
**abstention**, not a block and not a pass.  The caller treats abstention as
"no opinion" and falls back to the deterministic verdict.  Blocking on error
would take the whole service down with the model; passing silently would hide
the outage.

Cost control
------------
:func:`should_consult` gates the call.  Running an LLM on every turn would
double the latency of an always-on companion, so the semantic check is only
consulted when the utterance *could* be about the elder's own medication —
a cheap test that never needs a model.  The gate is deliberately wide (recall
over cost) because a missed gate is a missed safety net.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

from ..llm.base import LLMProvider, LLMProviderError
from ..llm.provider import strip_json_fence
from .medication_rules import (
    has_medication_context,
    is_decision_seeking,
    normalize_text,
)

#: Category reported when only the semantic layer flagged the turn. Kept
#: distinct from the rule categories so a log reader can tell which layer fired
#: — and so a semantic block can be audited without re-running the model.
CATEGORY_SEMANTIC_REVIEW = "semantic_review"

SEMANTIC_SYSTEM_PROMPT = """你是老人陪伴系统的用药安全复核组件。

你的唯一任务：判断老人这句话，是不是在**询问或暗示要改变自己的用药**
（加量、减量、停药、漏服补服、换药、掰开吃、过期了还吃不吃、和酒或别的药
一起吃、别人推荐能不能吃、吃了不舒服还要不要继续）。

判定为「是」的情形，**包括各种方言和高度省略的说法**：
- 「早上那个能不能免了」「我能不能搁两天」「那药我打算撂下了」
- 「是不是可以不用了」「那个还接着来不」「我少吃一回碍事不」
- 「我这两天没顾上吃，用不用补上」「剩那点吃完就不买了行不」
- 「这玩意儿吃多了要不要紧」「我寻思停停看行不行」

判定为「否」的情形：
- 单纯陈述已经按医生要求服药，没有询问改变（如「我早上按时吃了」）
- 闲聊、天气、家人、吃饭、睡眠等与用药无关的话题
- 询问药品价格、存放位置、叫什么名字等非用药调整问题
- 单纯报告身体不适，但没有提到药（如「我腰疼」）
- **只问能不能喝酒、能不能吃某种食物，通篇没有提到任何药物、也没有
  指向某个正在服用的东西**（如「我能不能喝两杯酒」）。
  注意与下一条区分：判断依据是「有没有指向一个正在服用的东西」，
  而不是「有没有出现『药』字」。
- 判断依据的正面例子：同样省略了药名的「是不是可以不用了」、
  「剩那点吃完就不买了行不」——**必须判 true**，因为它们指向的是
  「正在吃的那个东西」；而「喝两杯酒」不指向任何在服用的东西。

只输出 JSON，不要 Markdown，不要解释：
{"is_medication_decision": true 或 false, "reason": "不超过 20 字的理由"}

判断不确定时，宁可输出 true —— 这个问题会交给医生或药师确认，
多问一句没有害处，漏掉一句可能有害。"""

#: Words that suggest the elder is talking about taking something. Deliberately
#: narrow: an earlier version also carried 买/开, which made 「今天楼下花开了」
#: trigger a model call — a pure cost with no safety benefit.
_MEDICATION_HINT = re.compile(
    r"药|吃|服|喝|片|丸|粒|颗|剂|针|打|抹|贴|点|喷"
    r"|大夫|医生|医院|处方"
)

#: Deictic references. An elder who says 「那个」/「那玩意儿」 and nothing else
#: is very often talking about a medicine they cannot name — that is the whole
#: premise of this task, and it is invisible to a keyword layer.
_DEICTIC = re.compile(r"那个|这个|那种|这种|那玩意儿|这玩意儿|它|那东西|这东西")


@dataclass(frozen=True)
class SemanticVerdict:
    """Outcome of the LLM review layer."""

    is_risk: bool
    #: True when the model could not be consulted (timeout, bad JSON, no
    #: endpoint). The caller must fall back to the deterministic verdict.
    abstained: bool = False
    reason: str = ""
    latency_ms: float = 0.0
    error: str | None = None


_ABSTAINED = SemanticVerdict(is_risk=False, abstained=True)


def should_consult(text: str) -> bool:
    """Cheap gate deciding whether the LLM is worth a call on this turn.

    The principle: **consult when the rule layer has evidence it cannot
    resolve.**  A rule layer decides on three axes — asking for a decision,
    medication context, risk category — and blocks only when all three agree.
    One or two of them firing alone is exactly the ambiguity an LLM can settle
    and a regex cannot.

    That is why this is not just "does it mention medicine".  Measured on 15
    dialect/ellipsis utterances the rule layer misses, a keyword-only gate
    skipped three of them that the model *would* have caught — including
    「我寻思停停看行不行」, which the model judges correctly when asked.

    The **risk category is deliberately not used** as a signal here.  It is the
    noisiest of the three axes — it contains single-character patterns such as
    ``停`` and ``省``, which is safe inside the three-axis AND but ruins a gate:
    「雨停了」 matches stop_or_skip and would buy a model call for a sentence
    about the weather.  Decision-seeking and medication context are far more
    specific, and between them they cover every long-tail case measured.

    Deliberately wide otherwise.  The remaining risk is a false negative, so the
    gate leans toward calling the model: an extra call costs latency, a missed
    gate costs a safety net that is not there.
    """

    compact = normalize_text(text)
    if not compact:
        return False

    if has_medication_context(compact) or _MEDICATION_HINT.search(compact):
        return True

    # Pointing at *something* unnamed — 「那个」 with no object is the exact
    # shape this layer exists to catch.
    if _DEICTIC.search(compact):
        return True

    return is_decision_seeking(compact)


def parse_semantic_answer(raw: str) -> tuple[bool, str]:
    """Parse the model's JSON. Raises ValueError on anything unusable."""

    payload = json.loads(strip_json_fence(raw))

    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")

    decision = payload.get("is_medication_decision")
    if not isinstance(decision, bool):
        raise ValueError("is_medication_decision must be a boolean")

    reason = payload.get("reason")
    return decision, reason if isinstance(reason, str) else ""


class SemanticMedicationGuard:
    """Ask the LLM whether an utterance is a medication-change question."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def check(
        self,
        text: str,
        recent_context: Sequence[str] = (),
    ) -> SemanticVerdict:
        """Review ``text``. Never raises — failures become abstentions."""

        if not text or not text.strip():
            return _ABSTAINED

        user_prompt = self._build_user_prompt(text, recent_context)
        started = time.perf_counter()

        try:
            raw = await self._provider.generate_json(
                system_prompt=SEMANTIC_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except LLMProviderError as error:
            return SemanticVerdict(
                is_risk=False,
                abstained=True,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=str(error)[:200],
            )
        except Exception as error:  # noqa: BLE001 - an LLM must never break the service
            return SemanticVerdict(
                is_risk=False,
                abstained=True,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(error).__name__}: {error}"[:200],
            )

        latency_ms = (time.perf_counter() - started) * 1000

        try:
            decision, reason = parse_semantic_answer(raw)
        except (ValueError, TypeError) as error:
            return SemanticVerdict(
                is_risk=False,
                abstained=True,
                latency_ms=latency_ms,
                error=f"unparseable answer: {error}",
            )

        return SemanticVerdict(
            is_risk=decision,
            abstained=False,
            reason=reason,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _build_user_prompt(text: str, recent_context: Sequence[str]) -> str:
        lines = []
        if recent_context:
            lines.append("最近几轮老人说过：")
            lines.extend(f"  - {turn}" for turn in recent_context)
            lines.append("")
        lines.append("这一轮老人说：")
        lines.append(f"  {text}")
        return "\n".join(lines)


__all__ = [
    "CATEGORY_SEMANTIC_REVIEW",
    "SEMANTIC_SYSTEM_PROMPT",
    "SemanticMedicationGuard",
    "SemanticVerdict",
    "parse_semantic_answer",
    "should_consult",
]
