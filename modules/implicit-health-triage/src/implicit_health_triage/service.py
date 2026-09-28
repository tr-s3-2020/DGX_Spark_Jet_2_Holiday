"""Skill orchestration (task guide sections 23, 24, 40, 49).

Order is not negotiable — the guide is explicit that the safety check runs
first::

    1. Guardrail
    2. if BLOCK -> return immediately, never touching the normal reply model
    3. if PASS  -> extract the health signal
    4. decide the response mode from the extracted type

Priority (section 49)::

    Medication Safety  >  Health Extraction  >  Normal Chat
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from .extractor import HealthExtractor
from .guardrails_runtime import GuardrailsRuntime
from .logging_utils import LatencyBreakdown, TurnLogRecord, log_turn
from .responder import HealthResponder
from .safety.medication_guard import MedicationGuard
from .safety.medication_rules import RULE_MEDICATION_CHANGE_REQUEST
from .safety.safe_templates import MEDICATION_SAFETY_RESPONSE, safe_health_care_response
from .safety.semantic_guard import (
    CATEGORY_SEMANTIC_REVIEW,
    SemanticMedicationGuard,
    should_consult,
)
from .schemas import (
    HealthType,
    ResponseMode,
    TriageResult,
    medication_guard_signal,
    none_signal,
)


class ImplicitHealthTriageService:
    """Turn one final transcript into a :class:`TriageResult`."""

    def __init__(
        self,
        extractor: HealthExtractor,
        medication_guard: MedicationGuard,
        responder: HealthResponder,
        guardrails: GuardrailsRuntime | None = None,
        semantic_guard: SemanticMedicationGuard | None = None,
    ) -> None:
        self.extractor = extractor
        self.medication_guard = medication_guard
        self.responder = responder
        self.guardrails = guardrails
        #: Optional LLM review layer. It can only ever ADD a block — see
        #: :mod:`implicit_health_triage.safety.semantic_guard` for why the
        #: direction matters more than the model.
        self.semantic_guard = semantic_guard

    async def triage(
        self,
        text: str,
        history: Sequence[str] = (),
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> TriageResult:
        result, _ = await self.triage_detailed(
            text, history=history, session_id=session_id, turn_id=turn_id
        )
        return result

    async def triage_detailed(
        self,
        text: str,
        history: Sequence[str] = (),
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> tuple[TriageResult, LatencyBreakdown]:
        """Same as :meth:`triage` but also returns the section 40 timings."""

        text = (text or "").strip()
        latency = LatencyBreakdown()
        started = time.perf_counter()

        # Empty input: normal chat, no model call at all (section 48.1).
        if not text:
            result = TriageResult(
                text=text,
                health_signal=none_signal(),
                guardrail_triggered=False,
                response_mode=ResponseMode.NORMAL_CHAT,
                response="",
            )
            return self._finish(result, latency, started, text, session_id, turn_id)

        # ---- 1. medication safety, before anything generative -----------
        guard_started = time.perf_counter()
        decision = self.medication_guard.check(text, history)
        blocked = decision.blocked

        if self.guardrails is not None and self.guardrails.enabled:
            verdict = await self.guardrails.check_input(text)
            # Both layers share one rule set, so disagreement means a bug or a
            # rail that failed to run. Block on either answer (fail safe).
            blocked = blocked or verdict.blocked

        # ---- 1b. LLM review — can only widen the fence -------------------
        #
        # The rule layer matches phrasings; elderly speech escapes phrasings.
        # Measured on 15 dialect/ellipsis utterances that are all genuine
        # medication questions, the rules catch 1 and this layer takes it to 15
        # with no false positives. The combination is deliberately one-way:
        #
        #     blocked = rules OR colang OR semantic
        #
        # A model allowed to answer "this one is safe" could narrow a fence that
        # is currently perfect, and it would do so silently. On error the layer
        # abstains, so an LLM outage costs recall, never safety.
        risk_category = decision.category
        if not blocked and self.semantic_guard is not None and should_consult(text):
            review = await self.semantic_guard.check(text, history)
            latency.semantic_review_latency_ms = review.latency_ms
            if review.is_risk:
                blocked = True
                risk_category = CATEGORY_SEMANTIC_REVIEW

        latency.guardrail_latency_ms = (time.perf_counter() - guard_started) * 1000

        if blocked:
            result = TriageResult(
                text=text,
                health_signal=medication_guard_signal(),
                guardrail_triggered=True,
                response_mode=ResponseMode.MEDICATION_SAFETY,
                response=MEDICATION_SAFETY_RESPONSE,
                rule_id=decision.rule_id or RULE_MEDICATION_CHANGE_REQUEST,
            )
            return self._finish(
                result,
                latency,
                started,
                text,
                session_id,
                turn_id,
                risk_category=risk_category,
            )

        # ---- 2. health signal extraction --------------------------------
        extract_started = time.perf_counter()
        signal = await self.extractor.extract(text)
        latency.extraction_latency_ms = (time.perf_counter() - extract_started) * 1000

        # ---- 3a. nothing health related -> normal chat ------------------
        if signal.type == HealthType.NONE:
            result = TriageResult(
                text=text,
                health_signal=signal,
                guardrail_triggered=False,
                response_mode=ResponseMode.NORMAL_CHAT,
                response="",
            )
            return self._finish(result, latency, started, text, session_id, turn_id)

        # ---- 3b. health care: concern + confirm medication, no advice ---
        response_started = time.perf_counter()
        raw_reply = await self.responder.respond(text=text, signal=signal)
        # Section 28: never air an unsafe generated sentence; fall back instead.
        response = safe_health_care_response(raw_reply)
        latency.response_generation_latency_ms = (
            time.perf_counter() - response_started
        ) * 1000

        result = TriageResult(
            text=text,
            health_signal=signal,
            guardrail_triggered=False,
            response_mode=ResponseMode.HEALTH_CARE,
            response=response,
        )
        return self._finish(result, latency, started, text, session_id, turn_id)

    @staticmethod
    def _finish(
        result: TriageResult,
        latency: LatencyBreakdown,
        started: float,
        text: str,
        session_id: str | None,
        turn_id: str | None,
        risk_category: str | None = None,
    ) -> tuple[TriageResult, LatencyBreakdown]:
        latency.total_skill_latency_ms = (time.perf_counter() - started) * 1000
        log_turn(
            TurnLogRecord(
                session_id=session_id,
                turn_id=turn_id,
                input_length=len(text),
                guardrail_triggered=result.guardrail_triggered,
                rule_id=result.rule_id,
                risk_category=risk_category,
                health_type=str(result.health_signal.type),
                severity=str(result.health_signal.severity),
                response_mode=str(result.response_mode),
                latency_ms=latency.total_skill_latency_ms,
                latency=latency.as_dict(),
                input_text=text or None,
            )
        )
        return result, latency


__all__ = ["ImplicitHealthTriageService"]
