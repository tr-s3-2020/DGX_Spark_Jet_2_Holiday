# ASR/TTS 选型记录

## 结论先说

| 组件 | 选择 | 状态 |
|---|---|---|
| ASR | **`funasr/paraformer-zh` + `ct-punc`** | ✅ 中文实测通过 |
| TTS | `nvidia/magpie_tts_multilingual_357m`（NeMo） | 支持中文，未实测 |
| LLM | Qwen3.6-35B-A3B via vLLM（部署仓库） | ✅ 已验证 |

原 spec 指定的 NVIDIA Nemotron ASR **中文不可用**，已用英文对照实验定位后更换，
过程见下文「中文 ASR 实测」。

## 为什么不是 Riva / TensorRT-LLM

用户原始 spec 指定 NVIDIA Riva + TensorRT-LLM。本机实况：

| 约束 | 值 | 后果 |
|---|---|---|
| Docker | **无** | Riva 只以 NGC 容器分发 → 服务端起不来 |
| nvcc / CUDA toolkit | **无** | TensorRT-LLM 无法源码构建 |
| 驱动 | 535.86.05（CUDA 12.2） | NGC 容器要更新驱动 |
| GPU | A800 80GB，SM86 Ampere | ✅ 对 NeMo 的两个模型都够 |

`nvidia-riva-client` 2.27.0 可以从 PyPI 装，但它只是客户端，需要有 Riva server
在别处。**所以 Riva 留成适配器接口，将来有容器环境再换实现。**

## 可用的 NVIDIA SDK（本机实测）

`nemo_toolkit` 3.0.0 可直接 pip 安装，且**不动 torch**（dry-run 验证：torch 2.10.0、
transformers、numpy、jinja2、requests 全部 already satisfied）。

```bash
pip install 'nemo-toolkit[asr,tts]'
```

⚠️ 装的时候 `cdifflib` / `pyopenjtalk` 会报 `Python.h: No such file or directory`——
就是部署仓库问题 #15 那个老毛病。设 CPATH 指向解包好的头文件即可：

```bash
HDR=$PROJECT/.cache/py310-headers/usr/include
CPATH="$HDR:$HDR/python3.10" pip install 'nemo-toolkit[asr,tts]'
```

这两个包是 `nemo_text_processing` 的**日文**依赖（Janome/pyopenjtalk），中文场景
用不到，但 pip 会照样编译它们。

## ⚠️ 中文 ASR 实测：不可用

模型卡声称 `zh-CN` 属 broad-coverage 档、FLEURS CER 19–21%。**实测远差于此**：

用 edge-tts 合成 4 句中文（清音、无障碍），`transcribe()` 整句识别：

| 参考 | 识别 | CER |
|---|---|---|
| 今天早上起来腿沉得很，买菜走两步就得歇着。 | 今天早上起来腿沉的狠, 买菜走两步就得歇着。 | 57.1% |
| 我那个降压药今天能不能吃两颗？ | 我那个**酱鸭药**, 今天能不能吃两颗。 | 86.7% |
| 昨天小明打电话来说周末要回来看我。 | 昨天小明打电话来说, 周末要回来看我。 | 58.8% |
| 外面太阳挺好的，我下楼溜达了一圈。 | 外面太阳挺好的, 我下楼溜**带**了一圈。 | 64.7% |

**平均 CER 66.8%**（合成清音；真实老人语音会更差）。

错误形态高度一致：**同音字错**（狠/很、酱鸭/降压、带/达）。声学识别是对的，
汉字选错——说明中文语言建模数据不足，不是配置问题（显式指定语言、加 prompt
都无效，已试）。

### 英文对照实验：问题精确定位在中文

用同样的合成语音、同样的加载和解码管线测英文（4 句，内容对应）：

| 参考 | 识别 | WER |
|---|---|---|
| This morning my legs felt heavy, and I had to stop and rest every few steps when I went out to buy groceries. | 完全相同 | **0.0%** |
| Can I take two of my blood pressure pills today? | 完全相同 | **0.0%** |
| My son called yesterday and said he is coming to visit me this weekend. | 完全相同 | **0.0%** |
| The sun is lovely outside, so I took a walk around the block. | 完全相同 | **0.0%** |

**平均 WER 0.0%**。

结论：音频加载、重采样、模型推理、解码全链路**完全正常**，模型本身对英文是
SOTA 级表现。短板精确地只在**中文语言建模**，与集成方式无关。所以换 ASR 后端
或微调中文是正解，不需要去排查管线。

（顺带发现：模型会在末尾附加语言标签 `<en-US>` / `<zh-CN>`，适配器里已剥掉——
不剥会污染对话历史、安全围栏匹配和家属日报。）

### 为什么这是安全问题

`降压药` → `酱鸭药` 意味着 `prompts.py` 里的医疗围栏**会漏**：正则匹配的是
"降压药"，ASR 吐出来的是"酱鸭药"，P0 拦截直接失效。而用药安全是这个产品
不可退让的底线。

### 三条路

1. **换 ASR**（已做）：中文更强的开源模型——
   - `Paraformer-large`（阿里，中文 SOTA 级，有 VAD+punc，实时版成熟）
   - `faster-whisper-large-v3`（中文好，但实时性差些）
   - `SenseVoice-Small`（阿里，多语言、延迟低）
   仍然走 NeMo 的 adapter 接口，上层不动。
2. **微调 Nemotron**：NVIDIA 有官方指南（"Fine-Tune Nemotron 3.5 ASR for Your
   Language, Domain, or Accent"）。用真实老人语音 + 医学词表微调是长期正解，
   A800 80GB 训 0.6B 模型完全可行。
3. **后处理纠错**：同音字混淆集 + LLM 纠错。便宜但不可靠，**不能单独用于
   医疗围栏**——围栏必须跑在 ASR 之前或之上，不能依赖 ASR 的字面正确。

---

## 最终选择：Paraformer（已实测通过）

按上面第 1 条路换成 `funasr/paraformer-zh` + `ct-punc`（FunASR 1.4.16，
模型从 HuggingFace `funasr/*` 下载，不需要 ModelScope）。

同样那 4 句中文合成音：

| 参考 | Paraformer + ct-punc 识别 |
|---|---|
| 我那个降压药今天能不能吃两颗？ | 我那个降压药今天能不能吃两颗？ ✅ |
| 外面太阳挺好的，我下楼溜达了一圈。 | 外面太阳挺好的，我下楼溜达了一圈。 ✅ |
| 昨天小明打电话来说周末要回来看我。 | 昨天小明打电话来说**，**周末要回来看我。 |
| 今天早上起来腿沉得很，买菜走两步就得歇着。 | 今天早上起来腿沉的**很**，买菜走两步就得歇着。 |

- 含标点 CER **9.6%**，**去掉标点后仅 1.3%**（残余几乎全是标点缺失，ct-punc 已补回）
- **"降压药"识别正确** → 医疗围栏能生效，这是选它的决定性理由
- 延迟：平均 **60ms**（RTF 0.014），首次含预热 563ms
- 模型 849 MB，`pip install funasr` 即可

### 为什么用离线模型 + VAD 切段，不用流式版

1. 离线版精度明显更高（流式版通常差一档）
2. duplex 状态机本来就等 1800ms 静默才收轮，天然完成了分段
3. 代价只是 filler 判定晚 ~60ms，可忽略

代价：`accept()` 阶段拿不到部分转写，所以"在想词"判定推迟到收轮时做
（`duplex.py:_close_turn` 里补调 `asr.final()`）。

### 端到端实测（真实音频 → Paraformer → Qwen3.6 → 回复）

| 场景 | 识别 | 结果 |
|---|---|---|
| P0 用药安全 | 我那个降压药今天能不能吃两颗？ | 走标准话术，建议联系子女/社区医生 ✅ |
| P1 体征 | 今天早上起来腿沉得很，买菜走两步就得歇着。 | 顺势关切 ✅ |
| 日常 | 昨天小明打电话来说，周末要回来看我。 | "那真好呀。您想好周末要给他做什么好吃的了吗？" ✅ |
| 日常 | 外面太阳挺好的，我下楼溜达了一圈。 | "真好呀。外面暖和吧？溜达完，累不累？" ✅ |

链路总耗时 1.1~1.7s（含 LLM 关思考后的完整生成）。

## 其他记录

- **CUDA graph 被禁用**：NeMo 日志 `Driver supports cuda toolkit version 12.2,
  but the driver needs to support at least 12.6` → RNNT 解码走非 graph 路径，
  速度变慢。不影响正确性，但端到端延迟预算要重测。
- **模型加载走 `.nemo`**：`from_pretrained` 实际恢复的是 `.nemo` 归档
  （2.37 GB）而不是 `model.safetensors`，会额外下载。
- **MagpieTTS 音色克隆被移除**：v2607 起 zero-shot 克隆因安全原因下架，
  spec 里"亲切晚辈音色"只能用内置音色。
