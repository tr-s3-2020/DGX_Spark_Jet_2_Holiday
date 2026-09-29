# 任务四：家属摘要同步 `family-digest-sync`


一句话：**别人判断"老人有没有事"，我决定"告不告诉家属、什么时候告诉、说到什么程度"。**

---

## 我负责什么

| 归属 | 内容 |
|---|---|
| **D 独享** | ① 三档路由与通知编排（B 不做转换、C 不管通知）② **跨日趋势**（B 只看单句、C 不存健康序列，只有 D 有时间序列）③ 家属侧一切：授权、脱敏、渲染、推送、回执 |
| **D 不做** | 提取健康信号（B）、语音交互（A）、写记忆/年谱（C，**只读**）、诊断与剂量建议（B 的固定话术原样透传）、保存原文全文 |

**P0 纪律**：P0 的源头**只有** A 的 `safety_level=P0` 和 B 的 `safety.blocked=true`。趋势再差也只升到 P1——D 不自创告警。

详见 [`docs/module-4-scope.md`](docs/module-4-scope.md)。

## 目录

```
docs/
  module-4-scope.md            任务边界（我做什么、不做什么）
  to-b-interface-reply.md      给任务二 B 的接口回执（含三项待其确认）
  bugfix-datetime-z.md         ★ 3.10 时间戳 bug 的修复说明（含前后对照）
modules/family-digest-sync/
  SKILL.md                     ★ 技能定义：宿主/Agent 怎么调用我
  README.md                    模块说明、运行方式、验证过的行为
  pyproject.toml               依赖只有 pydantic>=2
  src/family_digest/           实现（routing / trend / dedup / consent / cards / state / store / service / adapters）
  scripts/                     digest_cli（跑场景）、host_invoke_demo（宿主调用留痕）
                               chain_smoke（A/B/C 报文串联）、live_b_to_d（★ 真跑 B 的代码）
  references/                  宿主契约、上游契约与坑、卡片模板与禁用措辞
  examples/                    四个场景 + 三份运行日志
  tests/                       68 项（含 3.10 回归）
```

**本分支只新增文件，不改动任何已有文件**，合入主分支零冲突。

## 怎么跑

```bash
cd modules/family-digest-sync
pip install -e .
python -m pytest tests/ -q                    # 68 项应全过
python scripts/digest_cli.py --scenario examples/scenario_p1_trend.json --channel console --reset
python scripts/host_invoke_demo.py            # 产出 examples/host-invocation-log.md
```

不需要 GPU、不需要模型服务、不需要网络。

已在 **真实 CPython 3.10.21** 上验证 68 项全过（本模块声明 `>=3.10`，
CI 会在 3.10/3.11/3.12/3.13 四个版本上跑）。

## 与上游的联调状态

| 链路 | 状态 | 证据 |
|---|---|---|
| **B → D** | ✅ **真跑通**：subprocess 起 B 的 CLI 拿真实输出喂 D | `examples/live-b-to-d-log.md` |
| A → D | 契约对齐，未联调（A 是 WebSocket，需 ASR/TTS 环境） | `references/upstream-contracts.md` |
| C → D | 契约对齐，未联调（C 是 HTTP 8765） | `examples/chain-smoke-log.md` |

B 跑法（免装 `nemoguardrails`）：

```bash
set TRIAGE_GUARDRAILS_ENABLED=false
set SEMANTIC_BACKEND=mock
python scripts/live_b_to_d.py
```

实测：用药阻断 → **P0**；腿沉/失眠 → 计入 bodily 趋势；闲聊 → 不作为健康内容。

## 待确认（需要群里拍板）

1. **给 B**：新版 `TriageResult` 请加 `degraded: bool`（或保留 `metadata`）。没有它，D 分不清"正常跑完没信号"和"模型挂了被降级"，只能在每次降级时对家属说"本次未能获取"——家属会习惯性忽略日报。D 这边接口已预留，B 一加即生效。
2. **给 B**：`docs/interfaces.md` 与代码有四处不一致（详见回执第 5 节），其中 `to_digest_record` 会丢 `guardrail_triggered`，那是 P0 的源头之一。。
