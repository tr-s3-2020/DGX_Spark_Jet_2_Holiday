# ASR/TTS 选型记录

## 结论先说

| 组件 | 选择 | 状态 |
|---|---|---|
| ASR | `nvidia/nemotron-3.5-asr-streaming-0.6b`（NeMo） | ⚠️ **中文不可用，见下** |
| TTS | `nvidia/magpie_tts_multilingual_357m`（NeMo） | 支持中文，未实测 |
| LLM | Qwen3.6-35B-A3B via vLLM（部署仓库） | ✅ 已验证 |

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

### 为什么这是安全问题

`降压药` → `酱鸭药` 意味着 `prompts.py` 里的医疗围栏**会漏**：正则匹配的是
"降压药"，ASR 吐出来的是"酱鸭药"，P0 拦截直接失效。而用药安全是这个产品
不可退让的底线。

### 三条路

1. **换 ASR**（推荐先做）：中文更强的开源模型——
   - `Paraformer-large`（阿里，中文 SOTA 级，有 VAD+punc，实时版成熟）
   - `faster-whisper-large-v3`（中文好，但实时性差些）
   - `SenseVoice-Small`（阿里，多语言、延迟低）
   仍然走 NeMo 的 adapter 接口，上层不动。
2. **微调 Nemotron**：NVIDIA 有官方指南（"Fine-Tune Nemotron 3.5 ASR for Your
   Language, Domain, or Accent"）。用真实老人语音 + 医学词表微调是长期正解，
   A800 80GB 训 0.6B 模型完全可行。
3. **后处理纠错**：同音字混淆集 + LLM 纠错。便宜但不可靠，**不能单独用于
   医疗围栏**——围栏必须跑在 ASR 之前或之上，不能依赖 ASR 的字面正确。

## 其他记录

- **CUDA graph 被禁用**：NeMo 日志 `Driver supports cuda toolkit version 12.2,
  but the driver needs to support at least 12.6` → RNNT 解码走非 graph 路径，
  速度变慢。不影响正确性，但端到端延迟预算要重测。
- **模型加载走 `.nemo`**：`from_pretrained` 实际恢复的是 `.nemo` 归档
  （2.37 GB）而不是 `model.safetensors`，会额外下载。
- **MagpieTTS 音色克隆被移除**：v2607 起 zero-shot 克隆因安全原因下架，
  spec 里"亲切晚辈音色"只能用内置音色。
