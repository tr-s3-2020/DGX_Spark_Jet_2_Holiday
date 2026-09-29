#!/usr/bin/env python3
"""打断（barge-in）回归：说话过程中老人插话，必须立刻停嘴。

这条用例是冲着踩过的坑写的。原来服务端的接收循环是
`await duplex.feed_audio(...)`，而 feed_audio 内部一路 await 到 LLM 流式和
tts.speak——这期间服务端根本没在 `ws.receive()`，老人后来的音频帧只能躺在
socket 缓冲区里。`_barge_in` 只有 feed_audio 一个入口，播放期间不可能被调用，
症状是"插话完全没反应，整句照常播完"。

修法是把播放丢到后台任务：feed_audio 拿到 TurnResult 就返回，服务端继续收
音频，状态机在 SPEAKING 期间正常检测插话。

这里用文本假后端驱动状态机，验证：
  1. 播放期间 feed_audio 能立刻返回（不被合成阻塞）
  2. 插话触发 BARGED_IN，且播放任务被取消
  3. 被打断的那段音频不会推给客户端
  4. 挂断后没有残留的 pending task

用法：
    python3 tests/test_barge_in.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.elderly_voice_duplex.adapters.text import (  # noqa: E402
    RecordingTTS, ScriptedASR, ScriptedLLM, TextVAD)
from skills.elderly_voice_duplex.duplex import (  # noqa: E402
    DuplexState, VoiceDuplex)

LOUD, QUIET = 0.9, 0.05
FRAME = 320


class SlowTTS(RecordingTTS):
    """合成很慢（模拟 edge-tts 一句话要好几百毫秒），合成完返回一段 PCM。

    注意真实后端（edge-tts）在流式接收中途不查 should_stop，所以打断实际
    靠的是**取消任务**——这里也一样验那条路径。
    """

    BLOB = b"\x7f\x00" * 16000        # 1s @ 16kHz PCM16

    def __init__(self, seconds: float = 3.0):
        super().__init__()
        self.seconds = seconds

    async def speak(self, text: str, should_stop) -> bytes:
        self.spoken.append(text)
        for _ in range(int(self.seconds * 100)):
            if should_stop():
                self.stopped_at = text
                return b""
            await asyncio.sleep(0.01)
        return self.BLOB


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    return cond


def _make_on_audio(pushed: list[str]):
    async def on_audio(text, pcm):
        pushed.append(text)
    return on_audio


def build(tts, pushed: list[str]):
    return VoiceDuplex(vad=TextVAD(), asr=ScriptedASR(["今天天气不错"]),
                       tts=tts,
                       llm=ScriptedLLM(replies={"天气": "那挺好呀"}),
                       kin="小明", silence_ms=0, min_speech_ms=0,
                       on_audio=_make_on_audio(pushed))


async def main() -> int:
    ok = True
    print("[barge-in] 说话过程中插话要能立刻停嘴")

    # ---- 场景一：feed_audio 不被合成阻塞 ----
    tts = SlowTTS(seconds=3.0)
    pushed: list[str] = []
    d = build(tts, pushed)
    await d.start()
    # 说一句，触发收轮 + 回复
    r = await d.feed_audio(b"\x00" * FRAME, LOUD)
    assert r is None or True
    # silence_ms=0：第一帧静默开始计时，第二帧收轮
    await d.feed_audio(b"\x00" * FRAME, QUIET)
    result = await d.feed_audio(b"\x00" * FRAME, QUIET)
    ok &= check("收轮并产生了回复", result is not None and bool(result.reply),
                repr(result and result.reply))
    ok &= check("返回时已经在 SPEAKING（播放交给了后台）",
                d.state is DuplexState.SPEAKING, d.state.name)

    # 关键：feed_audio 必须能立刻返回，不能被 3 秒的合成拖住
    t0 = asyncio.get_event_loop().time()
    # 打断判定要求能量持续超过阈值 BARGE_IN_HOLD_MS(120ms)，所以按真实节奏
    # 一帧帧喂——浏览器是每 20ms 一帧连续不断，hold 会自然累积。
    got = None
    for _ in range(8):
        r_in = await d.feed_audio(b"\x00" * FRAME, LOUD)
        if r_in is not None:
            got = r_in
            break
        await asyncio.sleep(0.03)
    dt = (asyncio.get_event_loop().time() - t0) * 1000
    ok &= check("插话后 feed_audio 立刻返回（不等合成结束）",
                dt < 2500, f"耗时 {dt:.0f}ms（合成要 3000ms）")
    ok &= check("插话被判定为打断", got is not None
                and "barge_in" in got.events, repr(got and got.events))
    ok &= check("打断后回到 LISTENING",
                d.state is DuplexState.LISTENING, d.state.name)
    # 等后台任务真的结束，确认它没有继续播放
    await asyncio.sleep(0.2)
    # 最关键的一条：被打断的那句一个字都不该送到客户端。edge-tts 在流式
    # 接收中途不查 should_stop，所以实际靠取消任务——音频必须在合成完成
    # 之前就被拦下，否则老人"明明打断了还是听到整句"。
    ok &= check("被打断的回复没有推给客户端", pushed == [], repr(pushed))
    ok &= check("没有残留的播放任务",
                d._tts_task is None or d._tts_task.done(),
                repr(d._tts_task))
    await d.close()
    ok &= check("挂断后没有 pending task",
                all(t.done() for t in asyncio.all_tasks()
                    if t is not asyncio.current_task()))

    # ---- 场景二：不插话时要能正常说完 ----
    tts2 = SlowTTS(seconds=0.3)
    pushed2: list[str] = []
    d2 = build(tts2, pushed2)
    await d2.start()
    await d2.feed_audio(b"\x00" * FRAME, LOUD)
    await d2.feed_audio(b"\x00" * FRAME, QUIET)
    r2 = await d2.feed_audio(b"\x00" * FRAME, QUIET)
    ok &= check("不插话时也能拿到回复", r2 is not None and bool(r2.reply))
    await asyncio.sleep(1.6)                  # 等它真的播完（1s 音频）
    ok &= check("说完整句后自动回 LISTENING",
                d2.state is DuplexState.LISTENING, d2.state.name)
    ok &= check("没被打断的回复正常推给客户端", pushed2 == ["那挺好呀"],
                repr(pushed2))
    await d2.close()

    # ---- 场景三：说完了才能开始下一轮 ----
    # 播放期间进来的音频不该被当成新一轮的发言（否则会把 AI 自己的话
    # 识别成老人说的话）
    tts3 = SlowTTS(seconds=0.5)
    pushed3: list[str] = []
    d3 = build(tts3, pushed3)
    await d3.start()
    await d3.feed_audio(b"\x00" * FRAME, LOUD)
    await d3.feed_audio(b"\x00" * FRAME, QUIET)
    r3 = await d3.feed_audio(b"\x00" * FRAME, QUIET)
    ok &= check("第三轮也拿到回复", r3 is not None and bool(r3.reply))
    # 播放期间喂静音：不该收第二轮
    extra = await d3.feed_audio(b"\x00" * FRAME, QUIET)
    ok &= check("播放期间静音不触发新一轮", extra is None, repr(extra))
    await d3.close()

    # ---- 场景四：安全话术那条分支 ----
    # 医疗围栏命中时走的是 _respond 里的 safety 分支，不走 LLM 流式。
    # 那条分支以前在 _speak 后面紧跟一个 _enter(LISTENING)，状态机于是
    # "以为在听、其实还在合成"，打断检测整段失效——线上就是这么漏的。
    tts4 = SlowTTS(seconds=2.0)
    pushed4: list[str] = []
    d4 = VoiceDuplex(vad=TextVAD(),
                     asr=ScriptedASR(["今天早上起来腿沉得很"]),
                     tts=tts4,
                     llm=ScriptedLLM(replies={}),
                     kin="小明", silence_ms=0, min_speech_ms=0,
                     on_audio=_make_on_audio(pushed4))
    await d4.start()
    await d4.feed_audio(b"\x00" * FRAME, LOUD)
    await d4.feed_audio(b"\x00" * FRAME, QUIET)
    r4 = await d4.feed_audio(b"\x00" * FRAME, QUIET)
    ok &= check("安全分支给出 P1 回复",
                r4 is not None and r4.safety_level == "P1",
                repr(r4 and (r4.safety_level, r4.reply[:30])))
    ok &= check("安全分支也停在 SPEAKING",
                d4.state is DuplexState.SPEAKING, d4.state.name)
    got4 = None
    for _ in range(8):
        r_in = await d4.feed_audio(b"\x00" * FRAME, LOUD)
        if r_in is not None:
            got4 = r_in
            break
        await asyncio.sleep(0.03)
    ok &= check("安全分支的话也能被打断",
                got4 is not None and "barge_in" in got4.events,
                repr(got4 and got4.events))
    await asyncio.sleep(0.2)
    ok &= check("安全分支被打断后也没推音频", pushed4 == [], repr(pushed4))
    await d4.close()

    # ---- 场景五：播放到一半才插话 ----
    # 音频是整句一次性推给上层的，合成结束不等于说完。原来 _play 推完就回
    # LISTENING，服务端于是"以为说完了、其实客户端还要播十几秒"，那期间
    # 插话会被当成新一轮发言，打断检测完全覆盖不到。
    # 现在按音频播放时长占住 SPEAKING，打断窗口才和真实播放窗口重合。
    tts5 = SlowTTS(seconds=0.1)
    pushed5: list[str] = []
    d5 = build(tts5, pushed5)
    await d5.start()
    await d5.feed_audio(b"\x00" * FRAME, LOUD)
    await d5.feed_audio(b"\x00" * FRAME, QUIET)
    r5 = await d5.feed_audio(b"\x00" * FRAME, QUIET)
    ok &= check("第五轮也拿到回复", r5 is not None and bool(r5.reply))
    # 等合成结束、音频已经推给上层，但还没播完
    await asyncio.sleep(0.15)
    ok &= check("音频已推给上层后仍在 SPEAKING",
                d5.state is DuplexState.SPEAKING, d5.state.name)
    got5 = None
    for _ in range(8):
        r_in = await d5.feed_audio(b"\x00" * FRAME, LOUD)
        if r_in is not None:
            got5 = r_in
            break
        await asyncio.sleep(0.03)
    ok &= check("播放到一半插话也能被打断",
                got5 is not None and "barge_in" in got5.events,
                repr(got5 and got5.events))
    await d5.close()

    print("[barge-in] " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
