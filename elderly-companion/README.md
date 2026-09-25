# elderly-voice-duplex

**全双工拟人倾听与外呼技能** —— AI 陪伴老年人产品的第一个 skill。

## 它解决什么问题

老人打电话时的三个真实困难：

| 困难 | 本技能的处理 |
|---|---|
| 停顿长、语速慢，说着说着去想词 | VAD 静默阈值放到 **1800ms**（常规 700ms），且判定"还在想词"时**只给垫音、不收轮** |
| 发音轻、音量小 | VAD 能量阈值低于常规；打断判定要求持续一小段人声，避免环境噪声误触 |
| 会突然插话 | TTS 播放中检测到人声**立刻停嘴**，已说出口的内容作废，转入倾听 |

## 当前状态

| 部分 | 状态 |
|---|---|
| 在想词判定 + 垫音 | ✅ 完成，已对真实 Qwen3.6 验证（4/4 场景通过） |
| 全双工状态机（含打断） | ✅ 完成，文本后端可测 |
| 医疗安全围栏 | ✅ 完成，P0/P1 分级 + 标准话术 |
| LLM 链路（流式 + 关思考） | ✅ 完成，**首字延迟 109ms**（预算 500ms） |
| ASR（Paraformer + ct-punc） | ✅ 中文 CER 1.3%，降压药识别正确 |
| TTS | ✅ edge-tts 中文可用（MagpieTTS 中文有缺陷，见 SDK-CHOICES） |
| WebSocket 服务 | ✅ 完成 |
| 真实音频端到端 | ✅ ASR+TTS 均已用真实音频验证；缺麦克风采集端 |

## 快速开始

```bash
# 1. 起 LLM（另一个仓库的部署，这里只用它的 HTTP 接口）
cd ../../ && PROFILE=full bash scripts/serve_v019.sh

# 2. 只验证技能逻辑（不需要音频硬件、不需要 NeMo）
cd elderly-companion
python3 skills/elderly_voice_duplex/tests/test_duplex_live.py

# 3. 起 WebSocket 服务
python3 skills/elderly_voice_duplex/server.py --port 8100
```

## 目录

```
elderly-companion/
├── README.md                      ← 本文
├── docs/
│   ├── ARCHITECTURE.md            全双工架构、状态机、延迟预算
│   └── SDK-CHOICES.md             ASR/TTS 选型记录（Riva 为什么不可用）
└── skills/
    └── elderly_voice_duplex/
        ├── SKILL.md               技能定义：定位、prompt 逻辑、边界
        ├── config.py              所有可调参数（VAD 阈值、延迟预算、音色…）
        ├── prompts.py             在想词判定、垫音生成、医疗安全围栏
        ├── duplex.py              全双工状态机 LISTENING/THINKING/SPEAKING/BARGED_IN
        ├── server.py              WebSocket 服务
        ├── adapters/
        │   ├── base.py            VAD/ASR/TTS/LLM 接口
        │   ├── text.py            文本假后端（无硬件也能单测）
        │   ├── llm_vllm.py        打到本地 Qwen3.6 的流式客户端
        │   ├── nemo_asr.py        NVIDIA Nemotron 流式 ASR（待接）
        │   ├── nemo_tts.py        NVIDIA MagpieTTS（待接）
        │   └── silero_vad.py      silero-vad（待接）
        └── tests/
            └── test_duplex_live.py  对真模型的端到端验证
```

## 关键设计决策（详见 docs/）

1. **语音链路必须关思考**。实测：开着思考首个"答案" token 要 2.3s+，关掉只要
   102ms、整句 400ms。老人 500ms 内听不到声音就会以为电话断了。做法是
   `chat_template_kwargs={"enable_thinking": false}`（Qwen 模板原生支持）。
2. **TTFT 要记到第一个"答案" token**，不是第一个 token——开着思考时第一个 token
   是思考内容，对等待声音的老人没有意义。
3. **医疗问题走确定性围栏，不进模型**。NeMo Guardrails 是后续增强，当前用
   关键词/正则预过滤直接返回标准话术：零依赖、可单测、行为确定。
4. **ASR/TTS 后端可插拔**。Riva 在这台机器上跑不了（无 Docker），先用 NeMo；
   将来换 Riva 只改 adapter，上层状态机和 prompt 逻辑不动。

## 环境依赖

```bash
# LLM 侧：复用部署仓库的 .venv-v019（vLLM 0.19.0 + torch 2.10.0+cu126）
# 语音侧：NeMo 装在同一个 venv 里（已验证不动 torch / 不破坏 vLLM）
pip install 'nemo-toolkit[asr,tts]'
```

⚠️ 装 NeMo 时如果 `cdifflib` / `pyopenjtalk` 编译报 `Python.h: No such file or
directory`，设 `CPATH` 指向解包好的 python3.10 头文件（见部署仓库问题 #15）。
