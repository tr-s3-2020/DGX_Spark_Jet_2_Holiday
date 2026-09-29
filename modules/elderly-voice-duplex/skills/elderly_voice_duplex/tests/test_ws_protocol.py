#!/usr/bin/env python3
"""WebSocket 协议层回归测试：二进制音频帧必须真的到达 ASR。

这条用例是冲着踩过的坑写的：服务端原来用 `msg.get("type") == "bytes"`
判断音频帧，而 ASGI 的原生形状是 `{"type":"websocket.receive","bytes":...}`，
于是每一帧音频都被当文本帧丢掉，状态机拿到空 chunk——
ASR 永远没音频，"说了多久"永远是 0，每一轮都被当噪声忽略，
症状是服务端只在日志里留一句 "No user query found in messages."。

测试不另外起进程：直接用 Starlette TestClient 打 server.py 里的 app，
后端全换成 text 假件，所以不需要声卡、不需要 vLLM、不需要 FunASR。

用法：
    python3 tests/test_ws_protocol.py
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from starlette.testclient import TestClient  # noqa: E402

from skills.elderly_voice_duplex.adapters.base import ASRBackend  # noqa: E402
from skills.elderly_voice_duplex.adapters.text import (  # noqa: E402
    RecordingTTS, ScriptedLLM, TextVAD)
from skills.elderly_voice_duplex import adapters, server as srv  # noqa: E402

FRAME = 320                       # 20ms @ 16kHz，与网页客户端一致
FRAME_BYTES = FRAME * 2
LOUD, QUIET = 0.9, 0.05
TRANSCRIPT = "昨天在公园遛弯，遇见老李了"
REPLY = "那您下次约他一起去逛逛。"


class CountingASR(ASRBackend):
    """记录收到的音频字节数；收到过音频才"识别"出一句话。"""

    def __init__(self, transcript: str = TRANSCRIPT):
        self.transcript = transcript
        self.bytes_in = 0
        self.finals = 0

    async def accept(self, chunk: bytes) -> str:
        self.bytes_in += len(chunk)
        return ""

    async def final(self) -> str:
        self.finals += 1
        return self.transcript if self.bytes_in > 0 else ""

    def reset(self) -> None:
        self.bytes_in = 0
        self.finals = 0


class BeepingTTS:
    """每次合成返回同一段固定 PCM，并记下被合成了几次、合的是什么。"""

    BLOB = b"\x7f\x00" * 1600          # 100ms @ 16kHz PCM16

    def __init__(self):
        self.calls: list[str] = []

    async def speak(self, text: str, should_stop=None) -> bytes:
        self.calls.append(text)
        return self.BLOB

    def reset(self) -> None:
        self.calls.clear()


def run_call(app, *, speech_frames: int, silence_frames: int,
             speech_energy: float = LOUD, read_timeout: float = 20.0,
             greeting: str | None = None):
    """走一通电话，返回 (文本消息列表, 音频字节数, asr, tts)。"""
    asr, tts, llm = CountingASR(), BeepingTTS(), ScriptedLLM(
        replies={TRANSCRIPT: REPLY})

    # 换成 text 假件；用完还原，避免影响同进程里的其他用例
    saved = (adapters.make_asr, adapters.make_tts,
             adapters.make_llm, adapters.make_vad)
    adapters.make_asr = lambda: asr
    adapters.make_tts = lambda: tts
    adapters.make_llm = lambda: llm
    adapters.make_vad = lambda: TextVAD()

    texts: list[dict] = []
    audio_bytes = 0
    states: list[str] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_json({"type": "start", "kin": "小明",
                              "greeting": greeting})
                # 第一条文本消息一定是 state。二进制帧（TTS 音频）可能先到，
                # 所以循环收到文本为止，顺手把音频字节数算上。
                while True:
                    fut = pool.submit(ws.receive)
                    msg = fut.result(timeout=read_timeout)
                    if "bytes" in msg:
                        audio_bytes += len(msg["bytes"])
                        continue
                    first = json.loads(msg["text"])
                    break
                assert first["type"] == "state", first
                states.append(first["state"])

                for _ in range(speech_frames):
                    ws.send_bytes(b"\x11\x22" * FRAME)
                    ws.send_json({"type": "audio", "energy": speech_energy})
                for _ in range(silence_frames):
                    ws.send_bytes(b"\x00" * FRAME_BYTES)
                    ws.send_json({"type": "audio", "energy": QUIET})

                # 收干净：文本帧进 texts，二进制帧累加字节数。
                # portal.call 会一直阻塞，所以放线程里加超时。
                while True:
                    fut = pool.submit(ws.receive)
                    try:
                        msg = fut.result(timeout=read_timeout)
                    except concurrent.futures.TimeoutError:
                        break
                    if "text" in msg:
                        d = json.loads(msg["text"])
                        texts.append(d)
                        if d.get("type") == "state":
                            states.append(d["state"])
                    elif "bytes" in msg:
                        audio_bytes += len(msg["bytes"])
                    if (any(t.get("type") == "reply" for t in texts)
                            and audio_bytes):
                        break

                ws.send_json({"type": "hangup"})
    finally:
        adapters.make_asr, adapters.make_tts, adapters.make_llm, \
            adapters.make_vad = saved
        pool.shutdown(wait=False)

    return texts, audio_bytes, asr, tts, states


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("[ws-protocol] 二进制音频帧是否真的到达 ASR")

    # 静默阈值压到 0、最短语音压到 0：TestClient 是同步的，send 之间不经过
    # 事件循环等待，墙钟不推进，1800ms 的静默阈值永远等不到。
    # 这里要验的是音频通路，不是时间参数，所以把时间因素摘掉。
    saved_silence, saved_min = srv.config.VAD_SILENCE_MS, srv.config.VAD_MIN_SPEECH_MS
    srv.config.VAD_SILENCE_MS = 0
    srv.config.VAD_MIN_SPEECH_MS = 0

    try:
        texts, audio_bytes, asr, tts, states = run_call(
            srv.app, speech_frames=30, silence_frames=3)

        print(f"  ASR 收到 {asr.bytes_in} 字节音频，final() 被调 {asr.finals} 次")
        print(f"  TTS 被合成 {len(tts.calls)} 次: {tts.calls}")
        print(f"  状态流转: {' -> '.join(states)}")
        ok &= check("ASR 真的收到了音频帧",
                    asr.bytes_in >= 30 * FRAME_BYTES,
                    f"只收到 {asr.bytes_in} 字节（期望 >= {30 * FRAME_BYTES}）")
        ok &= check("ASR final() 被调用过", asr.finals >= 1)

        kinds = [t.get("type") for t in texts]
        print("  服务端下发:", kinds)
        ok &= check("收到了 reply", "reply" in kinds)
        reply = next((t for t in texts if t.get("type") == "reply"), {})
        ok &= check("回复内容来自 ASR 识别结果",
                    REPLY in reply.get("text", ""), repr(reply.get("text")))
        ok &= check("TTS 音频被推回客户端", audio_bytes > 0,
                    f"只收到 {audio_bytes} 字节")
        ok &= check("TTS 播的正是回复文本",
                    any(REPLY in s for s in tts.calls), repr(tts.calls))
        # 一句话只能合成一次：状态机内部已经播过，服务端再播一遍老人会
        # 听到重复的两遍，而且白等一倍合成时间。
        ok &= check("同一句话只合成一次",
                    tts.calls.count(REPLY) == 1, repr(tts.calls))

        # 反向验证：全程静音 -> ASR 没音频 -> 空转写 -> 不该有回复
        texts2, audio2, asr2, tts2, _ = run_call(
            srv.app, speech_frames=0, silence_frames=3)
        ok &= check("全程静音时不产生回复",
                    not any(t.get("type") == "reply" for t in texts2),
                    repr([t.get("type") for t in texts2]))
        ok &= check("全程静音时 ASR 也没收到音频", asr2.bytes_in == 0,
                    f"{asr2.bytes_in} 字节")

        # 开场白：接通时先说一句。状态必须先 LISTENING 再 SPEAKING，
        # 不能在第一句还没说完时就告诉客户端"在听"。
        texts3, audio3, asr3, tts3, states3 = run_call(
            srv.app, speech_frames=2, silence_frames=2,
            greeting="我来啦，您最近怎么样？")
        ok &= check("带开场白时状态先 LISTENING 再 SPEAKING",
                    states3[:2] == ["LISTENING", "SPEAKING"], repr(states3))
        ok &= check("开场白被合成并推回客户端",
                    tts3.calls[:1] == ["我来啦，您最近怎么样？"]
                    and audio3 > 0, repr(tts3.calls))
    finally:
        srv.config.VAD_SILENCE_MS = saved_silence
        srv.config.VAD_MIN_SPEECH_MS = saved_min

    print("[ws-protocol] " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
