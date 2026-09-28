"""The committed JSON Schemas must match the pydantic models.

``schemas/*.schema.json`` is what neighbouring modules validate against, so it
is a published contract.  This test fails the moment a model changes without the
schemas being re-exported, which is the only way the files can stay honest.
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.support import PROJECT_ROOT

SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SCHEMA_DIR = PROJECT_ROOT / "schemas"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import export_schemas  # noqa: E402


def test_schemas_are_exported_from_the_models():
    """Run ``python scripts/export_schemas.py`` if this fails."""

    stale: list[str] = []

    for path, expected in export_schemas.build().items():
        if not path.exists():
            stale.append(f"missing: {path.name}")
        elif path.read_text(encoding="utf-8") != expected:
            stale.append(f"out of date: {path.name}")

    assert stale == [], (
        "schemas/ does not match implicit_health_triage.schemas — "
        f"{stale}. Run: python scripts/export_schemas.py"
    )


@pytest.mark.parametrize(
    ("filename", "expected_role"),
    [
        ("ConversationTurn.schema.json", "input: from elderly-voice-duplex"),
        ("TriageRequest.schema.json", "input: HTTP POST /v1/triage"),
        ("TriageResponse.schema.json", "output: HTTP POST /v1/triage"),
        ("PartialIgnoredResponse.schema.json", "output: HTTP is_final=false"),
        ("TriageResult.schema.json", "output: core result"),
        ("HealthSignal.schema.json", "output: the three core fields"),
        ("HealthSignalRecord.schema.json", "output: to family-digest-sync"),
    ],
)
def test_every_contract_file_is_present_and_labelled(filename: str, expected_role: str):
    payload = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))

    assert payload["x-role"] == expected_role
    assert payload["type"] == "object"
    assert payload["$schema"].startswith("https://json-schema.org/")


def test_health_signal_schema_pins_the_three_core_fields():
    payload = json.loads((SCHEMA_DIR / "HealthSignal.schema.json").read_text(encoding="utf-8"))

    assert set(payload["properties"]) == {"type", "detail", "severity"}
    assert payload["additionalProperties"] is False
    # Values are Chinese on the wire (decision: the product is Chinese-facing).
    assert payload["$defs"]["HealthType"]["enum"] == [
        "无",
        "身体不适",
        "睡眠",
        "疼痛",
        "用药",
        "行动能力",
        "食欲",
        "其他体征",
    ]
    assert payload["$defs"]["Severity"]["enum"] == ["轻微", "中等", "需留意"]


def test_response_modes_are_pinned():
    payload = json.loads((SCHEMA_DIR / "TriageResult.schema.json").read_text(encoding="utf-8"))

    modes = payload["$defs"]["ResponseMode"]["enum"]
    assert modes == ["normal_chat", "health_care", "medication_safety"]


def test_partial_response_status_is_pinned():
    payload = json.loads(
        (SCHEMA_DIR / "PartialIgnoredResponse.schema.json").read_text(encoding="utf-8")
    )

    assert payload["properties"]["status"]["const"] == "ignored_partial"


def test_schema_dir_is_not_empty():
    assert sorted(p.name for p in SCHEMA_DIR.glob("*.schema.json")) == sorted(
        f"{stem}.schema.json" for stem, _, _ in export_schemas.EXPORTS
    )
