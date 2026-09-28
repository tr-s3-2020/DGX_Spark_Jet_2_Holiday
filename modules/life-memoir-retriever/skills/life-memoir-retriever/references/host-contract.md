# 工具与宿主契约（0.1.0）

先安装 Python 包 life-memoir-retriever 并启动单进程常驻服务。宿主可注册操作为 tools 或调用 HTTP；此包不含自动注册 MCP 服务器。

- 实时路径：open_session → prepare_turn（最终用户轮次）→ observe_turn（可选，实际助手回复）→ close_session。
- prepare_turn.turn 包含 turn_id / speaker=user / text / is_final / occurred_at（带时区）/ text_locale。主控从本人明确选择产生 controls（stop_session、skip_topic、do_not_persist），不能从推測产生授权。
- 每个请求带 request_id。重试保持同一 ID 和内容；改内容须新 ID。JSON 整数不能传字符串。
- prepare_turn.context 可含 query、limit（1–8）、max_chars（200–3000）；deadline_ms（10–100）在顶层。只做本地检索，宿主另设端到端超时；失败继续自然对话，勿声称无历史。
- envelope.status：ok / accepted / ignored / degraded / error；另有 data、meta、error。错误看 error.code，每次保存最新 session_version。degraded 不等于记忆不存在。
- build_context 单独检索，get_chronicle 读取已发布视图；not_ready 不阻塞发话。
- get_job / list_jobs / list_entries 面向维护。set_policy / revise_entry / forget_entries / review_chronicle_event 需要维护角色和可信决定记录，不向对话模型暴露任意身份令牌。
- HTTP 前缀 /v1/memory，Bearer 令牌来自宿主环境变量。完整路径与字段以运行时 schemas/openapi.json 为准。
- Python 嵌入：Python 3.10+，按包依赖安装；await service.start()；await service.execute(operation, request, principal)；在 try/finally 的 finally 中 await service.close()，确保异常和取消时也清理。关闭会先停调度、再取消并等待后台任务，最后关闭数据库；可重复调用。调用方在关闭中被取消，会先完成清理再传播取消异常。关闭后需新建实例才能重启。不要每轮新建服务，否则易失缓冲丢失。
- CLI：python scripts/memory_cli.py prepare_turn --input request.json；需已安装运行时、MEMORY_API_TOKEN 和本地常驻服务，默认 http://127.0.0.1:8765。
- 同 GPU 后台模型并发默认 1；嵌入宿主可调用 set_foreground_busy(True/False) 暂停新模型任务。独立 HTTP 进程首版无此控制接口，部署层应预留前台资源或采用不同模型服务。已发出推理不自动抢占。
- fixture 是固定规则提取器，不代表真实模型理解能力。ChatCompletionsBackend 用于后台，不生成实时回复。
