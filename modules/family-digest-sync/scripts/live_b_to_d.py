"""真跑 B 的代码，把它的**真实输出**喂给 D。

不是 mock、不是手抄样例 —— 这个脚本用 subprocess 起 B 的 CLI
（``implicit_health_triage.task2.cli``），拿它当场吐的 JSON，归一化后跑 D 全链路。

B 需要 ``nemoguardrails`` 才能走完整围栏；这里设 ``TRIAGE_GUARDRAILS_ENABLED=false``
让它退到纯 Python 规则（``MedicationSafetyPolicy``），语义后端用 mock，
因此**不需要 GPU、不需要模型服务、不需要网络**。

用法：
    set IMPLICIT_TRIAGE_HOME=C:\\Users\\16324\\Desktop\\NV\\...\\implicit-health-triage
    python scripts/live_b_to_d.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(MODULE, "src"))

from family_digest import FamilyDigestService, Policy  # noqa: E402
from family_digest.adapters import MockChannel  # noqa: E402
from family_digest.adapters.upstream import (  # noqa: E402
    b_output_is_ignored,
    normalize_b_output,
)
from family_digest.cards import render_plain_text  # noqa: E402
from family_digest.models import DigestCard  # noqa: E402
from family_digest.store import JsonStore  # noqa: E402

STORE = os.path.join(MODULE, "var", "live-b-to-d.json")
LOG = os.path.join(MODULE, "examples", "live-b-to-d-log.md")
ELDER = "e001"

# 三句真话：一句用药风险（P0 源头）、一句体征（趋势素材）、一句纯闲聊
UTTERANCES = [
    ("t1", "我降压药今天能不能吃两颗？"),
    ("t2", "今天早上起来腿沉得很，买菜走两步就得歇着。"),
    ("t3", "今天菜市场青菜挺便宜。"),
    ("t4", "这两天晚上翻来覆去睡不着。"),
]


def find_b_home() -> str | None:
    env = os.environ.get("IMPLICIT_TRIAGE_HOME")
    if env and os.path.isdir(env):
        return env
    for base in (
        os.path.join(os.path.expanduser("~"), "Desktop", "NV"),
        os.path.join(MODULE, "..", "..", ".."),
    ):
        if not os.path.isdir(base):
            continue
        for root, dirs, _ in os.walk(base):
            if os.path.basename(root) == "implicit-health-triage" and "src" in dirs:
                return root
            dirs[:] = [d for d in dirs if not d.startswith(".")]
    return None


def run_b(python: str, home: str, text: str, turn_id: str) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.path.join(home, "src"),
        "TRIAGE_GUARDRAILS_ENABLED": "false",   # 退到纯规则，免装 nemoguardrails
        "SEMANTIC_BACKEND": "mock",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [python, "-m", "implicit_health_triage.task2.cli", text,
         "--session-id", "live", "--turn-id", turn_id],
        cwd=home, env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"B 退出码 {proc.returncode}: {proc.stderr[-500:]}")
    out = proc.stdout.strip()
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"B 没有输出 JSON: {out[:300]}")
    return json.loads(out[start:])


def main() -> int:
    home = find_b_home()
    if not home:
        print("未找到 B 的 implicit-health-triage 目录，设置 IMPLICIT_TRIAGE_HOME 重试。")
        return 2
    print(f"B 目录：{home}")

    if os.path.exists(STORE):
        os.remove(STORE)
    svc = FamilyDigestService(store=JsonStore(STORE), channel=MockChannel(), policy=Policy())
    svc.execute("set_consent", {
        "elder_id": ELDER,
        "scopes": ["health_summary", "medication_safety", "chronicle"],
    })

    stamp = datetime(2026, 9, 28, 9, 0, 0)
    raw_log, records = [], []
    for turn_id, text in UTTERANCES:
        raw = run_b(sys.executable, home, text, turn_id)
        raw_log.append({"utterance": text, "b_raw_output": raw})
        if b_output_is_ignored(raw):
            continue
        rec = normalize_b_output(raw, elder_id=ELDER, occurred_at=stamp)
        records.append(rec)
        print(f"  {text[:18]:<20} -> type={rec.signal_type.value:<12} "
              f"severity={rec.severity.value:<9} blocked={rec.safety_blocked}")

    # 补两天历史，验证跨日趋势（B 只看单句，历史只有 D 有）
    for days, tid, detail, stype in (
        (1, "h1", "下肢沉重/乏力", "symptom"),
        (2, "h2", "走路发沉", "pain"),   # 故意换子类，验证按组而非精确值
    ):
        records.append(normalize_b_output(
            {"session_id": "hist", "turn_id": tid,
             "health_signal": {"type": stype, "detail": detail, "severity": "moderate"},
             "safety": {"blocked": False, "rule_id": None},
             "response": {"mode": "health_care", "text": ""},
             "metadata": {"semantic_backend": "mock", "qwen_called": False}},
            elder_id=ELDER, occurred_at=stamp.replace(day=28 - days),
        ))

    svc.execute("record_signals", {"health_signals": [r.model_dump(mode="json") for r in records]})
    built = svc.execute("build_daily_digest", {
        "elder_id": ELDER, "date": "2026-09-28", "elder_name": "王奶奶"})
    card_id = built["data"]["card_id"]
    disp = svc.execute("dispatch_digest", {"card_id": card_id, "to": "女儿"})
    card = DigestCard(**svc.execute("get_digest", {"card_id": card_id})["data"])

    lines = [
        "# B → D 真实联调日志",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- B 目录：`{home}`",
        "- B 运行方式：`python -m implicit_health_triage.task2.cli`（"
        "TRIAGE_GUARDRAILS_ENABLED=false 退到纯规则，SEMANTIC_BACKEND=mock）",
        "- 本日志里的 B 输出是**当场跑出来的**，不是手抄样例",
        "",
        "## B 的原始输出",
        "",
        "```json",
        json.dumps(raw_log, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 归一化后进入 D 的记录",
        "",
        "```json",
        json.dumps([r.model_dump(mode="json") for r in records], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 路由与推送",
        "",
        "```json",
        json.dumps({"routing": built.get("meta", {}).get("routing"),
                    "dispatch": disp}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 家属实际收到的内容",
        "",
        "```text",
        render_plain_text(card),
        "```",
        "",
        f"档位：`{card.tier.value}`　采集状态：`{card.acquisition.value}`　"
        f"卡片状态：`{card.status.value}`",
    ]
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("---")
    print(render_plain_text(card))
    print(f"---\n档位={card.tier.value} 采集={card.acquisition.value} 状态={card.status.value}")
    print(f"日志：{LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
