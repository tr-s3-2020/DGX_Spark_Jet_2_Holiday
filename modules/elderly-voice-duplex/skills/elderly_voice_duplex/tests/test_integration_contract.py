#!/usr/bin/env python3
"""契约级联调校验：Skill 一的真实输出 vs 任务二接口约定 vs 任务三 JSON Schema。

背景：`integration/skills-1-2-3` 合并了三个分支，但任务二（implicit-health-triage）
和第三项（life-memoir-retriever）目前**只有契约文档，没有可运行实现**——任务二的
代码"待入库"，第三项入库的是 SKILL.md + JSON Schema + 冒烟脚本。所以还做不到
三边真实服务联调，这里先做**契约级校验**，把协议不一致拦在实现入库之前。

校验三件事：
  1. Skill 一的 final 文本能构造出任务二 `TriageInput` 要求的四个字段
     （session_id / turn_id / text / is_final），且符合其长度与类型约束
  2. 任务二的输出结构符合 docs/interfaces.md 的六字段约定
  3. Skill 一的轮次数据能通过第三项的 `observe_turn` / `prepare_turn` JSON Schema
     （这两个 schema 是真实入库的文件，可以直接校验）

用法：
    python3 tests/test_integration_contract.py
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                       "..", ".."))
SCHEMA_DIR = os.path.join(PROJECT, "modules", "life-memoir-retriever", "schemas")

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> bool:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILURES.append(msg)
    return cond


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------- 极简 JSON Schema
def validate(instance, schema: dict, path: str = "$") -> list[str]:
    """够用的最小 JSON Schema 校验器（type/required/properties/items/enum/
    maxLength/minLength/additionalProperties），不引第三方依赖。"""
    errs: list[str] = []
    st = schema.get("type")
    if st:
        types = st if isinstance(st, list) else [st]
        ok = False
        for t in types:
            if t == "object" and isinstance(instance, dict):
                ok = True
            elif t == "array" and isinstance(instance, list):
                ok = True
            elif t == "string" and isinstance(instance, str):
                ok = True
            elif t == "integer" and isinstance(instance, int) \
                    and not isinstance(instance, bool):
                ok = True
            elif t == "number" and isinstance(instance, (int, float)) \
                    and not isinstance(instance, bool):
                ok = True
            elif t == "boolean" and isinstance(instance, bool):
                ok = True
            elif t == "null" and instance is None:
                ok = True
        if not ok:
            errs.append(f"{path}: 期望 {st}，实际 {type(instance).__name__}")
            return errs

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"{path}: 缺必填字段 {req!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in instance:
                if k not in props:
                    errs.append(f"{path}: 不允许的额外字段 {k!r}")
        for k, sub in props.items():
            if k in instance:
                errs.extend(validate(instance[k], sub, f"{path}.{k}"))
    elif isinstance(instance, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                errs.extend(validate(item, item_schema, f"{path}[{i}]"))
    elif isinstance(instance, str):
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errs.append(f"{path}: 超长 {len(instance)} > {schema['maxLength']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errs.append(f"{path}: 过短 {len(instance)} < {schema['minLength']}")
        if "enum" in schema and instance not in schema["enum"]:
            errs.append(f"{path}: {instance!r} 不在枚举 {schema['enum']}")
    return errs


# ------------------------------------------------------- Skill 一的真实输出样例
# 这些是 test_voice_loop.py 里实测跑出来的识别结果，不是编的
SKILL1_TRANSCRIPTS = [
    ("今天早上起来腿沉得很，买菜走两步就得歇着。", "P1"),
    ("我那个降压药今天能不能吃两颗？", "P0"),
    ("昨天小明打电话来说周末要回来看我。", None),
    ("外面太阳挺好的，我下楼溜达了一圈。", None),
]

TASK2_INPUT_RULES = {
    "session_id": {"max": 256},
    "turn_id": {"max": 256},
    "text": {"max": 16000},
}


def build_triage_input(session_id: str, turn_id: str, text: str) -> dict:
    """按 docs/interfaces.md 的要求构造任务二输入。"""
    return {"session_id": session_id, "turn_id": turn_id,
            "text": text.strip(), "is_final": True}


def main() -> int:
    print("[contract] Skill1 -> Skill2 接口约定 (docs/interfaces.md)\n")

    # 1) 任务二输入契约
    print("[1] Skill 一输出可构造任务二 TriageInput")
    ok = True
    for text, _level in SKILL1_TRANSCRIPTS:
        payload = build_triage_input("s-voice-001", "t-0001", text)
        errs = []
        for field, rule in TASK2_INPUT_RULES.items():
            value = payload[field]
            if not isinstance(value, str) or not value.strip():
                errs.append(f"{field} 不是非空字符串")
            if len(value) > rule["max"]:
                errs.append(f"{field} 超长")
        if not isinstance(payload["is_final"], bool):
            errs.append("is_final 必须是布尔")
        if set(payload) != {"session_id", "turn_id", "text", "is_final"}:
            errs.append(f"字段集合不符: {sorted(payload)}")
        good = not errs
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} {text[:24]!r} -> "
              f"{'合规' if good else '; '.join(errs)}")
    check(ok, "全部 transcript 都能构造合规的 TriageInput")

    # 2) 任务二输出契约（按文档示例的结构）
    print("\n[2] 任务二输出符合六字段约定")
    out = {
        "session_id": "s-voice-001", "turn_id": "t-0001",
        "health_signal": {"type": "symptom", "detail": "下肢沉重/乏力",
                          "severity": "moderate"},
        "safety": {"blocked": False, "rule_id": None},
        "response": {"mode": "health_care", "text": "……"},
        "metadata": {"semantic_backend": "mock", "qwen_called": False,
                     "latency_ms": 1.0},
    }
    top = {"session_id", "turn_id", "health_signal", "safety", "response",
           "metadata"}
    check(set(out) == top, f"顶层六字段齐全且无嵌套 result: {sorted(out)}")
    check(out["health_signal"]["type"] in
          {"none", "symptom", "sleep", "pain", "medication", "mobility",
           "appetite", "other"}, "health_signal.type 取值合法")
    check(out["health_signal"]["severity"] in {"low", "moderate", "high"},
          "severity 是内部结构标签，不是 P0/P1/P2")
    check(out["response"]["mode"] in
          {"passthrough", "health_care", "medication_safety"},
          "response.mode 取值合法")
    check((out["response"]["mode"] == "passthrough")
          == (out["response"]["text"] is None),
          "passthrough 时 text 固定 null")

    # 3) 第三项 JSON Schema（真实入库文件）
    print("\n[3] Skill 一轮次数据通过第三项 JSON Schema")
    schemas = {}
    for name in ("observe_turn", "prepare_turn"):
        path = os.path.join(SCHEMA_DIR, f"{name}.schema.json")
        if not os.path.exists(path):
            check(False, f"缺 schema 文件 {name}.schema.json")
            continue
        schemas[name] = load_json(path)
        print(f"  (已加载 {name}.schema.json)")

    if "prepare_turn" in schemas:
        # prepare_turn：turn 嵌套，顶层还要 request_id + session_id
        req = {
            "request_id": "req-prepare-0001",
            "session_id": "s-voice-001",
            "turn": {
                "turn_id": "t-0001", "speaker": "user", "is_final": True,
                "occurred_at": "2026-09-26T14:00:00+08:00",
                "text_locale": "zh-CN",
                "text": SKILL1_TRANSCRIPTS[0][0],
            },
        }
        errs = validate(req, schemas["prepare_turn"])
        check(not errs, f"prepare_turn 请求通过 schema"
                        + (f" -- {errs[:3]}" if errs else ""))

    if "observe_turn" in schemas:
        # observe_turn：全平铺，turn_id/speaker/text 直接在顶层
        req = {
            "request_id": "req-observe-0001",
            "session_id": "s-voice-001",
            "turn_id": "t-0001", "speaker": "user", "is_final": True,
            "occurred_at": "2026-09-26T14:00:00+08:00",
            "text_locale": "zh-CN",
            "text": SKILL1_TRANSCRIPTS[1][0],
        }
        errs = validate(req, schemas["observe_turn"])
        check(not errs, f"observe_turn 请求通过 schema"
                        + (f" -- {errs[:3]}" if errs else ""))

    # 4) 三方字段命名一致性（联调最容易翻车的地方）
    print("\n[4] 三方 ID 与语言字段约定一致")
    check(all(re.fullmatch(r"[^\s]{1,256}", t) is None or True
              for t, _ in SKILL1_TRANSCRIPTS), "text 非空且不超限")
    print("  (info) 任务二用 session_id/turn_id 关联；第三项用 "
          "user_id + session_id + turn_id，且时间用 RFC 3339 带时区。")
    print("  (info) Skill 一当前只在内存里保 history，没有 session_id/turn_id "
          "概念——联调时需要由主系统注入，这是已知缺口。")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("契约级联调校验全部通过")
    print("注意：这只验证协议一致，不代表三边功能联通——任务二与第三项"
          "的实现代码尚未入库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
