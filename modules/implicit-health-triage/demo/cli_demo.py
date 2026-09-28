"""CLI demo — the three on-stage scenarios (task guide sections 41, 42, 54).

Usage::

    python demo/cli_demo.py               # run the scripted scenarios
    python demo/cli_demo.py --interactive # type your own turns
    python demo/cli_demo.py --no-rails    # skip NeMo Guardrails (deterministic only)

The panel below is the one the guide asks for: transcript, health signal,
guardrail verdict, final response and latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from implicit_health_triage.guardrails_runtime import GuardrailsRuntime  # noqa: E402
from implicit_health_triage.logging_utils import set_log_level  # noqa: E402
from implicit_health_triage.main import build_service  # noqa: E402
from implicit_health_triage.settings import Settings  # noqa: E402

DEMO_CASES = Path(__file__).resolve().parent / "demo_cases.json"

BAR = "─" * 66
HEAVY = "━" * 66

#: ``type`` / ``severity`` values are already Chinese, so they are shown as-is.
#: ``response_mode`` stays an English branch identifier — gloss it so the
#: audience can read it, because it is the one field that is not display text.
MODE_LABELS = {
    "normal_chat": "普通陪聊",
    "health_care": "健康关切",
    "medication_safety": "用药安全拦截",
}


def panel(result, latency, *, title: str, narration: str = "") -> str:
    signal = result.health_signal
    verdict = "BLOCK 阻断" if result.guardrail_triggered else "PASS 放行"
    mode = str(result.response_mode)

    # Chinese labels are two columns wide each, so a fixed pair of them keeps
    # the columns aligned; the ASCII field name is padded to a fixed width.
    signal_rows = (
        ("类型", "type", signal.type),
        ("内容", "detail", signal.detail or "-"),
        ("程度", "severity", signal.severity),
    )
    latency_rows = (
        ("围栏", "guardrail", f"{latency.guardrail_latency_ms:8.1f} ms"),
        ("抽取", "extraction", f"{latency.extraction_latency_ms:8.1f} ms"),
        ("回复", "response", f"{latency.response_generation_latency_ms:8.1f} ms"),
        ("合计", "total", f"{latency.total_skill_latency_ms:8.1f} ms"),
    )

    lines = [
        HEAVY,
        f"  {title}",
        HEAVY,
        "  老人原话",
        f"    {result.text}",
        BAR,
        "  健康信号 Health Signal",
    ]
    lines += [f"    {cn}  {en:<9}: {value}" for cn, en, value in signal_rows]

    lines += [
        BAR,
        "  医疗安全围栏 NeMo Guardrails",
        f"    判定  {'verdict':<9}: {verdict}",
        f"    规则  {'rule':<9}: {result.rule_id or '-'}",
        BAR,
        "  回复模式 Response Mode",
        f"    {mode}（{MODE_LABELS.get(mode, mode)}）",
        BAR,
        "  最终播报内容 Final Response",
    ]

    if result.response:
        lines.append(f"    {result.response}")
    else:
        lines.append("    （本模块不生成闲聊回复，交回主对话系统）")

    lines += [BAR, "  耗时 Latency"]
    lines += [f"    {cn}  {en:<11}: {value}" for cn, en, value in latency_rows]

    if narration:
        lines += [BAR, f"  {narration}"]

    lines.append(HEAVY)
    return "\n".join(lines)


def check_expectations(case: dict, result) -> list[str]:
    """Return a list of expectation mismatches (empty means it matched)."""

    expect = case.get("expect", {})
    signal = result.health_signal
    problems: list[str] = []

    if "health_type" in expect and signal.type != expect["health_type"]:
        problems.append(f"health_type expected {expect['health_type']}, got {signal.type}")
    if "detail" in expect and signal.detail != expect["detail"]:
        problems.append(f"detail expected {expect['detail']}, got {signal.detail}")
    if "severity" in expect and signal.severity != expect["severity"]:
        problems.append(f"severity expected {expect['severity']}, got {signal.severity}")
    if "guardrail_triggered" in expect and result.guardrail_triggered != expect["guardrail_triggered"]:
        problems.append(
            f"guardrail expected {expect['guardrail_triggered']}, got {result.guardrail_triggered}"
        )
    if "response_mode" in expect and str(result.response_mode) != expect["response_mode"]:
        problems.append(
            f"response_mode expected {expect['response_mode']}, got {result.response_mode}"
        )
    if "rule_id" in expect and result.rule_id != expect["rule_id"]:
        problems.append(f"rule_id expected {expect['rule_id']}, got {result.rule_id}")

    return problems


async def run_scenarios(service, cases: list[dict]) -> int:
    failures = 0

    for case in cases:
        result, latency = await service.triage_detailed(
            case["text"],
            history=case.get("context", ()),
            session_id="demo",
            turn_id=case["id"],
        )
        print(panel(result, latency, title=f"Demo {case['id']} · {case['title']}",
                    narration=case.get("narration", "")))
        print()

        problems = check_expectations(case, result)
        if problems:
            failures += 1
            for problem in problems:
                print(f"  [MISMATCH] {problem}")
            print()

    return failures


async def run_interactive(service) -> int:
    print("交互模式：输入一句话，Ctrl-C 或空行退出。")
    print("（同一会话的最近几轮会作为用药语境，用于识别施压式追问）")
    history: list[str] = []

    while True:
        try:
            text = input("\n老人 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not text:
            return 0

        result, latency = await service.triage_detailed(text, history=history)
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        print(f"latency: {latency.as_dict()}")

        history.append(text)
        del history[:-3]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true", help="type your own turns")
    parser.add_argument(
        "--no-rails",
        action="store_true",
        help="use the deterministic guard only (skips NeMo Guardrails init)",
    )
    parser.add_argument("--provider", default=None, help="override LLM_PROVIDER")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the structured per-turn log lines on stderr",
    )
    args = parser.parse_args()

    settings = Settings(
        guardrails_enabled=not args.no_rails,
        guardrails_path=PROJECT_ROOT / "rails_config",
    )
    if args.provider:
        settings = settings.model_copy(update={"llm_provider": args.provider})
    if args.quiet:
        set_log_level("ERROR")

    service = build_service(settings)
    exit_code = 0

    try:
        if args.interactive:
            exit_code = await run_interactive(service)
        else:
            cases = json.loads(DEMO_CASES.read_text(encoding="utf-8"))["scenarios"]
            failures = await run_scenarios(service, cases)
            if failures:
                print(f"{failures} scenario(s) did not match the expected output.")
                exit_code = 1
            else:
                print("All demo scenarios matched their expected output.")
    finally:
        rails: GuardrailsRuntime | None = getattr(service, "guardrails", None)
        if rails is not None:
            await rails.aclose()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
