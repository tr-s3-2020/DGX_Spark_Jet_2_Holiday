"""In-process medication safety fast path (task guide section 15, 24, 40).

This is the deterministic, zero-latency check the service uses for the
"BLOCK" short path.  The NeMo Guardrails custom action calls the very same
rule layer, so the two layers cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .medication_rules import assess_medication_risk


@dataclass(frozen=True)
class GuardDecision:
    """Outcome of the deterministic medication check."""

    blocked: bool
    rule_id: str | None = None
    category: str | None = None
    matched_terms: tuple[str, ...] = ()
    reason: str = ""


class MedicationGuard:
    """Decide whether a turn must be intercepted before normal generation."""

    def check(
        self,
        text: str,
        recent_context: Sequence[str] = (),
    ) -> GuardDecision:
        assessment = assess_medication_risk(text, recent_context)

        if assessment.is_risk:
            return GuardDecision(
                blocked=True,
                rule_id=assessment.rule_id,
                category=assessment.category,
                matched_terms=assessment.matched_terms,
                reason=assessment.reason,
            )

        return GuardDecision(blocked=False)


__all__ = ["GuardDecision", "MedicationGuard"]
