"""全双工会话状态机：LISTENING / THINKING / SPEAKING / BARGED_IN。

老人场景的三个特殊之处都落在这里：
  1. 停顿久 —— VAD 静默阈值放到 1800ms，且"只说了一半"时只给垫音、不收轮
  2. 声音轻 —— 打断检测阈值放低，但要求持续一小段时间才认定是插话
  3. 会插话 —— TTS 播放中检测到人声立刻停嘴，已说出口的内容作废

编排与音频后端解耦：VAD/ASR/TTS/LLM 都是接口，测试用文本假后端驱动。
"""
from __future__ import annotations

import asyncio
import logging
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
    memories: list = field(default_factory=list)
    events: list[str] = field(default_factory=list)


class VoiceDuplex:
    """驱动一次老人通话的全双工编排器。"""

    def __init__(self, *, vad, asr, tts, llm,
                 kin: str = "孩子",
                 silence_ms: int | None = None,
                 min_speech_ms: int | None = None,
                 scorer: HesitationScorer | None = None,
                 on_audio=None,
                 on_state=None,
                 on_recall=None):
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
        # 合成出音频后交给上层的钩子（WebSocket 服务用来推给客户端）。
        # 状态机必须自己调 tts.speak——要据此进入 SPEAKING，否则打断
        # （barge-in）无从判断——所以音频只能从这里交出去。上层不能再合成
        # 第二遍：那一遍既让老人听到重复内容，又白等一倍合成时间。
        self.on_audio = on_audio          # async (text, pcm) -> None
        self.on_state = on_state          # async (DuplexState) -> None
        # 生成回复前问上层要相关记忆。返回 list[dict]；失败返回空列表。
        # 放在 await 链里（不是后台任务）：prompt 要靠它拼。
        self.on_recall = on_recall        # async (transcript) -> list[dict]
        self.state = DuplexState.IDLE
        self.history: list[dict] = []
        self._partial = ""
        self._speech_started_at: float | None = None
        self._silence_since: float | None = None
        self._tts_task: asyncio.Task | None = None
        self._cancel_speech = False
        self._state_tasks: list[asyncio.Task] = []
        # 已喂给 ASR 的音频字节数。判定"说了多久"必须按音频时长算，
        # 不能用墙钟：WebSocket 客户端可能把整段音频很快发完（或反过来被
        # 事件循环拖慢），墙钟时长和真实语音长度没有关系，按它会整轮误丢。
        self._speech_bytes = 0
        # 每字节对应的毫秒数（PCM16 单声道）：1000 / (采样率 × 2)
        self._ms_per_byte = 1000.0 / (config.SAMPLE_RATE * 2)

    # ------------------------------------------------------------ 状态迁移

    def _enter(self, state: DuplexState, result: TurnResult | None = None):
        changed = self.state is not state
        self.state = state
        if result is not None:
            result.events.append(f"->{state.name}")
        # 已经在这个状态就不要再推一遍：_speak() 的 finally 会回 LISTENING，
        # 调用方紧跟的 _enter(LISTENING, result) 只是想往事件日志里补一条，
        # 那不是一次真实的状态变化，客户端不需要重复收。
        if changed and self.on_state is not None:
            self._queue_state(state)

    def _queue_state(self, state: DuplexState):
        """排一条状态推送。做成可 flush 的任务而不是裸 create_task：
        音频是同步 await 出去的，状态推送如果不同步等，客户端会先听到
        声音、后看到 SPEAKING——开场白时尤其明显。"""
        try:
            self._state_tasks.append(
                asyncio.get_running_loop().create_task(
                    self._push_state(state)))
        except RuntimeError:            # 没有事件循环（同步单测）
            pass

    async def _push_state(self, state: DuplexState):
        try:
            await self.on_state(state)
        except Exception:  # noqa: BLE001  客户端可能已经断开
            pass

    async def flush_state(self):
        """等排队的状态消息发完。"""
        while self._state_tasks:
            batch, self._state_tasks = self._state_tasks, []
            await asyncio.gather(*batch, return_exceptions=True)

    async def start(self, greeting: str | None = None) -> None:
        """接通电话：进入倾听状态，可按需先说一句开场白。"""
        self._speech_started_at = None
        self._speech_bytes = 0
        self._partial = ""
        self._enter(DuplexState.LISTENING)
        if greeting:
            await self._speak(greeting)

    async def hangup(self) -> None:
        """挂断。停嘴、收掉后台播放，等它真的结束再返回。"""
        self._cancel_speech = True
        self._speech_started_at = None
        self._speech_bytes = 0
        await self._stop_playback()
        self._enter(DuplexState.IDLE)

    async def close(self) -> None:
        """释放资源。测试和长驻进程退出前调用，避免留下 pending task。"""
        await self.hangup()
        await self.flush_state()

    async def _stop_playback(self) -> None:
        """取消并等掉后台播放任务。

        不 await 掉就返回的话，事件循环关闭时会看到
        "Task was destroyed but it is pending!"，而且合成可能还在跑。
        """
        task, self._tts_task = self._tts_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001  CancelledError 也走这条
            pass

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
                    return await self._barge_in()
            else:
                self._silence_since = None
            return None

        if self.state is not DuplexState.LISTENING:
            return None

        # 发言一旦开始，整段期间每一帧都要喂给 ASR——**包括词间的静音帧**。
        # 只在有人声时缓冲，会得到一段把静音全删掉的破碎音频，离线 ASR
        # 会识别成乱码（实测 "lovely outside" -> "ladí a tai"）。
        if self._speech_started_at is not None:
            await self.asr.accept(chunk)
            self._speech_bytes += len(chunk)

        # ---- 正常倾听 ----
        if speaking:
            if self._speech_started_at is None:
                self._speech_started_at = now
                self._speech_bytes = 0
                self._partial = ""
                # 第一帧人声本身也要进 ASR。上面的缓冲块在置位之前就跑完了，
                # 不在这里补一笔，每轮开头 20ms 会被丢掉。
                await self.asr.accept(chunk)
                self._speech_bytes += len(chunk)
            self._silence_since = None
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
        # "说了多久"按音频字节算（见 __init__ 里的说明）
        spoken_ms = self._speech_bytes * self._ms_per_byte
        self._speech_started_at = None
        self._speech_bytes = 0
        if spoken_ms < self.min_speech_ms:
            return None                      # 咳嗽/叹气，忽略
        return await self._close_turn()

    # ------------------------------------------------------------ 收轮判定

    async def _close_turn(self) -> TurnResult:
        # 离线 ASR（Paraformer）在 accept() 阶段只缓冲、给不出部分结果，
        # 所以这里补一次 final()。流式 ASR 已经在 _partial 里有累积文本。
        if not self._partial:
            self._partial = await self.asr.final()
        self._partial = (self._partial or "").strip()
        result = TurnResult(state=self.state, transcript=self._partial)
        judgement = self.scorer.judge(self._partial)
        result.events.append(f"judge={judgement.signal.value}({judgement.reason})")

        if not self._partial:
            # ASR 一个字都没给（纯噪声/麦克风没开）。这时候去问模型只会拿到
            # "No user query found in messages."，直接当这轮没发生。
            result.events.append("empty_transcript")
            self._enter(DuplexState.LISTENING, result)
            return result

        if judgement.signal is TurnSignal.THINKING:
            # 老人还在想词：只给垫音，不收轮，继续听
            filler = judgement.filler or await self._make_filler()
            result.filler = filler
            result.was_filler = True
            await self._speak(filler)
            return result

        if judgement.signal is TurnSignal.UNCERTAIN:
            filler = await self._make_filler(judgement.filler)
            if filler:
                # 模型也认为该垫一句
                result.filler = filler
                result.was_filler = True
                await self._speak(filler)
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
            # 这里不能再 _enter(LISTENING)：播放已经丢到后台了，状态要等
            # _play 真的说完才回去。提前回去会让打断检测整段失效。
            return result

        t0 = time.monotonic()
        first_at: float | None = None
        parts: list[str] = []
        # 先问上层要这一轮相关的记忆（skill3）。放在流式之前：prompt 要靠
        # 它拼。拿不到就用空列表，不影响本轮回复。
        recalled = await self._recall(self._partial)
        result.memories = recalled
        async for delta in self.llm.stream(
                system=build_reply_prompt(self._partial, self.history,
                                          self.kin, recalled),
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
        # 播放不在这里等：_speak 只负责切到 SPEAKING 并把播放丢到后台。
        # 要是 await 到合成结束，上层（WebSocket）就一直在等，收不到后面的
        # 音频帧，老人想插话时 _barge_in 根本没机会被调用。
        await self._speak(reply)
        self._remember(self._partial, reply)
        return result

    # ------------------------------------------------------------ 说话/打断

    async def _recall(self, transcript: str) -> list[dict]:
        """向上一层要相关记忆。没接 hook 或失败都返回空列表。"""
        if self.on_recall is None or not transcript:
            return []
        try:
            items = await self.on_recall(transcript)
            return list(items or [])
        except Exception:  # noqa: BLE001  记忆是增强，不是前提
            return []

    async def _speak(self, text: str):
        """切到 SPEAKING 并把播放丢到后台任务。不阻塞调用方。"""
        # 上一段没播完就又要说（只会是开场白紧接着的极端情况），先收掉
        old, self._tts_task = self._tts_task, None
        if old is not None and not old.done():
            old.cancel()
        self._cancel_speech = False
        self._enter(DuplexState.SPEAKING)
        # 先让 SPEAKING 到客户端，再出声音，否则客户端会先播音频后变状态
        await self.flush_state()
        try:
            self._tts_task = asyncio.get_running_loop().create_task(
                self._play(text))
        except RuntimeError:            # 没有事件循环（同步单测）：退化成同步播
            await self._play(text)

    async def _play(self, text: str):
        """后台播放一句话。可被 barge-in 取消。"""
        try:
            audio = await self.tts.speak(
                text, should_stop=lambda: self._cancel_speech)
            # 被插话打断时这一段已经作废，不要推给客户端
            if audio and self.on_audio is not None and not self._cancel_speech:
                await self.on_audio(text, audio)
                # 音频是**整句一次性**推过去的：合成结束不等于说完。不按播放
                # 时长占住 SPEAKING，服务端会立刻回 LISTENING，而客户端还要
                # 播剩下的十几秒——那期间老人插话会被当成新一轮发言，打断检测
                # 根本覆盖不到。这段时间也正好是 barge-in 真正发生的窗口。
                await self._hold_while_playing(audio)
        except asyncio.CancelledError:
            raise                          # 被打断：正常路径，别吞
        except Exception as exc:  # noqa: BLE001  合成失败不能让通话断掉
            logging.getLogger(__name__).warning(
                "播放失败（不影响后续通话）: %s: %s", type(exc).__name__, exc)
        finally:
            if not self._cancel_speech:
                # 说完了才回 LISTENING。被打断时 _barge_in 已经放过这个状态，
                # 这里再放一次会把客户端的状态机退回去。
                self._silence_since = None
                self._enter(DuplexState.LISTENING)
            self._tts_task = None

    async def _hold_while_playing(self, audio: bytes) -> None:
        """按音频播放时长占住 SPEAKING。被打断时由取消提前结束。"""
        seconds = len(audio) / 2 / max(config.SAMPLE_RATE, 1)
        if seconds > 0:
            await asyncio.sleep(seconds)

    async def _barge_in(self) -> TurnResult:
        """老人插话：立刻停嘴，已说出口的部分作废。"""
        result = TurnResult(state=self.state, transcript=self._partial)
        self._cancel_speech = True
        # 必须等任务真的结束再返回：不 await 的话，_play 可能已经把合成好的
        # 音频递给上层了，客户端于是"明明打断了还是听到整句"。
        await self._stop_playback()
        self._silence_since = None
        result.events.append("barge_in")
        # BARGED_IN 是给客户端的明确信号：收到就停掉正在排队的播放。
        # 服务端推回的是一整句 PCM，播放早就上了客户端的队列，服务端这边
        # 取消合成停不到它——必须客户端自己划掉。
        self._enter(DuplexState.BARGED_IN, result)
        self._enter(DuplexState.LISTENING, result)
        return result

    # ------------------------------------------------------------ 辅助

    async def _make_filler(self, fallback: str | None = None) -> str | None:
        """拿不准时让模型生成垫音；失败则用启发式给的兜底。"""
        if not self._partial:
            return fallback
        try:
            text = await self.llm.complete(
                system=build_filler_prompt(self._partial,
                                           config.FILLER_MAX_CHARS),
                # user 位必须放内容：只带 system 的请求会被 Qwen 模板拒掉
                # （"No user query found in messages."），垫音永远拿不到。
                history=[], user=self._partial,
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
