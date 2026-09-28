#!/usr/bin/env python3
"""Minimal HTTP client for implicit-health-triage.

**Depends on nothing but the Python standard library** — it does not import the
skill package at all. This is the copy-paste starting point for a neighbouring
module that only wants to talk to the HTTP interface.

Start the service first::

    cd implicit-health-triage
    uvicorn implicit_health_triage.api.app:app --host 127.0.0.1 --port 8080

Then::

    python examples/http_client.py                       # default 127.0.0.1:8080
    python examples/http_client.py 10.0.0.5:8080         # another host

Interactive API docs (all fields, enums, examples, try-it-out):
    http://<host>:8080/docs
Machine-readable OpenAPI 3.1 spec (generate a client in any language):
    http://<host>:8080/openapi.json
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8080"
URL = f"http://{BASE}/v1/triage"


def call(session_id: str, turn_id: str, text: str, is_final: bool = True) -> dict:
    """POST one turn and return the decoded JSON body."""

    payload = json.dumps(
        {"session_id": session_id, "turn_id": turn_id, "text": text, "is_final": is_final}
    ).encode("utf-8")

    request = urllib.request.Request(
        URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def handle(body: dict) -> None:
    """This is the whole integration surface: dispatch on the response shape."""

    # 1) is_final=false -> no "result" key at all
    if "result" not in body:
        print(f"    (被忽略：{body.get('status')})")
        return

    result = body["result"]
    mode = result["response_mode"]
    signal = result["health_signal"]

    print(f"    response_mode      = {mode}")
    print(f"    guardrail_triggered= {result['guardrail_triggered']}")
    print(f"    health_signal      = {json.dumps(signal, ensure_ascii=False)}")

    # 2) dispatch on response_mode
    if mode == "normal_chat":
        # response is "" — 闲聊交回你自己的对话系统，空串不要播报
        print("    → 走自己的闲聊回复")
    elif mode == "health_care":
        print(f"    → 送 TTS：{result['response']}")
    elif mode == "medication_safety":
        # 固定安全话术，必须原样播报，禁止其他模型补话
        print(f"    → 送 TTS（原样）：{result['response']}")


CASES = [
    ("s1", "t1", "今天楼下花开得挺漂亮。", True, "普通聊天 → normal_chat"),
    ("s1", "t2", "今天早上起来腿沉得很，买菜走两步就得歇着。", True, "隐式健康信号 → health_care"),
    ("s1", "t3", "我降压药今天能不能吃两颗？", True, "用药安全 → medication_safety"),
    ("s1", "t4", "早上的药我已经按时吃了。", True, "正常服药陈述 → 不该被拦"),
    ("s1", "t5", "我这个药……", False, "ASR 未成句 → ignored_partial"),
]


def main() -> int:
    print(f"服务地址: http://{BASE}\n")

    try:
        for session_id, turn_id, text, is_final, label in CASES:
            print(f"[{label}]")
            print(f"    输入: {text}")
            handle(call(session_id, turn_id, text, is_final))
            print()
    except urllib.error.URLError as error:
        print(f"连不上服务：{error}")
        print("请先启动： uvicorn implicit_health_triage.api.app:app --port 8080")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
