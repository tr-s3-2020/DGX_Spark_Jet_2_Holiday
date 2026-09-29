"""四模块串联冒烟：用队友的**真实报文格式**喂 D，证明 D 吃得下 A/B/C 的输出。

不是假装串起来 —— 输入用的是：
- B：团队库 `docs/task2-collaboration-handoff` 里那份 implicit-health-triage 样例
  （原样拷到 examples/upstream_b_triage_sample.json），格式是 B 真实的 HTTP 响应体。
- C：按 C 的 API.md 第 3.8 节写的 get_chronicle envelope（content 嵌套形态）。
- A：按 A 的 server.py 抛出的 {"safety": "P0"} WebSocket 消息形态。

运行：
    python scripts/chain_smoke.py
    python scripts/chain_smoke.py --with-a-safety   # 叠加 A 的 P0 场景
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from family_digest import FamilyDigestService, Policy  # noqa: E402
from family_digest.adapters import MockChannel  # noqa: E402
from family_digest.adapters.upstream import (  # noqa: E402
    normalize_a_safety,
    normalize_b_output,
    normalize_c_chronicle,
)
from family_digest.cards import render_plain_text  # noqa: E402
from family_digest.store import JsonStore  # noqa: E402

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(MODULE, "examples")
STORE = os.path.join(MODULE, "var", "chain-smoke.json")
LOG = os.path.join(EXAMPLES, "chain-smoke-log.md")

ELDER = "e001"
TODAY = datetime(2026, 9, 28, 9, 0, 0)


def load_b_cases() -> list:
    with open(os.path.join(EXAMPLES, "upstream_b_triage_sample.json"), "r", encoding="utf-8") as f:
        return json.load(f)["cases"]


def b_payloads(cases, skip_blocked: bool = False) -> list:
    """只取 B 真正有 triage 结果的用例；ignored/422 的一律不喂给 D。"""
    out = []
    for c in cases:
        subset = c.get("expected_output_subset")
        if not subset:
            continue  # status=ignored 或 422：B 没产出健康信号，D 不能拿它编故事
        if skip_blocked and (subset.get("safety") or {}).get("blocked"):
            continue  # 去掉用药阻断，用来验证「趋势再差也只到 P1」
        out.append(subset)
    return out


def c_envelope_ready() -> dict:
    """C 的 get_chronicle 真实 envelope（content 嵌套）。"""
    return {
        "status": "ok",
        "data": {
            "state": "ready",
            "view_version": "v2026-09-28",
            "based_on_memory_revision": 17,
            "format": "structured",
            "content": {
                "items": [
                    {"title": "1978 年在纺织厂当学徒", "time": "1978", "source": "self_report"},
                    {"title": "孙女上周来看她", "time": "2026-09-21", "source": "conversation"},
                ],
                "narrative": "王奶奶常提起年轻时在纺织厂的日子，也说孙女上周来陪她包了饺子。",
            },
            "warnings": [],
        },
    }


def c_envelope_degraded() -> dict:
    return {
        "status": "degraded",
        "data": {
            "state": "not_ready",
            "view_version": None,
            "based_on_memory_revision": None,
            "format": "structured",
            "content": None,
            "warnings": ["CHRONICLE_NOT_READY"],
        },
    }


def run(with_a_safety: bool, chronicle_mode: str, skip_blocked: bool = False) -> dict:
    if os.path.exists(STORE):
        os.remove(STORE)
    store = JsonStore(STORE)
    svc = FamilyDigestService(store=store, channel=MockChannel(), policy=Policy())

    trace = []

    # 1. 家属可见范围（硬门禁）
    svc.execute("set_consent", {
        "elder_id": ELDER,
        "scopes": ["health_summary", "medication_safety", "chronicle"],
    })
    trace.append(("set_consent", {"elder_id": ELDER,
                                  "scopes": ["health_summary", "medication_safety", "chronicle"]}))

    # 2. B 的报文 -> D
    records = [normalize_b_output(p, elder_id=ELDER, occurred_at=TODAY)
               for p in b_payloads(load_b_cases(), skip_blocked)]
    # 造两天历史，验证跨日趋势真的会触发
    for days_ago, detail in ((1, "下肢沉重/乏力"), (2, "下肢沉重/乏力")):
        r = normalize_b_output(
            {"session_id": f"s{days_ago}", "turn_id": "t9",
             "health_signal": {"type": "symptom", "detail": detail, "severity": "moderate"},
             "safety": {"blocked": False, "rule_id": None},
             "response": {"mode": "health_care", "text": ""},
             "metadata": {"semantic_backend": "mock", "qwen_called": False}},
            elder_id=ELDER, occurred_at=TODAY - timedelta(days=days_ago),
        )
        records.append(r)

    res = svc.execute("record_signals", {
        "health_signals": [r.model_dump(mode="json") for r in records],
    })
    trace.append(("record_signals", res))

    # 3. A 的 safety -> D
    if with_a_safety:
        a = normalize_a_safety(
            {"type": "final", "safety": "P0", "turn_id": "t7", "session_id": "s1",
             "text": "我那个降压药今天能不能吃两颗？"},
            elder_id=ELDER, occurred_at=TODAY,
        )
        res_a = svc.execute("record_signals", {
            "safety_events": [a.model_dump(mode="json")],
        })
        trace.append(("record_signals(A)", res_a))

    # 4. C 的 chronicle -> D
    envelope = c_envelope_ready() if chronicle_mode == "ready" else c_envelope_degraded()
    chronicle = normalize_c_chronicle(envelope)

    # 5. 生成 + 推送
    card_res = svc.execute("build_daily_digest", {
        "elder_id": ELDER, "date": "2026-09-28", "elder_name": "王奶奶",
        "chronicle": chronicle.model_dump(mode="json"),
    })
    trace.append(("build_daily_digest", card_res))

    card_id = card_res["data"]["card_id"]
    disp = svc.execute("dispatch_digest", {"card_id": card_id, "to": "女儿"})
    trace.append(("dispatch_digest", disp))

    final = svc.execute("get_digest", {"card_id": card_id})
    return {
        "trace": trace,
        "card": final["data"],
        "chronicle_mode": chronicle_mode,
        "with_a_safety": with_a_safety,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-a-safety", action="store_true")
    ap.add_argument("--chronicle", choices=["ready", "degraded"], default="ready")
    ap.add_argument("--skip-b-blocked", action="store_true",
                    help="丢掉 B 的用药阻断用例，用来验证趋势只会到 P1")
    args = ap.parse_args()

    r = run(args.with_a_safety, args.chronicle, args.skip_b_blocked)
    card = r["card"]

    lines = [
        "# 四模块串联冒烟日志（A/B/C 真实报文格式 -> D）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 输入来源：B=团队库 implicit-health-triage 样例；C=API.md 3.8 的 get_chronicle envelope；"
        f"A=server.py 的 `{{\"safety\": \"P0\"}}` 消息",
        f"- C 状态：{r['chronicle_mode']}；叠加 A 的 P0：{r['with_a_safety']}；去掉 B 的用药阻断：{args.skip_b_blocked}",
        "",
        "## 调用轨迹",
        "",
        "```json",
        json.dumps([{"op": k, "result": v} for k, v in r["trace"]],
                   ensure_ascii=False, indent=2),
        "```",
        "",
        "## 家属实际收到的内容",
        "",
        "```text",
        render_plain_text(__import__("family_digest.models", fromlist=["DigestCard"]).DigestCard(**card)),
        "```",
        "",
        f"档位：`{card['tier']}`　采集状态：`{card['acquisition']}`　卡片状态：`{card['status']}`",
    ]
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"档位={card['tier']} 采集={card['acquisition']} 状态={card['status']}")
    print("---")
    print(render_plain_text(__import__("family_digest.models", fromlist=["DigestCard"]).DigestCard(**card)))
    print(f"---\n日志已写入 {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
