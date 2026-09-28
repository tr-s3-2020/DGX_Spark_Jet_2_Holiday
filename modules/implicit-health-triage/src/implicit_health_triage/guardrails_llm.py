"""Bridge the skill's pluggable LLM backend into NeMo Guardrails.

``LLMRails(config, llm=...)`` expects an object satisfying the public
``nemoguardrails.types.LLMModel`` protocol.  This adapter wraps the skill's own
:class:`~implicit_health_triage.llm.base.LLMProvider` so the Guardrails runtime
and the extractor/responder always share one backend choice — and so that
``LLM_PROVIDER=mock`` keeps the entire stack, Guardrails included, fully
offline.

Only the ``input rails`` flow matters to this skill: a medication request is
aborted before generation.  Generation still runs for every other turn, which
is why a stub mode exists (see ``GUARDRAILS_LLM_MODE``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .llm.base import LLMProvider
from .llm.provider import build_llm_provider

try:  # pragma: no cover - import guard for API drift
    from nemoguardrails.types import (
        ChatMessage,
        LLMResponse,
        LLMResponseChunk,
    )
except ImportError:  # pragma: no cover
    ChatMessage = Any  # type: ignore[assignment,misc]
    LLMResponse = None  # type: ignore[assignment]
    LLMResponseChunk = None  # type: ignore[assignment]

#: Returned when ``GUARDRAILS_LLM_MODE=stub`` — the skill generates its own
#: replies, so the Colang ``main`` flow's output is never used.
STUB_REPLY = "(guardrails stub: generation disabled; the skill owns the reply)"


def split_prompt(prompt: Any) -> tuple[str, str]:
    """Split a Guardrails prompt into ``(system_prompt, user_prompt)``."""

    if isinstance(prompt, str):
        return "", prompt

    system_parts: list[str] = []
    user_parts: list[str] = []

    for message in prompt or []:
        role = getattr(message, "role", None)
        role_value = getattr(role, "value", role)
        content = getattr(message, "content", "") or ""
        if role_value == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)

    system_prompt = "\n\n".join(part for part in system_parts if part)
    user_prompt = "\n\n".join(part for part in user_parts if part)
    return system_prompt, user_prompt


class StubLLMModel:
    """``LLMModel`` that never calls a backend."""

    def __init__(self, model_name: str = "iht-stub") -> None:
        self._model_name = model_name

    async def generate_async(self, prompt: Any, *, stop=None, **kwargs: Any) -> Any:
        return LLMResponse(content=STUB_REPLY, model=self._model_name, finish_reason="stop")

    async def stream_async(
        self, prompt: Any, *, stop=None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        yield LLMResponseChunk(
            delta_content=STUB_REPLY, model=self._model_name, finish_reason="stop"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str | None:
        return "stub"

    @property
    def provider_url(self) -> str | None:
        return None


class SkillLLMModel:
    """``LLMModel`` backed by the skill's :class:`LLMProvider`."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        model_name: str = "iht-skill-llm",
    ) -> None:
        self._provider = provider or build_llm_provider()
        self._model_name = model_name

    async def generate_async(self, prompt: Any, *, stop=None, **kwargs: Any) -> Any:
        system_prompt, user_prompt = split_prompt(prompt)
        content = await self._provider.generate_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        return LLMResponse(content=content, model=self._model_name, finish_reason="stop")

    async def stream_async(
        self, prompt: Any, *, stop=None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        response = await self.generate_async(prompt, stop=stop, **kwargs)
        yield LLMResponseChunk(
            delta_content=response.content,
            model=self._model_name,
            finish_reason="stop",
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str | None:
        return "implicit-health-triage"

    @property
    def provider_url(self) -> str | None:
        return None


def build_guardrails_llm(mode: str = "skill", provider: LLMProvider | None = None) -> Any:
    """Create the ``LLMModel`` Guardrails should use.

    ``mode='stub'`` avoids generation entirely — appropriate here because the
    skill produces its own reply from the extraction result and only consumes
    the guardrail *verdict*.
    """

    if (mode or "skill").strip().lower() == "stub":
        return StubLLMModel()
    return SkillLLMModel(provider=provider)


__all__ = ["STUB_REPLY", "SkillLLMModel", "StubLLMModel", "build_guardrails_llm", "split_prompt"]
