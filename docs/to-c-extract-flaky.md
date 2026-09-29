# skill3 提炼 job 间歇性 MODEL_OUTPUT_INVALID（给卢易锋）

**报告人**：子扬（skill1 `elderly-voice-duplex`）
**日期**：2026-09-29
**现象**：老人分享了一句话、挂断等提炼，但卡片「近期回忆」不更新。
**根因**：`session_extract` job 失败，条目根本没创建，后面全免谈。

## 复现环境

- 语音服务 `modules/elderly-voice-duplex/skills/elderly_voice_duplex/server.py`
- skill3 用 `backend="chat_completions"` 打到本机 vLLM（Qwen3.6-35B-A3B）
- `Settings(timeout_seconds=120, total_budget_seconds=300)`，其余默认

调用路径：`memory.close()` → `close_session(expected_session_version=...)`
→ 内部 `_enqueue(uid, "session_extract")` → 后台 job 调
`ChatCompletionsBackend.analyze("extract_candidates", inputs)` →
`AnalysisResult.model_validate_json(content)` 抛错 →
`BackendError("MODEL_OUTPUT_INVALID")`。

## 今天实测的成败记录

| 时间 |  elder_id | 结果 |
|---|---|---|
| 20:55 | default | failed |
| 20:57 | default | failed |
| 21:28 | default | **ok**（提升 2 条） |
| 21:50 | share-2150 | failed |
| 22:03 | share-2203 | **ok** |
| 22:04 | share-2204 | failed |
| 22:10~22:20 | share-221x ×6 | **ok ×6** |

合计 8 成功 / 4 失败，**约 33% 失败率**。失败的那几次间隔只有 3 秒左右
（`close_session` → job failed），比正常一次 LLM 调用（3~17 秒）短得多，
像是模型很快返回了不合 schema 的内容。

## 我排除掉的原因

**① 不是 prompt/输入问题。** 用失败那几次的**完全相同**输入（含被 ASR
重复识别的那句 `'我年轻时在纺织厂上了三十年班。我年轻时在纺织厂上了三十年班。'`）
in-process 复跑，5/5 全部成功。

**② 不是 max_tokens 截断。** 请求没设 `max_tokens`；连打 10 次，
`finish_reason` 全是 `stop`，长度 898~927 字符，没有一次 `length`。

**③ 不是队列满。** `max_pending_jobs=64`，失败码是 `MODEL_OUTPUT_INVALID`
而不是 `BACKGROUND_QUEUE_FULL`。

**④ 不是 job 依赖/调度。** 失败时 `session_extract` 自己是 failed，
后面的 history job 压根没建。

## 还没定位到的

最大的嫌疑是**服务端同时还在跑语音 LLM 调用**，两边并发打同一个 vLLM 时
JSON 约束解码偶发出问题。我加过调试钩子想抓原始输出，但装了钩子之后
连跑 6 次全是成功的，没抓到现场（钩子已撤）。

想请你帮忙看两个地方：

1. `ChatCompletionsBackend.analyze` 里 `except (ValueError, KeyError,
   IndexError, TypeError)` 把 pydantic 的 ValidationError 和
   "content 是 null" 混成了同一个码。**建议把原始 content 和校验错误
   带进 failure**（比如 `{"code": "MODEL_OUTPUT_INVALID", "detail": ...}`），
   否则线上只能看到码、看不到内容，排查成本极高——我这次就是卡在这。

2. 这个模型在 `chat_template_kwargs={"enable_thinking": False}` 下，
   并发时有可能会返回 `content: null`（我在语音链路上见过：额度不够时
   只吐思考、答案是 null）。如果提炼也撞上，`model_validate_json(None)`
   会走 TypeError → MODEL_OUTPUT_INVALID。**建议显式判空并区分成
   MODEL_EMPTY_CONTENT**，这样至少能分清"模型没答"和"答得不对"。

## 对我这边的影响

我已在 skill1 侧做了兜底：分享前先查本次通话的提炼 job 状态，失败时明确
告诉老人"这一句没有存下来，让老人再说一遍即可"（而不是报"提升 0 条"那种
会把人带偏的话）。所以功能是可用的，只是偶尔要让老人重复一句。

## 复现脚本

`.cache/tmp-work/dbg_extract_fail.py`（in-process，输入可配置）和
`.cache/tmp-work/verify_share.py`（走真实 WebSocket 全链路）。
注意前者 5/5 成功、后者约 2/3 成功——差别就在并发。
