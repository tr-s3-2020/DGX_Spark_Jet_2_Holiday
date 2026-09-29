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
| 全双工状态机（含打断） | ✅ 完成，文本后端可测；WebSocket 上打断已实测生效 |
| 医疗安全围栏 | ✅ 完成，P0/P1 分级 + 标准话术 |
| LLM 链路（流式 + 关思考） | ✅ 完成，**首字延迟 109ms**（预算 500ms） |
| ASR（Paraformer + ct-punc） | ✅ 中文 CER 1.3%，降压药识别正确 |
| TTS | ✅ edge-tts 中文可用（MagpieTTS 中文有缺陷，见 SDK-CHOICES） |
| WebSocket 服务 | ✅ 完成，二进制音频帧 + 控制帧 |
| 网页客户端 | ✅ web/index.html，服务端 `/` 直接打开 |
| 真实音频端到端 | ✅ 音频进 → 识别 → 回复 → 语音出 全通（tests/test_ws_audio.py） |
| A → D 联调 | ✅ 安全分级喂给 skill4，家属卡片随通话更新（tests/test_family_card.py） |

## 快速开始

```bash
# 1. 起 LLM（另一个仓库的部署，这里只用它的 HTTP 接口）
cd ../../ && PROFILE=full bash scripts/serve_v019.sh

# 2. 只验证技能逻辑（不需要音频硬件、不需要 FunASR）
cd modules/elderly-voice-duplex
python3 skills/elderly_voice_duplex/tests/test_duplex_live.py

# 3. 起语音服务（ASR/TTS 用真后端）
cd skills/elderly_voice_duplex
EVD_ASR=funasr EVD_TTS=edge python3 server.py --port 8100

# 4. 浏览器打开 http://127.0.0.1:8100/ ，点"接听"授权麦克风即可对话
#    （getUserMedia 要安全上下文，从服务端打开比 file:// 稳）

# 5. 没有麦克风时的回归测试
python3 tests/test_ws_audio.py       # 真实音频走完整 WebSocket 链路
python3 tests/test_ws_protocol.py    # 不起进程、纯假后端，验证协议层
python3 tests/test_voice_loop.py     # ASR->LLM->TTS 闭环 + TTS 回读校验
python3 tests/test_family_card.py    # A→D：安全分级进 skill4 卡片
python3 tests/test_barge_in.py       # 打断：插话要能立刻停嘴
node    tests/test_web_framing.js    # 网页客户端攒帧/重采样逻辑
```

## 打断（barge-in）是怎么接上的

播放必须由状态机自己发起（要据此进 SPEAKING，否则无从判断打断），所以 `_speak`
只负责切状态，然后把合成丢到**后台任务**：

```
feed_audio ──收轮──► _respond ──► _speak ──切 SPEAKING、起 _play 后台任务──► 返回
                                              │
服务端继续 ws.receive() ◄─────────────────────┘
        │
        └─► 老人开口（持续 120ms 超过阈值）──► _barge_in ──取消 _play、回 LISTENING
```

原来是 `await self._speak(...)` 一路 await 到 TTS 合成结束，那期间上层根本没在
`ws.receive()`，音频帧全躺在 socket 缓冲区里，`_barge_in` 只有一个入口
（`feed_audio`）所以永远调不到——症状是"插话完全没反应，整句照常播完"。
`duplex.py` 里 `_tts_task` / `_cancel_speech` 本来就是为这件事准备的，但从没被赋值。

被打断时**已经合成好的音频也不会推给客户端**：`_barge_in` 会 await 任务真正结束，
`_play` 里再查一次 `_cancel_speech`。edge-tts 在流式接收中途不查 `should_stop`，
所以实际靠的是取消任务，不 await 掉就会有"明明打断了还是听到整句"。

### 两个容易漏的点

**1. 合成结束不等于说完。** 服务端是**整句一次性**把 PCM 推给客户端的，合成完
客户端还要播十几秒。所以 `_play` 推完音频后要**按播放时长占住 SPEAKING**
（`_hold_while_playing`），否则服务端合成一结束就回 LISTENING，那十几秒里老人
插话会被当成新一轮发言，打断检测完全覆盖不到。

**2. 已推出去的音频服务端停不到。** 音频早就上了客户端的播放队列，服务端取消
合成对它没用。所以 `_barge_in` 会额外下发一次 `BARGED_IN` 状态，客户端收到就把
已排队/正在播的全部 `stop()` 掉、播放时钟归零。少了这一环，症状就是
"明明插话了还是听到整句"。

实测（`.cache/tmp-work/ws_bargein.py` 合成窗口内插话、`ws_bargein2.py` 播放到
一半插话）：P1 话术整句 11.1s，两种时机都能在 130ms 内收到 `BARGED_IN`。
修复前是整句 11.14s 照常播完、状态自己走完。

## 家属卡片（A → D）

每收一轮、判出 P0/P1，服务端就用 skill4 自己的 `normalize_a_safety` 把分级转成
`SafetyEventRecord` 记进去（**不自己拼字段**）。网页上的「家属卡片」区块随通话刷新，
P0 会标红并可一键推送。skill4 不在时整条链路降级，不影响通话。

两个接口：

```
GET  /api/card?elder=小明          今日卡片（tier / title / sections / 触发原因）
POST /api/card/dispatch            {"card_id": "..."}  推送（要 card_id，不是 elder_id）
```

已知缺口（已报 skill4 负责人，见 `docs/to-d-a2d-findings.md`）：语音侧的 P0 只产生
safety event，而 skill4 的卡片正文只渲染健康记录，所以标题是通用的、`sources` 为空、
`acquisition` 误判成"未取得"。页面因此把 `routing.reasons`（如 `voice_safety_p0`）
显示在卡片底部，避免家属被"今天没有需要特别说明的健康观察"误导。

## 目录

```
modules/elderly-voice-duplex/
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
        ├── family_card.py         A→D 薄适配：安全分级进 skill4 + 建卡/推送
        ├── server.py              WebSocket 服务 + 网页客户端静态托管 + 卡片接口
        ├── web/
        │   └── index.html         网页客户端：采音、放音、接听/挂断
        ├── adapters/
        │   ├── base.py            VAD/ASR/TTS/LLM 接口
        │   ├── text.py            文本假后端（无硬件也能单测）
        │   ├── llm_vllm.py        打到本地 Qwen3.6 的流式客户端
        │   ├── funasr_asr.py      Paraformer 中文 ASR（离线 + VAD 切段）
        │   ├── edge_tts_tts.py    edge-tts 中文合成
        │   ├── nemo_asr.py        NVIDIA Nemotron 流式 ASR（中文不可用，见 SDK-CHOICES）
        │   ├── nemo_tts.py        NVIDIA MagpieTTS（中文有缺陷，见 SDK-CHOICES）
        │   └── silero_vad.py      silero-vad（待接）
        └── tests/
            ├── test_duplex_live.py   对真模型的技能逻辑端到端验证
            ├── test_ws_audio.py      真实音频走完整 WebSocket 链路
            ├── test_ws_protocol.py   协议层回归（不起进程）
            ├── test_voice_loop.py    ASR->LLM->TTS 闭环 + TTS 回读校验
            ├── test_family_card.py   A→D：安全分级进 skill4 卡片
            ├── test_barge_in.py     打断：插话要能立刻停嘴
            └── test_web_framing.js   网页客户端攒帧/重采样回归
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
