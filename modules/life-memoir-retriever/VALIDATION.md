# 0.1.0 联调验证记录

## 2026-09-28：Python 3.10 退出挂起修复

本机 macOS arm64，独立 Python 环境。完整测试开启 `PYTHONASYNCIODEBUG=1`，将 RuntimeWarning 和 ResourceWarning 视为错误，并用父进程设置硬超时。无真实模型或网络 API 调用。

| Python | 完整测试 | 退出压力测试 |
|---|---|---|
| 3.10.21 | 37 passed，3.19 秒 | 10 类场景各 100 轮，共 1,000 轮通过 |
| 3.11.16 | 37 passed，3.18 秒 | 10 类场景各 100 轮，共 1,000 轮通过 |
| 3.12.14 | 37 passed，2.80 秒 | 10 类场景各 100 轮，共 1,000 轮通过 |

合计 3,000 轮，未出现进程挂起、遗留任务、未处理任务异常或未退出的执行器线程。完整测试各有一个既有 Starlette/httpx 弃用提示。以上耗时是本地模拟测试耗时，不代表生产延迟。

覆盖场景：空闲立即关闭、分析中及并发槽排队、分析完成与关闭竞争、宿主取消后在 finally 中清理、关闭调用方连续取消、并发关闭等待者取消、模型超时、模型异常、未提交任务的持久库重启、主协程结束后直接进入 `_cancel_all_tasks`。并发度轮换 1-4，每个压力测试进程有 30 秒硬超时。

### 旧代码复现与根因

对提交 `2b16805` 的原始模块，用 Python 3.10.21 执行原始 `test_persistent_memory_and_temporal_sources`，父进程在 5 秒后强制结束。faulthandler 抓到与报告一致的位置：

```text
asyncio/runners.py:63 in _cancel_all_tasks
asyncio/runners.py:47 in run
tests/test_service.py:69 in test_persistent_memory_and_temporal_sources
```

同时记录到 `closed=False`、`registered_workers=0`，仍存活的是 `_scheduler`（原 service.py:714）和其 `Event.wait()` 子任务。后台作业已变成 `BACKGROUND_INTERNAL_ERROR`。原代码创建作业后的下一行已有 `self.tasks.add(task)`，因此此次复现不支持“作业漏登记”的推断。

故障链：3.10 没有 `asyncio.timeout()`，后台任务先失败；测试断言失败发生在 `close()` 之前；事件循环收尾取消调度器时，旧 `wait_for` 的唤醒/取消竞争吞掉取消，调度器继续无限循环。[Python 超时 API 文档](https://docs.python.org/3.11/library/asyncio-task.html#asyncio.timeout)与 [CPython #86296](https://github.com/python/cpython/issues/86296)记录了对应版本边界和竞态。

修复包括：3.10 按条件安装 `async-timeout`，3.11+ 使用标准库超时；同时兼容两种超时异常；调度器检查关闭状态；先停调度再收齐作业；共享关闭任务保护清理，取消在清理结束后传回；HTTP 生命周期使用 finally。另在本轮压力测试前发现并修复了“关闭调用方被取消导致数据库未关闭”的漏洞。

### 队友复跑

在本模块目录、目标 Python 虚拟环境中执行：

```bash
python -m pip install -c constraints-tested.txt -e '.[dev]'
python -X faulthandler -m pytest -q -o faulthandler_timeout=30
python tests/shutdown_scenarios.py cancel_closer --repeat 100
python tests/shutdown_scenarios.py runner_cleanup --repeat 100
```

已加入 `.github/workflows/life-memoir-tests.yml` 三版本测试矩阵和进程级硬超时；这里只记录本地执行结果，远程 CI 尚未运行。

## 2026-09-26：初次联调

日期：2026-09-26。环境：macOS arm64，Python 3.12.14。依赖版本见 constraints-tested.txt。

| 检查 | 结果 |
|---|---|
| `python -m pytest -q` | 23 passed；一个 Starlette TestClient 关于 httpx 的弃用提示，不影响结果 |
| 临时 SQLite + 实际 uvicorn 回环 HTTP + integration_smoke.py | 通过授权、会话、后台发布、年谱范围和下一会话读取 |
| 已安装 CLI 对实际 HTTP 的 open_session | 通过，返回正确 envelope 和 session_version |
| Skill quick_validate.py | 通过；入口脚本 --help 可运行 |
| `python -m build --no-isolation` | wheel 与 sdist 构建成功 |
| `python -m pip check` | 无依赖冲突 |
| 仓库 Markdown 本地链接 | 无缺失目标 |

用阻塞中的分析后端检查：其尚未完成时，prepare_turn 可独立返回；这是依赖隔离测试，不是端到端语音延迟基准。时间推理测试包括出生 1950 + 二十周岁 → 1970–1971，再加三年 → 1973–1974、虚岁/历法/人物不明不强算、冲突/循环不强填、更正与删除使旧推算失效。

许可测试包括用户隔离、最小原话来源、任一轮不保存排除整场提取、后台执行中撤权不发布、家属看不到被隐藏出生锚点的派生年份、TTL 清理。额外覆盖间接偏好保持待确认、后台更新不自循环及否定时间推算后仍能重建年谱。

真实模型仅做 HTTPX 模拟协议测试（JSON、重定向、超时和错误脱敏），没有调用实际外部 API。尚未验证：NVIDIA/DGX 实机、中文 ASR/TTS、主控实际加载 Skill、真实长者互动、模型提取与偏好学习质量。Demo 页面按当前协作优先级暂缓。
