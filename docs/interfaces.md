# 接口记录：任务二单句 final 健康探针

依据《任务二_明确输入输出实现文档》及本地完成的 `implicit_health_triage.task2` 实现填写。此为提供方交接协议，待任务一、任务四和主系统负责人检查后合并；不要使用旧版中文枚举或嵌套 `result` 协议。

## 提供方 / 调用方

- 提供方 / 负责人：任务二 `implicit-health-triage` / **Yunsheng**。
- 调用方 / 负责人：主对话系统及任务一（输入）、任务四（结果消费）/ 待团队指定。
- 调用方式与入口：首选进程内 `await skill.handle(TriageInput(...))`，导入 `from implicit_health_triage.task2 import ImplicitHealthTriageSkill, TriageInput`。HTTP 可选 `POST /v1/implicit-health-triage`。
- 仓库目录：`modules/implicit-health-triage/`，代码随本 PR 提交。

## 输入字段和示例

| 字段 | 类型 | 必填 / 默认 | 含义 |
|---|---|---|---|
| session_id | string | 必填，无默认 | 会话关联 ID，非空白，最长 256 |
| turn_id | string | 必填，无默认 | 轮次关联 ID，非空白，最长 256 |
| text | string | 必填，无默认 | 当前完整文本，最长 16000；内部去首尾空白 |
| is_final | boolean | 必填，无默认 | 仅 true 处理；不接受字符串或整数替代 |

```json
{"session_id":"s001","turn_id":"t001","text":"今天早上起来腿沉得很，买菜走两步就得歇着。","is_final":true}
```

UTF-8 JSON；拒绝额外字段。上游若有 speaker/timestamp 等字段，应先选出以上四个字段。一次通话的 ID 生成方式与去重策略由上游约定，任务二不保存历史。

## 输出字段和示例

| 字段 | 类型 / 取值 | 含义 |
|---|---|---|
| session_id / turn_id | string | 回显关联 ID |
| health_signal.type | none/symptom/sleep/pain/medication/mobility/appetite/other | 健康类别 |
| health_signal.detail | string，最长 256 | 提取信息；none 时固定空串 |
| health_signal.severity | low/moderate/high | 内部结构标签，不是诊断或 P0/P1/P2 |
| safety.blocked | boolean | 是否用药安全阻断 |
| safety.rule_id | string/null | 命中规则，否则 null |
| response.mode | passthrough/health_care/medication_safety | 路由模式 |
| response.text | string/null | 模板回复；passthrough 固定 null |
| metadata.semantic_backend | none/mock/qwen | 未调用语义模型为 none |
| metadata.degraded | boolean，始终存在 | 两次语义尝试失败才为 true；成功、重试恢复和安全阻断为 false |
| metadata.qwen_called | boolean | 是否尝试调用 Qwen；超时也为 true，阻断为 false |
| metadata.latency_ms | number，毫秒 | 实测安全检查、模型与重试耗时，非固定值 |

```json
{
  "session_id":"s001", "turn_id":"t001",
  "health_signal":{"type":"symptom","detail":"下肢沉重/乏力","severity":"moderate"},
  "safety":{"blocked":false,"rule_id":null},
  "response":{"mode":"health_care","text":"听起来您今天提到下肢沉重、没什么力气，确实有些不舒服。您平时该吃的药都按原来的安排吃了吗？"},
  "metadata":{"semantic_backend":"mock","qwen_called":false,"degraded":false,"latency_ms":1.0}
}
```

这里的 1.0 只是延迟示意。成功时顶层六个字段始终存在，不再包装 `result`。用药风险输出 medication/high、固定安全话术、`semantic_backend=none` 和 `qwen_called=false`。细分规则见[使用说明](task2-readme.md)。

## 异常或错误格式

- partial 或空文本：Python 返回 `None`；HTTP 200 `{"status":"ignored","reason":"partial_or_empty"}`。不执行 safety 或模型。
- 缺必填字段、类型错误：Python Pydantic `ValidationError`；HTTP 422，FastAPI 标准 `{"detail":[...]}`。例如仅提交 `{"text":"你好"}`。
- 安全门异常：Python `RuntimeError`；HTTP 503 `{"detail":"Safety gate unavailable"}`。不调用语义模型，不自动放行至普通生成。
- 模型 HTTP/超时错误或 JSON 校验失败：最多两次尝试后按需求降级为 none/空 detail/low，HTTP 仍为 200，记录不含原文的警告。输出 `metadata.degraded=true`，下游按“未获取到结果”处理。正常无信号、重试恢复和用药阻断时该值为 false；即使正常无信号，也不代表健康正常。
- `SEMANTIC_BACKEND=qwen` 但未填模型名称：构造 Settings 失败，服务启动失败。

## 超时 / 重试责任

模块内部负责语义失败重试一次；HTTP 客户端不额外隐式重试。`QWEN_TIMEOUT_SECONDS` 默认 30，为单次 HTTP 请求的超时配置，不是整个链路的硬截止时间。调用方负责整体请求超时、取消与 UI 状态；不要无界重试，不要把安全门失败视为普通聊天。

## 环境和资源要求

Python 3.11 已验证；依赖清单声明 3.10–3.13。FastAPI、Pydantic 2、httpx、NeMo Guardrails 0.24.1 等由实现的 pyproject.toml 管理。Mock 无需 GPU 或模型服务；真实 Qwen 由独立本地兼容 HTTP 服务提供，模型名称和资源待部署确认。端口 8080 是建议值，尚未登记为 DGX 已分配端口。

## 最小验证方法

进入 `modules/implicit-health-triage/`，按 README 安装依赖后执行：

```bash
python -m pytest tests/test_task2.py -q
python -m implicit_health_triage.task2.cli '我降压药今天能不能吃两颗？'
```

机器可读样例见 [examples/implicit-health-triage.json](../examples/implicit-health-triage.json)。普通聊天检查 passthrough/null；原始腿沉示例检查 symptom/下肢沉重/乏力/moderate；用药风险检查固定话术、阻断和零次模型调用；partial 检查 ignored。模板可精确比较，延迟只检查非负，模型语义泛化需另外评测。

## 任务四消费边界

传递完整 `TriageOutput`，不要只投影 health_signal。任务四需同时读取 safety.blocked 和 metadata.degraded；type=none 时以 degraded 区分正常无信号与提取失败。任务四可关联 session_id/turn_id 并自行添加采集时间戳。旧版 `implicit_health_triage.integration.to_digest_record` 确实存在，但只接收旧中文协议的 TriageResult；它不保留 safety/response/metadata/关联 ID，且旧 type=无 返回 None。新版没有同名转换入口，任务四应接收完整 TriageOutput。任务二不提供摘要写入入口，不承诺 P0/P1/P2 转换。任务四原适配器的对接验证见下方反馈文档。


## Schema 与降级字段版本说明（2026-09-28）

新版机器协议：[TriageOutput](../modules/implicit-health-triage/schemas/task2/TriageOutput.schema.json)、[TriageInput](../modules/implicit-health-triage/schemas/task2/TriageInput.schema.json)。
模块 schemas 根目录的旧中文 JSON Schema 仅供 legacy 兼容，详见[版本索引](../modules/implicit-health-triage/schemas/README.md)。
metadata.degraded 为新版输出必填布尔值；旧报文缺失时表示未知，不能默认理解为未降级。
忽略与错误返回保持原结构；不根据 metadata.semantic_backend/qwen_called 推断成功与否。
任务四对接说明见[反馈文档](to-d-interface-feedback.md)。
