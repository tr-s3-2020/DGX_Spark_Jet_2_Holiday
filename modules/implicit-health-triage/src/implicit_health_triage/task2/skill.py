from .input_adapter import InputAdapter
from .safety.gate import SafetyGate
from .schemas import TriageInput, TriageOutput
from .semantic.mock import MockSemanticModel
from .semantic.qwen import QwenSemanticModel
from .settings import Settings
from .triage_engine import HealthTriageEngine


class ImplicitHealthTriageSkill:
    def __init__(self, settings: Settings | None = None, semantic_model=None):
        self.settings = settings or Settings()
        self.input_adapter = InputAdapter()
        if semantic_model is None:
            semantic_model = (
                MockSemanticModel()
                if self.settings.semantic_backend == "mock"
                else QwenSemanticModel(
                    self.settings.qwen_base_url,
                    self.settings.qwen_model,
                    self.settings.qwen_api_key,
                    self.settings.qwen_timeout_seconds,
                )
            )
        self.triage_engine = HealthTriageEngine(
            semantic_model,
            self.settings.semantic_backend,
            SafetyGate(self.settings.triage_guardrails_enabled),
        )

    async def handle(self, input_data: TriageInput) -> TriageOutput | None:
        if not isinstance(input_data, TriageInput):
            input_data = TriageInput.model_validate(input_data)
        turn = self.input_adapter.adapt(input_data)
        return None if turn is None else await self.triage_engine.triage(turn)

    async def aclose(self):
        close = getattr(self.triage_engine.semantic_model, "aclose", None)
        if close is not None:
            await close()
        await self.triage_engine.safety_gate.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


async def handle(input_data: TriageInput) -> TriageOutput | None:
    async with ImplicitHealthTriageSkill() as skill:
        return await skill.handle(input_data)
