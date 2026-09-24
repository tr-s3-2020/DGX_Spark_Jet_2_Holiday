"""全双工会话状态机：LISTENING / THINKING / SPEAKING / BARGED_IN。

老人场景的三个特殊之处都落在这里：
  1. 停顿久 —— VAD 静默阈值放到 1800ms，且"只说了一半"时只给垫音、不收轮
  2. 声音轻 —— 打断检测阈值放低，但要求持续一小段时间才认定是插话
  3. 会插话 —— TTS 播放中检测到人声立刻停嘴，已说出口的内容作废

编排与音频后端解耦：VAD/ASR/TTS/LLM 都是接口，测试用文本假后端驱动。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from . import config
from .prompts import (HesitationScorer, TurnJudgement, TurnSignal,
                      build_filler_prompt, build_reply_prompt, check_safety)


class DuplexState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    BARGED_IN = auto()


@dataclass
class TurnResult:
    """一轮交互的结果，供上层（WebSocket/测试）观测。"""
    state: DuplexState
    transcript: str = ""
    reply: str = ""
    filler: str = ""
    was_filler: bool = False
    safety_level: str = ""
    ttft_ms: int = 0
    events: list[str] = field(default_factory=list)


class VoiceDuplex:
    """驱动一次老人通话的全双工编排器。"""

    def __init__(self, *, vad, asr, tts, llm,
                 kin: str = "孩子",
                 silence_ms: int | None = None,
                 min_speech_ms: int | None = None,
                 scorer: HesitationScorer | None = None):
        self.vad = vad
        self.asr = asr
        self.tts = tts
        self.llm = llm
        self.kin = kin
        self.silence_ms = (config.VAD_SILENCE_MS if silence_ms is None
                           else silence_ms)
        self.min_speech_ms = (config.VAD_MIN_SPEECH_MS
                              if min_speech_ms is None else min_speech_ms)
        self.scorer = scorer or HesitationScorer()
        self.state = DuplexState.IDLE
        self.history: list[dict] = []
        self._partial = ""
        self._speech_started_at: float | None = None
        self._silence_since: float | None = None
        self._tts_task: asyncio.Task | None = None
        self._cancel_speech = False

    # ------------------------------------------------------------ 状态迁移

    def _enter(self, state: DuplexState, result: TurnResult | None = None):
        self.state = state
        if result is not None:
            result.events.append(f"->{state.name}")

    async def start(self, greeting: str | None = None) -> None:
        """接通电话：进入倾听状态，可按需先说一句开场白。"""
        self._enter(DuplexState.LISTENING)
        if greeting:
            await self._speak(greeting)

    async def hangup(self) -> None:
        """挂断。"""
        self._cancel_speech = True
        self._enter(DuplexState.IDLE)

    # ------------------------------------------------------------ 音频入口

    async def feed_audio(self, chunk: bytes, energy: float) -> TurnResult | None:
        """客户端音频流入。返回非 None 表示这一帧触发了某个动作。"""
        now = time.monotonic()
        speaking = self.vad.is_speech(chunk, energy)

        # 我们正在说话时，老人开口 = 打断
        if self.state is DuplexState.SPEAKING:
            if speaking and energy >= config.BARGE_IN_ENERGY:
                self._silence_since = self._silence_since or now
                if (now - self._silence_since) * 1000 >= config.BARGE_IN_HOLD_MS:
                    return self._barge_in()
            else:
                self._silence_since = None
            return None

        if self.state is not DuplexState.LISTENING:
            return None

        # ---- 正常倾听 ----
        if speaking:
            if self._speech_started_at is None:
                self._speech_started_at = now
                self._partial = ""
            self._silence_since = None
            partial = await self.asr.accept(chunk)
            if partial:
                self._partial = partial
            return None

        # ---- 静默中：判断是否该收轮 ----
        if self._speech_started_at is None:
            return None
        if self._silence_since is None:
            self._silence_since = now
            return None
        if (now - self._silence_since) * 1000 < self.silence_ms:
            return None

        # 静默够久了
        self._silence_since = None
        spoken_ms = (now - self._speech_started_at) * 1000
        self._speech_started_at = None
        if spoken_ms < self.min_speech_ms:
            return None                      # 咳嗽/叹气，忽略
        return await self._close_turn()

    # ------------------------------------------------------------ 收轮判定

    async def _close_turn(self) -> TurnResult:
        result = TurnResult(state=self.state, transcript=self._partial)
        judgement = self.scorer.judge(self._partial)
        result.events.append(f"judge={judgement.signal.value}({judgement.reason})")

        if judgement.signal is TurnSignal.THINKING:
            # 老人还在想词：只给垫音，不收轮，继续听
            filler = judgement.filler or await self._make_filler()
            result.filler = filler
            result.was_filler = True
            await self._speak(filler)
            self._enter(DuplexState.LISTENING, result)
            return result

        if judgement.signal is TurnSignal.UNCERTAIN:
            filler = await self._make_filler(judgement.filler)
            if filler:
                # 模型也认为该垫一句
                result.filler = filler
                result.was_filler = True
                await self._speak(filler)
                self._enter(DuplexState.LISTENING, result)
                return result
            # 否则当作说完了，继续走完整回复

        return await self._respond()

    async def _respond(self) -> TurnResult:
        result = TurnResult(state=self.state, transcript=self._partial)
        self._enter(DuplexState.THINKING, result)

        safety = check_safety(self._partial, self.kin)
        if safety:
            level, text = safety
            result.safety_level = level
            result.reply = text
            await self._speak(text)
            self._remember(self._partial, text)
            self._enter(DuplexState.LISTENING, result)
            return result

        t0 = time.monotonic()
        first_at: float | None = None
        parts: list[str] = []
        async for delta in self.llm.stream(
                system=build_reply_prompt(self._partial, self.history,
                                          self.kin),
                history=self.history,
                user=self._partial,
                think=False):          # 语音链路关思考，见 adapters/llm_vllm.py
            if first_at is None:
                first_at = time.monotonic()
            parts.append(delta)
        reply = "".join(parts).strip()
        # TTFT 记到第一个"答案" token：开着思考时第一个 token 是思考，
        # 对等着听声音的老人没有意义。
        result.ttft_ms = (int((first_at - t0) * 1000) if first_at else 0)
        result.reply = reply
        if result.ttft_ms > config.TTFT_BUDGET_MS:
            result.events.append(f"ttft_over_budget({result.ttft_ms}ms)")
        await self._speak(reply)
        self._remember(self._partial, reply)
        self._enter(DuplexState.LISTENING, result)
        return result

    # ------------------------------------------------------------ 说话/打断

    async def _speak(self, text: str):
        """播放一句话。可被 barge-in 取消。"""
        self._cancel_speech = False
        self._enter(DuplexState.SPEAKING)
        try:
            await self.tts.speak(text, should_stop=lambda: self._cancel_speech)
        finally:
            if not self._cancel_speech:
                self._enter(DuplexState.LISTENING)

    def _barge_in(self) -> TurnResult:
        result = TurnResult(state=self.state, transcript=self._partial)
        self._cancel_speech = True
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        self._silence_since = None
        result.events.append("barge_in")
        self._enter(DuplexState.LISTENING, result)
        return result

    # ------------------------------------------------------------ 辅助

    async def _make_filler(self, fallback: str | None = None) -> str | None:
        """拿不准时让模型生成垫音；失败则用启发式给的兜底。"""
        try:
            text = await self.llm.complete(
                system=build_filler_prompt(self._partial,
                                           config.FILLER_MAX_CHARS),
                history=[], user="",
                max_tokens=config.FILLER_MAX_TOKENS,
                temperature=config.FILLER_TEMPERATURE,
                think=False,
            )
            text = (text or "").strip().strip('"').strip('"').strip("'")
            if text and len(text) <= config.FILLER_MAX_CHARS + 4:
                return text
        except Exception:  # noqa: BLE001  垫音失败不能让通话断掉
            pass
        return fallback

    def _remember(self, user: str, assistant: str):
        self.history.append({"role": "user", "content": user})
        self.history.append({"role": "assistant", "content": assistant})
        if len(self.history) > 40:            # 超出按 2 轮一组丢最旧的
            del self.history[:2]
