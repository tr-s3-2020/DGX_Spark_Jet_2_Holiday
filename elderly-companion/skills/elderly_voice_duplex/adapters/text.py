"""文本假后端：没有声卡/没装 NeMo 时驱动全双工状态机。

这让状态机、垫音判定、打断逻辑可以在任何机器上单测，
不依赖音频硬件和模型权重。
"""
from __future__ import annotations

from .base import ASRBackend, LLMBackend, TTSBackend, VADBackend


class TextVAD(VADBackend):
    """按 energy 阈值判定人声，模拟老人发音轻（阈值低于常规）。"""

    def __init__(self, threshold: float = 0.35):
        self.threshold = threshold

    def is_speech(self, chunk: bytes, energy: float) -> bool:
        return energy >= self.threshold


class ScriptedASR(ASRBackend):
    """按脚本吐出 partial 文本，模拟流式识别。"""

    def __init__(self, partials: list[str] | None = None):
        self.partials = list(partials or [])
        self._i = 0
        self._buf = ""

    async def accept(self, chunk: bytes) -> str:
        if self._i < len(self.partials):
            text = self.partials[self._i]
            self._i += 1
            self._buf = text
            return text
        return ""

    async def final(self) -> str:
        return self._buf

    def reset(self) -> None:
        self._i = 0
        self._buf = ""


class RecordingTTS(TTSBackend):
    """记录被播放的文本，不真的发声。"""

    def __init__(self):
        self.spoken: list[str] = []
        self.stopped_at: str | None = None

    async def speak(self, text: str, should_stop) -> None:
        import asyncio
        self.spoken.append(text)
        for _ in range(50):                     # 模拟播放，可被中断
            if should_stop():
                self.stopped_at = text
                return
            await asyncio.sleep(0.01)


class ScriptedLLM(LLMBackend):
    """返回预置回答；用于不依赖真实服务的单测。"""

    def __init__(self, replies: dict[str, str] | None = None,
                 default: str = "好的，我知道了。"):
        self.replies = replies or {}
        self.default = default
        self.calls: list[dict] = []

    async def complete(self, *, system: str, history: list[dict], user: str,
                       max_tokens: int | None = None,
                       temperature: float | None = None,
                       think: bool = True) -> str:
        self.calls.append({"user": user, "system": system[:80],
                           "max_tokens": max_tokens, "think": think})
        for key, value in self.replies.items():
            if key in (user or system):
                return value
        return self.default

    async def stream(self, *, system: str, history: list[dict], user: str,
                     max_tokens: int | None = None,
                     temperature: float | None = None,
                     think: bool = True):
        self.calls.append({"user": user, "system": system[:80],
                           "max_tokens": max_tokens, "think": think,
                           "stream": True})
        for key, value in self.replies.items():
            if key in (user or system):
                yield value
                return
        yield self.default
