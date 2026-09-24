"""Paraformer 中文 ASR 适配器（FunASR）。

选它的实测依据（同样 4 句中文合成音，edge-tts 生成）：

| 模型 | CER |
|---|---|
| nvidia/nemotron-3.5-asr-streaming-0.6b | 66.8%（全是同音字错：酱鸭/降压） |
| paraformer-zh + ct-punc | **1.3%（去标点）**，且"降压药"识别正确 |

注意 Nemotron 把"降压药"听成"酱鸭药"会让医疗围栏失效，那是不可接受的风险。
延迟：平均 60ms（RTF 0.014），见 docs/SDK-CHOICES.md。

设计说明：这里用**离线模型 + VAD 切段**，不是流式模型。原因是
  1. 离线版精度明显更高（流式版通常差一截）
  2. 我们的 duplex 状态机本来就等 1800ms 静默才收轮，天然完成了分段
  3. 代价只是 filler 判定晚 ~60ms，可忽略
所以 accept() 只缓冲音频，真正的识别发生在 final()。
"""
from __future__ import annotations

import asyncio
import os
import re

from .. import config
from .base import ASRBackend


class ParaformerASR(ASRBackend):
    """Paraformer-zh 离线识别 + ct-punc 标点恢复。"""

    def __init__(self, model: str = "paraformer-zh",
                 punc_model: str = "ct-punc", device: str = "cuda",
                 use_punc: bool = True):
        from funasr import AutoModel

        # 模型权重走项目内 HF 缓存，不写 HOME
        os.environ.setdefault("HF_HOME", os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", ".cache", "hf"))
        self.device = device
        self.model = AutoModel(model=model, hub="hf", device=device,
                               disable_pbar=True, disable_log=True)
        self.punc = (AutoModel(model=punc_model, hub="hf", device=device,
                               disable_pbar=True, disable_log=True)
                     if use_punc else None)
        self._buf = bytearray()
        self._text = ""

    async def accept(self, chunk: bytes) -> str:
        """缓冲一帧。离线模型给不出部分结果，所以这里返回空。"""
        self._buf.extend(chunk)
        return ""

    async def final(self) -> str:
        """对缓冲的整段音频做识别 + 标点恢复。"""
        if not self._buf:
            return ""
        return await asyncio.to_thread(self._final_sync)

    def _final_sync(self) -> str:
        import numpy as np

        pcm = np.frombuffer(bytes(self._buf), dtype=np.int16)
        audio = pcm.astype(np.float32) / 32768.0
        self._buf = bytearray()
        if audio.size < config.SAMPLE_RATE * 0.2:      # 太短，当噪声
            return ""
        text = self.model.generate(input=audio)[0]["text"].strip()
        if not text:
            return ""
        if self.punc is not None:
            try:
                text = self.punc.generate(input=text)[0]["text"].strip()
            except Exception:  # noqa: BLE001  标点恢复失败不影响识别本身
                pass
        self._text = text
        return text

    def reset(self) -> None:
        self._buf = bytearray()
        self._text = ""
