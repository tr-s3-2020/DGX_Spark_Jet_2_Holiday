import logging
from time import perf_counter

import httpx

from .response_builder import ResponseBuilder
from .safety.gate import SafetyGate
from .schemas import NormalizedTurn, TriageMetadata, TriageOutput, none_signal
from .semantic.parser import HealthSignalParser
from .semantic.port import SemanticModelPort
from .semantic.prompts import build_retry_prompt

logger = logging.getLogger(__name__)


class HealthTriageEngine:
    def __init__(self, semantic_model: SemanticModelPort, backend="mock", safety_gate=None):
        self.semantic_model = semantic_model
        self.backend = backend
        self.safety_gate = safety_gate or SafetyGate()
        self.parser = HealthSignalParser()
        self.response_builder = ResponseBuilder()

    async def triage(self, turn: NormalizedTurn) -> TriageOutput:
        start = perf_counter()
        decision = await self.safety_gate.evaluate(turn.text)
        signal = none_signal()
        calls = 0
        degraded = False
        if not decision.blocked:
            for attempt in range(2):
                calls += 1
                try:
                    raw = await self.semantic_model.analyze(
                        turn.text if attempt == 0 else build_retry_prompt(turn.text)
                    )
                    parsed = self.parser.parse(raw)
                except (httpx.HTTPError, ValueError, TimeoutError):
                    parsed = None
                if parsed is not None:
                    signal = parsed
                    break
            else:
                degraded = True
                logger.warning("Semantic analysis failed after two attempts; returning none signal")
        metadata = TriageMetadata(
            degraded=degraded,
            semantic_backend=self.backend if calls else "none",
            qwen_called=self.backend == "qwen" and calls > 0,
            latency_ms=round((perf_counter() - start) * 1000, 3),
        )
        return self.response_builder.build(turn, signal, decision, metadata)
