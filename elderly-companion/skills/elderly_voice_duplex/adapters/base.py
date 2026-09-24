"""音频/模型后端接口。换实现（text → nemo → riva）不动上层。"""
from __future__ import annotations

import abc
from typing import Awaitable, Callable


class VADBackend(abc.ABC):
    """判定一帧音频里有没有人声。老人特化参数在 config 里。"""

    @abc.abstractmethod
    def is_speech(self, chunk: bytes, energy: float) -> bool: ...


class ASRBackend(abc.ABC):
    """流式语音识别。accept() 返回增量文本，final() 返回整句。"""

    @abc.abstractmethod
    async def accept(self, chunk: bytes) -> str: ...

    @abc.abstractmethod
    async def final(self) -> str: ...

    @abc.abstractmethod
    def reset(self) -> None: ...


class TTSBackend(abc.ABC):
    """播放一句话。should_stop 用于 barge-in 打断。"""

    @abc.abstractmethod
    async def speak(self, text: str,
                    should_stop: Callable[[], bool]) -> None: ...


class LLMBackend(abc.ABC):
    """对话模型。think=False 用于垫音这类要快的调用。"""

    @abc.abstractmethod
    async def complete(self, *, system: str, history: list[dict],
                       user: str, max_tokens: int | None = None,
                       temperature: float | None = None,
                       think: bool = True) -> str: ...

    @abc.abstractmethod
    def stream(self, *, system: str, history: list[dict], user: str,
               max_tokens: int | None = None,
               temperature: float | None = None,
               think: bool = True):
        """异步产出答案增量（思考内容被跳过）。首段到达时刻即 TTFT。"""
        raise NotImplementedError
