"""Voice-input compatibility check — for the upstream ASR module.

Point this at real transcripts from ``elderly-voice-duplex`` and it reports how
this module handles them, with the emphasis on finding **damage the ASR did**:
sentences that read like a medication question but were not intercepted.

That is the only reliable way to discover the homophone substitutions a given
ASR actually makes on a given speaker. Regex fixes downstream cannot enumerate
them; an ASR hotword list (see ``examples/voice_hotwords.txt``) can — and this
script tells you which words still need boosting.

Usage::

    # one utterance per line
    python scripts/check_voice_input.py --input transcripts.txt

    # or JSONL with a "text" field (the ConversationTurn shape)
    python scripts/check_voice_input.py --input turns.jsonl

    # or pipe it in
    cat transcripts.txt | python scripts/check_voice_input.py

    # instant check of the rail only, no extraction, no model
    python scripts/check_voice_input.py --input transcripts.txt --rails-only

Exit code is 1 when anything needs review, so it can gate a rehearsal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from implicit_health_triage.logging_utils import set_log_level  # noqa: E402
from implicit_health_triage.safety.medication_rules import (  # noqa: E402
    assess_medication_risk,
    normalize_text,
)

#: Markers that make an utterance a *question about what to do*.
_DECISION_RE = re.compile(
    r"能不能|可不可以|能不能够|可以吗|可以么|行不行|行吗|行么|好吗|好不好"
    r"|要不要|该不该|需不需要|是否可以|中不中|妥不妥"
)

#: Characters that, after normalisation, suggest the utterance is about
#: medication or dosing — including the homophones an ASR produces.
#: Deliberately broad: this drives a *review* list, not an automatic verdict.
_MEDICATION_HINT_RE = re.compile(
    r"药|要|颗|棵|科|课|粒|立|片|篇|丸|剂|停药|断药|减|加量|剂量|漏服|漏吃"
    r"|一起吃|混着吃|换药|多吃|少吃"
)

#: Intake verb followed by a numeral — "吃两", "服2".
_INTAKE_NUM_RE = re.compile(r"[吃服][一二两三四五六半几数1-6]")


def read_inputs(path: str | None) -> list[str]:
    """Read utterances from a file, or stdin. Accepts plain lines or JSONL.

    ``utf-8-sig`` so a byte-order mark — common in files exported from Windows
    tools — does not end up glued to the first utterance.
    """

    if path:
        raw = Path(path).read_text(encoding="utf-8-sig")
    else:
        raw = sys.stdin.read().lstrip("\ufeff")

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return []

    # JSONL if the first non-empty line parses as an object with a "text" field.
    try:
        first = json.loads(lines[0])
        if isinstance(first, dict) and "text" in first:
            out = []
            for line in lines:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("text"):
                    out.append(str(payload["text"]))
            return out
    except json.JSONDecodeError:
        pass

    return lines


def looks_like_a_damaged_question(text: str) -> bool:
    """Heuristic: this reads like a dosing question but was not intercepted.

    Either the sentence is explicitly a decision question and mentions
    something medication-shaped, or it pairs an intake verb with a numeral.
    """

    compact = normalize_text(text)
    if not compact:
        return False

    if _DECISION_RE.search(compact) and _MEDICATION_HINT_RE.search(compact):
        return True

    return bool(_INTAKE_NUM_RE.search(compact) and _DECISION_RE.search(compact))


async def run_full(utterances: list[str]) -> list[tuple[str, str, bool, str]]:
    """(text, response_mode, blocked, detail) through the whole pipeline."""

    from implicit_health_triage.main import build_service

    service = build_service()
    rows = []
    try:
        for index, text in enumerate(utterances):
            result = await service.triage(text, turn_id=f"chk{index}")
            rows.append(
                (
                    text,
                    str(result.response_mode),
                    result.guardrail_triggered,
                    result.health_signal.detail,
                )
            )
    finally:
        rails = getattr(service, "guardrails", None)
        if rails is not None:
            await rails.aclose()
    return rows


def run_rails_only(utterances: list[str]) -> list[tuple[str, str, bool, str]]:
    rows = []
    for text in utterances:
        blocked = assess_medication_risk(text).is_risk
        rows.append(
            (
                text,
                "medication_safety" if blocked else "not_blocked",
                blocked,
                "",
            )
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=None, help="file with utterances (default: stdin)")
    parser.add_argument(
        "--rails-only",
        action="store_true",
        help="check the medication rail only — instant, no model, no extraction",
    )
    args = parser.parse_args()

    utterances = read_inputs(args.input)
    if not utterances:
        print("没有读到任何输入。用 --input <文件>，或把文本管道进来。")
        return 2

    set_log_level("ERROR")

    if args.rails_only:
        rows = run_rails_only(utterances)
    else:
        rows = asyncio.run(run_full(utterances))

    blocked = [r for r in rows if r[2]]
    review = [r for r in rows if not r[2] and looks_like_a_damaged_question(r[0])]

    print("=" * 70)
    print("  任务一 → 任务二  输入兼容性检查")
    print("=" * 70)
    print(f"  样本总数      : {len(rows)}")
    print(f"  被用药围栏拦截: {len(blocked)}")
    print("  模式分布      : ", end="")
    counts: dict[str, int] = {}
    for _, mode, _, _ in rows:
        counts[mode] = counts.get(mode, 0) + 1
    print("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    if review:
        print()
        print("-" * 70)
        print(f"  ⚠️  需要人工复核 {len(review)} 条")
        print("     这些句子读起来像在用询问药，但**没有被拦住**。")
        print("     大概率是 ASR 把关键词听错了 —— 请对照音频确认，")
        print("     并把听错的词补进 examples/voice_hotwords.txt 的热词表。")
        print("-" * 70)
        for text, _, _, _ in review:
            print(f"    {text}")

    if blocked:
        print()
        print("-" * 70)
        print(f"  参考：被拦截的 {len(blocked)} 条（确认没有误伤正常对话）")
        print("-" * 70)
        for text, _, _, _ in blocked:
            print(f"    {text}")

    print()
    print("=" * 70)
    if review:
        print(f"  结论：有 {len(review)} 条待复核")
        print("=" * 70)
        return 1

    print("  结论：全部符合预期")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
