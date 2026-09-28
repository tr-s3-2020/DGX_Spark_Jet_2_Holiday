"""LLM provider interface (task guide section 9.1).

The skill must never depend on a fixed cloud API, a fixed NIM name or a fixed
model name (section 9.2).  Everything goes through this two-method interface so
the same business code runs against a mock in CI and against a DGX Spark local
endpoint during the demo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProviderError(RuntimeError):
    """Raised when the backend fails in a way the caller should handle."""


class LLMTimeoutError(LLMProviderError):
    """Raised when the backend does not answer in time (section 48.2)."""


class LLMProvider(ABC):
    """Minimal async text/JSON generation interface."""

    @abstractmethod
    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Return the model's raw answer, expected to be a JSON object."""

        raise NotImplementedError

    @abstractmethod
    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Return the model's raw answer as free text."""

        raise NotImplementedError

    async def aclose(self) -> None:
        """Release backend resources. Default is a no-op."""

        return None


__all__ = ["LLMProvider", "LLMProviderError", "LLMTimeoutError"]
