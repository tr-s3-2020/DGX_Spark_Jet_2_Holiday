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
  {"type":"transcript","text":"..."}                服务端识别到的老人原话
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
import array
import json
import logging
import os
import sys
import uuid

# 包根是 modules/elderly-voice-duplex/（本文件的祖父目录），这样
# `import skills.elderly_voice_duplex` 才成立。dirname(__file__) 已经是
# .../skills/elderly_voice_duplex，所以只要 2 个 ".."；且必须 abspath，
# os.path.join 不解析 ".."，未规范化的路径进 sys.path 后 import 找不到。
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..")))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
import uvicorn  # noqa: E402

from skills.elderly_voice_duplex import (  # noqa: E402
    adapters, config, family_card, memory)
from skills.elderly_voice_duplex.duplex import VoiceDuplex  # noqa: E402
from skills.elderly_voice_duplex.prompts import wants_share  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("elderly-voice-duplex")

app = FastAPI(title="elderly-voice-duplex")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def pcm_energy(chunk: bytes) -> float:
    """按 int16 幅值算一帧的 RMS 能量，除 3000。

    口径必须和 config.VAD_ENERGY_THRESHOLD 一致。原来完全信任客户端报上来
    的 energy，而不同客户端算法不一样（网页端曾经用 float32 的 RMS × 4，
    正常说话只有 0.1~0.3），低于 0.35 就被当静音，症状是"接听了说话没反应"。
    现在服务端自己从音频字节算，客户端的值只当兜底。
    """
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(chunk[:n * 2])
    if sys.byteorder == "big":            # array 按本机字节序，PCM 是小端
        samples.byteswap()
    total = 0.0
    for v in samples:
        total += float(v) * v
    return min(1.0, (total / n) ** 0.5 / 3000.0)


@app.get("/health")
async def health():
    return {"ok": True, "asr": config.ASR_BACKEND, "tts": config.TTS_BACKEND,
            "vad": config.VAD_BACKEND, "llm": config.LLM_BASE,
            "memory": memory.available(),
            "family_card": family_card.available()}


@app.get("/api/card")
async def api_card(elder: str = "老人", elder_id: str = "default"):
    """今日家属卡片。skill4 不在时返回 ok=False，页面显示"暂无"。

    elder_id 是 skill4 存储里的命名空间。默认 "default" 给页面用；
    探针/测试传别的值，否则会把测试数据写进家属真正看的那份日报里。

    chronicle 是 skill3 的回忆摘要。不传的话 skill4 会把「近期回忆」段显示成
    "尚未生成"——看着像记忆功能坏了，其实只是没喂数据。
    """
    chrono = await memory.chronicle(elder_id)
    log.info("卡片 elder_id=%s chronicle=%s", elder_id,
             ("None" if chrono is None else
              f"state={chrono.get('state')} items={len(chrono.get('items') or [])}"
              f" narrative={str(chrono.get('narrative'))[:60]!r}"))
    res = family_card.build_card(elder_id, elder, chrono)
    # skill4 的卡片按 (elder_id, 日期, tier) **幂等缓存**：今天建过就定型，
    # 之后新分享的内容不会反映上去。检测到"回忆有内容但卡片里没有"就明说，
    # 否则用户会以为分享失败了——这个坑今天已经绊了我好几次。
    res["stale"] = _card_is_stale(res, chrono)
    return res


def _card_is_stale(res: dict, chrono: dict | None) -> bool:
    """回忆有内容，但卡片正文里没体现——说明拿到的是当天早先建的旧卡。

    注意不能靠"正文非空"判断：skill4 在没回忆时会写
    「（回忆摘要尚未生成，本次不展示）」/「（暂无可展示的内容）」，
    这两个占位句也是非空字符串。
    """
    if not chrono:
        return False
    lines = [str(x).strip() for x in
             str(chrono.get("narrative") or "").splitlines() if str(x).strip()]
    if not lines:
        return False
    card = res.get("card") or {}
    for sec in card.get("sections") or []:
        if sec.get("heading") != "近期回忆":
            continue
        body = str(sec.get("body") or "")
        # 卡片里出现了回忆的任意一句，就不算旧
        return not any(line in body for line in lines)
    return True


@app.post("/api/card/dispatch")
async def api_card_dispatch(body: dict):
    """把卡片推给家属。card_id 由 /api/card 的响应带回来。"""
    return family_card.dispatch_card(str(body.get("card_id") or ""))


@app.get("/")
async def index():
    """网页客户端。getUserMedia 要安全上下文，从服务端打开比 file:// 稳。"""
    page = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(page):
        return {"detail": "网页客户端不在 web/index.html", "ws": "/ws/voice"}
    return FileResponse(page)


if os.path.isdir(WEB_DIR):
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.websocket("/ws/voice")
async def voice(ws: WebSocket):
    await ws.accept()
    duplex: VoiceDuplex | None = None
    # 最近一帧二进制音频。协议是「音频帧 + 紧随的控制帧」，所以要先缓存，
    # 等 {"type":"audio"} 到达时一起交给状态机——原来这里直接丢弃，
    # 导致 ASR 永远拿不到音频，只能靠客户端给的 energy 猜有人声。
    pending_audio = b""
    # 通话统计，断开时打一条，排查"说话没反应"全靠它
    stats = {"frames": 0, "bytes": 0, "speech_frames": 0, "turns": 0}
    seen_types: list[str] = []
    # 这次通话的标识，喂给 skill4 做去重键
    session_id = uuid.uuid4().hex[:12]
    # skill4 日报的命名空间，start 消息可以覆盖
    elder_id = "default"

    async def send(obj: dict):
        await ws.send_text(json.dumps(obj, ensure_ascii=False))

    async def on_audio(text: str, pcm: bytes):
        """状态机合成好一句话，推给客户端。"""
        await ws.send_bytes(pcm)

    async def on_state(state):
        """状态变化实时下发，客户端靠它显示"在听/在想/在说"。"""
        await send({"type": "state", "state": state.name})

    async def on_recall(transcript: str) -> list[dict]:
        """状态机要生成回复了，先问 skill3 有没有相关记忆。

        放在 await 链里而不是后台任务：prompt 要靠它拼。skill3 不在或超时
        返回空列表，通话照常——记忆是增强，不是前提。
        """
        try:
            return await asyncio.wait_for(
                memory.prepare_turn(session_id, f"T{stats['turns'] + 1}",
                                    transcript, elder_id),
                timeout=config.MEMORY_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.info("skill3 取记忆超时（不影响本轮回复）")
            return []
        except Exception as exc:  # noqa: BLE001
            log.info("skill3 取记忆失败（不影响本轮回复）: %s", exc)
            return []

    try:
        while True:
            msg = await ws.receive()
            # 排查用：把前几条消息的原始形状打出来。浏览器客户端"自检面板
            # 显示已收音但服务端帧数=0"时，靠这条分清是没发出来还是形状不对。
            if stats["frames"] + len(seen_types) < 12:
                seen_types.append(msg.get("type"))
                log.info("收到消息 #%d type=%s keys=%s bytes=%s text=%s",
                         stats["frames"] + len(seen_types), msg.get("type"),
                         sorted(msg.keys()),
                         len(msg.get("bytes") or b""),
                         (msg.get("text") or "")[:60])
            # 注意：ASGI 的原生形状是 {"type":"websocket.receive","bytes":...}，
            # 不是 {"type":"bytes"}。按后者判断会把每一帧音频都当文本帧丢掉，
            # 状态机拿到空 chunk——ASR 永远没音频，"说了多久"也永远是 0，
            # 每一轮都被当噪声忽略。断开消息也要显式收，否则会在这里空转。
            if msg.get("type") == "websocket.disconnect":
                break
            chunk = msg.get("bytes")
            if chunk is not None:
                pending_audio = chunk
                continue
            raw = msg.get("text")
            if raw is None:
                continue
            ctrl = json.loads(raw)
            kind = ctrl.get("type")

            if kind == "start":
                # elder_id 是 skill4 日报存储里的命名空间。默认 "default" 给
                # 页面用；测试/探针传别的值，否则会把测试数据写进家属真正
                # 看的那份日报里——表现就是"刚进去一句话没说就显示 P0"。
                elder_id = str(ctrl.get("elder_id") or "default")
                duplex = VoiceDuplex(
                    vad=adapters.make_vad(), asr=adapters.make_asr(),
                    tts=adapters.make_tts(), llm=adapters.make_llm(),
                    kin=ctrl.get("kin", "孩子"),
                    on_audio=on_audio, on_state=on_state,
                    on_recall=on_recall)
                # 记忆（skill3）也要跟着这通电话开会话。失败不影响通话。
                await memory.start(elder_id, session_id)
                await duplex.start(ctrl.get("greeting"))
                # 状态由 on_state 推。这里不能再补发一条 duplex.state.name：
                # 带了开场白时 start() 返回后状态已经是 SPEAKING，
                # 补发一条 LISTENING 会让客户端以为我们没在说话。
                await duplex.flush_state()
                log.info("通话开始 kin=%s elder_id=%s",
                         ctrl.get("kin"), elder_id)

            elif kind == "share" and duplex is not None:
                # 老人点「分享给家属」：把刚才那一轮标成要分享。
                # 真正提升条目要等挂断后的提炼跑完（条目那时候才创建）。
                turn_id = f"T{max(stats['turns'], 1)}"
                memory.mark_share(session_id, turn_id)
                await send({"type": "shared", "turn": turn_id,
                            "text": "好，这句我讲给家人听。"})
                log.info("标记分享 %s/%s", session_id, turn_id)

            elif kind == "audio" and duplex is not None:
                # 能量以服务端自己算的为准，客户端的只当兜底（见 pcm_energy）
                energy = max(float(ctrl.get("energy", 0.0)),
                             pcm_energy(pending_audio))
                stats["frames"] += 1
                stats["bytes"] += len(pending_audio)
                if energy >= config.VAD_ENERGY_THRESHOLD:
                    stats["speech_frames"] += 1
                result = await duplex.feed_audio(pending_audio, energy)
                pending_audio = b""
                if result is None:
                    continue
                # 状态变化已经由 on_state 实时推过了，这里只补 judge 事件
                for event in result.events:
                    if event.startswith("judge="):
                        sig = event[6:].split("(", 1)[0]
                        reason = event[6:].split("(", 1)[1].rstrip(")")
                        await send({"type": "judge", "signal": sig,
                                    "reason": reason})
                if result.filler:
                    await send({"type": "filler", "text": result.filler})
                if result.reply:
                    stats["turns"] += 1
                    log.info("收轮 #%d 识别=%r 回复=%r ttft=%dms 分级=%s",
                             stats["turns"], result.transcript,
                             result.reply[:40], result.ttft_ms,
                             result.safety_level or "-")
                    # A → D：把安全分级喂给 skill4。放在后台做——写盘和
                    # 导入都不该让老人等。失败只记日志，不影响这轮回复。
                    if result.safety_level:
                        asyncio.get_running_loop().run_in_executor(
                            None, family_card.record_safety, elder_id,
                            session_id, f"T{stats['turns']}",
                            result.safety_level, result.transcript)
                    # 老人亲口说"这句讲给孩子听"：也算分享请求。
                    # 语音是**辅助**路径（怕误认，所以宁漏勿滥），按钮才是主路径。
                    if wants_share(result.transcript, duplex.kin):
                        memory.mark_share(session_id,
                                          f"T{stats['turns']}")
                        await send({"type": "shared",
                                    "turn": f"T{stats['turns']}",
                                    "text": "好，这句我讲给家人听。"})
                    # 老人说的话也要回显：原来协议里只有 partial（离线 ASR
                    # 给不出），页面上永远看不到自己说了什么。
                    if result.transcript:
                        await send({"type": "transcript",
                                    "text": result.transcript})
                    await send({"type": "reply", "text": result.reply,
                                "ttft_ms": result.ttft_ms,
                                "safety": result.safety_level})
                    # P0 立刻告诉家属：这种不能等日报
                    if result.safety_level == "P0":
                        await send({"type": "family_alert",
                                    "text": result.reply})

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
    finally:
        # 挂断时可能还有话在后台合成/播放。不在这里收掉，事件循环关闭时会
        # 看到 "Task was destroyed but it is pending!"，而且 TTS 还在往外打。
        if duplex is not None:
            try:
                await duplex.close()
            except Exception:  # noqa: BLE001  清理失败不影响已经结束的通话
                pass
        # 挂断后 skill3 才开始提炼这一通电话。要等它跑完——不等的话进程
        # 这边先返回，job 永远停在 queued，记忆一条都不产出。
        try:
            # 先等提炼跑完——条目是那时候才创建的，提前提升会找不到
            await memory.close(elder_id, session_id,
                               config.MEMORY_DRAIN_S)
            if memory.shared_turns(session_id):
                out = await memory.promote_shared(elder_id, session_id)
                # 提炼失败要当成 warning：这是"分享了但卡片没变化"最常见的
                # 成因，而它本身不抛异常，只报"提升 0 条"根本看不出来。
                line = ("分享给家属: 提炼=%s 提升 %d 条 跳过 %d 条 %s"
                        % (out.get("extraction"), out.get("promoted", 0),
                           out.get("skipped", 0), out.get("note") or ""))
                if out.get("extraction") == "failed" or out.get("note"):
                    log.warning(line)
                else:
                    log.info(line)
                # 卡片是当天幂等缓存的：不作废的话，这次分享的内容永远上不了
                # 已经建好的那张卡（只作废还没推给家属的）。
                if out.get("promoted"):
                    family_card.drop_unsent_cards(elder_id)
                memory.clear_share(session_id)
        except Exception:  # noqa: BLE401
            pass
        # 一通电话的体检报告：帧数/音频量/有多少帧越过人声阈值/收了几轮。
        # "说话没反应"时先看这条——frames=0 说明音频根本没到，
        # speech_frames=0 说明麦克风没出声或太轻，turns=0 说明没收轮。
        log.info("通话结束 帧数=%d 音频=%.1fs 人声帧=%d(%.0f%%) 收轮=%d",
                 stats["frames"], stats["bytes"] / 2 / config.SAMPLE_RATE,
                 stats["speech_frames"],
                 (100.0 * stats["speech_frames"] / stats["frames"]
                  if stats["frames"] else 0.0),
                 stats["turns"])


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
