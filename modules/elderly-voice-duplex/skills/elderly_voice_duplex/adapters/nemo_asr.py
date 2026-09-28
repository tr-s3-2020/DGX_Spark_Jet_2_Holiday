"""NVIDIA Nemotron 流式 ASR 适配器（英文场景）。

模型：nvidia/nemotron-3.5-asr-streaming-0.6b
  - Cache-Aware FastConformer-RNNT，600M，原生流式，为低延迟 voice agent 设计
  - **英文实测 WER 0.0%**（中文 66.8% 不可用，故本 skill 走英文场景）
  - Ampere 可用（A800 实测通过）；chunk 80~1120ms 推理时可调

⚠️ 当前实现是**缓冲整段 + 收轮时一次识别**，不是逐 chunk 流式。
   模型本身支持 cache-aware 流式，但 NeMo 的流式调用方式还没调通
   （试过 streaming_transcribe，没拿到可用结果），所以先用整段识别把链路打通。
   duplex 本来就等 1800ms 静默才收轮，天然完成了分段，代价只是识别晚 ~60ms。
   真正的流式是后续工作，见 SKILL.md 的待办。
"""
from __future__ import annotations

import asyncio
import os
import re

from .. import config
from .base import ASRBackend

# 模型会在结果里附加语言标签（可能出现在句中而非行尾），必须全局剥掉，
# 否则会污染对话历史、安全围栏匹配和家属日报
_LANG_TAG = re.compile(r"\s*<[a-z]{2}-[A-Z]{2}>")


class NemoStreamingASR(ASRBackend):
    """Nemotron ASR：accept() 缓冲一帧，final() 返回整段识别结果。"""

    def __init__(self, model: str | None = None, chunk_ms: int | None = None,
                 device: str = "cuda"):
        from nemo.collections.asr.models import ASRModel

        self.model_name = model or config.NEMO_ASR_MODEL
        self.chunk_ms = chunk_ms or config.NEMO_ASR_CHUNK_MS
        self.device = device
        # 模型权重走项目内的 HF 缓存，不写 HOME
        os.environ.setdefault("HF_HOME", os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "..", ".cache", "hf"))
        self._model = ASRModel.from_pretrained(self.model_name)
        if device == "cuda":
            self._model = self._model.cuda()
        self._model.eval()
        self._reset_state()

    def _reset_state(self):
        self._buffer = bytearray()
        self._text = ""

    async def accept(self, chunk: bytes) -> str:
        """缓冲一帧。整段识别在 final() 做，所以这里返回空。"""
        self._buffer.extend(chunk)
        return ""

    async def final(self) -> str:
        if not self._buffer:
            return ""
        return await asyncio.to_thread(self._final_sync)

    def _final_sync(self) -> str:
        import numpy as np

        pcm = np.frombuffer(bytes(self._buffer), dtype=np.int16)
        audio = pcm.astype(np.float32) / 32768.0
        self._buffer = bytearray()
        if audio.size < config.SAMPLE_RATE * 0.2:      # 太短，当噪声
            return ""
        try:
            out = self._model.transcribe([audio], verbose=False)
        except TypeError:
            out = self._model.transcribe([audio])
        text = out[0] if hasattr(out, "__getitem__") else str(out)
        text = getattr(text, "text", text) or ""
        text = _LANG_TAG.sub("", text).strip()
        self._text = text
        return text

    def reset(self) -> None:
        self._reset_state()
