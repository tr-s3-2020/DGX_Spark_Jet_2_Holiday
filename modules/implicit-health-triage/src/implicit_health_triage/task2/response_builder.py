from .safety.templates import MEDICATION_SAFETY_RESPONSE
from .schemas import (
    HealthSignal,
    NormalizedTurn,
    ResponseOutput,
    SafetyDecision,
    SafetyOutput,
    TriageMetadata,
    TriageOutput,
)

DETAILS = {
    "MEDICATION_DOSE_INCREASE": "询问增加用药剂量",
    "MEDICATION_DOSE_DECREASE": "询问减少用药剂量",
    "MEDICATION_STOP_OR_SKIP": "询问停药或跳过服药",
    "MEDICATION_REPEAT_DOSE": "询问重复服药或补服",
    "MEDICATION_COMBINATION": "询问混合服药",
}
CARE = {
    "symptom": "听起来您身体有些不舒服，您愿意再说说现在的感受吗？",
    "sleep": "听起来您最近睡得不太好，您愿意再说说吗？",
    "pain": "听起来您有疼痛不适，您愿意再说说现在的感受吗？",
    "mobility": "听起来您走路有些不方便，您愿意再说说吗？",
    "appetite": "听起来您胃口不太好，您愿意再说说吗？",
    "medication": "听到您提到了服药情况，谢谢您告诉我。",
    "other": "听到您提到了身体情况，您愿意再说说吗？",
}


class ResponseBuilder:
    def build(
        self,
        turn: NormalizedTurn,
        signal: HealthSignal,
        decision: SafetyDecision,
        metadata: TriageMetadata,
    ) -> TriageOutput:
        if decision.blocked:
            detail = DETAILS.get(decision.rule_id, "涉及具体用药调整询问")
            if decision.rule_id == "MEDICATION_DOSE_INCREASE" and "降压药" in turn.text:
                detail = "询问增加降压药剂量"
            signal = HealthSignal(type="medication", detail=detail, severity="high")
            response = ResponseOutput(mode="medication_safety", text=MEDICATION_SAFETY_RESPONSE)
        elif signal.type == "none":
            response = ResponseOutput(mode="passthrough", text=None)
        else:
            text = CARE[signal.type]
            if signal.detail == "下肢沉重/乏力":
                text = "听起来您今天提到下肢沉重、没什么力气，确实有些不舒服。您平时该吃的药都按原来的安排吃了吗？"
            # Never echo generated detail into the user-facing reply.
            response = ResponseOutput(mode="health_care", text=text)
        return TriageOutput(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            health_signal=signal,
            safety=SafetyOutput(blocked=decision.blocked, rule_id=decision.rule_id),
            response=response,
            metadata=metadata,
        )
