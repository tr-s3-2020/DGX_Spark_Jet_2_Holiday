#!/usr/bin/env python3
"""用真实音频走一遍 WebSocket，验证语音链路（不依赖浏览器）。

本机没有声卡，所以用 edge-tts 合成的中文当"老人说的话"，
按协议的「二进制帧 + 控制帧」推进去，检查：
  1. 服务端真的收到了音频（ASR 能识别出内容）
  2. 回复文本合理
  3. 服务端推回了可播放的 PCM 音频

这个文件是冲着踩过的坑留着的：服务端原来用 `msg.get("type") == "bytes"`
判断音频帧，而 ASGI 的原生形状是 `{"type":"websocket.receive","bytes":...}`，
于是每帧音频都被丢掉，症状只有日志里一句
"LLM HTTP 400: No user query found in messages."。协议层回归见
tests/test_ws_protocol.py（不起进程、用假后端，跑得快）。

用法（先起服务）：
    EVD_ASR=funasr EVD_TTS=edge python3 server.py --port 8100
    python3 tests/test_ws_audio.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import numpy as np
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", ".."))
AUDIO = os.path.join(PROJECT, ".cache", "voice-probe", "audio")

URL = os.environ.get("EVD_WS", "ws://127.0.0.1:8100/ws/voice")
FRAME_SAMPLES = 320        # 每帧样本数（20ms @16kHz），与网页客户端一致
FRAME_BYTES = FRAME_SAMPLES * 2

# 场景：文件名 -> (期望安全分级或 None, 回复里不该出现的词)
SCENARIOS = [
    ("0.mp3", "P1", []),        # "今天早上起来腿沉得很，买菜走两步就得歇着。"
    ("2.mp3", None, []),        # 家常话，走完整 LLM 回复
]


def load_pcm16(name: str) -> np.ndarray:
    """把测试 mp3 解成 16kHz 单声道 int16 PCM。"""
    import miniaudio
    with open(os.path.join(AUDIO, name), "rb") as fh:
        data = fh.read()
    dec = miniaudio.decode(data, nchannels=1, sample_rate=16000,
                           output_format=miniaudio.SampleFormat.SIGNED16)
    return np.array(dec.samples, dtype=np.int16)


def frame_energy(pcm: np.ndarray) -> float:
    return float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) / 3000.0


async def run_scenario(name: str, expect_safety: str | None) -> bool:
    pcm = load_pcm16(name)
    print(f"\n[ws] {name} {len(pcm) / 16000:.1f}s")

    texts: list[str] = []
    audios: list[bytes] = []
    states: list[str] = []
    safety_seen = ""
    got_reply = False
    deadline = asyncio.get_event_loop().time() + 120

    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "kin": "小明"},
                                 ensure_ascii=False))

        async def pump():
            """把已经回来的消息读干。二进制帧是 TTS 音频，不能 json.loads。"""
            nonlocal got_reply, safety_seen
            while True:
                m = await asyncio.wait_for(ws.recv(), 0.05)
                if isinstance(m, bytes):
                    audios.append(m)
                    continue
                d = json.loads(m)
                t = d.get("type")
                if t == "reply":
                    texts.append(d["text"])
                    safety_seen = d.get("safety", "")
                    got_reply = True
                elif t == "filler":
                    texts.append("[垫音] " + d["text"])
                elif t == "state":
                    states.append(d["state"])

        # 等 start 回包（服务端可能正现加载 ASR 模型，给足时间）
        while True:
            d = json.loads(await asyncio.wait_for(ws.recv(), 120))
            if d.get("type") == "state":
                states.append(d["state"])
                break

        # 1) 按真实节奏推音频：一帧二进制 + 紧随的控制帧
        for i in range(0, len(pcm) - FRAME_SAMPLES, FRAME_SAMPLES):
            seg = pcm[i:i + FRAME_SAMPLES]
            await ws.send(seg.tobytes())
            await ws.send(json.dumps({"type": "audio",
                                      "energy": min(frame_energy(seg), 1.0)}))
            try:
                await pump()
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.02)
            if got_reply:
                break

        # 2) 推静默帧直到收轮（默认静默阈值 1800ms）
        while not got_reply and asyncio.get_event_loop().time() < deadline:
            await ws.send(b"\x00" * FRAME_BYTES)
            await ws.send(json.dumps({"type": "audio", "energy": 0.0}))
            try:
                await pump()
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.02)

        # 3) 等 TTS 音频到齐
        quiet = 0
        while quiet < 10 and asyncio.get_event_loop().time() < deadline:
            try:
                m = await asyncio.wait_for(ws.recv(), 1.0)
                if isinstance(m, bytes):
                    audios.append(m)
                    quiet = 0
                elif json.loads(m).get("type") == "reply":
                    texts.append(json.loads(m)["text"])
                    got_reply = True
            except asyncio.TimeoutError:
                quiet += 1

        await ws.send(json.dumps({"type": "hangup"}))

    total = sum(len(a) for a in audios)
    print(f"[ws] 状态流转: {' -> '.join(states)}")
    for t in texts:
        print(f"[ws] 文本: {t[:120]}")
    print(f"[ws] 收到音频 {len(audios)} 段共 {total} 字节 "
          f"({total / 2 / 16000:.2f}s @16kHz)")

    ok = got_reply and total > 0
    print(f"[ws] {name} " + ("PASS" if ok else "FAIL"))
    if expect_safety is not None and safety_seen != expect_safety:
        print(f"[ws] {name} FAIL 安全分级期望 {expect_safety}，"
              f"实际 {safety_seen!r}")
        ok = False
    return ok


async def main() -> int:
    ok = True
    for name, expect_safety, _ in SCENARIOS:
        path = os.path.join(AUDIO, name)
        if not os.path.exists(path):
            print(f"[ws] 缺测试音频 {path}，跳过")
            continue
        ok &= await run_scenario(name, expect_safety)
    print("[ws] " + ("PASS：音频进 -> 识别 -> 回复 -> 语音出 全通"
                     if ok else "FAIL：链路不完整"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
