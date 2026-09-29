# family-digest-sync

家属摘要同步技能（第四项）。负责人：成员 4（Zoe）。形态：Agent Skills 规范下的 Skill。

**一句话**：把老人今天发生的事，按授权、脱敏、分档同步给家属；跨日趋势只有 D 能判；永远不把"没数据"说成"没事"。

## 目录

```
family-digest-sync/
├── SKILL.md                       技能定义（宿主/Agent 读这份）
├── scripts/
│   ├── digest_cli.py              命令行：跑一个场景文件到底
│   ├── host_invoke_demo.py        宿主调用演示，产出调用留痕
│   ├── chain_smoke.py             A/B/C 真实报文 → D 的端到端串联冒烟
│   └── live_b_to_d.py             ★ 真跑 B 的代码（subprocess）拿真实输出喂 D
├── src/family_digest/
│   ├── models.py                  契约（上游枚举沿用 A/B，D 只新增 Acquisition/Tier）
│   ├── routing.py                 三档路由（P0 只做映射，不自创告警）
│   ├── trend.py                   跨日趋势与基线
│   ├── dedup.py                   幂等三件套
│   ├── consent.py                 授权校验 + 脱敏
│   ├── cards.py                   确定性卡片渲染
│   ├── state.py                   发送状态机（重试 → 死信）
│   ├── timeutil.py                ★ 时间戳收口（兼容 3.10，统一本地墙钟）
│   ├── store.py                   JSON 存储（换数据库只改这个文件）
│   ├── service.py                 统一入口 execute(operation, request)
│   └── adapters/
│       ├── upstream.py            ★ A/B/C 原始报文归一化（补主键、展平 C，
│       │                            兼容 B 的中英文两套 schema）
│       ├── channel.py             推送渠道（mock / console，可换微信）
│       └── llm.py                 摘要润色（默认关闭，确定性优先）
├── references/
│   ├── host-contract.md           宿主怎么调我
│   ├── upstream-contracts.md      A/B/C 的字段与坑
│   └── card-templates.md          文案模板与禁用措辞
├── examples/                      四个场景 + 宿主调用留痕 + B 官方样例 + 串联日志
└── tests/                         68 项（含 3.10 回归）
```

## 安装与运行

```bash
pip install -e .
python -m pytest tests/ -q                                  # 68 项应全过
python scripts/digest_cli.py --scenario examples/scenario_p1_trend.json --channel console --reset
python scripts/host_invoke_demo.py                          # 产出 examples/host-invocation-log.md
python scripts/chain_smoke.py --with-a-safety               # 产出 examples/chain-smoke-log.md
python scripts/live_b_to_d.py                               # 真跑 B：产出 examples/live-b-to-d-log.md
```

`live_b_to_d.py` 会 subprocess 起 B 的 CLI 拿**当场跑出来的**输出（需先设
`TRIAGE_GUARDRAILS_ENABLED=false`、`SEMANTIC_BACKEND=mock`，或设
`IMPLICIT_TRIAGE_HOME` 指向 B 的目录）。

依赖只有 `pydantic>=2`；不需要 GPU、不需要模型服务、不需要网络。

## 上游契约变更（2026-09-28 B 交付代码后）

B 从"只有文档"变成完整 Python 包，同时带来三处影响 D 的变化，均已适配：

| 变化 | 处理 |
|---|---|
| 两套 schema 并存：`task2` 英文扁平 vs 顶层中文 `result` 嵌套 | 适配层两套都认，自动探测，D 内部统一规范形 |
| `type` 是浅层次结构（疼痛/睡眠/食欲/行动能力 是"身体不适"的子类） | 跨日趋势改按**分组**判定，不再按精确 type 相等 |
| 新增 `to_digest_record`，但会丢 `guardrail_triggered` 与 `type=无` 的记录 | 支持其输出作兜底，**推荐消费原始报文**；已在回执中说明 |

详见 `docs/to-b-interface-reply.md`（给 B 的正式回执，含三项待其确认事项）。

## 上游契约变更（2026-09-28 B 交付代码后）

B 从"只有文档"变成完整 Python 包，同时带来三处影响 D 的变化，均已适配：

| 变化 | 处理 |
|---|---|
| 两套 schema 并存：`task2` 英文扁平 vs 顶层中文 `result` 嵌套 | 适配层两套都认，自动探测，D 内部统一规范形 |
| `type` 是浅层次结构（疼痛/睡眠/食欲/行动能力 是"身体不适"的子类） | 跨日趋势改按**分组**判定，不再按精确 type 相等 |
| 新增 `to_digest_record`，但会丢 `guardrail_triggered` 与 `type=无` 的记录 | 支持其输出作兜底，**推荐消费原始报文**；已在回执中说明 |

详见 `docs/to-b-interface-reply.md`（给 B 的正式回执，含三项待其确认事项）。

## Python 3.10 兼容

本模块声明 `requires-python = ">=3.10"`。曾出现过一个 3.10 上必然报错的 bug：
pydantic 把 UTC 时间序列化成 `...Z`，而 `fromisoformat` 3.11 才认 `Z`。
已在 `timeutil.py` 收口修掉，并在**真实 CPython 3.10.21** 上验证 68 项全过。
CI（`.github/workflows/tests.yml`）在 3.10 / 3.11 / 3.12 / 3.13 四个版本上跑。

详见 `docs/bugfix-datetime-z.md`。

## 验证过的行为

| 场景 | 结果 |
|---|---|
| B 用药安全阻断 / A 的 P0 | → P0 立即推送，标题「用药/安全风险，建议尽快联系」 |
| 同类信号连续 3 天中度 | → P1 当日高亮（2 天仍为 P2） |
| 单日 high | → P1 |
| 趋势再差也不会自升 P0 | 有测试守着 |
| 日常低强度信号 | → P2 日报 |
| B 降级（qwen 调用过但无结果） | → `acquisition=no_signal` + "不能据此认定没有问题"，**不出现"良好/正常"** |
| 未跑模型（backend=none） | → `not_acquired` + "本次未能获取健康信息" |
| 同一 event_id 重放 | → 跳过，不重复计数 |
| 同一天重复生成 | → 幂等返回同一张卡片 |
| 渠道抖动 2 次 | → 重试后发送成功 |
| 渠道一直失败 | → 3 次后进死信，返回 `degraded`，可查 |
| 无授权 | → `ignored: no_family_digest_consent` |
| 回忆未授权 / 未就绪 | → 占位文案，不编造 |
| 手机号/身份证/门牌 | → 脱敏后再进卡片 |

## 与队友的接口

- **B → D**：读 `health_signal` / `safety` / `metadata`；B 不做 P0/P1/P2 转换，分级归 D。
- **C → D**：`get_chronicle(purpose="family_digest")`；D 用 `set_consent` 回答 C 要的"可见范围与撤回机制"。
- **A → D**：读 `safety_level`；D 不重复判定危险源。

待群里确认的映射表与优先级见 `references/upstream-contracts.md` 末尾。
