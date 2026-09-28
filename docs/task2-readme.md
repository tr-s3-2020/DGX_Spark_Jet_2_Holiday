# implicit-health-triage：任务二实现与使用说明

> 任务二代码已放入 `modules/implicit-health-triage/`。负责人：Yunsheng。以下从仓库根目录进入模块后运行。

实现依据：`implicit-health-triage_任务二_明确输入输出实现文档.md`。

任务二接收一条 final 文本，先执行用药安全检查，再调用 Mock 或本地 Qwen 提取健康信号，返回统一 JSON。实现位于现有项目 `src/implicit_health_triage/task2/`，可独立启动和调用。

## 1. 快速启动

建议使用 Python 3.11。项目声明支持 Python 3.10–3.13，本次验证使用 3.11.7。除首次 `cd` 外，安装、测试和启动命令均在 `modules/implicit-health-triage/` 执行。

已有虚拟环境的 Windows 项目：

```powershell
cd modules/implicit-health-triage
$env:PYTHONIOENCODING = 'utf-8'
.venv\Scripts\python.exe -m implicit_health_triage.task2.cli "今天早上起来腿沉得很，买菜走两步就得歇着。"
.venv\Scripts\python.exe -m uvicorn implicit_health_triage.task2.api:app --host 127.0.0.1 --port 8080
```

全新环境安装：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Linux / DGX：

```bash
cd modules/implicit-health-triage
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m implicit_health_triage.task2.cli '我降压药今天能不能吃两颗？'
uvicorn implicit_health_triage.task2.api:app --host 127.0.0.1 --port 8080
```

默认 `SEMANTIC_BACKEND=mock`，NeMo Guardrails 启用，不需要模型服务、API 密钥或 GPU。首次安装依赖需要网络；运行 Mock 流程不需要下载模型。首次 Colang 编译会增加延迟，服务会复用已编译的实例。

接口文档：[Swagger UI](http://127.0.0.1:8080/docs)。

## 2. Python 统一入口

```python
import asyncio
from implicit_health_triage.task2 import ImplicitHealthTriageSkill, TriageInput

async def main():
    async with ImplicitHealthTriageSkill() as skill:
        result = await skill.handle(TriageInput(
            session_id="s001",
            turn_id="t001",
            text="今天早上起来腿沉得很，买菜走两步就得歇着。",
            is_final=True,
        ))
        print(result.model_dump_json(indent=2) if result else "ignored")

asyncio.run(main())
```

也提供 `from implicit_health_triage.task2 import handle`，签名为 `async handle(input_data: TriageInput) -> TriageOutput | None`。高频调用应复用 `ImplicitHealthTriageSkill`，用异步上下文管理器或 `await skill.aclose()` 释放资源。

四个输入字段全部必填；`is_final` 必须是布尔值，不接受字符串 `"true"` 或整数。未知字段拒绝；ID 不能全空白，长度上限 256；文本长度上限 16000。适配器仅清理文本首尾空白。

`is_final=false` 或清理后为空：Python 返回 `None`，安全检查和语义模型均不执行。

## 3. HTTP API

```http
POST /v1/implicit-health-triage
Content-Type: application/json
```

请求：

```json
{
  "session_id": "s001",
  "turn_id": "t001",
  "text": "今天早上起来腿沉得很，买菜走两步就得歇着。",
  "is_final": true
}
```

PowerShell 调用：

```powershell
$body = @{
  session_id = 's001'
  turn_id = 't001'
  text = '我降压药今天能不能吃两颗？'
  is_final = $true
} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8080/v1/implicit-health-triage' `
  -Method Post -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

健康信息响应示例（`latency_ms` 为示意数值，实际逐请求测量）：

```json
{
  "session_id": "s001",
  "turn_id": "t001",
  "health_signal": {
    "type": "symptom",
    "detail": "下肢沉重/乏力",
    "severity": "moderate"
  },
  "safety": {"blocked": false, "rule_id": null},
  "response": {
    "mode": "health_care",
    "text": "听起来您今天提到下肢沉重、没什么力气，确实有些不舒服。您平时该吃的药都按原来的安排吃了吗？"
  },
  "metadata": {"semantic_backend": "mock", "qwen_called": false, "degraded": false, "latency_ms": 1.0}
}
```

三种模式：

| 条件 | response.mode | response.text | metadata |
|---|---|---|---|
| 普通聊天 | `passthrough` | `null` | 使用的 `mock` / `qwen` 后端 |
| 健康信息 | `health_care` | 简短模板关切 | 使用的 `mock` / `qwen` 后端 |
| 用药风险 | `medication_safety` | 固定安全话术 | `semantic_backend=none`、`qwen_called=false` |

忽略的输入返回 HTTP 200：

```json
{"status":"ignored","reason":"partial_or_empty"}
```

字段校验失败返回 HTTP 422。安全流程异常会终止请求，绝不继续调用语义模型；安全门运行异常返回 HTTP 503。

## 4. 两模块架构

```text
TriageInput
  → Module 1: InputAdapter（校验、final 判断、去首尾空白）
  → NormalizedTurn
  → Module 2: HealthTriageEngine
      → SafetyGate / Colang Input Rail / MedicationSafetyPolicy
          BLOCK → 固定安全话术 → TriageOutput
          PASS  → SemanticModelPort → JSON/Pydantic → 模板回复 → TriageOutput
```

Module 1 不做医疗判断。Module 2 内部组件：

```text
src/implicit_health_triage/task2/
├── schemas.py             # 输入、内部及输出协议
├── input_adapter.py       # Module 1
├── triage_engine.py       # Module 2 编排
├── response_builder.py    # 三类输出、固定模板
├── skill.py               # handle 及生命周期
├── settings.py            # 独立配置
├── api.py                 # FastAPI
├── cli.py                 # CLI Demo
├── semantic/
│   ├── port.py            # async analyze(text) -> str
│   ├── mock.py
│   ├── qwen.py            # 本地 OpenAI-compatible HTTP
│   ├── parser.py
│   └── prompts.py
└── safety/
    ├── policy.py          # 共享确定性策略
    ├── gate.py            # NeMo 实例及 action 注册
    ├── templates.py
    └── rails/
        ├── config.yml
        └── medical_safety.co
```

复用项目原有的确定性规则词表、无联网的 NeMo stub 和纯 Python embedding 实现。Colang 的 `MedicationSafetyCheckAction` 直接调用 `MedicationSafetyPolicy`；规则未在 Colang 中复制。NeMo 分支使用 stub，不调用 Qwen。Colang 完成检查后停止自身 continuation，由 Python 根据检查结果决定是否执行语义分析。NeMo 请求串行化，避免并发时 action 结果串线；检查结果是请求局部变量。

配置和 Colang 文件随 Python wheel 打包。目录使用 `safety/rails/`，避免项目根目录的 `guardrails/` 遮蔽 Colang 标准库。

## 5. 用药规则与模型校验

| 用药风险 | rule_id |
|---|---|
| 增加剂量 | `MEDICATION_DOSE_INCREASE` |
| 减少剂量 | `MEDICATION_DOSE_DECREASE` |
| 停药、跳过服药 | `MEDICATION_STOP_OR_SKIP` |
| 重复服药、补服 | `MEDICATION_REPEAT_DOSE` |
| 混合服药 | `MEDICATION_COMBINATION` |
| 复用规则识别到的其他用药调整 | `MEDICATION_CHANGE_REQUEST` |

拦截后 `health_signal.type=medication`、`severity=high`。固定安全话术与原需求文档一致，绝不交给 Qwen 改写。健康回复也使用模板，不把模型的自由文本直接拼入面向用户的回复。

模型仅返回 `type/detail/severity`。Parser 支持纯 JSON 和完整 Markdown JSON 代码块，校验枚举、类型、必填字段、额外字段、重复 JSON 键及 `none` 的一致性。解析失败或 HTTP/超时错误最多重试一次；两次失败按文档降级为 `none/空 detail/low`，设置 `metadata.degraded=true`，并记录不含原文的警告日志。取消请求不会被吞掉。

`qwen_called` 记录是否尝试调用 Qwen，因此 Qwen 超时后降级仍为 `true`；Mock 始终为 `false`。延迟覆盖安全检查、语义分析及重试。

## 6. 切换本地 Qwen

复制 `.env.task2.example` 为 `.env.task2`，配置：

```dotenv
SEMANTIC_BACKEND=qwen
QWEN_BASE_URL=http://127.0.0.1:8000/v1
QWEN_MODEL=实际已部署的模型名称
QWEN_API_KEY=EMPTY
QWEN_TIMEOUT_SECONDS=30
TRIAGE_GUARDRAILS_ENABLED=true
```

重启服务即可，无需修改输入、安全策略、Parser 或输出。适配器请求 `${QWEN_BASE_URL}/chat/completions`，关闭隐式网络重试，保证业务最多两次语义请求。超时为每次 HTTP 请求的超时设置。

配置优先级：显式构造参数 → 进程环境变量 → `.env.task2` → 默认值。不读取旧版 `.env`，以免意外使用旧版外部服务。`SEMANTIC_BACKEND=qwen` 时未配置模型名称会立即报错。

`TRIAGE_GUARDRAILS_ENABLED=false` 可在测试时直接执行同一个 Python 策略；默认是 `true`。此开关不关闭用药安全检查。

## 7. 测试与验收

```powershell
.venv\Scripts\python.exe -m pytest tests/test_task2.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src/implicit_health_triage/task2 tests/test_task2.py
```

新增测试覆盖文档全部六类危险用药示例、普通聊天、原始 few-shot、空文本/partial、严格输入、非法模型 JSON、一次重试及降级、真实 Colang 执行、并发隔离、Qwen HTTP 请求协议、HTTP 统一响应、安全门异常阻止模型调用。

关键验收：使用调用计数器断言 BLOCK 后模型调用次数为 **0**，而不只是检查 `qwen_called` 字段。

本地测试证据与仓库集成状态见 [交接状态](task2-handoff.md)。

## 8. 范围与兼容关系

新协议入口必须使用 `implicit_health_triage.task2` 和 `implicit_health_triage.task2.api:app`。旧 `implicit_health_triage.api.app:app`、`/v1/triage`、中文枚举及历史会话功能保留，未替换为本次协议。不能混用两版 schema。

本实现无 ASR、TTS、音频、长期记忆、家庭摘要或 UI；每个请求只分析当前一句文本，不查询会话历史。`session_id/turn_id` 用于关联输出，不执行持久化或去重。

Mock 是用于验收的有限规则集合，不能代表真实模型的泛化能力。单句规则可能遗漏方言或省略信息，也可能对文档明确要求拦截的模糊句（如“今天不吃行不行？”）保守阻断。JSON 校验保证结构，不证明模型判断正确。真实 Qwen 的效果和 DGX GPU 部署需要在模型上线后另行验证；本次仅验证其 HTTP 适配器与失败路径。

NeMo 接入参考：[NVIDIA Input Rails](https://docs.nvidia.com/nemo/guardrails/configure-guardrails/colang/colang-2/getting-started/input-rails)、[NVIDIA Python Actions](https://docs.nvidia.com/nemo/guardrails/configure-guardrails/colang/colang-2/language-reference/python-actions)。

## 9. 给任务四的新对接约定（2026-09-28）

当前协议是 `task2.TriageOutput`，不是旧版 `TriageResult`。新增必填输出字段 `metadata.degraded`：

| 路径 | degraded | 说明 |
|---|---|---|
| 正常识别健康信息 / 正常无信号 | false | 成功完成语义提取；无信号不代表健康正常 |
| 首次失败，重试成功 | false | 最终拿到有效结果 |
| 两次语义尝试均失败 | true | HTTP/超时/JSON 校验失败后的兜底 |
| 用药安全阻断 | false | 策略正常生效，未调用语义模型 |

partial/空文本仍返回 ignored，不附 metadata；安全门异常仍返回 503，不伪装成降级成功。
任务四接收完整输出，保留 `safety.blocked`、`metadata.degraded` 和关联 ID。
不要用 `qwen_called` 推断调用是否成功；也不要在缺失 degraded 的旧报文上默认填写 false。

新 Schema 在模块 `schemas/task2/`，运行 `python scripts/export_task2_schemas.py --check` 校验。
根部 `schemas/*.json` 是 legacy 中文协议，仅供旧接口兼容。
`integration.to_digest_record` 同样是旧接口：输入旧 TriageResult，输出健康三字段加时间戳；
不携带 safety、response、metadata、会话/轮次 ID，且旧 type=无 返回 None。它不能作为任务四唯一数据源，也不接受新版 TriageOutput。

这是输出字段扩展；严格拒绝额外字段的消费方须更新其 Schema。任务四现有适配器已识别 metadata.degraded。
