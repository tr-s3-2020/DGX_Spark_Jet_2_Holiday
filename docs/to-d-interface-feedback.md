# 任务二 → 任务四：接口反馈与修订说明

提供方：Yunsheng（任务二）；接收方：Zoe（任务四）。更新日期：2026-09-28。
针对任务四 `docs/to-b-interface-reply.md` 中降级标志和旧转换函数的反馈，本次已修改任务二实现及文档。

## 1. 已加入明确降级标志

当前对接协议为 `implicit_health_triage.task2.schemas.TriageOutput`，字段位置固定为：

```json
{
  "metadata": {
    "semantic_backend": "qwen",
    "qwen_called": true,
    "degraded": true,
    "latency_ms": 100.0
  }
}
```

上面仅为 metadata 片段，延迟为示意值；完整结果仍有 session_id、turn_id、health_signal、safety、response、metadata 六个顶层字段。

| 路径 | metadata.degraded | D 处理 |
|---|---|---|
| 正常得到健康信号 | false | acquired |
| 正常得到 none/空 detail/low | false | no_signal；不等于健康正常 |
| 第一次失败，第二次返回有效 JSON | false | 按最终有效信号处理 |
| 两次语义尝试均失败 | true | not_acquired；不能表述为无异常 |
| 用药安全阻断 | false | 保留 safety.blocked=true；安全策略正常生效 |

HTTP/超时异常或 JSON 校验失败最多重试一次。最终降级仍按原约定返回 none/空 detail/low，HTTP 200，但现在带 degraded=true。
用药阻断仍为 semantic_backend=none、qwen_called=false，模型调用次数为零。
partial/空文本仍返回 ignored，安全门异常仍返回 HTTP 503；两者不是 degraded=true 的正常结果。

`degraded` 是新版输出必填字段，无默认值，避免缺失状态被误认为 false。旧报文没有此字段时仍是“未知”，不要仅凭 qwen_called 或 semantic_backend 推断模型成功。

## 2. 确认任务四接收完整报文

确认继续使用 `normalize_b_output(raw, elder_id=..., occurred_at=...)`，由主控传完整 TriageOutput。
保留 session_id、turn_id、health_signal、safety、response、metadata。elder_id 与 occurred_at 仍由宿主补充，不塞入 B 的严格四字段输入。

任务四现有适配器已经读取 metadata.degraded。本次直接使用任务四分支原代码完成验证，无需为本次新增字段修改 D 的适配器。

## 3. 澄清 to_digest_record 与新旧版本

反馈正确：`implicit_health_triage.integration.to_digest_record` 确实存在。
之前文档“不提供新版 to_digest_record”指新协议没有这个转换入口，但表达不够明确，本次已补全说明及函数文档。

- 旧版：`schemas.py` 的中文枚举 TriageResult/TriageResponse；`integration.to_digest_record` 只服务这个协议。
- 新版：`task2/schemas.py` 的英文枚举、扁平 TriageOutput，包含 metadata。
- 旧转换函数只输出 timestamp/type/detail/severity，不保留阻断标志、response、metadata 和关联 ID；旧 type=无 返回 None。
- 因而它不能作为 D 的唯一数据源，也不能直接用于新版 TriageOutput。保留函数是为旧调用兼容，并非推荐新集成使用。

任务四回执中“新版 TriageResult 没有 metadata”的新旧命名需要更正；实际对接的 TriageOutput 一直带 metadata，本次是在其中补 degraded。

## 4. Schema 已分开标注

- 新版：[schemas/task2/TriageOutput.schema.json](../modules/implicit-health-triage/schemas/task2/TriageOutput.schema.json)。同目录提供 TriageInput、HealthSignal、IgnoredOutput。
- 旧版：模块 schemas 根目录七份中文 JSON Schema，已通过 [schemas/README.md](../modules/implicit-health-triage/schemas/README.md) 标为 legacy / 不用于新集成。
- 新版导出和校验：`python scripts/export_task2_schemas.py` / `--check`。
- 英文传输枚举不变，回复内容保持中文。

新增字段可能被严格拒绝额外字段的旧消费方拒收，其他消费方应同步新 Schema。任务四当前适配器不存在这一问题。

## 5. 验证结果

- 任务二所在模块全量测试：**498 passed**，23.75 秒；6 条第三方弃用警告。
- 正常 Qwen 无信号、重试恢复、两次失败、HTTP 超时均验证了明确降级状态。
- 新 Schema 与 Pydantic 源模型一致性有自动测试；BLOCK 零次模型调用测试保留。
- 用任务四 `zoe-module-4` 的 `6968c2df61c72ffa6bff0a53b77ecea0b77c60b7` 原适配器接收 B 实际生成的四种结果，检查通过：

| 场景 | D acquisition | D looks_degraded | safety_blocked |
|---|---|---|---|
| 正常无信号 | no_signal | false | false |
| 重试成功 | no_signal | false | false |
| 两次失败兜底 | not_acquired | true | false |
| 用药阻断 | acquired | false | true |

机器可读验证结果见 [examples/task2-to-task4-degraded-verification.json](../examples/task2-to-task4-degraded-verification.json)。本次验证使用受控语义模型，不是实机 Qwen 推理或真实家属通知。

## 6. 仍需团队决定的边界

B 的 safety.blocked 表示“具体用药决策被阻断”，并不等同于临床紧急事件。是否将所有阻断统一作为 D 的 P0 通知源，由团队确认通知策略；本次不修改任务四的 P0 逻辑。
时间戳、去重、授权、推送、P0/P1/P2 分级仍由主控/任务四负责。
