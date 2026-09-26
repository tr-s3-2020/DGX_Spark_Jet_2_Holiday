# 0.1.0 联调验证记录

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
