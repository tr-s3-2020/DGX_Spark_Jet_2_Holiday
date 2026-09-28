# DGX_Spark_Jet_2_Holiday

AI 陪伴老人：全双工语音对话 + 隐式健康探针 + 长程记忆 + 家属摘要。
本次登记任务二 `implicit-health-triage`：隐式健康探针与用药安全，负责人 **Yunsheng**。

## 协作文档入口

- [团队协作指南](团队协作指南.md)：分工、分支、PR 和完成标准。
- [模块架构](docs/architecture.md)：模块范围、数据流和集成边界。
- [任务二接口约定](docs/interfaces.md)：输入输出、异常、超时及下游责任。
- [给任务四的最新反馈](docs/to-d-interface-feedback.md)：degraded 字段、完整报文交接和 legacy 接口说明。
- [任务二交接与资源登记](docs/task2-handoff.md)：实现状态、验证证据、待办和 DGX 资源。
- [任务二使用说明](docs/task2-readme.md)：安装、Python/HTTP 调用和 Qwen 切换。
- [接口样例](examples/implicit-health-triage.json)：可检查的输入与预期输出。
- [第三项：关怀式生命故事对话与记忆支持（卢易锋）](docs/module-3/README.md)：目标、方法、模块边界与协同方式。
- [第三项可运行联调包](modules/life-memoir-retriever/README.md)：安装、接口、测试与协作分工。
- [第三项设计档案](docs/module-3/DESIGN.md)：完整架构目标；已实现范围以联调包为准。
- [老人对话与口述史资料库](research/elder-conversation-oral-history/README.md)：来源笔记、研究综述、对话 SOP 与评估草案。
- [语音 Skill 选型记录](modules/elderly-voice-duplex/docs/SDK-CHOICES.md)：ASR/TTS 的全部实测数据与方案变更。

## 三个 Skill 的当前状态

| Skill | 负责分支 | 状态 |
|---|---|---|
| 一 `elderly-voice-duplex`（全双工拟人倾听与外呼） | `ziyang-module-1` | ✅ 可跑。代码 `modules/elderly-voice-duplex/skills/elderly_voice_duplex/`；ASR/TTS = Paraformer + edge-tts；端到端语音闭环 4/4 通过 |
| 二 `implicit-health-triage`（隐式健康探针与用药安全） | `docs/task2-collaboration-handoff` | ⚠️ **仅接口文档**，实现代码尚未入库（拟放 `modules/implicit-health-triage/`） |
| 三 `life-memoir-retriever`（口述史图谱与长程记忆） | `codex/luyifeng-module-3` | ⚠️ Skill 定义 + JSON Schema + `src/life_memoir/` 实现 + 冒烟脚本已入库，**尚未在本环境安装验证** |

| 四 `family-digest-sync`（代际降噪与异常分级） | `zoe-module-4` | ⚠️ Skill 定义 + `src/family_digest/` 实现 + 54 项测试，**尚未在本环境验证** |

## 联调分支

`integration/skills-1-2-3` 合并以上四个分支用于串联验证。任务二只有接口文档
（实现待入库），所以目前先做**契约级校验**：用 Skill 一的真实输出去比对任务二的
输入输出约定和任务三的 JSON Schema。见
`modules/elderly-voice-duplex/skills/elderly_voice_duplex/tests/test_integration_contract.py`。

## 当前状态

2026-09-28：任务二代码、依赖、Colang 配置、测试、CLI 和样例已迁入 `modules/implicit-health-triage/`，由本 PR 提交，等待团队审查合并。本次不填写其他模块实现状态。



默认分支为 `master`；日常改动走任务分支与 PR，至少由另一名成员检查后合并。

## 各 Skill 启动方式

### 任务一（语音）

```bash
# 1. 起 LLM（Qwen3.6-35B-A3B）
PROFILE=full bash scripts/serve_v019.sh

# 2. 起 WebSocket 全双工服务
cd modules/elderly-voice-duplex
EVD_ASR=funasr EVD_TTS=edge python3 skills/elderly_voice_duplex/server.py --port 8100
```

ASR/TTS 后端可选 `funasr`（Paraformer）/ `edge`（edge-tts）/ `nemo`（Nemotron + MagpieTTS）。

### 任务二（健康探针）

代码位于 [modules/implicit-health-triage/](modules/implicit-health-triage/README.md)，可按以下命令安装运行。

```bash
cd modules/implicit-health-triage
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m implicit_health_triage.task2.cli '我降压药今天能不能吃两颗？'
uvicorn implicit_health_triage.task2.api:app --host 127.0.0.1 --port 8080
```

Windows 命令及输入输出见[使用说明](docs/task2-readme.md)。默认 Mock；本地 Qwen 接口已预留，真实模型和完整项目联调尚未验证。

### 任务三（长程记忆）

```bash
cd modules/life-memoir-retriever
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -c constraints-tested.txt -e '.[dev]'
export MEMORY_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
memory-skill serve --config examples/config.fixture.json --auth examples/auth.fixture.json
```

### 任务四（家属摘要）

```bash
cd modules/family-digest-sync
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python scripts/digest_cli.py --scenario examples/scenario_p0_medication.json
```
