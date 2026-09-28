"""DGX Spark readiness check (task guide sections 6.1, 6.2, 47).

Run this on the Spark before the demo:

    python scripts/check_dgx_spark.py

It reports the interpreter, the architecture, whether every runtime dependency
imports on this platform, whether the Colang config compiles, and whether the
three demo scenarios still match.  It exits non-zero if anything is wrong, so it
can gate a rehearsal.

Why there is no onnxruntime row: this skill deliberately does not depend on it.
The Colang flow index uses the pure-Python embedder registered by
``guardrails_embeddings`` instead, which removes a ~50 MB native dependency
from the safety path and sidesteps the loader problems that engine has on some
hosts.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

DEMO_CASES = PROJECT_ROOT / "demo" / "demo_cases.json"

#: Packages that must import on the target platform. All are pure-Python or
#: ship linux/arm64 wheels.
REQUIRED_MODULES = (
    "pydantic",
    "pydantic_settings",
    "fastapi",
    "uvicorn",
    "httpx",
    "orjson",
    "nemoguardrails",
)

OK = "  [ OK ]"
WARN = "  [WARN]"
FAIL = "  [FAIL]"


def check_runtime() -> list[str]:
    problems: list[str] = []

    version = sys.version_info
    print(f"{OK} Python {platform.python_version()} ({sys.executable})")
    if not (3, 10) <= version[:2] < (3, 14):
        print(f"{FAIL} nemoguardrails 0.24.1 requires Python >=3.10,<3.14")
        problems.append("python-version")

    machine = platform.machine()
    system = platform.system()
    print(f"{OK} Platform {system} / {machine}")
    if machine.lower() in {"aarch64", "arm64"}:
        print(f"{OK} ARM64 detected — DGX Spark architecture")
    else:
        print(f"{WARN} Not ARM64 ({machine}); fine for a rehearsal, not the Spark itself")
    if system.lower() != "linux":
        print(f"{WARN} Not Linux ({system}); the Spark runs Linux")

    return problems


def check_imports() -> list[str]:
    problems: list[str] = []

    for name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 - report whatever happens
            print(f"{FAIL} import {name}: {type(error).__name__}: {error}")
            problems.append(f"import:{name}")
            continue
        version = getattr(module, "__version__", "?")
        print(f"{OK} import {name} ({version})")

    return problems


async def check_pipeline() -> list[str]:
    from implicit_health_triage.guardrails_runtime import GuardrailsRuntime
    from implicit_health_triage.main import build_service
    from implicit_health_triage.settings import Settings

    problems: list[str] = []

    settings = Settings(guardrails_path=PROJECT_ROOT / "rails_config")
    print(f"{OK} LLM_PROVIDER={settings.llm_provider!r}  GUARDRAILS_LLM_MODE={settings.guardrails_llm_mode!r}")

    service = build_service(settings)
    try:
        cases = json.loads(DEMO_CASES.read_text(encoding="utf-8"))["scenarios"]
        for case in cases:
            result, latency = await service.triage_detailed(
                case["text"], history=case.get("context", ())
            )
            expect = case.get("expect", {})
            mismatches = []
            if "health_type" in expect and result.health_signal.type != expect["health_type"]:
                mismatches.append("health_type")
            if "guardrail_triggered" in expect and result.guardrail_triggered != expect["guardrail_triggered"]:
                mismatches.append("guardrail_triggered")
            if "response_mode" in expect and str(result.response_mode) != expect["response_mode"]:
                mismatches.append("response_mode")

            if mismatches:
                print(f"{FAIL} Demo {case['id']}: mismatch {mismatches}")
                problems.append(f"demo:{case['id']}")
            else:
                print(
                    f"{OK} Demo {case['id']}: mode={result.response_mode} "
                    f"guardrail={'BLOCK' if result.guardrail_triggered else 'PASS'} "
                    f"{latency.total_skill_latency_ms:.1f}ms"
                )
    finally:
        rails: GuardrailsRuntime | None = getattr(service, "guardrails", None)
        if rails is not None:
            await rails.aclose()

    return problems


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="only check the interpreter, platform and dependencies (fast; does not boot Guardrails)",
    )
    args = parser.parse_args()

    print("=" * 66)
    print("  implicit-health-triage — DGX Spark readiness check")
    print("=" * 66)

    print("\n[1/3] Runtime")
    problems = check_runtime()

    print("\n[2/3] Dependencies")
    problems += check_imports()

    if args.skip_pipeline:
        print("\n[3/3] Pipeline — skipped (--skip-pipeline)")
    else:
        print("\n[3/3] Pipeline (NeMo Guardrails input rail + demo scenarios)")
        try:
            problems += await check_pipeline()
        except Exception as error:  # noqa: BLE001
            print(f"{FAIL} pipeline: {type(error).__name__}: {error}")
            problems.append("pipeline")

    print("\n" + "=" * 66)
    if problems:
        print(f"  RESULT: NOT READY — {len(problems)} problem(s): {', '.join(problems)}")
        print("=" * 66)
        return 1

    print("  RESULT: READY")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
