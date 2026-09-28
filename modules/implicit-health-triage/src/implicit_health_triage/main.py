"""Unified skill entry point (task guide section 34).

``implicit_health_triage(text)`` is the single call the rest of the hackathon
project needs.  The service — and therefore the model, the rule layer and the
compiled Colang flows — is built once per process and reused (sections 22, 34).
"""

from __future__ import annotations

import threading

from .extractor import HealthExtractor
from .guardrails_runtime import GuardrailsRuntime
from .llm.base import LLMProvider
from .llm.provider import MockLLMProvider, build_llm_provider
from .responder import HealthResponder
from .safety.medication_guard import MedicationGuard
from .safety.semantic_guard import SemanticMedicationGuard
from .schemas import TriageResult
from .service import ImplicitHealthTriageService
from .settings import Settings, get_settings

_service: ImplicitHealthTriageService | None = None
_service_lock = threading.Lock()


def build_service(
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
) -> ImplicitHealthTriageService:
    """Construct a service. Callers own the result; the module singleton uses it."""

    settings = settings or get_settings()
    provider = llm if llm is not None else build_llm_provider(settings)

    guardrails = GuardrailsRuntime(settings) if settings.guardrails_enabled else None

    # The LLM review layer is opt-out, not opt-in: without it the rule layer
    # catches 1 of 15 dialect/ellipsis medication questions. It is skipped
    # automatically when the backend is the offline mock, which cannot judge.
    semantic_guard = (
        SemanticMedicationGuard(provider)
        if settings.semantic_guard_enabled and not isinstance(provider, MockLLMProvider)
        else None
    )

    return ImplicitHealthTriageService(
        extractor=HealthExtractor(provider, max_retries=settings.llm_max_retries),
        medication_guard=MedicationGuard(),
        responder=HealthResponder(provider),
        guardrails=guardrails,
        semantic_guard=semantic_guard,
    )


def get_service() -> ImplicitHealthTriageService:
    """Process-wide service singleton."""

    global _service

    if _service is None:
        with _service_lock:
            if _service is None:
                _service = build_service()

    return _service


def reset_service() -> None:
    """Drop the singleton (used by tests and by the CLI demo)."""

    global _service
    with _service_lock:
        _service = None


async def implicit_health_triage(text: str) -> TriageResult:
    """Documented skill entry point."""

    return await get_service().triage(text)


__all__ = ["build_service", "get_service", "implicit_health_triage", "reset_service"]
