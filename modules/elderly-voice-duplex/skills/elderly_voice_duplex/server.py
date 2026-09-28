#!/usr/bin/env python3
"""elderly-voice-duplex 的 WebSocket 服务：老人端 <-> 全双工状态机。

老人端（手机/平板/树莓派）只负责采音和放音，这一台机器做
VAD -> ASR -> LLM -> TTS 的完整链路。本机没有声卡，所以音频必须从客户端来。

协议（JSON 文本帧 + 二进制音频帧混用）
--------------------------------------------------
client -> server:
  {"type":"start","kin":"小明"}     接通电话，kin 是老人对子女的称呼
  <binary>                          一帧 PCM16 16kHz 单声道，紧邻下一条
  {"type":"audio","energy":0.82}    上一帧的能量（VAD 用；客户端算更准）
  {"type":"hangup"}                 挂断

server -> client:
  {"type":"state","state":"LISTENING"}
  {"type":"partial","text":"..."}                  ASR 部分结果
  {"type":"judge","signal":"thinking","reason":"..."}  在想词判定
  {"type":"filler","text":"诶，您慢慢想，我等您。"}
  {"type":"reply","text":"...","ttft_ms":109,"safety":""}
  <binary>                                        TTS 音频（PCM16 16kHz）

跑起来：
    PROFILE=full bash scripts/serve_v019.sh            # 先起 LLM（8000）
    EVD_ASR=nemo EVD_TTS=nemo python3 server.py --port 8100

测试（不装 NeMo 也能跑，用文本后端）：
    python3 server.py --port 8100
    python3 tests/test_ws_client.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", ".."))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
import uvicorn  # noqa: E402

from skills.elderly_voice_duplex import adapters, config  # noqa: E402
from skills.elderly_voice_duplex.duplex import VoiceDuplex  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("elderly-voice-duplex")

app = FastAPI(title="elderly-voice-duplex")


@app.get("/health")
async def health():
    return {"ok": True, "asr": config.ASR_BACKEND, "tts": config.TTS_BACKEND,
            "vad": config.VAD_BACKEND, "llm": config.LLM_BASE}


@app.websocket("/ws/voice")
async def voice(ws: WebSocket):
    await ws.accept()
    duplex: VoiceDuplex | None = None

    async def send(obj: dict):
        await ws.send_text(json.dumps(obj, ensure_ascii=False))

    async def speak(text: str):
        """把一句回答合成语音推给客户端。"""
        if not text:
            return
        audio = await duplex.tts.speak(text, lambda: False)
        if audio:
            await ws.send_bytes(audio)

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "bytes":
                continue                       # 音频体，等随后的控制帧
            raw = msg.get("text")
            if raw is None:
                continue
            ctrl = json.loads(raw)
            kind = ctrl.get("type")

            if kind == "start":
                duplex = VoiceDuplex(
                    vad=adapters.make_vad(), asr=adapters.make_asr(),
                    tts=adapters.make_tts(), llm=adapters.make_llm(),
                    kin=ctrl.get("kin", "孩子"))
                await duplex.start(ctrl.get("greeting"))
                await send({"type": "state", "state": duplex.state.name})
                log.info("通话开始 kin=%s", ctrl.get("kin"))

            elif kind == "audio" and duplex is not None:
                energy = float(ctrl.get("energy", 0.0))
                result = await duplex.feed_audio(b"", energy)
                if result is None:
                    continue
                for event in result.events:
                    if event.startswith("judge="):
                        sig = event[6:].split("(", 1)[0]
                        reason = event[6:].split("(", 1)[1].rstrip(")")
                        await send({"type": "judge", "signal": sig,
                                    "reason": reason})
                    elif event.startswith("->"):
                        await send({"type": "state", "state": event[2:]})
                if result.filler:
                    await send({"type": "filler", "text": result.filler})
                    await speak(result.filler)
                if result.reply:
                    await send({"type": "reply", "text": result.reply,
                                "ttft_ms": result.ttft_ms,
                                "safety": result.safety_level})
                    await speak(result.reply)

            elif kind == "hangup":
                if duplex is not None:
                    await duplex.hangup()
                await send({"type": "state", "state": "IDLE"})
                break

    except WebSocketDisconnect:
        log.info("客户端断开")
    except Exception as exc:  # noqa: BLE001
        log.exception("通话出错: %s", exc)
        try:
            await send({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args()
    log.info("启动 elderly-voice-duplex ws://%s:%d/ws/voice  asr=%s tts=%s vad=%s",
             args.host, args.port, config.ASR_BACKEND, config.TTS_BACKEND,
             config.VAD_BACKEND)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
