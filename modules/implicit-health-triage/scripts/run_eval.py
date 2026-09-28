"""Evaluation harness (task guide sections 39, 40, 45).

Runs the three case files through the skill and reports::

    Extraction      JSON Valid Rate / Type Accuracy / Severity Accuracy /
                    image few-shot pass-fail
    Guardrail       Medication Safety Recall / False Positive Rate
    Performance     guardrail / extraction / response / total latency

The extraction numbers depend on the configured backend.  With the default
``LLM_PROVIDER=mock`` they measure the offline stub, **not** a language model —
the report states which backend produced it so the numbers are never mistaken
for model quality.

Usage::

    python scripts/run_eval.py                    # default backend
    python scripts/run_eval.py --no-rails         # deterministic guard only
    python scripts/run_eval.py -o reports/eval.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from implicit_health_triage.guardrails_runtime import GuardrailsRuntime  # noqa: E402
from implicit_health_triage.logging_utils import set_log_level  # noqa: E402
from implicit_health_triage.main import build_service  # noqa: E402
from implicit_health_triage.settings import Settings  # noqa: E402

CASES_DIR = PROJECT_ROOT / "tests" / "cases"
IMAGE_FEW_SHOT = "今天早上起来腿沉得很，买菜走两步就得歇着。"


def load_cases(filename: str) -> list[dict]:
    records = []
    for line in (CASES_DIR / filename).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


@dataclass
class Tally:
    total: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    def record(self, ok: bool, label: str) -> None:
        self.total += 1
        if ok:
            self.passed += 1
        else:
            self.failures.append(label)

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def line(self) -> str:
        return f"{self.passed}/{self.total} ({self.rate:.1%})"


@dataclass
class LatencyStats:
    samples: dict[str, list[float]] = field(default_factory=dict)

    def add(self, breakdown) -> None:
        for key, value in breakdown.as_dict().items():
            self.samples.setdefault(key, []).append(value)

    @staticmethod
    def _fmt(values: list[float]) -> str:
        if not values:
            return "n/a"
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
        return (
            f"mean {statistics.fmean(values):7.1f}  "
            f"p50 {statistics.median(values):7.1f}  "
            f"p95 {p95:7.1f}  max {max(values):7.1f}"
        )

    def report(self) -> list[str]:
        order = [
            "guardrail_latency_ms",
            "extraction_latency_ms",
            "response_generation_latency_ms",
            "total_skill_latency_ms",
        ]
        return [
            f"- `{key}`: {self._fmt(self.samples.get(key, []))}" for key in order
        ]


async def evaluate(service, *, use_rails: bool) -> dict:
    extraction = Tally()
    json_valid = Tally()
    severity = Tally()
    guardrail = Tally()
    false_positive = Tally()
    latency = LatencyStats()

    # --- extraction ----------------------------------------------------
    extraction_cases = load_cases("extraction_cases.jsonl")
    for case in extraction_cases:
        result, breakdown = await service.triage_detailed(case["text"])
        latency.add(breakdown)

        signal = result.health_signal
        extraction.record(signal.type == case["expected_type"], f"type: {case['text']}")
        json_valid.record(signal.type is not None, case["text"])

        if "expected_severity" in case:
            severity.record(
                signal.severity == case["expected_severity"], f"severity: {case['text']}"
            )
        else:
            severity.record(True, case["text"])

    # --- medication guardrail ------------------------------------------
    medication_cases = load_cases("medication_safety_cases.jsonl")
    bypass_cases = load_cases("bypass_cases.jsonl")

    for case in medication_cases + bypass_cases:
        result, breakdown = await service.triage_detailed(
            case["text"], history=case.get("context", ())
        )
        latency.add(breakdown)

        if case["blocked"]:
            guardrail.record(
                result.guardrail_triggered, f"recall miss: {case['text']}"
            )
        else:
            false_positive.record(
                not result.guardrail_triggered, f"false positive: {case['text']}"
            )

    # --- the image few-shot --------------------------------------------
    few_shot_result, breakdown = await service.triage_detailed(IMAGE_FEW_SHOT)
    latency.add(breakdown)
    few_shot_ok = few_shot_result.health_signal.model_dump(mode="json") == {
        "type": "身体不适",
        "detail": "下肢沉重/乏力",
        "severity": "中等",
    }

    # --- blocked turns must not reach the reply model -------------------
    blocked_never_generates = True
    for case in medication_cases:
        if not case["blocked"]:
            continue
        _, breakdown = await service.triage_detailed(case["text"])
        if breakdown.response_generation_latency_ms != 0.0:
            blocked_never_generates = False

    # --- the LLM review layer (needs a real model) ----------------------
    #
    # Kept in its own corpus on purpose. The deterministic corpus above is a
    # *mock contract*: it must pass offline, with no model, in CI. These cases
    # cannot — by construction the rule layer misses them, so only a real model
    # can answer them. Mixing the two would make one of the numbers a lie.
    semantic_cases = load_cases("semantic_cases.jsonl")
    semantic_recall = Tally()
    semantic_fp = Tally()
    semantic_ran = False

    if service.semantic_guard is not None:
        semantic_ran = True
        for case in semantic_cases:
            result, breakdown = await service.triage_detailed(case["text"])
            latency.add(breakdown)
            if case["blocked"]:
                semantic_recall.record(
                    result.guardrail_triggered, f"recall miss: {case['text']}"
                )
            else:
                semantic_fp.record(
                    not result.guardrail_triggered, f"false positive: {case['text']}"
                )

    return {
        "use_rails": use_rails,
        "extraction_type_accuracy": extraction,
        "json_valid_rate": json_valid,
        "severity_accuracy": severity,
        "medication_recall": guardrail,
        "false_positive": false_positive,
        "semantic_ran": semantic_ran,
        "semantic_recall": semantic_recall,
        "semantic_false_positive": semantic_fp,
        "few_shot_ok": few_shot_ok,
        "blocked_never_generates": blocked_never_generates,
        "latency": latency,
        "counts": {
            "extraction_cases": len(extraction_cases),
            "medication_cases": len(medication_cases),
            "bypass_cases": len(bypass_cases),
            "semantic_cases": len(semantic_cases),
        },
    }


def render(report: dict, settings: Settings) -> str:
    lines = [
        "# implicit-health-triage — evaluation report",
        "",
        "Generated by `scripts/run_eval.py`.",
        "",
        "## Backend",
        "",
        f"- `LLM_PROVIDER`: `{settings.llm_provider}`",
        f"- `LLM_MODEL`: `{settings.llm_model or '(unset)'}`",
        f"- NeMo Guardrails enabled: `{report['use_rails']}`",
        "- Colang version: `2.x`",
        "",
    ]

    if settings.llm_provider == "mock":
        lines += [
            "> **The extraction numbers below were produced by the offline mock",
            "> provider, which is a deterministic keyword stub — not a language",
            "> model.** They verify the pipeline and the documented few-shots; they",
            "> are not a measurement of model accuracy. Re-run against the real",
            "> endpoint before quoting them for the judges.",
            "",
        ]

    lines += [
        "## Extraction",
        "",
        f"- JSON valid rate: **{report['json_valid_rate'].line()}**",
        f"- Type accuracy: **{report['extraction_type_accuracy'].line()}**",
        f"- Severity accuracy: **{report['severity_accuracy'].line()}**",
        f"- Image few-shot reproduced exactly: **{'PASS' if report['few_shot_ok'] else 'FAIL'}**",
        "",
        "## Medication guardrail (deterministic rules — model-independent)",
        "",
        f"- Medication Safety Recall: **{report['medication_recall'].line()}**",
        f"- No false positive on safe turns: **{report['false_positive'].line()}**",
        "- Blocked turns never reach the reply model: "
        f"**{'PASS' if report['blocked_never_generates'] else 'FAIL'}**",
        "",
    ]

    if report["semantic_ran"]:
        lines += [
            "## Medication guardrail (LLM review layer — needs a real model)",
            "",
            "These cases are dialect and heavy ellipsis that the rules above miss "
            "by construction.",
            "",
            f"- Recall with the LLM layer: **{report['semantic_recall'].line()}**",
            f"- False positives: **{report['semantic_false_positive'].line()}**",
            "- The two layers combine as `rules OR llm`; the LLM can only add blocks.",
            "",
        ]
    else:
        lines += [
            "## Medication guardrail (LLM review layer)",
            "",
            "- **NOT MEASURED** — no real model is configured (`LLM_PROVIDER=mock`).",
            "  The rule layer's numbers above stand on their own; the LLM layer is an",
            "  additional widening net whose recall cannot be measured offline.",
            "",
        ]

    lines += [
        "## Latency",
        "",
        *report["latency"].report(),
        "",
        "## Dataset",
        "",
        f"- extraction cases: {report['counts']['extraction_cases']}",
        f"- medication safety cases (rules): {report['counts']['medication_cases']}",
        f"- bypass cases: {report['counts']['bypass_cases']}",
        f"- semantic cases (LLM layer): {report['counts']['semantic_cases']}",
        "",
    ]

    problems = (
        report["extraction_type_accuracy"].failures
        + report["medication_recall"].failures
        + report["false_positive"].failures
    )
    if problems:
        lines += ["## Failures", ""]
        lines += [f"- {problem}" for problem in problems]
        lines.append("")

    return "\n".join(lines)


async def _preflight(service) -> str | None:
    """Return a reason string when the backend cannot actually be used.

    ``None`` means "safe to measure" — which includes the offline mock, whose
    numbers are a deliberate contract rather than a model measurement.
    """

    provider = getattr(service.extractor, "_provider", None)
    client = getattr(provider, "_client", None)
    if client is None:  # mock, or anything without an HTTP client
        return None

    try:
        response = await client.get("/models", timeout=10.0)
    except Exception as error:  # noqa: BLE001 - any transport failure is one case
        return f"cannot reach {client.base_url}: {type(error).__name__}: {error}"

    if response.status_code >= 400:
        return f"GET {client.base_url}models returned HTTP {response.status_code}"

    try:
        served = [item["id"] for item in response.json().get("data", [])]
    except (KeyError, TypeError, ValueError):
        return None

    configured = getattr(provider, "_model", None)
    if served and configured not in served:
        return (
            f"LLM_MODEL={configured!r} is not served by this endpoint; "
            f"it reports: {', '.join(served)}"
        )

    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-rails", action="store_true", help="deterministic guard only")
    parser.add_argument("-o", "--output", default=None, help="write the report to this path")
    parser.add_argument("--provider", default=None, help="override LLM_PROVIDER")
    args = parser.parse_args()

    settings = Settings(
        guardrails_enabled=not args.no_rails,
        guardrails_path=PROJECT_ROOT / "rails_config",
        log_level="ERROR",
    )
    if args.provider:
        settings = settings.model_copy(update={"llm_provider": args.provider})

    set_log_level("ERROR")
    service = build_service(settings)

    # ---- preflight -----------------------------------------------------
    # A configured-but-unreachable endpoint does not crash this pipeline: the
    # extractor degrades to type=无 and the semantic layer abstains, so the run
    # completes and prints a report full of zeros. That report looks exactly
    # like "the feature is broken" and is very screenshot-able. Refuse to
    # produce it instead.
    unreachable = await _preflight(service)
    if unreachable:
        print(f"  [FAIL] {unreachable}", file=sys.stderr)
        print(
            "\n  评测已中止：后端不可达时跑出来的报告会把「端点没起」显示成"
            "\n  「功能坏了」。修好端点再跑，或用 --provider mock 跑离线基线。",
            file=sys.stderr,
        )
        return 2

    try:
        report = await evaluate(service, use_rails=not args.no_rails)
    finally:
        rails: GuardrailsRuntime | None = getattr(service, "guardrails", None)
        if rails is not None:
            await rails.aclose()

    markdown = render(report, settings)
    print(markdown)

    if args.output:
        destination = Path(args.output)
        if not destination.is_absolute():
            destination = PROJECT_ROOT / destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown, encoding="utf-8")
        print(f"\nWritten to {destination}")

    problems = (
        len(report["extraction_type_accuracy"].failures)
        + len(report["medication_recall"].failures)
        + len(report["false_positive"].failures)
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
