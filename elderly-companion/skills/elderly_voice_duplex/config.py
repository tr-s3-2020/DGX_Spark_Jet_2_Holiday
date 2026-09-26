"""elderly-voice-duplex: 全双工拟人倾听与外呼技能。

解决老人说话停顿长、语速慢、发音轻、偶尔伴随咳嗽叹气的交互延迟与打断问题。

设计文档见 docs/，本文件是可调参数集中地——所有"魔法数字"都放这里，
现场调优时只改这一个文件。
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------- VAD（老人特化）
# 常用 VAD 静默阈值是 700ms，对老人远远不够：他们一句话中间会停顿很久去想词，
# 700ms 就会被误判为"说完了"而强行切入。默认放到 1800ms。
VAD_SILENCE_MS = _i("EVD_VAD_SILENCE_MS", 1800)
# 老人发音轻、音量小，VAD 能量阈值要比常规更低，否则整句被当静音丢掉。
VAD_ENERGY_THRESHOLD = _f("EVD_VAD_ENERGY", 0.35)
# 判定为有效语音的最短时长，过滤咳嗽/叹气/环境噪声。
VAD_MIN_SPEECH_MS = _i("EVD_VAD_MIN_SPEECH_MS", 350)
# 一次发言允许的最大时长，超时强制切轮（防老人说着说着断了）。
VAD_MAX_SPEECH_MS = _i("EVD_VAD_MAX_SPEECH_MS", 20000)

# ---------------------------------------------------------------- 延迟预算
# 端到端首字发声目标 300~500ms。TTFT 超预算时记日志，便于现场排查。
TTFT_BUDGET_MS = _i("EVD_TTFT_BUDGET_MS", 500)
# 垫音必须短：老人已经在等我们接话了，垫音本身不能再造成等待感。
FILLER_MAX_CHARS = _i("EVD_FILLER_MAX_CHARS", 12)

# ---------------------------------------------------------------- 打断（barge-in）
# 老人在我们说话时插话，需要立刻停嘴。阈值同样要放低（声音轻），
# 但不能低到环境噪声就触发——这里要求连续多少ms超过阈值才认定是插话。
BARGE_IN_ENERGY = _f("EVD_BARGE_IN_ENERGY", 0.30)
BARGE_IN_HOLD_MS = _i("EVD_BARGE_IN_HOLD_MS", 120)
# 停嘴后留一小段静默再开始听，避免把 TTS 的尾音当成老人插话。
BARGE_IN_COOLDOWN_MS = _i("EVD_BARGE_IN_COOLDOWN_MS", 250)

# ---------------------------------------------------------------- LLM
LLM_BASE = os.environ.get("EVD_LLM_BASE", "http://127.0.0.1:8000/v1")
LLM_MODEL = os.environ.get("EVD_LLM_MODEL", "qwen3.6-35b-a3b")
# 这个模型的思考过程很长，额度不够会只吐思考、答案是 null。
LLM_MAX_TOKENS = _i("EVD_LLM_MAX_TOKENS", 2048)
LLM_TEMPERATURE = _f("EVD_LLM_TEMPERATURE", 0.6)
# 垫音用单独的低额度调用：要快，且不需要思考。
FILLER_MAX_TOKENS = _i("EVD_FILLER_MAX_TOKENS", 48)
FILLER_TEMPERATURE = _f("EVD_FILLER_TEMPERATURE", 0.9)

# ---------------------------------------------------------------- ASR / TTS
ASR_BACKEND = os.environ.get("EVD_ASR", "nemo")    # text | nemo | funasr
TTS_BACKEND = os.environ.get("EVD_TTS", "nemo")     # text | nemo | edge
VAD_BACKEND = os.environ.get("EVD_VAD", "text")     # text | silero

NEMO_ASR_MODEL = os.environ.get(
    "EVD_NEMO_ASR_MODEL", "nvidia/nemotron-3.5-asr-streaming-0.6b")
# 流式 chunk：80/160/320/560/1120ms。老人语速慢，chunk 太碎会把一句话切烂。
NEMO_ASR_CHUNK_MS = _i("EVD_NEMO_ASR_CHUNK_MS", 320)
NEMO_TTS_MODEL = os.environ.get(
    "EVD_NEMO_TTS_MODEL", "nvidia/magpie_tts_multilingual_357m")
# Magpie 说话人代号；v2607 起没有 zero-shot 克隆，只能用内置音色。
NEMO_TTS_SPEAKER = _i("EVD_NEMO_TTS_SPEAKER", 0)
# 语速：老人听力反应慢，比默认稍慢更合适（1.0=默认）。
NEMO_TTS_PACE = _f("EVD_NEMO_TTS_PACE", 0.92)

SAMPLE_RATE = _i("EVD_SAMPLE_RATE", 16000)
