"""NVIDIA Nemotron 流式 ASR 适配器。

模型：nvidia/nemotron-3.5-asr-streaming-0.6b
  - Cache-Aware FastConformer-RNNT，600M，原生流式，为低延迟 voice agent 设计
  - 中文（zh-CN）开箱可用；chunk 80/160/320/560/1120ms 推理时可调
  - Ampere 可用（A800 实测通过）

⚠️ 中文 CER 在 FLEURS 上约 19~21%，老人发音含混时大概率更差。
   必须用真实老人语音实测；不行就走 NVIDIA 的 fine-tune 路径。
"""
from __future__ import annotations

import asyncio
import os

from .. import config
from .base import ASRBackend


class NemoStreamingASR(ASRBackend):
    """流式 ASR：accept() 喂一帧，返回增量文本。"""

    def __init__(self, model: str | None = None, chunk_ms: int | None = None,
                 device: str = "cuda"):
        from nemo.collections.asr.models import ASRModel

        self.model_name = model or config.NEMO_ASR_MODEL
        self.chunk_ms = chunk_ms or config.NEMO_ASR_CHUNK_MS
        self.device = device
        # 模型权重走项目内的 HF 缓存，不写 HOME
        os.environ.setdefault("HF_HOME", os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", ".cache", "hf"))
        self._model = ASRModel.from_pretrained(self.model_name)
        if device == "cuda":
            self._model = self._model.cuda()
        self._model.eval()
        self._reset_state()

    def _reset_state(self):
        self._buffer = bytearray()
        self._text = ""
        # 流式状态（cache-aware 的卷积缓存 + RNNT 状态）
        self._cache = None
        self._prev = ""

    async def accept(self, chunk: bytes) -> str:
        """喂一帧 16kHz 单声道 PCM16，返回新增的识别文本。"""
        return await asyncio.to_thread(self._accept_sync, chunk)

    def _accept_sync(self, chunk: bytes) -> str:
        import numpy as np

        self._buffer.extend(chunk)
        # 每积累一个 chunk 时长就跑一次增量推理
        need = int(config.SAMPLE_RATE * self.chunk_ms / 1000) * 2
        if len(self._buffer) < need:
            return ""
        pcm = np.frombuffer(bytes(self._buffer[:need]), dtype=np.int16)
        self._buffer = self._buffer[need:]
        audio = pcm.astype(np.float32) / 32768.0

        try:
            out = self._model.streaming_transcribe(
                [audio], chunk_ms=self.chunk_ms, verbose=False)
        except AttributeError:
            # 老版本 NeMo 没有 streaming_transcribe，退回整句识别
            out = self._model.transcribe([audio], verbose=False)
        text = out[0] if hasattr(out, "__getitem") else str(out)
        text = getattr(text, "text", text) or ""
        new = text[len(self._prev):] if text.startswith(self._prev) else text
        self._prev = text
        self._text = text
        return new.strip()

    async def final(self) -> str:
        return self._text

    def reset(self) -> None:
        self._reset_state()
