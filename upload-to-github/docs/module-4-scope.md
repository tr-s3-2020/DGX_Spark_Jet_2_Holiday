# 第四项 family-digest-sync：任务边界（v0.1）

> 日期：2026-09-28。负责人：成员 4（Zoe）。依据：库内已登记的 B（Yunsheng）、C（卢易锋）、A（子阳）三方文档推导，不是凭空提案。
> 交付形态：Agent Skills 规范下的 Skill（`SKILL.md` + scripts + references + examples + tests）。

## 1. 一句话定义

把"老人今天发生了什么"，以**获授权、脱敏、可追溯**的方式同步给家属；按紧急度分三档推送，**跨日趋势只有 D 能判**，且**永远不把"没数据"说成"没事"**。

## 2. D 的输入：三条，全部来自别人，D 不生产任何原始信号

| 来源 | 拿什么 | 硬约束 |
|---|---|---|
| **B** `implicit-health-triage` | `health_signal{type, detail, severity}`、`safety{blocked, rule_id}`、`response.mode`、`metadata{semantic_backend, qwen_called, latency_ms}` | `severity` 是 low/moderate/high **内部标签，不是 P0/P1/P2**；B **不做**分级转换；B 失败会降级为 `none/""/low` 且**无降级标志** |
| **A** `elderly-voice-duplex` | `safety_level`（P0/P1）+ 当轮文本 | A 自己已有 P0/P1 围栏；D 只消费判定结果，不重复判定 |
| **C** `life-memoir-retriever` | `get_chronicle(purpose="family_digest")` → `{state, view_version, content, warnings}` | 无 family_digest 授权**不生成**家属版；`degraded` ≠ 无记忆；内容过期立即失效 |

**D 不读的东西**：老人对话原文全文（C 已声明原文仅进程内短存）、音频、模型权重、任何诊断结论。

## 3. D 独享的三项职责（其余三人都不做，所以必须 D 做）

1. **分级映射 + 通知编排**：把 B 的 severity / A 的 P0/P1 映射成"发什么、发给谁、什么时候发"。（B 明确不转换，C 不管通知）
2. **跨日趋势判定**：B 每次只看单句、不保存历史；C 不存健康序列。**只有 D 持有时间序列**，因此"连续 N 天劣化""偏离基线"这类判断天然属于 D。
3. **家属侧的一切**：同意范围校验、脱敏、卡片渲染、渠道推送、发送状态机与重试、回执与失效。

## 4. D 明确不做（越界清单）

- ❌ 提取健康信号 —— 是 B
- ❌ 语音、轮次、打断 —— 是 A
- ❌ 记忆存储与年谱生成 —— 是 C；D **只读** `get_chronicle`
- ❌ 医疗诊断、剂量建议 —— B 的 `medication_safety` 固定话术必须**原样透传**，D 一个字不改
- ❌ 判定"危险源" —— A/B 已判；D 只做**映射 + 编排**，不自创告警
- ❌ 保存老人原文全文 —— 只存脱敏字段 + 可回溯的引用 ID
- ❌ v0 接真实渠道（微信/短信） —— 用可替换 adapter + mock，正式版换实现不改上层

## 5. 三档定义（D 自定，标注为提案，待群里确认）

| 档 | 触发条件 | 行为 |
|---|---|---|
| **P0 立即** | B `safety.blocked=true`（用药风险）或 A `safety_level=P0` | 立即推送，**不进日报排队**，独立回执 |
| **P1 当日** | 跨日趋势劣化（同类信号连续 N 天中度及以上 / 偏离个人基线）或单日 `severity=high` | 当日摘要内高亮 |
| **P2 日报** | 其余所有 | 每日固定时刻一份 |

## 6. 四条不可违反的硬约束

1. **降级 ≠ 正常**：B 语义失败降级时，卡片必须写"本次未能获取健康信息"，**禁止写"状况良好"**。这是库里三方都点名的坑。
2. **幂等三件套**：`event_id` / `alert_key` / `(elder_id, date, signal_type)`，重放不重复推送。
3. **同意优先**：无 family_digest 授权 → 不生成、不推送；撤回立即失效，不因缓存绕过。
4. **最小披露**：脱敏（手机/身份证/门牌）+ 只输出授权范围内字段；年谱引用保留来源，不暴露私有锚点推算出的日期。

## 7. 今天交付什么

```
modules/family-digest-sync/
├── SKILL.md                    # 技能定义：触发时机、不适用任务、输入输出、失败处理
├── scripts/
│   ├── digest_cli.py           # 统一入口：consume → route → render → send
│   └── cards.py                # 确定性卡片渲染（模板，不靠模型自由文本）
├── src/family_digest/
│   ├── routing.py              # 三档映射（提案版，可配置）
│   ├── trend.py                # 跨日趋势与基线
│   ├── dedup.py                # 幂等
│   ├── consent.py              # 授权范围 + 脱敏
│   ├── state.py                # 发送状态机 + 重试/死信
│   └── adapters/               # llm / channel 可替换（v0 mock）
├── references/
│   ├── host-contract.md        # 主控怎么调我
│   ├── upstream-contracts.md   # A/B/C 三方的字段与坑
│   └── card-templates.md       # 家属可读的文案模板
├── examples/                   # fixture 输入 + 预期卡片
└── tests/                      # 路由、幂等、状态机、脱敏、降级
```

## 8. 今天明确砍掉（防止范围扩散）

- 真实渠道接入、真实 LLM（用确定性模板 + adapter 占位）
- 多老人 / 多家属账号体系（v0：单老人、单家属）
- 数据库（v0：JSON 文件存储）
- 与 A/B/C 的真实联调（用 fixture 模拟三方返回）
