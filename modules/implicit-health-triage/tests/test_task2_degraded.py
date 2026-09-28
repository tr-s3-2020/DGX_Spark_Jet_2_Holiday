import json

import httpx
import pytest
from fastapi.testclient import TestClient

from implicit_health_triage.task2 import ImplicitHealthTriageSkill
from implicit_health_triage.task2.api import create_app
from implicit_health_triage.task2.schemas import none_signal
from implicit_health_triage.task2.settings import Settings
from scripts.export_task2_schemas import build


class FakeSemantic:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    async def analyze(self, text):
        self.calls += 1
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.parametrize(
    "results,degraded,calls",
    [
        ([none_signal().model_dump_json()], False, 1),
        (["invalid", none_signal().model_dump_json()], False, 2),
        (["invalid", "invalid"], True, 2),
        ([httpx.ReadTimeout("timeout"), httpx.ReadTimeout("timeout")], True, 2),
    ],
)
def test_http_distinguishes_valid_none_from_fallback(results, degraded, calls):
    model = FakeSemantic(results)
    skill = ImplicitHealthTriageSkill(
        Settings(
            _env_file=None,
            semantic_backend="qwen",
            qwen_model="test",
            triage_guardrails_enabled=False,
        ),
        model,
    )
    with TestClient(create_app(skill)) as client:
        response = client.post(
            "/v1/implicit-health-triage",
            json={"session_id": "s1", "turn_id": "t1", "text": "今天花开了", "is_final": True},
        )
    assert response.status_code == 200
    result = response.json()
    assert result["metadata"]["degraded"] is degraded
    assert result["metadata"]["qwen_called"] is True
    assert result["health_signal"] == none_signal().model_dump()
    assert model.calls == calls


def test_current_schemas_are_exported_and_degraded_required():
    for path, content in build().items():
        assert path.read_text(encoding="utf-8") == content
        if path.name == "TriageOutput.schema.json":
            schema = json.loads(content)
            assert "degraded" in schema["$defs"]["TriageMetadata"]["required"]
