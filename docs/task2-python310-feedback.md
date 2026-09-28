# 任务二：Python 3.10 UTC 兼容问题修复回执

负责人：Yunsheng。日期：2026-09-29。

反馈成立：模块声明支持 Python 3.10，但 logging_utils.py 和 integration.py 使用了 Python 3.11 才提供的 datetime.UTC，导致相关导入链及测试收集在 3.10 失败。此前测试只覆盖 3.11，未发现此问题。

## 正式修复

两个文件统一改为 `from datetime import datetime, timezone`，使用 `datetime.now(timezone.utc)`。
保留 `requires-python = ">=3.10,<3.14"`，无需兼容分支，不改变时间戳含义、UTC 时区或 `+00:00` 格式。业务接口及 metadata.degraded 均不变。

联调侧 try/except 回退补丁方向正确；正式实现采用等效的 timezone.utc 写法。无需先撤销可用补丁，合并任务二最新提交时将这两处统一为正式实现即可。不要整目录覆盖联调分支，以免覆盖其他联调修改。

## 验证

- 新建 Python **3.10.19** 环境，安装模块 `.[dev]`：成功。
- Python 3.10 全量测试：**498 passed**，12.49 秒，5 条第三方弃用警告。
- Python 3.10 导入两个受影响模块成功，日志时间戳输出保留 `+00:00`。
- Python 3.10 CLI / NeMo 用药阻断：通过，qwen_called=false。
- Python 3.11 相关回归：test_integration.py、test_service.py 共 **22 passed**。
- 两个改动文件 Ruff 检查通过。

新增 `.github/workflows/task2-python.yml`，在 Ubuntu 上对 Python 3.10、3.11、3.12、3.13 执行安装、模块导入、全量测试及新版 Schema 一致性检查。工作流已配置，远程结果应以 GitHub Actions 为准；本地 Windows 验证不替代 DGX Linux/ARM64 验证。

本次仅修改任务二，不修改任务三、任务四或编排层代码。
