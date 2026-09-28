from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TriageInput(Contract):
    session_id: str = Field(min_length=1, max_length=256, pattern=r"\S")
    turn_id: str = Field(min_length=1, max_length=256, pattern=r"\S")
    text: str = Field(max_length=16000)
    is_final: bool


class NormalizedTurn(Contract):
    session_id: str
    turn_id: str
    text: str


HealthType = Literal[
    "none", "symptom", "sleep", "pain", "medication", "mobility", "appetite", "other"
]
Severity = Literal["low", "moderate", "high"]


class HealthSignal(Contract):
    type: HealthType
    detail: str = Field(max_length=256)
    severity: Severity

    @model_validator(mode="after")
    def consistent_signal(self):
        if self.type == "none" and (self.detail != "" or self.severity != "low"):
            raise ValueError("none requires empty detail and low severity")
        if self.type != "none" and not self.detail.strip():
            raise ValueError("health signals require detail")
        return self


def none_signal() -> HealthSignal:
    return HealthSignal(type="none", detail="", severity="low")


class SafetyDecision(Contract):
    blocked: bool = False
    rule_id: str | None = None
    matched_text: str | None = None


class SafetyOutput(Contract):
    blocked: bool
    rule_id: str | None = None


class ResponseOutput(Contract):
    mode: Literal["passthrough", "health_care", "medication_safety"]
    text: str | None


class TriageMetadata(Contract):
    semantic_backend: Literal["none", "mock", "qwen"]
    qwen_called: bool
    latency_ms: float = Field(ge=0)


class TriageOutput(Contract):
    session_id: str
    turn_id: str
    health_signal: HealthSignal
    safety: SafetyOutput
    response: ResponseOutput
    metadata: TriageMetadata


class IgnoredOutput(Contract):
    status: Literal["ignored"] = "ignored"
    reason: Literal["partial_or_empty"] = "partial_or_empty"
