"""Shared pytest fixtures.

Everything here runs fully offline: ``MockLLMProvider`` for text/JSON and
``GUARDRAILS_LLM_MODE=stub`` so the Colang ``main`` flow never calls a model.
"""

from __future__ import annotations

import pytest

from implicit_health_triage.extractor import HealthExtractor
from implicit_health_triage.guardrails_runtime import GuardrailsRuntime
from implicit_health_triage.llm.provider import MockLLMProvider
from implicit_health_triage.responder import HealthResponder
from implicit_health_triage.safety.medication_guard import MedicationGuard
from implicit_health_triage.service import ImplicitHealthTriageService
from tests.support import guardrails_settings


@pytest.fixture(scope="session")
def mock_provider() -> MockLLMProvider:
    return MockLLMProvider()


@pytest.fixture
def guard() -> MedicationGuard:
    return MedicationGuard()


@pytest.fixture
def extractor(mock_provider: MockLLMProvider) -> HealthExtractor:
    return HealthExtractor(mock_provider)


@pytest.fixture
def responder(mock_provider: MockLLMProvider) -> HealthResponder:
    return HealthResponder(mock_provider)


@pytest.fixture
def service(
    extractor: HealthExtractor,
    guard: MedicationGuard,
    responder: HealthResponder,
) -> ImplicitHealthTriageService:
    """Deterministic-only service — fast, used by most tests."""

    return ImplicitHealthTriageService(
        extractor=extractor,
        medication_guard=guard,
        responder=responder,
        guardrails=None,
    )


@pytest.fixture(scope="session")
def guardrails_runtime():
    """One ``LLMRails`` for the whole session (initialisation is expensive)."""

    runtime = GuardrailsRuntime(guardrails_settings())
    yield runtime
    runtime.close()


@pytest.fixture
def guardrails_service(
    extractor: HealthExtractor,
    guard: MedicationGuard,
    responder: HealthResponder,
    guardrails_runtime: GuardrailsRuntime,
) -> ImplicitHealthTriageService:
    """Service wired to the real NeMo Guardrails input rail."""

    return ImplicitHealthTriageService(
        extractor=extractor,
        medication_guard=guard,
        responder=responder,
        guardrails=guardrails_runtime,
    )
