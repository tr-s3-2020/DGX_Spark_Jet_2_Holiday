# 任务二架构与集成边界（负责人：Yunsheng）

登记日期：2026-09-24。只描述 Yunsheng 负责的任务二。任务一、主系统和任务四仅作为接口上下游，不登记其人员与实现状态。

## 数据流

```text
老人音频 → 任务一 elderly-voice-duplex → final 文本
                                         ↓
                               主系统调用任务二 handle
                                         ↓
                            InputAdapter → SafetyGate
                              ├─ BLOCK → 固定安全输出
                              └─ PASS  → Mock/Qwen → 校验 → 模板输出
                                         ↓
                                    TriageOutput
                      ┌──────────────────┴────────────────┐
                      ↓                                   ↓
        主系统处理 response → 任务一 TTS       任务四消费 health_signal

```

## 任务二内部只有两个顶层模块

1. Input Adapter：严格字段校验、忽略 partial/空文本、清理首尾空白。
2. Health Triage Engine：先用药安全，再语义提取、JSON/Pydantic 校验和响应构建。

Qwen、Mock、Colang、NeMo Guardrails、Python Policy、Parser、ResponseBuilder 都是第二模块内部组件。用药安全 action 与 Python 路径共享同一策略；阻断后不能调用语义模型。NeMo 的生成后端是无网络 stub，关切回复也是模板。

## 边界与下游行为

- 每次只消费当前一句 final 文本；不读取会话历史，不处理音频、长期记忆、家庭摘要、UI。
- `session_id` 与 `turn_id` 原样关联结果，不承担存储或去重。
- `passthrough`：主系统继续普通对话；`response.text=null`，不能直接送 TTS。
- `health_care`：主系统可转交模板回复。
- `medication_safety`：固定话术原样交接，不能让其他模型补写剂量建议。
- `health_signal` 给任务四作为输入；时间戳、持久化、趋势聚合、P0/P1/P2 由调用方/任务四负责。
- 本次不注册旧协议 `POST /v1/triage`，也不混用旧中文枚举与新版英文字段。

## 状态

任务二实现已放入 `modules/implicit-health-triage/`，等待 PR 审查合并。跨模块调用、任务四对枚举的消费、真实 Qwen 和 DGX 环境均待联调。当前不存在已验证的完整项目启动入口。
