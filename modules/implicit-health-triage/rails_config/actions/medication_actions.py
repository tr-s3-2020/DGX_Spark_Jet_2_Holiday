"""NeMo Guardrails custom action for medication safety (task guide section 20).

The guide's snippet re-declares ``MEDICATION_TERMS`` / ``RISK_PATTERNS`` inline.
This implementation deliberately delegates to
:mod:`implicit_health_triage.safety.medication_rules` instead, so the Colang
guardrail and the in-process fast path share one rule set and can never report
different verdicts for the same sentence — the failure mode the guide warns
about when it says the two layers "不冲突".

Registering the action is the only thing this module does; the rule logic and
its rationale live in the safety package.
"""

from __future__ import annotations

from collections.abc import Sequence

from nemoguardrails.actions import action

from implicit_health_triage.safety.medication_rules import (
    assess_medication_risk,
    looks_like_medication_risk,
)


@action(name="MedicationSafetyCheckAction")
async def medication_safety_check(text: str) -> bool:
    """``True`` when the utterance is a dangerous medication decision request."""

    return looks_like_medication_risk(text or "")


@action(name="MedicationRiskCategoryAction")
async def medication_risk_category(
    text: str,
    recent_context: Sequence[str] = (),
) -> str:
    """Expose the fine-grained category for logging and the demo panel."""

    assessment = assess_medication_risk(text or "", recent_context)
    return assessment.category or ""


__all__ = ["medication_risk_category", "medication_safety_check"]
