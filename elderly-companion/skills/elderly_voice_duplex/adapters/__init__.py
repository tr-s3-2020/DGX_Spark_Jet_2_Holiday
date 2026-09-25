"""后端工厂：按 config 选 text / nemo / vllm。"""
from __future__ import annotations

from .. import config as _cfg


def make_llm():
    from .llm_vllm import VLLMBackend
    return VLLMBackend()


def make_vad():
    if _cfg.VAD_BACKEND == "text":
        from .text import TextVAD
        return TextVAD()
    if _cfg.VAD_BACKEND == "silero":
        from .silero_vad import SileroVAD
        return SileroVAD()
    raise ValueError(f"未知 VAD 后端: {_cfg.VAD_BACKEND}")


def make_asr():
    if _cfg.ASR_BACKEND == "text":
        from .text import ScriptedASR
        return ScriptedASR()
    if _cfg.ASR_BACKEND in ("nemo", "funasr"):
        if _cfg.ASR_BACKEND == "funasr":
            from .funasr_asr import ParaformerASR
            return ParaformerASR()
        from .nemo_asr import NemoStreamingASR
        return NemoStreamingASR()
    raise ValueError(f"未知 ASR 后端: {_cfg.ASR_BACKEND}")


def make_tts():
    if _cfg.TTS_BACKEND == "text":
        from .text import RecordingTTS
        return RecordingTTS()
    if _cfg.TTS_BACKEND == "nemo":
        from .nemo_tts import MagpieTTS
        return MagpieTTS()
    if _cfg.TTS_BACKEND == "edge":
        from .edge_tts_tts import EdgeTTS
        return EdgeTTS()
    raise ValueError(f"未知 TTS 后端: {_cfg.TTS_BACKEND}")
