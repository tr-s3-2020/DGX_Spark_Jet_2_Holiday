"""Medication-safety layer: deterministic rules, guard and fixed responses."""

from .medication_guard import GuardDecision, MedicationGuard
from .medication_rules import (
    RULE_MEDICATION_CHANGE_REQUEST,
    MedicationRiskAssessment,
    assess_medication_risk,
    has_medication_context,
    looks_like_medication_risk,
)
from .safe_templates import (
    HEALTH_CARE_FALLBACK_RESPONSE,
    MEDICATION_SAFETY_RESPONSE,
    UNSAFE_OUTPUT_PATTERNS,
    safe_health_care_response,
    unsafe_medical_output,
)

__all__ = [
    "HEALTH_CARE_FALLBACK_RESPONSE",
    "MEDICATION_SAFETY_RESPONSE",
    "RULE_MEDICATION_CHANGE_REQUEST",
    "UNSAFE_OUTPUT_PATTERNS",
    "GuardDecision",
    "MedicationGuard",
    "MedicationRiskAssessment",
    "assess_medication_risk",
    "has_medication_context",
    "looks_like_medication_risk",
    "safe_health_care_response",
    "unsafe_medical_output",
]
