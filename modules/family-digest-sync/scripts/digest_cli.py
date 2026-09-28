"""family-digest-sync 命令行入口。

用法：
    python scripts/digest_cli.py --scenario examples/scenario_p2_daily.json
    python scripts/digest_cli.py --scenario examples/scenario_p0_medication.json --channel console
    python scripts/digest_cli.py --scenario ... --store var/demo.json --reset
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from family_digest import FamilyDigestService  # noqa: E402
from family_digest.adapters import ConsoleChannel, MockChannel  # noqa: E402
from family_digest.store import JsonStore  # noqa: E402


def run(scenario_path: str, store_path: str | None, channel: str, reset: bool) -> int:
    with open(scenario_path, "r", encoding="utf-8") as f:
        scenario = json.load(f)

    if reset and store_path and os.path.exists(store_path):
        os.remove(store_path)

    chan = ConsoleChannel() if channel == "console" else MockChannel()
    service = FamilyDigestService(store=JsonStore(store_path), channel=chan)

    steps: list[tuple[str, dict]] = [
        ("set_consent", {
            "elder_id": scenario["elder_id"],
            "scopes": scenario.get("consents", []),
        }),
        ("record_signals", {
            "health_signals": scenario.get("health_signals", []),
            "safety_events": scenario.get("safety_events", []),
        }),
        ("build_daily_digest", {
            "elder_id": scenario["elder_id"],
            "date": scenario["date"],
            "elder_name": scenario.get("elder_name", "老人"),
            "chronicle": scenario.get("chronicle"),
        }),
    ]
    built_card_id = None
    for op, req in steps:
        res = service.execute(op, req)
        print(f"\n### {op}\n" + json.dumps(res, ensure_ascii=False, indent=2))
        if op == "build_daily_digest" and res["status"] in ("ok", "degraded"):
            built_card_id = res["data"]["card_id"]

    if built_card_id:
        res = service.execute("dispatch_digest", {"card_id": built_card_id, "to": "女儿"})
        print("\n### dispatch_digest\n" + json.dumps(res, ensure_ascii=False, indent=2))
        res = service.execute("get_digest", {"card_id": built_card_id})
        print("\n### get_digest\n" + json.dumps(res, ensure_ascii=False, indent=2))

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="family-digest-sync CLI")
    ap.add_argument("--scenario", required=True, help="场景 JSON 路径")
    ap.add_argument("--store", default=None, help="存储文件路径（默认模块内 var/）")
    ap.add_argument("--channel", default="mock", choices=["mock", "console"])
    ap.add_argument("--reset", action="store_true", help="运行前清空存储")
    args = ap.parse_args()
    return run(args.scenario, args.store, args.channel, args.reset)


if __name__ == "__main__":
    raise SystemExit(main())
