"""Integration adapters for the neighbouring hackathon skills.

Task guide sections 32 and 33:

* upstream  — ``elderly-voice-duplex`` sends a finished transcript per turn
* downstream — ``family-digest-sync`` consumes ``type / detail / severity``

This module is the only place that knows about those two shapes, so the core
service stays free of neighbour-specific concerns.
"""

from __future__ import annotations

from datetime import datetime
try:                      # datetime.UTC 是 3.11 才加的
    from datetime import UTC
except ImportError:       # 3.10 兼容
    from datetime import timezone
    UTC = timezone.utc

from .schemas import (
    ConversationTurn,
    HealthSignalRecord,
    TriageResult,
    none_signal,
)
from .service import ImplicitHealthTriageService


class PartialTranscript:
    """Returned when an ASR partial is ignored (section 31)."""

    status = "ignored_partial"

    def __init__(self, session_id: str, turn_id: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id

    def model_dump(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "status": self.status,
        }


async def triage_turn(
    service: ImplicitHealthTriageService,
    turn: ConversationTurn,
    history: list[str] | None = None,
) -> TriageResult | PartialTranscript:
    """Handle one upstream turn.

    Only ``is_final=True`` turns are triaged: a partial transcript such as
    ``"我这个药……"`` may still be mid-thought and must never trigger the
    medication rail or the extractor.
    """

    if not turn.is_final:
        return PartialTranscript(turn.session_id, turn.turn_id)

    return await service.triage(
        turn.text,
        history=history or (),
        session_id=turn.session_id,
        turn_id=turn.turn_id,
    )


def to_digest_record(
    result: TriageResult,
    timestamp: str | None = None,
) -> HealthSignalRecord | None:
    """Project a triage result into the record ``family-digest-sync`` consumes.

    Returns ``None`` for ``type=无`` turns, which carry nothing worth
    summarising.  No P0/P1/P2 judgement is made here — that belongs to the
    digest skill (section 33).
    """

    if result.health_signal.type == none_signal().type:
        return None

    return HealthSignalRecord(
        timestamp=timestamp or datetime.now(UTC).isoformat(timespec="seconds"),
        type=result.health_signal.type,
        detail=result.health_signal.detail,
        severity=result.health_signal.severity,
    )


__all__ = ["PartialTranscript", "to_digest_record", "triage_turn"]
