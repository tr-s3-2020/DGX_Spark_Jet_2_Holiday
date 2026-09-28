"""Test support helpers: paths, case loading and shared settings factories."""

from __future__ import annotations

import json
import os
from pathlib import Path

# Must be set before nemoguardrails starts its telemetry thread.
os.environ.setdefault("NEMO_GUARDRAILS_NO_USAGE_STATS", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TESTS_ROOT = Path(__file__).resolve().parent
CASES_DIR = TESTS_ROOT / "cases"
GUARDRAILS_DIR = PROJECT_ROOT / "rails_config"


def load_cases(filename: str) -> list[dict]:
    """Read a ``tests/cases/*.jsonl`` file into a list of dicts."""

    records: list[dict] = []
    for line in (CASES_DIR / filename).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def guardrails_settings():
    """Settings that drive the real Colang runtime with no model and no network."""

    from implicit_health_triage.settings import Settings

    return Settings(
        llm_provider="mock",
        guardrails_enabled=True,
        guardrails_path=GUARDRAILS_DIR,
        guardrails_model="unset-model",
        guardrails_llm_mode="stub",
        guardrails_fail_fast=True,
    )


__all__ = [
    "CASES_DIR",
    "GUARDRAILS_DIR",
    "PROJECT_ROOT",
    "SRC_ROOT",
    "guardrails_settings",
    "load_cases",
]
