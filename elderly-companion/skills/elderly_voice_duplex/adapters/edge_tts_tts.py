"""edge-tts TTS 适配器（微软神经语音，中文质量好）。

为什么用它而不是 MagpieTTS：见 docs/SDK-CHOICES.md 的语音环回测试——
MagpieTTS v2607 把"降压药"读成"将鸭药"（医疗围栏失配）、把"您"读成"你"
（对老人失去敬语）、短句崩坏（"嗯。"→"然后。"）。而 edge-tts 的中文发音是清楚的：
ASR 测试用的合成音全部来自它，Paraformer 拿到 1.3% CER 且"降压药"识别正确。

⚠️ 隐私边界：只有**文本**发往微软，老人语音不出网（ASR 在本地）。
   若产品要求全本地，换 CosyVoice（适配器接口不变）。

⚠️ 依赖公网。断了就没声音——上层应能降级（见 server.py 的错误分支）。
"""
from __future__ import annotations

import asyncio
import os

from .. import config
from .base import TTSBackend

# 微软中文音色。Xiaoxiao 偏温暖女声，Yunjian 偏中年男声，Xiaoyi 偏年轻。
# 老人陪伴场景默认温暖女声；可用 EVD_TTS_VOICE 覆盖。
VOICES = {
    "warm-female": "zh-CN-XiaoxiaoNeural",
    "young-female": "zh-CN-XiaoyiNeural",
    "middle-male": "zh-CN-YunjianNeural",
    "warm-male": "zh-CN-YunxiNeural",
}


class EdgeTTS(TTSBackend):
    """edge-tts 合成，产出 16kHz 单声道 PCM16 字节。"""

    def __init__(self, voice: str | None = None, rate: str | None = None,
                 sample_rate: int | None = None):
        import edge_tts

        self._mod = edge_tts
        self.voice = (voice or os.environ.get("EVD_TTS_VOICE")
                      or VOICES.get("warm-female"))
        # 老人听力反应慢，比默认稍慢更合适
        self.rate = rate or os.environ.get("EVD_TTS_RATE") or "-10%"
        self.sample_rate = sample_rate or config.SAMPLE_RATE

    async def speak(self, text: str, should_stop=None) -> bytes:
        """合成一句话，返回 PCM16 字节。空文本返回 b""。"""
        text = (text or "").strip()
        if not text:
            return b""
        chunks: list[bytes] = []
        communicate = self._mod.Communicate(text, self.voice, rate=self.rate)
        # stream() 是异步生成器；要更低延迟可边收边送 miniaudio 流式解码
        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        if not chunks:
            return b""
        return await asyncio.to_thread(self._to_pcm16, b"".join(chunks))

    def _to_pcm16(self, mp3: bytes) -> bytes:
        import miniaudio
        import numpy as np

        decoded = miniaudio.decode(
            mp3, nchannels=1, sample_rate=self.sample_rate,
            output_format=miniaudio.SampleFormat.SIGNED16)
        samples = np.array(decoded.samples, dtype=np.int16)
        return samples.tobytes()
