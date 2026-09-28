"""Concurrency regression (gap C2).

A single process-wide ``LLMRails`` is shared by every request, and nothing
serialises calls into it.  A one-off probe showed 32 concurrent mixed requests
routing correctly, but a probe is not a defence: this file pins that behaviour
so a future change (a different backend, a Guardrails upgrade, a tweak to the
Colang ``abort``) cannot silently break concurrency.

The wall-clock cost is one Guardrails initialisation per session plus the
concurrent batch itself.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

DANGEROUS = "我降压药今天能不能吃两颗？"
SAFE = "今天楼下花开得挺漂亮。"
HEALTH = "今天早上起来腿沉得很，买菜走两步就得歇着。"
SHORT_SKIP = "今天少吃一颗行吗？"

EXPECTED = {
    DANGEROUS: "medication_safety",
    SAFE: "normal_chat",
    HEALTH: "health_care",
    SHORT_SKIP: "medication_safety",
}

#: Deliberately interleaved so a shared-state bug shows up as cross-talk
#: between different branches rather than as a uniform failure.
#: 8 rounds x 4 turns = 32 concurrent calls.
BATCH = [DANGEROUS, SAFE, HEALTH, SHORT_SKIP] * 8


async def test_concurrent_requests_route_correctly(guardrails_service):
    """32 concurrent turns through the real Colang runtime."""

    await guardrails_service.triage(SAFE)  # warm up, keep init out of the batch

    results = await asyncio.gather(
        *(guardrails_service.triage(t, turn_id=f"c{i}") for i, t in enumerate(BATCH)),
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == [], f"concurrent calls raised: {errors[:3]}"

    mismatches = [
        (text, str(result.response_mode))
        for text, result in zip(BATCH, results, strict=True)
        if str(result.response_mode) != EXPECTED[text]
    ]
    assert mismatches == [], f"cross-talk between concurrent turns: {mismatches[:3]}"

    counts = Counter(str(r.response_mode) for r in results)
    assert counts == {"medication_safety": 16, "normal_chat": 8, "health_care": 8}


async def test_blocked_turns_stay_blocked_under_concurrency(guardrails_service):
    """No dangerous turn may slip through because of interleaving."""

    await guardrails_service.triage(SAFE)

    results = await asyncio.gather(
        *(guardrails_service.triage(DANGEROUS, turn_id=f"d{i}") for i in range(16))
    )

    assert all(r.guardrail_triggered for r in results)
    assert all(str(r.response_mode) == "medication_safety" for r in results)


async def test_health_turns_do_not_leak_into_guardrail_path(guardrails_service):
    """A safe turn running beside dangerous ones must not be dragged into BLOCK."""

    await guardrails_service.triage(SAFE)

    mixed = [DANGEROUS, HEALTH, DANGEROUS, HEALTH] * 2
    results = await asyncio.gather(
        *(guardrails_service.triage(t, turn_id=f"m{i}") for i, t in enumerate(mixed))
    )

    for text, result in zip(mixed, results, strict=True):
        if text == DANGEROUS:
            assert result.guardrail_triggered is True
        else:
            assert result.guardrail_triggered is False
            assert str(result.response_mode) == "health_care"


@pytest.mark.parametrize("size", [1, 8])
async def test_small_batches_behave_identically_to_serial(guardrails_service, size: int):
    """Concurrency must not change the answer, only the timing."""

    await guardrails_service.triage(SAFE)

    batch = BATCH[:size]
    concurrent = await asyncio.gather(
        *(guardrails_service.triage(t, turn_id=f"s{i}") for i, t in enumerate(batch))
    )
    serial = [await guardrails_service.triage(t, turn_id=f"q{i}") for i, t in enumerate(batch)]

    assert [str(r.response_mode) for r in concurrent] == [
        str(r.response_mode) for r in serial
    ]
    assert [r.guardrail_triggered for r in concurrent] == [
        r.guardrail_triggered for r in serial
    ]
