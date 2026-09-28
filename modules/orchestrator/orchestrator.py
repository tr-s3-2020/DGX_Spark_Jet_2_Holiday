#!/usr/bin/env python3
"""编排层：把 Skill 1/2/3/4 串成一次完整的老人通话。

四个 skill 各自独立开发、独立测试，这一层负责它们之间的顺序、ID 注入、
超时与降级。它是联调的主入口，也是主系统（或宿主 Agent）的参考实现。

一次通话的数据流
----------------
    老人说话
       │
       ▼
  [1] elderly-voice-duplex ──► final 文本（+ 自己的正则安全围栏）
       │                          若命中 P0/P1，直接用标准话术回复，仍继续往下传
       ▼
  [3] life-memoir-retriever ──► prepare_turn：取回相关记忆，注入回复上下文
       │                          （跨会话记忆；未命中就 match_status=empty）
       ▼
     Qwen3.6 生成回复（上下文里带上记忆）
       │
       ▼
  [2] implicit-health-triage ─► 从 final 文本提取健康信号 / 用药安全
       │                          返回 response.mode：passthrough / health_care
       │                          / medication_safety（后者覆盖回复）
       ▼
  [4] family-digest-sync ─────► 三档路由 P0/P1/P2 + 跨日趋势 + 家属卡片
                                  （只读 skill3，不写记忆；P0 立即通知）

设计要点
--------
1. **session_id / turn_id 由本层生成并贯穿四方**。Skill 1 原本只在内存里保
   history，没有 ID 概念；Skill 2/3 都要求这两个字段。这是联调的前置缺口。
2. **每个 skill 独立超时、独立降级**。任何一个挂掉不影响通话继续——
   老人听到回复优先于家属侧记录完整。
3. **Skill 2 的 medication_safety 覆盖 Skill 1 的回复**：用药风险是硬底线，
   模型生成的任何内容都不能盖过标准话术。
4. **Skill 3 只被 prepare_turn 读、被 Skill 4 只读消费**，本层不替它们写记忆；
   记忆写入由 Skill 3 自己的 observe_turn / close_session 后台 job 完成。

用法
----
    python3 tests/test_orchestration.py             # 全场景
    python3 tests/test_orchestration.py dose        # 只跑指定场景

依赖：四个 skill 各自的 venv 已装好；LLM 服务在 127.0.0.1:8000。
Skill 3 需要 HTTP 服务（8765）或同进程调用，本层用同进程。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
# 本文件在 modules/orchestrator/ 下，上两级就是项目根
PROJECT = os.path.abspath(os.path.join(HERE, "..", ".."))
MODULES = os.path.join(PROJECT, "modules")

# 四个 skill 的源码都加进 path（各自 venv 的依赖由调用方保证可用）。
# 注意 skill1 的包在 skills/ 下而不是 src/。
for _m in ("implicit-health-triage", "life-memoir-retriever",
           "family-digest-sync"):
    _src = os.path.join(MODULES, _m, "src")
    if os.path.isdir(_src):
        sys.path.insert(0, _src)
_voice_skills = os.path.join(MODULES, "elderly-voice-duplex", "skills")
if os.path.isdir(_voice_skills):
    sys.path.insert(0, _voice_skills)

TIMEOUT = {"triage": 5.0, "memory": 3.0, "digest": 5.0}


def _json_safe(value):
    """把 pydantic 的 JSON 序列化结果转成 Python 3.10 也能解析的形式。

    pydantic 的 model_dump(mode="json") 会把 UTC 时间写成 "...Z"，而
    Python 3.10 的 datetime.fromisoformat() 不认 Z 后缀（3.11 才支持）。
    skill4 的 _load_health/_load_safety 正是用 fromisoformat 解析，且它声明
    requires-python >=3.10 —— 这是 skill4 的兼容 bug，这里在编排层绕开，
    已向 skill4 负责人反馈。
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, str) and value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


@dataclass
class TurnOutcome:
    """一轮通话的完整结果，供测试与主系统观测。"""
    session_id: str
    turn_id: str
    transcript: str = ""
    reply: str = ""
    reply_source: str = ""          # voice | triage_safety | voice_p0_guard
    safety_level: str = ""          # Skill 1 的正则围栏分级
    memories: list[dict] = field(default_factory=list)
    match_status: str = ""
    triage: dict | None = None
    digest: dict | None = None
    events: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)


class Orchestrator:
    """一次老人通话的编排器。"""

    def __init__(self, *, elder_id: str = "e001", kin: str = "your son",
                 locale: str = "zh-CN", use_llm: bool = True):
        self.elder_id = elder_id
        self.kin = kin
        self.locale = locale
        self.use_llm = use_llm
        self.session_id = f"s-{uuid.uuid4().hex[:12]}"
        self.turn_no = 0
        self._triage = None
        self._memory = None
        self._digest = None
        self._llm = None

    # ------------------------------------------------------------ 惰性加载

    def triage(self):
        if self._triage is None:
            from implicit_health_triage.task2 import ImplicitHealthTriageSkill
            self._triage = ImplicitHealthTriageSkill()
        return self._triage

    def memory(self):
        if self._memory is None:
            from life_memoir.config import Settings, Principal
            from life_memoir.service import MemoryService
            settings = Settings(
                storage_path=os.path.join(PROJECT, ".cache", "orchestrator",
                                          "memory.sqlite3"))
            os.makedirs(os.path.dirname(settings.storage_path), exist_ok=True)
            self._memory = MemoryService(settings)
            self._memory_principal = Principal(
                "host", frozenset({self.elder_id}),
                frozenset({"host", "maintenance"}),
                frozenset({"consent", "review"}))
        return self._memory

    def digest(self):
        if self._digest is None:
            from family_digest import FamilyDigestService, Policy
            from family_digest.adapters import MockChannel
            from family_digest.store import JsonStore
            path = os.path.join(PROJECT, ".cache", "orchestrator",
                                "digest.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._digest = FamilyDigestService(store=JsonStore(path),
                                               channel=MockChannel(),
                                               policy=Policy())
            self._digest.execute("set_consent", {
                "elder_id": self.elder_id,
                "scopes": ["health_summary", "medication_safety",
                           "chronicle"]})
        return self._digest

    def llm(self):
        if self._llm is None:
            from elderly_voice_duplex.adapters.llm_vllm import VLLMBackend
            self._llm = VLLMBackend()
        return self._llm

    async def start(self):
        """接通电话：开 session、授权、清空轮次计数。"""
        self.memory()                      # 先建服务，否则 _mem_call 拿到 None
        await self._mem_call("set_policy", user_id=self.elder_id,
                             expected_version=0, consent_ref="consent",
                             grants={"long_term_memory": True,
                                     "profile_learning": True,
                                     "family_digest": True,
                                     "remote_analysis": False})
        await self._mem_call("open_session", user_id=self.elder_id,
                             session_id=self.session_id, locale=self.locale)
        self.turn_no = 0

    async def close(self):
        """挂断：关 session（触发 skill3 的后台提炼），再关服务。"""
        try:
            await self._mem_call("close_session", session_id=self.session_id,
                                 reason="completed")
        except Exception as exc:  # noqa: BLE001
            pass
        if self._memory is not None:
            await self._memory.close()

    # ------------------------------------------------------------ 一轮通话

    async def turn(self, transcript: str) -> TurnOutcome:
        self.turn_no += 1
        turn_id = f"t-{self.turn_no:04d}"
        out = TurnOutcome(session_id=self.session_id, turn_id=turn_id,
                          transcript=transcript)

        # ---- Skill 1 的正则安全围栏（在 LLM 之前）----
        from elderly_voice_duplex.prompts import check_safety, build_reply_prompt
        guard = check_safety(transcript, self.kin)
        if guard:
            out.safety_level = guard[0]
            out.reply = guard[1]
            out.reply_source = "voice_p0_guard"
            out.events.append(f"skill1_guard={guard[0]}")

        # ---- Skill 3：取回相关记忆，注入上下文 ----
        t0 = time.monotonic()
        try:
            prepared = await asyncio.wait_for(
                self._mem_call("prepare_turn", session_id=self.session_id,
                               turn={"turn_id": turn_id, "speaker": "user",
                                     "is_final": True,
                                     "occurred_at": _now_iso(),
                                     "text_locale": self.locale,
                                     "text": transcript}),
                timeout=TIMEOUT["memory"])
            data = prepared.get("data") or {}
            out.memories = data.get("memories") or []
            out.match_status = data.get("match_status", "")
            out.events.append(f"skill3_memories={len(out.memories)}"
                              f"({out.match_status})")
        except asyncio.TimeoutError:
            out.degraded.append("memory_timeout")
            out.events.append("skill3=timeout")
        except Exception as exc:  # noqa: BLE001
            out.degraded.append(f"memory:{type(exc).__name__}")
            out.events.append("skill3=error")
        out.timings["memory_ms"] = (time.monotonic() - t0) * 1000

        # ---- 生成回复（没被 skill1 围栏拦的情况下）----
        if not out.reply:
            if self.use_llm:
                t0 = time.monotonic()
                try:
                    system = build_reply_prompt(transcript, [], self.kin)
                    if out.memories:
                        system += ("\n\n老人以前说过这些相关的事，可以自然接上："
                                   + json.dumps(
                                       [m.get("summary") or m.get("text") or m
                                        for m in out.memories[:3]],
                                       ensure_ascii=False))
                    reply = await self.llm().complete(
                        system=system, history=[], user=transcript,
                        think=False)
                    out.reply = reply
                    out.reply_source = "voice"
                except Exception as exc:  # noqa: BLE001
                    out.reply = "诶，我没听清，您再说一遍好不好？"
                    out.reply_source = "fallback"
                    out.degraded.append(f"llm:{type(exc).__name__}")
                out.timings["llm_ms"] = (time.monotonic() - t0) * 1000
            else:
                out.reply = "(no-llm mode)"
                out.reply_source = "stub"

        # ---- Skill 2：健康分诊（可能覆盖回复）----
        t0 = time.monotonic()
        try:
            from implicit_health_triage.task2 import TriageInput
            tri = await asyncio.wait_for(
                self.triage().handle(TriageInput(
                    session_id=self.session_id, turn_id=turn_id,
                    text=transcript, is_final=True)),
                timeout=TIMEOUT["triage"])
            if tri is None:
                out.events.append("skill2=ignored")
            else:
                out.triage = {
                    "health_signal": tri.health_signal.model_dump(),
                    "safety": tri.safety.model_dump(),
                    "response": tri.response.model_dump(),
                    "metadata": tri.metadata.model_dump(),
                }
                out.events.append(
                    f"skill2={tri.response.mode}/{tri.health_signal.type}"
                    f"/blocked={tri.safety.blocked}")
                if tri.response.mode == "medication_safety":
                    # 用药安全是硬底线：标准话术覆盖任何模型生成
                    out.reply = tri.response.text
                    out.reply_source = "triage_safety"
                    out.safety_level = out.safety_level or "P0"
        except asyncio.TimeoutError:
            out.degraded.append("triage_timeout")
            out.events.append("skill2=timeout")
        except Exception as exc:  # noqa: BLE001
            out.degraded.append(f"triage:{type(exc).__name__}")
            out.events.append("skill2=error")
        out.timings["triage_ms"] = (time.monotonic() - t0) * 1000

        # ---- Skill 4：家属侧路由（不影响老人听到的回复）----
        # 用 skill4 自带的 normalize_* 适配器转换，不自己拼字段
        t0 = time.monotonic()
        try:
            from datetime import datetime, timezone
            from family_digest.adapters.upstream import (
                normalize_a_safety, normalize_b_output)
            stamp = datetime.now(timezone.utc)
            payload: dict = {"elder_id": self.elder_id,
                             "session_id": self.session_id,
                             "turn_id": turn_id,
                             "occurred_at": stamp}
            health, safety = [], []
            if out.triage:
                health.append(normalize_b_output(
                    {"session_id": self.session_id, "turn_id": turn_id,
                     **out.triage}, elder_id=self.elder_id,
                    occurred_at=stamp))
            if out.safety_level:
                safety.append(normalize_a_safety(
                    {"type": "final", "safety": out.safety_level,
                     "session_id": self.session_id, "turn_id": turn_id,
                     "text": transcript},
                    elder_id=self.elder_id, occurred_at=stamp))
            if health:
                payload["health_signals"] = [
                    _json_safe(h.model_dump(mode="json")) for h in health]
            if safety:
                payload["safety_events"] = [
                    _json_safe(s.model_dump(mode="json")) for s in safety]
            dig = await asyncio.wait_for(
                asyncio.to_thread(self.digest().execute, "record_signals",
                                  payload),
                timeout=TIMEOUT["digest"])
            out.digest = dig
            out.events.append(f"skill4={dig.get('status')}")
        except asyncio.TimeoutError:
            out.degraded.append("digest_timeout")
            out.events.append("skill4=timeout")
        except Exception as exc:  # noqa: BLE001
            out.degraded.append(f"digest:{type(exc).__name__}")
            out.events.append("skill4=error")
        out.timings["digest_ms"] = (time.monotonic() - t0) * 1000
        return out

    # ------------------------------------------------------------ 辅助

    def _digest_rows(self, out: TurnOutcome) -> dict:
        """把 skill2 的输出转换成 skill4 的摄入行。"""
        health, safety = [], []
        tri = out.triage
        if tri:
            hs = tri["health_signal"]
            if hs.get("type") not in (None, "none"):
                health.append({"type": hs.get("type"),
                               "detail": hs.get("detail"),
                               "severity": hs.get("severity")})
            if tri["safety"].get("blocked"):
                safety.append({"rule_id": tri["safety"].get("rule_id"),
                               "level": "P0"})
        if out.safety_level and not safety:
            safety.append({"rule_id": f"voice_guard_{out.safety_level}",
                           "level": out.safety_level})
        return {"health": health, "safety": safety}

    async def _mem_call(self, operation: str, **request):
        return await self._memory.execute(
            operation, {"request_id": uuid.uuid4().hex, **request},
            self._memory_principal)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 演示/测试

SCENARIOS: dict[str, list[str]] = {
    # 日常闲聊 -> 全部 passthrough，不惊动家属
    "daily": ["外面太阳挺好的，我下楼溜达了一圈。",
              "昨天小明打电话来说周末要回来看我。"],
    # 体征不适 -> skill2 提取 symptom，skill4 记 P1
    "symptom": ["今天早上起来腿沉得很，买菜走两步就得歇着。"],
    # 用药安全 -> skill1 和 skill2 双重拦截，skill4 P0 通知
    "dose": ["我那个降压药今天能不能吃两颗？"],
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="*", default=None)
    ap.add_argument("--no-llm", action="store_true",
                    help="不调 Qwen，只验证四方协议串联")
    ap.add_argument("--elder", default="e001")
    args = ap.parse_args()
    names = args.scenario or list(SCENARIOS)

    for name in names:
        if name not in SCENARIOS:
            print(f"未知场景 {name}，可选: {', '.join(SCENARIOS)}")
            return 2

    ok = True
    for name in names:
        print(f"\n{'='*66}\n=== {name} ===\n{'='*66}")
        orch = Orchestrator(elder_id=args.elder, use_llm=not args.no_llm)
        await orch.start()
        try:
            for text in SCENARIOS[name]:
                out = await orch.turn(text)
                print(f"\n  老人: {text}")
                print(f"  事件: {' | '.join(out.events)}")
                if out.memories:
                    print(f"  记忆: {len(out.memories)} 条 "
                          f"({out.match_status})")
                print(f"  助手[{out.reply_source}]: {out.reply[:120]}")
                if out.triage:
                    hs = out.triage["health_signal"]
                    print(f"  分诊: {hs['type']}/{hs['detail']}/"
                          f"{hs['severity']} blocked="
                          f"{out.triage['safety']['blocked']}")
                if out.digest:
                    print(f"  家属: {json.dumps(out.digest.get('data') or {}, ensure_ascii=False)[:150]}")
                if out.degraded:
                    print(f"  ⚠️ 降级: {out.degraded}")
                print(f"  耗时: " + " ".join(
                    f"{k}={v:.0f}ms" for k, v in out.timings.items()))
                if out.degraded:
                    ok = False
        finally:
            await orch.close()

    print(f"\n{'='*66}")
    print("编排层串联完成" if ok else "有环节降级，见上方 ⚠️")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
