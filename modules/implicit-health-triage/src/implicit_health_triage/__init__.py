"""implicit-health-triage: 隐式健康探针与用药安全技能.

Task 2 of the NVIDIA DGX Spark elderly-companion hackathon project.

Public entry point::

    from implicit_health_triage import implicit_health_triage

    result = await implicit_health_triage("我降压药今天能不能吃两颗？")

Heavy modules (LLM providers, NeMo Guardrails runtime, FastAPI app) are imported
lazily so that the pure deterministic safety layer stays importable on its own —
useful for CI, for the eval script, and for keeping Colang actions lightweight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .schemas import (
    PAIN_OR_GENERIC,
    PAIN_TYPES,
    SYMPTOM_FAMILY,
    ConversationTurn,
    HealthSignal,
    HealthSignalRecord,
    HealthType,
    PartialIgnoredResponse,
    ResponseMode,
    Severity,
    TriageRequest,
    TriageResponse,
    TriageResult,
    is_bodily_symptom,
    is_pain,
    none_signal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .main import get_service, implicit_health_triage, reset_service

__all__ = [
    "PAIN_OR_GENERIC",
    "PAIN_TYPES",
    "SYMPTOM_FAMILY",
    "ConversationTurn",
    "HealthSignal",
    "HealthSignalRecord",
    "HealthType",
    "PartialIgnoredResponse",
    "ResponseMode",
    "Severity",
    "TriageRequest",
    "TriageResponse",
    "TriageResult",
    "get_service",
    "implicit_health_triage",
    "is_bodily_symptom",
    "is_pain",
    "none_signal",
    "reset_service",
]

_LAZY = {
    "implicit_health_triage": (".main", "implicit_health_triage"),
    "get_service": (".main", "get_service"),
    "reset_service": (".main", "reset_service"),
}

__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute access for the heavy service entry points."""

    if name in _LAZY:
        import importlib

        module_name, attribute = _LAZY[name]
        module = importlib.import_module(module_name, __name__)
        value = getattr(module, attribute)
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
