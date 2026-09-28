"""Structured, explainable logging for the hackathon demo (sections 35, 51).

Every request emits one compact record so the on-stage UI can show *why* a
turn was blocked::

    2026-09-22T07:00:31 module=implicit-health-triage turn=t029
    guardrail=true rule=MEDICATION_CHANGE_REQUEST health_type=medication
    severity=high latency_ms=42

Full conversation text is only included when ``LOG_INPUT_TEXT`` is enabled,
because the default must not persist private conversations.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .settings import get_settings

LOGGER_NAME = "implicit-health-triage"
MODULE_NAME = "implicit-health-triage"


@dataclass
class LatencyBreakdown:
    """Per-stage timings required by section 40."""

    guardrail_latency_ms: float = 0.0
    extraction_latency_ms: float = 0.0
    response_generation_latency_ms: float = 0.0
    total_skill_latency_ms: float = 0.0

    #: Time spent on the optional LLM safety review. Counted inside
    #: ``guardrail_latency_ms`` as well, so the total stays a sum of its parts;
    #: this field exists to show what the extra layer actually costs. Zero on
    #: every turn the rules already decided, which is most of them.
    semantic_review_latency_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 2) for k, v in asdict(self).items()}


@dataclass
class TurnLogRecord:
    """One observable decision record (section 51)."""

    module: str = MODULE_NAME
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    session_id: str | None = None
    turn_id: str | None = None
    input_length: int = 0
    guardrail_triggered: bool = False
    rule_id: str | None = None
    risk_category: str | None = None
    health_type: str | None = None
    severity: str | None = None
    response_mode: str | None = None
    latency_ms: float = 0.0
    latency: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    input_text: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.input_text is None:
            payload.pop("input_text", None)
        return payload


def get_logger() -> logging.Logger:
    """Return the module logger, configured once.

    The level is applied only on first configuration so that an explicit
    :func:`set_log_level` call is not silently undone on the next log record.
    """

    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(getattr(logging, get_settings().log_level.upper(), logging.INFO))
    return logger


def set_log_level(level: str | int) -> None:
    """Override the log level for the process.

    ``Settings`` is cached process-wide, so callers that build a service with
    ad-hoc settings (the CLI demo, the eval harness) use this to silence the
    per-turn records without touching the environment.
    """

    resolved = getattr(logging, level.upper(), logging.INFO) if isinstance(level, str) else level
    get_logger().setLevel(resolved)


def log_turn(record: TurnLogRecord) -> dict[str, Any]:
    """Emit one structured turn record and return it as a dict."""

    if record.input_text is not None and not get_settings().log_input_text:
        record.input_text = None

    payload = record.as_dict()
    get_logger().info(json.dumps(payload, ensure_ascii=False))
    return payload


__all__ = [
    "LOGGER_NAME",
    "MODULE_NAME",
    "LatencyBreakdown",
    "TurnLogRecord",
    "get_logger",
    "log_turn",
    "set_log_level",
]
