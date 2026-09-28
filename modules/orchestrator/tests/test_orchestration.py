#!/usr/bin/env python3
"""编排层的 pytest 用例。

覆盖四类：
  1. **数据流顺序与 ID 贯穿** —— session_id/turn_id 必须传到每个 skill
  2. **用药安全硬底线** —— skill2 的 medication_safety 必须覆盖模型回复；
     skill1 的正则围栏也必须能独立触发
  3. **降级** —— 任一 skill 挂掉/超时，通话必须继续，老人仍要听到回复
  4. **跨会话记忆** —— 上一通话说的事，下一通能取回（skill3 的核心卖点）

协议类用例用 `use_llm=False` 跑成确定性断言；只有标注 live 的用例打真模型。

跑法（在 modules/orchestrator/ 下）：
    ../../.venv-v019/bin/python -m pytest -q
    ../../.venv-v019/bin/python -m pytest -q -k medication
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

# 路径由 conftest.py 提前补好，这里直接 import
from orchestrator import Orchestrator, _json_safe  # noqa: E402

DOSE = "我那个降压药今天能不能吃两颗？"
SYMPTOM = "今天早上起来腿沉得很，买菜走两步就得歇着。"
DAILY = "外面太阳挺好的，我下楼溜达了一圈。"


# --------------------------------------------------------------- 测试辅助

class BoomSkill:
    """永远抛异常的假 skill，用于验证降级。"""

    async def handle(self, input_data):  # noqa: D102
        raise RuntimeError("skill exploded")


class FakeMemory:
    """记录调用、可预置返回值的假 skill3。"""

    def __init__(self, prepare_result=None):
        self.calls: list[tuple[str, dict]] = []
        self.prepare_result = prepare_result or {
            "status": "ok",
            "data": {"observation": {"session_version": 1},
                     "context": {"memories": [], "preferences": [],
                                 "match_status": "empty"}}}
        self.closed = False
        self.started = False

    async def start(self):  # noqa: D102
        self.started = True
        return self

    async def execute(self, operation, request, principal):  # noqa: D102
        self.calls.append((operation, request))
        if operation == "prepare_turn":
            return self.prepare_result
        return {"status": "ok", "data": {}}

    async def close(self):  # noqa: D102
        self.closed = True


class RecordingDigest:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, operation, request):  # noqa: D102
        self.calls.append((operation, request))
        return {"status": "ok", "data": {"accepted": 1}}


@pytest.fixture
def orch():
    """一个不碰磁盘、不碰网络的编排器（skill3/4 用假的，skill2 用真的）。"""
    o = Orchestrator(elder_id="e001", kin="your son", use_llm=False)
    mem = FakeMemory()
    o._memory = mem
    o._memory_principal = object()      # _mem_call 只透传，假实现不校验
    o._digest = RecordingDigest()
    o._triage = None                    # 用真的 skill2：它是纯本地 + mock 后端
    return o


def events_of(out) -> str:
    return " | ".join(out.events)


# ------------------------------------------------------- 1. 数据流与 ID 贯穿

def test_turn_runs_all_four_skills_in_order(orch):
    out = asyncio.run(orch.turn(DAILY))
    # 顺序：skill1 围栏 → skill3 记忆 → (生成) → skill2 分诊 → skill4 家属
    joined = events_of(out)
    assert "skill3_memories" in joined
    assert "skill2=" in joined
    assert "skill4=" in joined
    assert out.session_id and out.turn_id


def test_session_and_turn_id_reach_every_skill(orch):
    out = asyncio.run(orch.turn(DAILY))
    sid, tid = out.session_id, out.turn_id
    # skill3
    for op, req in orch._memory.calls:
        if op == "prepare_turn":
            assert req["session_id"] == sid
            assert req["turn"]["turn_id"] == tid
            assert req["turn"]["is_final"] is True
            assert req["turn"]["text"] == DAILY
    # skill2 / skill4 的请求体里也要有同一对 ID
    for _op, req in orch._digest.calls:
        assert req["session_id"] == sid
        assert req["turn_id"] == tid


def test_turn_id_increments(orch):
    asyncio.run(orch.turn(DAILY))
    out2 = asyncio.run(orch.turn(DAILY))
    assert out2.turn_id.endswith("0002")


# ------------------------------------------------- 2. 用药安全（硬底线）

@pytest.mark.parametrize("text", [DOSE])
def test_medication_safety_overrides_reply(orch, text):
    """skill2 判 medication_safety 时，回复必须被标准话术替换。"""
    out = asyncio.run(orch.turn(text))
    assert out.triage is not None
    assert out.triage["safety"]["blocked"] is True
    assert out.reply_source == "triage_safety"
    # 标准话术里不能出现任何剂量建议
    for banned in ("可以吃", "两颗", "没问题", "建议您吃"):
        assert banned not in out.reply


def test_voice_guard_also_fires_on_dose(orch):
    """skill1 的正则围栏要独立命中（与 skill2 是两层，不是一层）。"""
    out = asyncio.run(orch.turn(DOSE))
    assert out.safety_level == "P0"
    assert "skill1_guard=P0" in events_of(out)


def test_daily_chat_is_not_blocked(orch):
    out = asyncio.run(orch.turn(DAILY))
    assert out.triage["safety"]["blocked"] is False
    assert out.reply_source in ("voice", "stub", "fallback")


def test_symptom_routes_to_health_care(orch):
    out = asyncio.run(orch.turn(SYMPTOM))
    assert out.triage["health_signal"]["type"] == "symptom"
    assert out.triage["response"]["mode"] == "health_care"
    # skill1 的 P1 围栏也会命中，但不应升级成 P0
    assert out.safety_level == "P1"


# --------------------------------------------------------------- 3. 降级

def test_triage_failure_does_not_stop_the_call(orch):
    """skill2 挂掉不能影响老人听到回复。"""
    orch._triage = BoomSkill()
    out = asyncio.run(orch.turn(DAILY))
    assert out.reply, "老人仍然要听到回复"
    assert any(d.startswith("triage:") for d in out.degraded)
    assert "skill2=error" in events_of(out)


def test_memory_failure_does_not_stop_the_call(orch):
    orch._memory = BoomMemory = type("B", (), {
        "execute": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
        "close": lambda self: asyncio.sleep(0)})
    out = asyncio.run(orch.turn(DAILY))
    assert out.reply
    assert any(d.startswith("memory:") for d in out.degraded)


def test_digest_failure_does_not_stop_the_call(orch):
    class BoomDigest:
        def execute(self, *a, **k):
            raise RuntimeError("digest down")

    orch._digest = BoomDigest()
    out = asyncio.run(orch.turn(DAILY))
    assert out.reply, "家属侧挂掉更不能影响老人"
    assert any(d.startswith("digest:") for d in out.degraded)


def test_all_skills_down_still_returns_a_reply(orch):
    orch._triage = BoomSkill()
    orch._digest = type("B", (), {"execute": lambda *a, **k: (_ for _ in ()).throw(RuntimeError())})()
    out = asyncio.run(orch.turn(DAILY))
    assert out.reply
    assert len(out.degraded) >= 2


# --------------------------------------------------------- 4. 跨会话记忆

def test_memories_from_skill3_are_returned(orch):
    orch._memory.prepare_result = {
        "status": "ok",
        "data": {"match_status": "hit",
                 "context": {"memories": [], "match_status": "hit",
                             "preferences": [
                                 {"content": "老人在纺织厂工作过"}]}}}
    out = asyncio.run(orch.turn("我以前是做什么工作的？"))
    assert out.match_status == "hit"
    assert out.memories and "纺织厂" in out.memories[0]["content"]
    assert "skill3_memories=1" in events_of(out)


def test_preference_memories_are_not_dropped(orch):
    """skill3 把 preference 类条目放 preferences 而不是 memories。

    回归保护：曾因为只读 data.memories（且没嵌到 context 底下）把这类
    记忆全漏掉，用户实测"说过最喜欢泰坦尼克号，重进后助手说不知道"。
    """
    orch._memory.prepare_result = {
        "status": "ok",
        "data": {"context": {"match_status": "matched",
                             "memories": [],
                             "preferences": [
                                 {"kind": "preference",
                                  "content": "最喜欢看的电影是泰坦尼克号"}]}}}
    out = asyncio.run(orch.turn("我最喜欢看的电影是什么？"))
    assert out.match_status == "matched"
    assert out.memories, "preference 类记忆不能被丢掉"
    assert "泰坦尼克号" in out.memories[0]["content"]


def test_empty_memories_is_not_an_error(orch):
    out = asyncio.run(orch.turn(DAILY))
    assert out.match_status == "empty"
    assert not out.degraded


# ------------------------------------------------------------ 5. 工具函数

@pytest.mark.parametrize("raw,expected", [
    ("2026-09-28T10:16:30.471618Z", "2026-09-28T10:16:30.471618+00:00"),
    ("2026-09-28T10:16:30+00:00", "2026-09-28T10:16:30+00:00"),
    ("plain string", "plain string"),
    ({"a": ["2026-01-01T00:00:00Z"]}, {"a": ["2026-01-01T00:00:00+00:00"]}),
])
def test_json_safe_rewrites_z_suffix(raw, expected):
    """skill4 在 3.10 上解析不了 Z 后缀，编排层必须转换。"""
    assert _json_safe(raw) == expected


def test_json_safe_output_is_parseable_on_this_python():
    """转换后的字符串必须真能被本机 datetime 解析（回归保护）。"""
    from datetime import datetime
    fixed = _json_safe("2026-09-28T10:16:30.471618Z")
    datetime.fromisoformat(fixed)      # 3.10 上不抛异常即通过


# ------------------------------------------------------------ 6. live 用例

@pytest.mark.live
def test_live_full_chain_with_real_llm():
    """打真 Qwen3.6 的完整链路。跑：pytest -m live"""
    o = Orchestrator(elder_id="e001", use_llm=True)
    o._memory = FakeMemory()
    o._memory_principal = object()
    o._digest = RecordingDigest()
    asyncio.run(o.start())
    try:
        out = asyncio.run(o.turn("你好，今天天气不错。"))
        assert out.reply
        assert "skill2=" in events_of(out)
        assert "skill4=" in events_of(out)
        assert not out.degraded, f"联调链路出现降级: {out.degraded}"
    finally:
        asyncio.run(o.close())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
