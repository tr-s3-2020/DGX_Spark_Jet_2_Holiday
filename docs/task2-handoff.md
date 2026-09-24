# 任务二交接状态与 DGX 资源登记

登记日期：2026-09-24。本次仅提交协作信息、接口与样例，代码迁入为后续独立任务。

## 交付状态

| 项目 | 已核实情况 | 后续动作 |
|---|---|---|
| 任务二实现 | 本地完成 InputAdapter、Engine、Mock/Qwen、Colang、安全策略、API、CLI | 迁入 modules/implicit-health-triage/ |
| 本地验证 | 新增 46 项通过；完整项目回归 493 项通过 | 代码入库后在仓库环境复跑 |
| 静态检查 / 打包 | 新增代码 Ruff 通过；wheel 包含 rails，解压后真实 NeMo+Mock 三分支冒烟通过 | DGX Linux/ARM64 环境验证 |
| 真实模型 | Qwen HTTP 适配器及错误路径已测，真实 Qwen 推理未测 | 确认模型服务并评测 |
| 任务一 / 四联调 | 尚未进行 | 确认新版协议并串联 |
| Git / PR | 本次文档在独立任务分支；未合入 master | 另一位成员审查后合并 |

上列测试是前一阶段本地实现的证据，不是本次文档分支执行了 493 项测试。完整回归在最后安全异常包装前执行；最后改动后的任务二 46 项和 Ruff 已复跑通过。6 条完整回归警告来自第三方弃用提示。

## 服务与资源登记

| 项目 | 登记值 |
|---|---|
| 服务 | implicit-health-triage 任务二 |
| 负责人 / 备份检查人 | Yunsheng / 待团队指定 |
| 仓库目录 | modules/implicit-health-triage/，拟入库 |
| DGX 实际工作目录 | 待分配，各成员独立 clone 和环境 |
| HTTP 地址 / 端口 | 建议 127.0.0.1:8080；占用情况与访问方式待协调 |
| 启动入口 | uvicorn implicit_health_triage.task2.api:app --host 127.0.0.1 --port 8080 |
| 默认后端 | SEMANTIC_BACKEND=mock；NeMo Guardrails 启用 |
| Qwen 地址 | 建议 http://127.0.0.1:8000/v1；未证明有服务运行 |
| GPU / 内存 | Mock 不需要 GPU；Qwen 用量取决于模型，待部署测量 |
| 日志 | Python logging/uvicorn 标准错误输出；实际收集路径由部署方登记，不记录完整原文 |
| 模型权重目录 / 使用时段 | 待模型部署负责人指定；不提交权重和缓存 |
| 配置文件 | 模块目录 .env.task2；环境变量见使用说明；真实密钥不入库 |

## 联调顺序

1. 任务二负责人 Yunsheng 已登记；备份检查人待指定，调用方检查 docs/interfaces.md。
2. 独立代码 PR 迁入本地实现、依赖、.env.task2.example、测试与 rails。
3. 仓库内执行 46 项任务二测试和三类 CLI/HTTP 样例，记录提交 SHA。
4. 任务一交接 final 文本；检查 partial/空文本、ID 关联和异常响应。
5. 主系统按 response.mode 分流；任务四消费三字段，独立实现分级与时间戳。
6. 在 DGX 验证资源、Qwen 模型与真实语义效果，再登记完整流程结果。

## 待确认项

- 任务二备份检查人。
- 描述性模块路径、session_id/turn_id 生成和去重约定。
- 服务端口、DGX 目录、模型权重路径和共享资源时段。
- 当前 mock 只能验证固定样例，不能作为真实模型准确率结果。
