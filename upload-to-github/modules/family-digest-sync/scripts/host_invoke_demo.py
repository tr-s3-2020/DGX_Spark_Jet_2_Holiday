"""宿主调用演示：证明这个 Skill 不只"脚本能跑"，而是能被 Agent 发现并调用。

产出：examples/host-invocation-log.md —— 一次完整的宿主调用留痕，可直接放进提交材料。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.dirname(HERE)
SRC = os.path.join(MODULE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from family_digest import FamilyDigestService  # noqa: E402
from family_digest.adapters import MockChannel  # noqa: E402
from family_digest.store import JsonStore  # noqa: E402

EXAMPLES = os.path.join(MODULE, "examples")
SCENARIOS = [
    ("P0 用药/安全风险", "scenario_p0_medication.json"),
    ("P1 跨日趋势劣化", "scenario_p1_trend.json"),
    ("P2 日常摘要", "scenario_p2_daily.json"),
    ("降级：模型未稳定返回", "scenario_degraded.json"),
]


def read_skill_frontmatter() -> dict:
    with open(os.path.join(MODULE, "SKILL.md"), "r", encoding="utf-8") as f:
        head = f.read(400)
    name = re.search(r"^name:\s*(.+)$", head, re.M)
    desc = re.search(r"^description:\s*(.+)$", head, re.M)
    return {"name": name.group(1).strip() if name else "",
            "description": (desc.group(1).strip() if desc else "")[:120]}


def run_scenario(svc: FamilyDigestService, scenario: dict) -> list[tuple[str, dict]]:
    steps: list[tuple[str, dict]] = []
    svc.execute("set_consent", {"elder_id": scenario["elder_id"],
                                "scopes": scenario.get("consents", [])})
    steps.append(("set_consent", {"elder_id": scenario["elder_id"],
                                  "scopes": scenario.get("consents", [])}))
    rec = svc.execute("record_signals", scenario)
    steps.append(("record_signals",
                  {"health_signals": len(scenario.get("health_signals", [])),
                   "safety_events": len(scenario.get("safety_events", []))}))
    steps.append(("→ record_signals 结果", rec))

    built = svc.execute("build_daily_digest", {
        "elder_id": scenario["elder_id"], "date": scenario["date"],
        "elder_name": scenario.get("elder_name", "老人"),
        "chronicle": scenario.get("chronicle"),
    })
    steps.append(("build_daily_digest", built))
    if built["status"] in ("ok", "degraded"):
        disp = svc.execute("dispatch_digest",
                           {"card_id": built["data"]["card_id"], "to": "女儿"})
        steps.append(("dispatch_digest", disp))
    return steps


def main() -> int:
    meta = read_skill_frontmatter()
    lines = [
        "# 宿主调用留痕",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 技能：{meta['name']}",
        f"- 描述：{meta['description']}……",
        "- 宿主：本地 Python 宿主（模拟主控 Agent）",
        "- 存储：var/host-demo.json；渠道：MockChannel（可替换为真实渠道）",
        "",
        "以下每一步都是宿主通过统一入口 `FamilyDigestService.execute(operation, request)` 的**实际调用与真实返回**，不是手写样例。",
        "",
    ]

    for title, filename in SCENARIOS:
        with open(os.path.join(EXAMPLES, filename), "r", encoding="utf-8") as f:
            scenario = json.load(f)
        # 每个场景独立存储：场景之间不能互相污染（尤其 P0 会盖住后面的分级）
        store_path = os.path.join(MODULE, "var", f"host-demo-{filename.stem if hasattr(filename, 'stem') else filename[:-5]}.json")
        if os.path.exists(store_path):
            os.remove(store_path)
        svc = FamilyDigestService(store=JsonStore(store_path), channel=MockChannel())
        lines.append(f"## {title} —— `{filename}`")
        lines.append("")
        for label, payload in run_scenario(svc, scenario):
            lines.append(f"**调用** `{label}`")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
        if svc.channel.sent:
            last = svc.channel.sent[-1]
            lines.append("**家属实际收到的内容**")
            lines.append("")
            lines.append("```text")
            lines.append(last["body"])
            lines.append("```")
            lines.append("")

    out = os.path.join(EXAMPLES, "host-invocation-log.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"已写入调用留痕：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
