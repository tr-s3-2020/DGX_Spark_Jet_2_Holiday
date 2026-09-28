import re

from ...safety.medication_rules import assess_medication_risk, normalize_text
from ..schemas import SafetyDecision

RULE_IDS = {
    "dose_increase": "MEDICATION_DOSE_INCREASE",
    "dose_decrease": "MEDICATION_DOSE_DECREASE",
    "stop_or_skip": "MEDICATION_STOP_OR_SKIP",
    "repeat_or_missed": "MEDICATION_REPEAT_DOSE",
    "combination": "MEDICATION_COMBINATION",
}


class MedicationSafetyPolicy:
    """Reuse deterministic rules without passing conversation history."""

    def evaluate(self, text: str) -> SafetyDecision:
        result = assess_medication_risk(text)
        if result.is_risk:
            return SafetyDecision(
                blocked=True,
                rule_id=RULE_IDS.get(result.category, "MEDICATION_CHANGE_REQUEST"),
                matched_text=text,
            )
        # Medication-elliptical examples explicitly required by the specification.
        compact = normalize_text(text)
        supplements = [
            (
                r"^(?:我)?(?:今天|今晚|这次)?不吃(?:了)?(?:行不行|可以吗|行吗)[？?。]*$",
                "MEDICATION_STOP_OR_SKIP",
            ),
            (r"忘了.*吃没吃.*(?:再吃|补吃)", "MEDICATION_REPEAT_DOSE"),
        ]
        for pattern, rule in supplements:
            match = re.search(pattern, compact)
            if match:
                return SafetyDecision(blocked=True, rule_id=rule, matched_text=match.group())
        return SafetyDecision()
