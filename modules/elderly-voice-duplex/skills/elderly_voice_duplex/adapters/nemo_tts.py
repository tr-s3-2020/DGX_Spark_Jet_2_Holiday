"""NVIDIA MagpieTTS 适配器（多语言，含中文）。

选型：`nvidia/magpie_tts_multilingual_357m`，NeMo `MagpieTTSModel`。
支持 12 种语言（含 zh），357M 参数，Ampere 可用。

⚠️ **v2607 起移除了 zero-shot 音色克隆**（NVIDIA 以安全原因下架），所以 spec 里
   "亲切晚辈音色"只能用内置音色。要克隆音色需回退早期版本或改用 Riva。

⚠️ NeMo 在加载 codec 时会从一个**硬编码 URL** 拉 speaker encoder 权重
   （`audio_codec.py` 里 `use_scl_loss` 分支，推理用不到但要下载）。本适配器把它
   重定向到项目内缓存，避免每次加载都走一遍公网。

用法上的两个坑（都踩过）：
  1. 单次合成上限约 20 秒；长文本要先按句切分
  2. 需要 text normalization（数字/日期读法），长文本必须带标点——
     我们的 ASR 侧已经用 ct-punc 补了标点，正好接上
"""
from __future__ import annotations

import asyncio
import os

from .. import config
from .base import TTSBackend

_SPK_ENC_URL = ("https://huggingface.co/Edresson/Speaker_Encoder_H_ASP/"
                "resolve/main/pytorch_model.bin")


def _local_speaker_encoder() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..",
                        ".cache", "voice-probe", "tts", "speaker_encoder.bin")


def _patch_speaker_encoder_url() -> None:
    """把硬编码的 speaker encoder URL 改指到本地缓存。"""
    local = _local_speaker_encoder()
    if not os.path.exists(local):
        return
    try:
        from nemo.collections.tts.modules import audio_codec_modules
        orig = audio_codec_modules.load_fsspec

        def patched(path, *a, **kw):
            if isinstance(path, str) and path == _SPK_ENC_URL:
                path = local
            return orig(path, *a, **kw)

        audio_codec_modules.load_fsspec = patched
    except Exception:  # noqa: BLE001  打不上补丁就照原样走公网
        pass


class MagpieTTS(TTSBackend):
    """MagpieTTS 中文合成。speak() 产出 16kHz 单声道 PCM16 字节。"""

    def __init__(self, model: str | None = None, speaker: int | None = None,
                 pace: float | None = None, device: str = "cuda"):
        from nemo.collections.tts.models import MagpieTTSModel

        os.environ.setdefault("HF_HOME", os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "..", ".cache", "hf"))
        _patch_speaker_encoder_url()

        self.model_name = model or config.NEMO_TTS_MODEL
        self.speaker = config.NEMO_TTS_SPEAKER if speaker is None else speaker
        self.pace = config.NEMO_TTS_PACE if pace is None else pace
        self.device = device
        self.model = MagpieTTSModel.from_pretrained(self.model_name)
        if device == "cuda":
            self.model = self.model.cuda()
        self.model.eval()
        self.sample_rate = int(self.model.output_sample_rate)

    async def speak(self, text: str, should_stop=None) -> bytes:
        """合成一句话，返回 PCM16 字节（单声道 16kHz）。空文本返回 b""。"""
        text = (text or "").strip()
        if not text:
            return b""
        return await asyncio.to_thread(self._speak_sync, text)

    def _speak_sync(self, text: str) -> bytes:
        import numpy as np
        import torch

        # 超过 ~20s 的文本按句切分，逐段合成后拼接
        parts = self._split_for_limit(text)
        chunks: list[np.ndarray] = []
        for part in parts:
            audio = self._synth_one(part)
            if audio is not None:
                chunks.append(audio)
        if not chunks:
            return b""
        wav = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        # 语速：重采样到目标速率（>1 变快）
        if self.pace and abs(self.pace - 1.0) > 1e-3:
            idx = np.arange(0, len(wav), self.pace)
            wav = np.interp(idx, np.arange(len(wav)), wav)
        pcm = np.clip(wav, -1.0, 1.0)
        return (pcm * 32767.0).astype(np.int16).tobytes()

    def _synth_one(self, text: str):
        import torch

        # 中文走 mandarin_phoneme 这套 tokenizer（模型内置 15 种语言的音素/字符
        # tokenizer，AggregatedTTSTokenizer.encode 必须显式指定用哪套）
        ids = self.model.tokenizer.encode(text, "mandarin_phoneme")
        if not ids:
            return None
        batch = {
            "text": torch.tensor([ids], device=self.device),
            "text_lens": torch.tensor([len(ids)], device=self.device),
            # 多音色模型靠这个键选音色；v2607 起只有 5 个内置音色（无 zero-shot 克隆）
            "speaker_indices": torch.tensor([self.speaker], device=self.device),
        }
        state = self.model.create_chunk_state(1)
        with torch.no_grad():
            out = self.model.generate_speech(batch, state, [True], True)
            audio, _lens, _ = self.model._codec_helper.codes_to_audio(
                out.predicted_codes, out.predicted_codes_lens)
        return audio[0].float().cpu().numpy()

    @staticmethod
    def _split_for_limit(text: str, max_chars: int = 60) -> list[str]:
        """按中文句读标点切分，避免单次合成超过 20s 上限。"""
        import re
        sents = [s for s in re.split(r"(?<=[。！？；])", text) if s.strip()]
        parts, cur = [], ""
        for s in sents:
            if len(cur) + len(s) > max_chars and cur:
                parts.append(cur)
                cur = s
            else:
                cur += s
        if cur.strip():
            parts.append(cur)
        return parts or [text]
