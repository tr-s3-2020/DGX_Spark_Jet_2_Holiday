# 任务二交接状态与 DGX 资源登记

更新日期：2026-09-28。任务二代码已迁入本分支，负责人 Yunsheng。

## 交付状态

| 项目 | 已核实情况 | 后续动作 |
|---|---|---|
| 任务二实现 | 本地完成 InputAdapter、Engine、Mock/Qwen、Colang、安全策略、API、CLI | 已迁入 modules/implicit-health-triage/，待 PR 合并 |
| 仓库副本验证 | 2026-09-28 迁入后全量 493 项通过，含任务二 46 项 | 合并后继续跨模块联调 |
| 静态检查 / 打包 | 新增代码 Ruff 通过；wheel 包含 rails，解压后真实 NeMo+Mock 三分支冒烟通过 | DGX Linux/ARM64 环境验证 |
| 真实模型 | Qwen HTTP 适配器及错误路径已测，真实 Qwen 推理未测 | 确认模型服务并评测 |
| 任务一 / 四联调 | 尚未进行 | 确认新版协议并串联 |
| Git / PR | 代码和文档在独立任务分支；未合入 master | 另一位成员审查后合并 |

2026-09-28 在本仓库迁入的代码上复跑：`python -m pytest modules/implicit-health-triage/tests -q`，493 passed，44.02 秒；6 条警告来自第三方弃用提示。使用已有 Python 3.11 依赖环境并将 PYTHONPATH 指向本仓库模块 src，不依赖原位置的源码。任务二 Ruff 检查通过；从模块目录运行 CLI，真实 NeMo 用药阻断输出正确且 qwen_called=false。真实 Qwen 推理和 DGX 部署仍未验证。

## 服务与资源登记

| 项目 | 登记值 |
|---|---|
| 服务 | implicit-health-triage 任务二 |
| 负责人 / 备份检查人 | Yunsheng / 待团队指定 |
| 仓库目录 | modules/implicit-health-triage/ |
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
2. 本 PR 已迁入实现、依赖、.env.task2.example、测试与 rails，待另一位成员检查合并。
3. 仓库内执行 46 项任务二测试和三类 CLI/HTTP 样例，记录提交 SHA。
4. 任务一交接 final 文本；检查 partial/空文本、ID 关联和异常响应。
5. 主系统按 response.mode 分流；任务四消费三字段，独立实现分级与时间戳。
6. 在 DGX 验证资源、Qwen 模型与真实语义效果，再登记完整流程结果。

## 待确认项

- 任务二备份检查人。
- 描述性模块路径、session_id/turn_id 生成和去重约定。
- 服务端口、DGX 目录、模型权重路径和共享资源时段。
- 当前 mock 只能验证固定样例，不能作为真实模型准确率结果。
