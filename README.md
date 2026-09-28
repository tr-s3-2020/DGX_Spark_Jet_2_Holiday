# DGX_Spark_Jet_2_Holiday

本次登记任务二 `implicit-health-triage`：隐式健康探针与用药安全，负责人 **Yunsheng**。其他模块分工保持原样。

## 协作与接口入口

- [团队协作指南](团队协作指南.md)：分工、分支、PR 和完成标准。
- [模块架构](docs/architecture.md)：模块范围、数据流和集成边界。
- [任务二接口约定](docs/interfaces.md)：输入输出、异常、超时及下游责任。
- [给任务四的最新反馈](docs/to-d-interface-feedback.md)：degraded 字段、完整报文交接和 legacy 接口说明。
- [任务二交接与资源登记](docs/task2-handoff.md)：实现状态、验证证据、待办和 DGX 资源。
- [任务二使用说明](docs/task2-readme.md)：安装、Python/HTTP 调用和 Qwen 切换。
- [接口样例](examples/implicit-health-triage.json)：可检查的输入与预期输出。

## 当前状态

2026-09-28：任务二代码、依赖、Colang 配置、测试、CLI 和样例已迁入 `modules/implicit-health-triage/`，由本 PR 提交，等待团队审查合并。本次不填写其他模块实现状态。

默认分支为 `master`；日常改动走任务分支与 PR，至少由另一名成员检查后合并。

## 任务二启动方式

代码位于 [modules/implicit-health-triage/](modules/implicit-health-triage/README.md)，可按以下命令安装运行。

```bash
cd modules/implicit-health-triage
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m implicit_health_triage.task2.cli '我降压药今天能不能吃两颗？'
uvicorn implicit_health_triage.task2.api:app --host 127.0.0.1 --port 8080
```

Windows 命令及输入输出见[使用说明](docs/task2-readme.md)。默认 Mock；本地 Qwen 接口已预留，真实模型和完整项目联调尚未验证。
