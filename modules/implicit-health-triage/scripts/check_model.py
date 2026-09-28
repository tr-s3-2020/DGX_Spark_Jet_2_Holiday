"""One entry point for everything about the LLM backend.

Module 2 (health-signal extraction) is the only part of this skill that depends
on a model, and that model is expected to change — a hosted API while the Spark
endpoint is not ready, then a self-hosted Qwen on the same machine.  Three
questions come up every single time it changes:

``connect``
    Where is the server, and what is the model actually called?  On a Spark box
    the usual failure is not a bad model: it is ``LLM_BASE_URL`` pointing at a
    port nothing listens on, or ``LLM_MODEL`` not matching the name the server
    reports (vLLM then answers 404 ``The model X does not exist``).

``verify``
    Does the new backend still produce the JSON this module needs?  Drives the
    real :class:`HealthExtractor`, not a hand-built request, because the
    extractor's prompt, timeout, retry and JSON-repair paths are all part of the
    contract.

``stability``
    Is that result reproducible, or did one green run get lucky?  Separates two
    very different failures: a case that *flips* between rounds means the prompt
    is under-specified; a case that is *consistently* wrong means the label or
    the model is at fault.  A single accuracy number cannot tell them apart.

These were three separate scripts until the repetition between them (provider
construction, mock guard, banner, JSON tail) outweighed the separation.

Usage::

    python scripts/check_model.py connect               # find and wire up a local server
    python scripts/check_model.py connect --write-env   # ...and save it to .env
    python scripts/check_model.py verify                # grade the configured endpoint
    python scripts/check_model.py verify --list-models  # just show what the server serves
    python scripts/check_model.py stability --rounds 5
    python scripts/check_model.py all                   # verify + stability

Every subcommand exits non-zero when it fails, so any of them can gate a
rehearsal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx  # noqa: E402

from implicit_health_triage.extractor import HealthExtractor  # noqa: E402
from implicit_health_triage.llm.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    build_llm_provider,
)
from implicit_health_triage.schemas import HealthType, Severity  # noqa: E402
from implicit_health_triage.settings import Settings, get_settings  # noqa: E402

ENV_PATH = PROJECT_ROOT / ".env"
CORPUS = PROJECT_ROOT / "tests" / "cases" / "extraction_cases.jsonl"

#: Ports a local OpenAI-compatible server is likely to be on. 8000 is the vLLM
#: default, 30000 is SGLang's, 11434 is Ollama's, 8001/8080 are common overrides.
DEFAULT_PORTS = (8000, 8001, 8080, 30000, 11434)

#: The image's own example — the smoke test uses the sentence the demo shows.
SMOKE_TEXT = "今天早上起来腿沉得很"

#: If a backend cannot reproduce this one exactly, the demo's headline scenario
#: is not safe to show.
IMAGE_CASE = {
    "text": SMOKE_TEXT,
    "type": HealthType.SYMPTOM.value,
    "severity": Severity.MODERATE.value,
}

#: One case per remaining health type, so a backend that collapses everything
#: into a single label is caught rather than passing on a single lucky answer.
#:
#: The mobility case is deliberately unambiguous: the prompt defines 行动能力 as
#: 走路不稳/容易跌倒 and files 乏力 under the generic 身体不适, so a sentence like
#: "腿脚没劲，走两步就喘" is *correctly* generic and must not be used here.
SPREAD_CASES = [
    ("昨晚翻来覆去睡不着", HealthType.SLEEP.value),
    ("膝盖疼得下不了楼", HealthType.PAIN.value),
    ("这两天不想吃饭", HealthType.APPETITE.value),
    ("这两天走路不太稳，得扶着墙走", HealthType.MOBILITY.value),
    ("早上那药我吃过了", HealthType.MEDICATION.value),
]

#: Must never be extracted as a health signal — it is a non-health turn.
PLAIN_CASE = "今天楼下花开了。"


# ---------------------------------------------------------------------------
# Shared plumbing — the reason these three used to be one script each
# ---------------------------------------------------------------------------


def add_endpoint_args(parser: argparse.ArgumentParser) -> None:
    """Flags that let any subcommand bypass ``.env`` for one run."""

    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--model", help="model name as the endpoint knows it")
    parser.add_argument("--api-key", help="bearer token, if the endpoint needs one")
    parser.add_argument(
        "--json-mode",
        action="store_true",
        help="send response_format=json_object (vLLM/DeepSeek honour it)",
    )
    parser.add_argument(
        "--disable-thinking",
        action="store_true",
        help="send chat_template_kwargs={'enable_thinking': False} (Qwen3 on vLLM)",
    )


def build_provider(args: argparse.Namespace):
    """Explicit flags win; otherwise fall back to the configured provider."""

    if getattr(args, "base_url", None) or getattr(args, "model", None):
        settings = get_settings()
        return (
            OpenAICompatibleProvider(
                base_url=args.base_url or settings.llm_base_url,
                model=args.model or settings.llm_model,
                api_key=args.api_key or settings.llm_api_key,
                timeout_seconds=settings.llm_timeout_seconds,
                use_json_mode=args.json_mode,
                disable_thinking=args.disable_thinking,
            ),
            "command line",
        )

    return build_llm_provider(), "environment / .env"


def print_banner(title: str, provider, source: str) -> None:
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)
    print(f"  provider class : {type(provider).__name__}")
    print(f"  configured via : {source}")
    for attr, label in (("_model", "model"), ("_use_json_mode", "json mode")):
        if hasattr(provider, attr):
            print(f"  {label:<14} : {getattr(provider, attr)}")
    if hasattr(provider, "_client"):
        print(f"  base url       : {provider._client.base_url}")
    print()


def is_mock(provider) -> bool:
    return type(provider).__name__ == "MockLLMProvider"


def explain_mock() -> None:
    print("  This is the offline mock, not a real endpoint.")
    print("  Set LLM_PROVIDER=openai_compatible and LLM_BASE_URL/LLM_MODEL,")
    print("  or pass --base-url and --model. See .env.example.")


def print_summary(payload: dict) -> None:
    """Machine-readable tail, so a rehearsal can diff two backends."""

    print()
    print("SUMMARY " + json.dumps(payload, ensure_ascii=False))


async def list_models(provider) -> tuple[list[str], str | None]:
    """Ask the endpoint which models it serves.

    Returns ``(names, error)``.  A server that does not implement ``/v1/models``
    is not fatal — some TensorRT-LLM builds omit it — so an empty list with no
    error means "could not determine", not "no models".
    """

    client = getattr(provider, "_client", None)
    if client is None:
        return [], None

    try:
        response = await client.get("/models", timeout=10.0)
    except Exception as error:  # noqa: BLE001 - any transport failure is one case
        return [], f"cannot reach the endpoint: {type(error).__name__}: {error}"

    if response.status_code >= 400:
        return [], f"GET /models returned HTTP {response.status_code}"

    try:
        return [item["id"] for item in response.json().get("data", [])], None
    except (KeyError, TypeError, ValueError):
        return [], None


async def probe_port(port: int, timeout: float = 2.0) -> list[str]:
    """Return the model names served on ``port``, or [] if nothing usable."""

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/v1/models")
    except Exception:  # noqa: BLE001 - every transport failure means "not here"
        return []

    if response.status_code >= 400:
        return []

    try:
        return [item["id"] for item in response.json().get("data", [])]
    except (KeyError, TypeError, ValueError):
        return []


def port_of(base_url: str) -> int | None:
    match = re.search(r":(\d+)", base_url or "")
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def write_env(base_url: str, model: str) -> None:
    """Rewrite the three interface lines in .env, preserving everything else."""

    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    updates = {
        "LLM_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": base_url,
        "LLM_MODEL": model,
    }

    for key, value in updates.items():
        pattern = re.compile(rf"^{key}=.*$", re.MULTILINE)
        text = (
            pattern.sub(f"{key}={value}", text)
            if pattern.search(text)
            else text + f"\n{key}={value}\n"
        )

    ENV_PATH.write_text(text, encoding="utf-8")
    print(f"  wrote {', '.join(updates)} to {ENV_PATH.name}")


async def cmd_connect(args: argparse.Namespace) -> int:
    settings = Settings()
    configured_url, configured_model = settings.llm_base_url, settings.llm_model
    ports = [int(p) for p in args.ports.split(",")] if args.ports else list(DEFAULT_PORTS)

    print("=" * 70)
    print("  Connect to a local model")
    print("=" * 70)
    print(f"  configured base url : {configured_url}")
    print(f"  configured model    : {configured_model or '(empty)'}")
    print()

    print("[1/3] Probing for an OpenAI-compatible server on 127.0.0.1")
    found: dict[int, list[str]] = {}
    for port in ports:
        names = await probe_port(port)
        if names:
            found[port] = names
            print(f"  [FOUND] port {port:<6} {len(names)} model(s)")
            for name in names:
                print(f"            {name}")
        else:
            print(f"  [ --  ] port {port:<6} nothing listening")

    if not found:
        print()
        print("  No local server found on any probed port.")
        print("  Start one first — for vLLM that is typically:")
        print()
        print("      vllm serve Qwen/Qwen3-8B --port 8000 \\")
        print("          --max-model-len 8192 --gpu-memory-utilization 0.85")
        print()
        print("  Then re-run this script. If your server listens elsewhere, use")
        print("  --ports, or set LLM_BASE_URL by hand in .env.")
        return 1

    print()
    print("[2/3] Reconciling LLM_MODEL")
    base_url, model = configured_url, configured_model
    port = port_of(base_url)

    if port not in found:
        port = next(iter(found))
        base_url = f"http://127.0.0.1:{port}/v1"
        print(f"  configured port is not serving; using port {port} instead")

    served = found[port]
    if model in served:
        print(f"  [ OK ] LLM_MODEL={model!r} is served on port {port}")
    elif len(served) == 1:
        model = served[0]
        print(f"  [FIX ] LLM_MODEL was {configured_model!r}; server serves {model!r}")
        print("         (vLLM serves the HuggingFace path by default)")
    else:
        print(f"  [FAIL] LLM_MODEL={model!r} is not among the served models:")
        for name in served:
            print(f"            {name}")
        print("         Pick one and set it in .env, then re-run with --write-env.")
        print("         Refusing to guess between several models.")
        return 1

    if args.write_env:
        write_env(base_url, model)

    print()
    print("[3/3] Smoke test — one real extraction")
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        model=model,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
        use_json_mode=settings.llm_json_mode,
        disable_thinking=settings.llm_disable_thinking,
    )
    extractor = HealthExtractor(provider)

    try:
        signal = await extractor.extract(SMOKE_TEXT)
    except Exception as error:  # noqa: BLE001 - report whatever the backend did
        print(f"  [FAIL] {type(error).__name__}: {str(error)[:200]}")
        print()
        print("  If this is HTTP 400 mentioning 'response_format', the server does")
        print("  not implement JSON mode — set LLM_JSON_MODE=false.")
        print("  If it mentions 'chat_template_kwargs', set LLM_DISABLE_THINKING=false.")
        return 1
    finally:
        await provider.aclose()

    print(f"  input    : {SMOKE_TEXT}")
    print(f"  type     : {signal.type}")
    print(f"  detail   : {signal.detail}")
    print(f"  severity : {signal.severity}")
    print()

    ok = bool(signal.type)
    print("=" * 70)
    if ok:
        print("  RESULT: CONNECTED")
        print(f"  {model} on port {port} satisfies the extraction contract.")
        if not args.write_env:
            print()
            print("  Next: python scripts/check_model.py verify     # full contract")
            print("        python scripts/check_model.py connect --write-env   # save config")
    else:
        print("  RESULT: NOT USABLE — the model returned an empty type")
    print("=" * 70)

    print_summary({"base_url": base_url, "model": model, "port": port, "type": signal.type})
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


async def cmd_verify(args: argparse.Namespace) -> int:
    provider, source = build_provider(args)
    print_banner("Verify an LLM endpoint for implicit-health-triage", provider, source)

    if is_mock(provider):
        explain_mock()
        return 1

    served, preflight_error = await list_models(provider)
    configured = getattr(provider, "_model", None)

    # `all` shares this function but does not define --list-models, so read it
    # defensively rather than assuming every caller's parser added the flag.
    if getattr(args, "list_models", False):
        if preflight_error:
            print(f"  [FAIL] {preflight_error}")
            await provider.aclose()
            return 1
        print(f"  The server reports {len(served)} model(s):")
        for name in served:
            mark = "  <-- LLM_MODEL" if name == configured else ""
            print(f"      {name}{mark}")
        if configured not in served:
            print()
            print(f"  LLM_MODEL is {configured!r}, which is NOT in that list.")
            print("  Copy one of the names above into LLM_MODEL.")
        await provider.aclose()
        return 0

    print("[0/3] Preflight")
    if preflight_error:
        print(f"  [FAIL] {preflight_error}")
        print()
        print("  A local server that is not reachable cannot be checked. Start it first;")
        print("  see docs/本地部署.md for the per-server command.")
        await provider.aclose()
        return 1

    if served and configured not in served:
        print(f"  [FAIL] LLM_MODEL={configured!r} is not served by this endpoint.")
        print(f"         The server reports: {', '.join(served)}")
        print("         Run with --list-models to see them, or use `connect`.")
        await provider.aclose()
        return 1

    print(f"  [ OK ] endpoint reachable, {len(served)} model(s) served, LLM_MODEL matches")
    print()

    extractor = HealthExtractor(provider)
    failures: list[str] = []

    async def run_one(text: str):
        started = time.perf_counter()
        try:
            return await extractor.extract(text), (time.perf_counter() - started) * 1000, None
        except Exception as error:  # noqa: BLE001 - report anything the backend does
            return None, (time.perf_counter() - started) * 1000, error

    print("[1/3] Image reference case")
    signal, elapsed, error = await run_one(IMAGE_CASE["text"])
    if error is not None:
        print(f"  [FAIL] {type(error).__name__}: {error}")
        failures.append(f"image case raised {type(error).__name__}")
    else:
        ok_type = signal.type == IMAGE_CASE["type"]
        ok_sev = signal.severity == IMAGE_CASE["severity"]
        print(f"  input    : {IMAGE_CASE['text']}")
        print(f"  type     : {signal.type!r}  (want {IMAGE_CASE['type']!r})"
              f"  {'OK' if ok_type else 'MISMATCH'}")
        print(f"  severity : {signal.severity!r}  (want {IMAGE_CASE['severity']!r})"
              f"  {'OK' if ok_sev else 'MISMATCH'}")
        print(f"  detail   : {signal.detail!r}")
        print(f"  latency  : {elapsed:.0f} ms")
        if not ok_type:
            failures.append("image case type mismatch")
        if not ok_sev:
            failures.append("image case severity mismatch")
    print()

    print("[2/3] Health-type spread")
    hit = 0
    for text, want in SPREAD_CASES:
        signal, elapsed, error = await run_one(text)
        if error is not None:
            print(f"  [FAIL] {text}  -> {type(error).__name__}: {error}")
            continue
        ok = signal.type == want
        hit += ok
        print(f"  [{'OK  ' if ok else 'MISS'}] want={want:<6} got={signal.type:<6}"
              f" {elapsed:>6.0f}ms  {text}")
    print(f"  -> {hit}/{len(SPREAD_CASES)}")
    if hit < len(SPREAD_CASES):
        failures.append(f"health-type spread {hit}/{len(SPREAD_CASES)}")
    print()

    print("[3/3] Non-health turn must come back empty")
    signal, elapsed, error = await run_one(PLAIN_CASE)
    if error is not None:
        print(f"  [FAIL] {type(error).__name__}: {error}")
        failures.append("plain case raised")
    else:
        ok = signal.type == HealthType.NONE.value
        print(f"  [{'OK  ' if ok else 'FAIL'}] type={signal.type!r}"
              f" (want {HealthType.NONE.value!r})  {elapsed:.0f}ms")
        if not ok:
            failures.append(f"non-health turn produced type={signal.type!r}")
    print()

    await provider.aclose()

    print("=" * 70)
    if failures:
        print(f"  RESULT: NOT READY — {len(failures)} problem(s)")
        for item in failures:
            print(f"      ! {item}")
        print()
        print("  These are *model* results, not rule results. A backend that fails")
        print("  here should not be used for the demo; the deterministic medication")
        print("  guardrail is unaffected and still passes either way.")
    else:
        print("  RESULT: READY")
        print(f"  ({type(provider).__name__} satisfies the extraction contract)")
    print("=" * 70)

    print_summary({
        "provider": type(provider).__name__,
        "model": configured,
        "failures": failures,
    })
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# stability
# ---------------------------------------------------------------------------


async def cmd_stability(args: argparse.Namespace) -> int:
    cases = [
        json.loads(line)
        for line in CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    provider, source = build_provider(args)
    if is_mock(provider):
        print_banner("Extraction stability", provider, source)
        print("  This is the offline mock — it is deterministic by construction,")
        print("  so a stability run proves nothing. Point LLM_PROVIDER at a real")
        print("  endpoint first (see .env.example).")
        return 1

    extractor = HealthExtractor(provider)

    print("=" * 70)
    print(f"  Extraction stability — {len(cases)} cases x {args.rounds} rounds")
    print(f"  model: {getattr(provider, '_model', '?')}")
    print("=" * 70)

    seen: dict[str, list[str]] = {}
    for round_index in range(args.rounds):
        for case in cases:
            signal = await extractor.extract(case["text"])
            seen.setdefault(case["text"], []).append(signal.type)
        print(f"  round {round_index + 1}/{args.rounds} done", flush=True)

    await provider.aclose()

    unstable: list[str] = []
    wrong: list[str] = []
    for case in cases:
        counts = Counter(seen[case["text"]])
        if len(counts) > 1:
            unstable.append(f"{dict(counts)} want={case['expected_type']}  {case['text']}")
        elif next(iter(counts)) != case["expected_type"]:
            wrong.append(
                f"got={next(iter(counts))} want={case['expected_type']}  {case['text']}"
            )

    print()
    if unstable:
        print("  UNSTABLE — the prompt does not pin this case down:")
        for item in unstable:
            print(f"      ! {item}")
    if wrong:
        print("  CONSISTENTLY WRONG — mislabelled case or real model limit:")
        for item in wrong:
            print(f"      ! {item}")

    total = len(cases)
    print()
    print(f"  calls                            : {total * args.rounds}")
    print(f"  unstable (flipped between rounds): {len(unstable)}")
    print(f"  consistently wrong               : {len(wrong)}")
    print(f"  stable and correct               : {total - len(unstable) - len(wrong)}/{total}")
    print("=" * 70)

    print_summary({
        "model": getattr(provider, "_model", None),
        "rounds": args.rounds,
        "unstable": len(unstable),
        "wrong": len(wrong),
    })
    return 1 if (unstable or wrong) else 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Everything about the LLM backend for implicit-health-triage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage::")[-1],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_connect = sub.add_parser("connect", help="find a local server and wire it up")
    p_connect.add_argument("--ports", help="comma-separated ports to probe")
    p_connect.add_argument(
        "--write-env", action="store_true", help="save the detected endpoint to .env"
    )
    p_connect.set_defaults(func=cmd_connect)

    p_verify = sub.add_parser("verify", help="grade the configured endpoint")
    add_endpoint_args(p_verify)
    p_verify.add_argument(
        "--list-models",
        action="store_true",
        help="only query /v1/models and print the names the server reports",
    )
    p_verify.set_defaults(func=cmd_verify)

    p_stability = sub.add_parser("stability", help="repeat the corpus across rounds")
    add_endpoint_args(p_stability)
    p_stability.add_argument(
        "--rounds", type=int, default=3, help="repetitions (default 3)"
    )
    p_stability.set_defaults(func=cmd_stability)

    p_all = sub.add_parser("all", help="verify, then stability")
    add_endpoint_args(p_all)
    p_all.add_argument("--rounds", type=int, default=3, help="stability rounds (default 3)")
    p_all.set_defaults(func=None)

    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "all":
        first = await cmd_verify(args)
        print()
        second = await cmd_stability(args)
        return 0 if (first == 0 and second == 0) else 1

    return await args.func(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
