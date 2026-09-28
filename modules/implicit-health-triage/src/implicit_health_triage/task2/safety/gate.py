import asyncio
from pathlib import Path

from ..schemas import SafetyDecision
from .policy import MedicationSafetyPolicy


class SafetyGate:
    def __init__(self, use_guardrails: bool = True):
        self.policy = MedicationSafetyPolicy()
        self.use_guardrails = use_guardrails
        self._rails = None
        self._lock = asyncio.Lock()

    async def evaluate(self, text: str) -> SafetyDecision:
        try:
            return await self._evaluate(text)
        except Exception as exc:
            # Unavailable/invalid rails never fall through to semantic analysis.
            # asyncio cancellation inherits BaseException and propagates unchanged.
            raise RuntimeError("Safety gate unavailable") from exc

    async def _evaluate(self, text: str) -> SafetyDecision:
        if not self.use_guardrails:
            return self.policy.evaluate(text)
        async with self._lock:
            if self._rails is None:
                from nemoguardrails import LLMRails, RailsConfig

                from ...guardrails_embeddings import register
                from ...guardrails_llm import StubLLMModel

                register()
                path = Path(__file__).parent / "rails"
                config = RailsConfig.from_content(
                    colang_content=(path / "medical_safety.co").read_text(encoding="utf-8"),
                    yaml_content=(path / "config.yml").read_text(encoding="utf-8"),
                )
                self._rails = LLMRails(config, llm=StubLLMModel())
            decisions = []

            async def medication_safety_check(text: str) -> bool:
                decision = self.policy.evaluate(text)
                decisions.append(decision)
                return decision.blocked

            self._rails.register_action(medication_safety_check, name="MedicationSafetyCheckAction")
            await self._rails.generate_async(
                messages=[{"role": "user", "content": text}],
                options={"rails": ["input"]},
            )
            if len(decisions) != 1:
                raise RuntimeError("Medication input rail did not execute exactly once")
            return decisions[0]

    async def aclose(self):
        self._rails = None
