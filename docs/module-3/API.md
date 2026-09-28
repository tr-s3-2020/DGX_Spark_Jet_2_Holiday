# 记忆 Skill 接口契约草案 v0.5

日期：2026-09-26｜状态：**v0.5 设计契约档案。现有 0.1.0 联调实现以[正式 OpenAPI](../../modules/life-memoir-retriever/schemas/openapi.json)及[运行说明](../../modules/life-memoir-retriever/README.md)为准；本稿未实现字段保留作后续目标。**

配套：[总体设计](DESIGN.md) · [完整 JSON 交互示例](api-examples.json)。本文给出首版拟固定的语义；获批后用共享数据模型生成 JSON Schema、OpenAPI、CLI 参数校验和接口测试，避免文档与实现各写一套。

v0.2 新增 `prepare_turn` 实时合并入口、检索等待预算、降级状态和耗时元数据，依据见 [GitHub 与低延迟调研](LATENCY-RESEARCH.md)。协议主版本仍拟为 1.0，当前尚未上线或冻结。

v0.3 新增语言字段与原文／译文约定，见 [语言适配设计](LANGUAGE-DESIGN.md)。API 字段和枚举使用英文，文本内容保持实际语言；不隐含每轮翻译。

v0.4 新增后台任务查询字段与 `list_jobs`，明确来源快照、依赖和发布版本。任务由数据变化触发，实时调用方不负责启动或等待每个分析步骤。

v0.5 新增可选情境提示与年谱读取／复核操作，时间模型见 [CHRONICLE-DESIGN.md](CHRONICLE-DESIGN.md)。仍为可评审契约，未实现服务。

## 1. 三种调用方式，共用同一核心

| 使用者 | 拟交付入口 | 说明 |
|---|---|---|
| 同进程主控 | `await service.observe_turn(request, caller_context)` 等 Python 方法 | 方法名见下面接口表；身份上下文由宿主注入 |
| 可以执行命令的 Agent | `memory-skill <operation> --input request.json` | stdout 仅输出 JSON；诊断写 stderr，默认不写用户原话 |
| 独立进程／其他语言 | `/v1/memory/...` HTTP API | UTF-8 JSON；鉴权、权限和校验与 Python 一致 |

实时轮次优先使用同进程 `prepare_turn` 或常驻 HTTP 客户端复用连接；不要求模型先决策调用哪个工具，不逐轮启动 CLI 进程。后台总结任务状态仅供监控，不能成为下一轮回复的等待条件。

`caller_context` 是可信宿主提供的调用者身份、可操作用户范围与角色，不能由模型生成的请求正文覆盖。HTTP 首版为已认证主控的回环服务；CLI 必须配置宿主授权范围，不能把读到的 `user_id` 当作授权。家属角色不能通过更换 `purpose` 获得本人会话上下文。

**不同模型厂商的接口属于内部分析适配层，不影响上述业务 API。** 本 API 不接受自由 SQL、任意 URL、模型系统提示词或原样工具执行指令。

## 2. 统一请求与响应约定

- 字符串 ID 为非空字符串，最长 128 字符；时间为带时区的 RFC 3339 字符串。
- 语言使用 BCP 47 标签（如 zh-CN、en-US）；未知文本语言为 und，混合语言为 mul。会话 locale 是偏好，不作为实际文本语言证据。
- 正文严格类型校验，拒绝未知字段；文本上限由服务配置，首版建议单轮 16,000 字符。
- 写入请求必带 `request_id`，HTTP 的 `Idempotency-Key` 若提供须与之一致。幂等作用域为调用者、用户、操作及资源。同键同载荷返回已发生操作的当前可见结果，同键不同载荷返回 409。
- 不缓存可在删除后重放的故事正文。记录已删除时，重放返回删除状态，不返回历史内容；被关闭会话的旧请求不能重新创建已清理记录。
- 所有操作进行用户归属检查；访问他人会话、记录或任务，统一返回 `NOT_FOUND`，不泄露是否存在。
- 每个成功响应包括 `api_version / request_id / status / data / error / meta`。`error` 成功时为 null，失败时 `data` 为 null。

```json
{
  "api_version": "1.0",
  "request_id": "req-context-1",
  "status": "ok",
  "data": {
    "session_state": {},
    "memories": [],
    "preferences": [],
    "review_suggestions": [],
    "match_status": "empty"
  },
  "error": null,
  "meta": {
    "policy_version": 1,
    "memory_revision": 0,
    "replayed": false,
    "warnings": [],
    "timings": {"total_ms": 4.0, "memory_ms": 2.0},
    "cache_status": "miss",
    "retrieval_complete": true,
    "deadline_exceeded": false
  }
}
```

`status = ok / accepted / ignored / degraded / error`。`degraded` 只用于部分可用且明确附警告的结果，不能把模型失败伪装成正常学习完成。无对应用户状态时 meta 中版本为 null。`memory_revision` 每次内容或权限变化单调递增，用于调用方失效旧缓存，不包含情绪评分。

上述 timings 为虚构样例值。meta 的基础字段为 policy_version、memory_revision、replayed、warnings；其余四项仅在上下文操作中额外必需，具体见 3.4 节。

## 3. 操作列表

| Python／CLI 操作名 | HTTP | 用途 |
|---|---|---|
| `set_policy` | `PUT /v1/memory/users/{user_id}/policy` | 建立或更改用户许可；仅可信主控／本人授权流程 |
| `open_session` | `POST /v1/memory/sessions` | 建立绑定用户的会话 |
| `observe_turn` | `POST /v1/memory/sessions/{session_id}/turns` | 记录用户最终轮次或实际助手回复 |
| `prepare_turn` | `POST /v1/memory/sessions/{session_id}/prepare-turn` | 实时入口：一次接收用户轮次、更新控制并读取上下文 |
| `build_context` | `POST /v1/memory/sessions/{session_id}/context` | 为本人对话或家属摘要提取受限上下文 |
| `close_session` | `POST /v1/memory/sessions/{session_id}/close` | 封闭会话并提交异步总结 |
| `get_job` | `GET /v1/memory/jobs/{job_id}` | 查询单个后台任务；不重新启动任务 |
| `list_jobs` | `GET /v1/memory/users/{user_id}/jobs` | 授权维护方查看自动触发的后台任务与依赖 |
| `list_entries` | `GET /v1/memory/users/{user_id}/entries` | 本人或授权维护方查看与复核记录 |
| `revise_entry` | `PATCH /v1/memory/entries/{entry_id}` | 更正内容、确认偏好或调整单条用途 |
| `forget_entries` | `POST /v1/memory/users/{user_id}/forget` | 删除指定记录或整段会话所衍生的内容 |
| `get_chronicle` | `GET /v1/memory/users/{user_id}/chronicle` | 读取已发布且按用途过滤的年谱；不等待生成 |
| `review_chronicle_event` | `PATCH /v1/memory/chronicle/events/{event_id}` | 本人复核时间或否定推算，触发受影响部分重建 |

CLI 请求 JSON 将 HTTP 路径／查询参数作为顶层字段传入；HTTP 正文不重复路径字段。只读 GET 可用 `X-Request-ID` 关联日志，未提供时由服务生成；其他操作正文带 `request_id`。同一字段不因调用方式改变含义。

### 3.1 `set_policy`

必填：`request_id`、`expected_version`（非负整数，首次为 0）、`grants`、`consent_ref`（宿主同意记录的 ID）。

`grants` 必须完整给出以下四个布尔值，服务初始均为 false：

| 字段 | 含义 |
|---|---|
| `long_term_memory` | 保存和用于本人对话的跨会话故事、细节、近期事项 |
| `profile_learning` | 保存交流观察并跨会话总结、使用沟通偏好 |
| `family_digest` | 允许提供家属摘要用途的记录；还需单条记录明确允许 |
| `remote_analysis` | 允许向配置的远程分析服务发送所需内容；不代表自动同意其他保存用途 |

当次必要的短期处理由宿主在开会话前说明；这些 grants 不代表可以默认永久保存聊天。`consent_ref` 必须可由宿主授权适配器核验，模型不能提供一个随机字符串自行打开权限。

成功返回 `policy_version` 与实际 `grants`。权限缩小时立刻阻止对应读写，失效上下文与相关在途写入；保留数据的删除由 `forget_entries` 执行。重新开启权限不自动让旧记录可分享，单条 `allowed_uses` 仍需复核。维护接口可供本人查看和删除受限的已有数据，但不能用作普通对话或家属读取的绕行入口。

### 3.2 `open_session`

必填：`request_id`、`user_id`、`session_id`、`locale`（如 `zh-CN`）。会话 ID 必须在服务内全局唯一，由宿主生成；服务解析 session_id 后仍检查所属用户是否在调用者授权范围内，不能因为知道 ID 就获得访问权。

可选 `reply_locale`（默认等于 locale）：希望对老人使用的语言；`conditions` 对象：`noise = quiet / noisy / unknown`、`communication_mode = voice / text`，未提供按 unknown 与 text 处理。仅记录实际条件，不由 locale 推断能力。

成功返回 `session_id`、`session_version: 1`、`state: open`、`policy_version`、`locale`、`reply_locale`。本服务回显语言选择不代表其他语音／健康组件支持；主控需先核对整条链路。已经存在的同请求按幂等规则返回；不同请求试图重建同会话返回 409。开会话可预读授权范围内少量有效偏好／近期事项到内存快照；不向模型发送历史。对外取上下文仍走 `build_context` 或 `prepare_turn` 并复核权限和有效期。

### 3.3 `observe_turn`

必填字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `request_id` | string | 该提交的幂等键 |
| `turn_id` | string | 用户轮次 ID；助手回复使用同 ID 关联 |
| `speaker` | `user / assistant` | 谁的发言 |
| `text` | string | 用户原语言最终转写／直接输入或实际助手回复；不把译文冒充原话，助手正文不能成为老人事实 |
| `is_final` | boolean | false 时返回 ignored，不更新状态、不进入总结 |
| `occurred_at` | datetime | 事件时间；有效期采用服务接收时间计算，避免任意客户端时间延长保留 |

可选字段：

- `text_locale`：实际文本语言，未知默认 und；混合语言用 mul。适配器已经确定语言时应显式提交，不额外调用实时语言检测 LLM。只拿到翻译文本却没有原语言转写的上游须明确暴露这一限制，不将其经本接口自动写为本人原话。
- `conditions`：与开会话同结构，仅更新本次实际观察。
- `response_style`：仅 assistant 可传，`question_form = open / concrete / none / unknown`、`tts_rate` 为大于 0 的实际倍率（未知省略）。不能将期望设置假装为已执行设置。
- `controls`：仅可信主控提交的当轮要求，`stop_session`（bool，默认 false）、`skip_topic`（string，省略表示无）、`do_not_persist`（bool，默认 false）。这些值只能收紧当前行为；不能覆盖更严格的用户许可。用户明确要求不记录时，主控应在提交前设置。

成功返回 `turn_id`、`speaker`、`session_version`、`storage: volatile`、`effective_controls`。内容首先只进入短期缓冲，持久记录由获准的总结生成。`do_not_persist` 的轮次及其关联上下文不得成为会后记忆、观察、偏好或证据的来源。明确禁止某内容后主控不得把它再次作为“新文本”提交。

用户轮次按主控顺序串行提交；同轮同角色重复提交不重复计入反馈。相同 `(session_id, turn_id, speaker)` 的不同正文返回 `TURN_CONFLICT`，不当作新事实。助手提交必须关联已存在用户轮次，补交后增加会话版本；总结只处理关闭时冻结的数据。

主控遇到不明确的保存范围时，应先禁止本轮持久化并澄清，不依赖会后模型决定是否已经获得许可。长期禁谈／不分享要求由主控同时调用权限或记录维护操作；`controls` 本身不默默生成永久画像。

### 3.4 `build_context`

必填：`request_id`、`purpose = conversation / family_digest`。可选：`query`（string，默认空，空查询只返回有效偏好和短期状态）、`limit`（1—8，默认 8）、`max_chars`（200—3000，默认 3000）、`deadline_ms`（10—100 的整数，默认 100）、`context_locale`（默认 original，也可指定实际语言标签）。服务可以配置更低上限；文本预算在 JSON 序列化之外计算，按原内容、可选译文及返回证据的总字符数限制。

输出始终有：

- `session_state`：当前话题、控制与临时状态；家属用途固定为空对象。
- `memories`：相关 `story / detail / recent`，每项包含 `entry_id / kind / content / content_locale / basis / source_refs / expires_at / version`。
- `preferences`：适用于当前条件的 active 偏好，同样包含来源及版本，并带 `conditions`。
- `review_suggestions`：待确认推断的核实建议，包含 `entry_id / question / source_refs`，不作确定偏好；家属用途为空。
- `match_status = matched / empty / partial / not_searched`：前两项表示已完成检索，后两项分别表示只有部分可用结果或未完成且没有可用历史结果；读取失败不能伪装成 empty。

`source_refs` 为证据 ID、会话与轮次引用，不把整个原始转写返回给调用方。本人维护接口可以查看获准留存的必要片段。家属用途只返回单条 `allowed_uses` 包含 `family_digest` 且全局许可允许的记录，`preferences` 固定为空。

v0.5 可选输出 `interaction_hints`，缺省视为 `[]`，家属用途固定为空。每项为 `{mode_id, basis, source_refs, scope, reason}`：mode_id 取情境草案六类之一；basis 为 `explicit_request / explicit_preference / tentative_observation`；scope 为 `turn / session`。只返回已有、获准的提示，不为此调用实时分类模型；tentative_observation 不能成为疾病事实或强制策略。当前发言由主控直接响应，无需等待提示。字段计入同一 max_chars 上限。

`context_locale` 请求其他语言时，content 仍保留主记录；只有已有、获准且源版本相同的译文才附加 `translation: {locale, content, source_entry_version}`。没有译文则省略 translation，返回原内容并附 `LANGUAGE_VARIANT_UNAVAILABLE` 警告、status 为 degraded；这不改变已完成检索的 match_status，也不表示原记录不存在。不得在线调用翻译突破预算。译文由后台适配产生且记录翻译来源，不能当第二条个人事实；未知／混合语言不猜测成目标语言。

检索先过滤用户、权限、状态与有效期，再按主题匹配、近期相关性和明确重要性排序。首版可用 SQLite 中的关键词／标签匹配；中文检索效果需用实际样例验证，不预先声称具有语义召回能力。向量检索后续接同一结果结构。

返回结果仅用于当前响应或当前摘要任务，主控不可跨会话无限缓存。本人撤回／删除时主控需丢弃已取得的上下文，并在输出前检查版本或取消当前生成；本服务无法追回已经播出或已发送的内容。

一次上下文读取基于一致的已发布 `memory_revision`，不读取未提交候选。普通后台更新在后续轮次重新取上下文时可见，不把迟到分析插入正在生成的回复；调用方不要求所有分析支路完成或达到某个未来版本才发话。权限收紧、更正和删除导致的失效仍须即时处理。

**等待预算与缓存：**在请求进入服务时用单调时钟计算截止点；上下文读取不调用分析模型、云 embedding 或 reranker。快照按用户、用途许可、`policy_version`、`memory_revision` 和有效期过滤，当前轮次状态每次重新组装；最终上下文不能只用 user_id 作缓存键。截止后取消额外检索，且不得把迟到结果插进已经开始的那段回复。

若权限和当轮控制已可靠确定，可返回仍有效的快照／短期信息，HTTP 200、`status: degraded`、警告 `CONTEXT_DEADLINE_EXCEEDED`，并用 partial 或 not_searched 表示不完整检索。没有可靠许可或数据可用性时返回错误，不使用无法核实的旧缓存。主控仍可按当前发言和安全流程回应，不声称记得未取到的事。预算是调度目标而非硬实时承诺，必须记录实际超时。

这两个上下文接口的 `meta` 额外包含：`timings: {total_ms, memory_ms}`（实际非负耗时）、`cache_status: hit / miss / bypass`、`retrieval_complete`（bool）、`deadline_exceeded`（bool）。`total_ms` 从服务接收至响应就绪，包含本服务校验与排队；调用方网络往返另测。hit 仅指用了有效快照，不代表主题检索完整。

### 3.4a `prepare_turn`（实时推荐入口）

必填：`request_id`、`turn`；可选 `context`、`deadline_ms`（默认 100，范围同上）。`turn` 使用 observe_turn 中除 request_id 外的字段，仅允许 `speaker=user`；`context` 使用 build_context 的 `query / limit / max_chars / context_locale`，不再嵌套 request_id 或 deadline，purpose 固定 conversation。

一次调用按顺序校验身份与许可、执行当轮控制、记录短期轮次并构造上下文，**同一预算覆盖整次调用，不给两个阶段各自再等 100 ms**。超过预算也不能跳过身份／控制检查；必要检查无法完成则返回错误。热会话 total_ms 的 p95 目标 ≤ 50 ms，尚未实测。

返回 `data: {observation, context}`：observation 使用 observe_turn 的成功结构，context 使用 build_context 的结构。partial 输入返回 ignored，context 为 null。`stop_session=true` 时保留停止信号，不继续额外主题搜索，主控据此停止追问；此时 context 的 match_status 为 not_searched，不冒充无匹配。

幂等语义只保证同一用户轮次不重复生效；重试返回相同 observation 标识，但 context 根据最新许可、记忆版本、有效期和当轮控制重建，不重放已删除内容。助手实际回复仍用 observe_turn 补交；不得为同一用户轮次既调用 prepare_turn，又把它作为新事件重复发送。

### 3.5 `close_session`、`get_job` 与 `list_jobs`

`close_session` 必填：`request_id`、`expected_session_version`（正整数）、`reason = completed / user_stopped / disconnected`。关闭首先冻结会话并增加会话版本，后续新轮次返回 `SESSION_CLOSED`。已有同载荷写入重放可以返回原记录状态，但不能再次写入。

有可总结内容且成功排队时 HTTP 202 返回 `job_id`、`state: queued`、`session_version`；没有可持久化内容时 HTTP 200 返回 `job_id: null`、`state: skipped`、`reason: no_eligible_content`，并清理缓冲。关闭成功但任务因容量不足无法排队时，HTTP 200、status 为 ok 只表示关闭成功，data 返回 `job_id`、`state: failed`、`session_version`，失败原因由 get_job 查询；不能声称总结已排队。模型任务只能使用获准类别的输入；一个授权不能隐含另一个授权。

`get_job` 返回 `job_id`、`state = queued / running / completed / skipped / failed / cancelled`、`created_entry_ids`、`updated_entry_ids`、`skipped_reasons`、`failure`（无失败为 null），以及以下调度字段：

| 字段 | 含义 |
|---|---|
| `kind` | `session_extract / story_reconcile / preference_reconcile / context_prepare / language_variant / chronicle_build` |
| `depends_on` | 所需前序任务 ID 数组；可以为空，不能形成环 |
| `snapshot` | `{policy_version, memory_revision, session_id, session_version, source_entry_versions}`；来源记录版本为 `[{entry_id, version}]`；历史整理不读会话缓冲时两个 session 字段为 null |
| `published_revision` | 本任务成功提交内容的记忆版本；未发布、没有变更或仅预热缓存时为 null |

任务 completed 只指本任务按规则结束，结果也可能为空；不表示形成了已确认偏好，也不表示其他支路完成。`snapshot.memory_revision` 用于追溯读取时刻，提交检查实际依赖的来源、权限和目标记录版本；无关记录变化不令所有并行任务失败。模型推理期间不锁住数据库写事务。

`list_jobs` 仅向本人授权维护角色开放，检查 user_id 的所属权限。可选查询参数：`state`、`kind`（枚举同上）、`cursor`、`limit`（1—50，默认 20）；返回 `{items, next_cursor}`，items 使用 get_job 同一结构。家属和普通实时会话调用不能借此查看分析记录。两种查询都不会触发模型调用，也不返回原文缓冲。

- 外层 HTTP 200 表示成功查到任务，任务本身是否成功看 `state`。
- 关闭后缓冲仅供 `session_extract` 使用；45 秒原型总预算包含排队与重试，到期清理，结果保存成功后提前清理。后续任务只用已保存且获准的记录与必要证据，不延长原文保留。
- 崩溃后没有输入的任务报告 `INPUT_UNAVAILABLE`；无原始输入则不能盲目重跑。版本变化导致取消的任务不自动恢复旧权限。
- 任务提交、候选落库、证据关联与幂等凭据在事务边界内处理。同一任务不会因重复查询再次写入。

**后台编排：**会后提取提交或相关记录变化时，服务按影响范围生成任务。同一用户、种类和来源版本的重复任务合并；依赖未完成保持 queued，前序失败或取消则取消依赖它的任务，无关支路继续。故事整理与偏好整合可以并行，索引／快照更新可以与模型分析重叠；模型请求受独立并发上限与前台优先策略控制。不因普通下一轮发言到来就取消所有历史分析。

队列须有容量与时限；无法排队的任务以 failed 与 `BACKGROUND_QUEUE_FULL` 可见，排队超时用 `QUEUE_TIMEOUT`。已关闭的会话不回滚成 open；受影响原文按既定预算清理。任务状态与待处理事件在短事务中记录，服务恢复时根据来源引用和任务凭据处理未完成工作；来源缺失则失败，不重新构造已删除内容。派生缓存更新不无条件再触发生成它的模型任务。

### 3.6 `list_entries` 与 `revise_entry`

`list_entries` 仅本人授权维护角色可用。查询参数：`kind`（五类之一，可省略）、`record_status`（可省略）、`cursor`（可省略）、`limit`（1—50，默认 20）。返回 `items`、`next_cursor`；每项包含完整可维护字段和证据引用。列表读取不能跨用户，也不能赋予家属角色。

`revise_entry` 必填：`request_id`、`expected_version`、`action`、`decision_ref`（宿主可核验的本人复核记录）。三种 action：

| action | 额外必填字段 | 行为 |
|---|---|---|
| `correct_content` | `content`（string） | 更正记录并增加版本，重新评估依赖旧内容的推断；不伪造原始证据 |
| `confirm_preference` | 无 | 仅用于待确认偏好，以本次决定作为确认依据；不适用于任意故事 |
| `set_allowed_uses` | `allowed_uses`（数组） | 设置单条用途；只能在全局许可内授予。允许空数组以限制普通调用 |

`allowed_uses` 枚举为 `conversation / family_digest`，去重且无其他值；观察与偏好首版只支持 conversation。默认新条目至多用于 conversation，家属用途必须有独立可核验许可。成功返回更新后的 entry 及新版本；过期或冲突条目不能仅靠改状态重新激活，需要有效的复核依据。

### 3.7 `forget_entries`

必填：`request_id`、`scope = entries / session`、`decision_ref`。scope 为 entries 时必填 `entry_ids`（非空数组，最多 50）；scope 为 session 时必填 `session_id`，禁止同时传两种选择条件。

先检查所选资源全属于同一被授权用户，再事务化删除，不能处理到一半才发现用户不匹配。删除记录、相关最小证据与派生内容；共享证据只有仍有合法依赖时可保留。偏好失去支持时删除或重新评估，不能继续以删除前证据使用。

成功返回 `deleted_entry_ids`、`invalidated_entry_ids`、`memory_revision`、`scope`。同步失效短期上下文，并取消可能写回所选来源的任务。scope=session 还要清除对应缓冲并阻止该会话再次总结；旧事件或幂等请求不能复活已遗忘内容。

这里的成功只覆盖服务管理的在线数据，不代表已经清除调用方副本、外部供应商记录或部署备份；首版不提供自动外发和自动备份。错误时不返回删除成功。

年谱也是派生内容：删除锚点时，依赖它的日期、相应年谱句子及视图缓存同步失效。另有独立合法依据的事件可保留，但不能继续携带已失效日期或隐藏的证据内容。

### 3.8 `get_chronicle`

查询参数：`purpose = conversation / family_digest`（必填），`format = structured / markdown`（默认 structured）。首版返回完整的已发布视图，大小超过部署上限则返回 `CHRONICLE_TOO_LARGE`，不得静默截断成完整年谱；分页作为后续扩展。请求须通过与上下文查询相同的用户、用途和当前权限检查。

data 返回 `{state, view_version, based_on_memory_revision, format, content, warnings}`。state 为 `ready / not_ready`；ready 时 content 在 structured 模式包含 `{items, narrative}`，条目使用年谱设计中的事件、时间候选与来源，narrative 每句关联其支持引用；markdown 为同源文本并显式标注原话、自述／推算、AI 整理及引用。临床情绪推测不进入年谱。

尚未生成、正在更新且旧视图已经失效时，HTTP 200、status 为 degraded、state 为 not_ready、view_version 与 content 为 null，附 `CHRONICLE_NOT_READY`；based_on_memory_revision 为 null，不返回已过期个人内容。没有已保存事件的已完成视图可 ready 并返回空列表，不能和未完成混淆。查询不触发模型，也不阻塞到任务完成。

只读视图在数据变化时由后台准备；没有有效 family_digest 授权不准备家属版。读取仍复核每条来源依赖和用途；家属视图不能仅隐藏源文本而保留依赖私有锚点推算出的日期。内容过期或权限变化时立即拒绝／失效；不能因缓存存在而绕过当前政策。年谱可读状态不表示老人已确认所有 AI 整理内容。

### 3.9 `review_chronicle_event`

仅本人授权维护流程；必填 `request_id / expected_event_version / action / decision_ref`。先解析 event_id 并验证所属用户。action：

| action | 额外输入 | 效果 |
|---|---|---|
| `confirm_time` | `resolution_version / alternative_id` | 本人明确确认这一时间候选，以可核验决定为新依据；不能把仅确认事件发生视为确认年份 |
| `correct_time` | `time_text`（本人更正的原语言说法）、`text_locale` | 保存更正依据，立即失效旧时间候选及相关年谱；后台重新解析，不要求调用方自己算年 |
| `reject_inference` | `resolution_version / alternative_id` | 撤销指定推算，保留尚未被否定的故事和原始说法；后续不得在无新依据时再推荐同一结论 |

成功返回 `{event_id, event_version, time_state, invalidated_view_versions, queued_job_ids}`，time_state 为 `current / pending_rebuild`；版本冲突返回 409。后台失败不回滚已经生效的更正或否定，旧视图继续不可用；不会把所有未知日期说成已核实。queued_job_ids 可能为空，失败的调度由 list_jobs 可见。幂等与拒绝重放删除内容的规则沿用现有接口。所有复核都必须有实际授权记录，不能把用户沉默当作决定。

## 4. 错误与降级

错误对象固定为 `{ "code": "...", "message": "...", "retryable": false }`，message 不包含用户原话或密钥。

| HTTP | code | 调用方处理 |
|---|---|---|
| 401 | `UNAUTHENTICATED` | 修复宿主身份配置 |
| 403 | `PERMISSION_DENIED` | 不换用途绕过；交回授权流程 |
| 404 | `NOT_FOUND` | 资源不存在或不在当前授权范围 |
| 409 | `VERSION_CONFLICT / TURN_CONFLICT / SESSION_CLOSED / IDEMPOTENCY_CONFLICT` | 读取当前状态后明确处理；不盲目重试写入 |
| 422 | `VALIDATION_ERROR` | 按契约修正输入；partial 不属此错误 |
| 422 | `CHRONICLE_TOO_LARGE` | 提示部署维护方调整输出规模或采用后续分页版本，不返回伪完整结果 |
| 503 | `STORAGE_UNAVAILABLE` | 可有限重试同一 request_id；主控明确无记忆结果，不能伪造已保存 |

后台分析失败使用任务的 `failure.code`：`MODEL_TIMEOUT / MODEL_OUTPUT_INVALID / INPUT_UNAVAILABLE / BACKGROUND_QUEUE_FULL / QUEUE_TIMEOUT / LANGUAGE_UNSUPPORTED`；权限或相关来源版本变化通常进入 cancelled，注明原因。CLI 在 ok／accepted／ignored 返回退出码 0，degraded 返回 3，error 返回非零；异步任务最终状态仍需查询。Python 返回同一 envelope，初始化配置错误单独抛出异常，不伪造业务响应。

超时由调用方设置；写入不确定时仅重试相同幂等键。读取延迟预算首轮联调测定，会后模型总预算拟定 45 秒且至多 2 次尝试，客户端不得叠加无限自动重试。

## 5. 模型适配契约

分析适配器接收内部 `AnalysisInput`：`task_id`、`operation`、`eligible_turns`、`existing_entries`、`existing_evidence`、`output_schema_version`。不接收整个用户数据库。后台历史整理的 eligible_turns 为空，使用限定范围的已保存记录及证据；每条输入携带可引用的 source ID、版本、实际语言及允许分析的类别。适配器保留原语言证据，需要英文处理时在获准的后台路径中转换，不覆盖 source 文本。

统一输出 `AnalysisResult`：`candidates`、`warnings`。candidate 至少有 `kind`、`content`、`content_locale`、`basis`、`source_ids`、`conditions`、`temporal_scope = session / recent / long_term`；偏好更新另带支持／反例 source IDs。主记录优先保留原语言摘要，需要的英文变体单独关联。无法可靠保留语义时标记待核实，不因流畅译文升级为事实。客户端不采纳模型输出的权限、身份、SQL 或删除指令。候选必须通过 Schema、来源存在性、许可和版本检查才能进入业务层。

`extract_candidates` 负责本次信息；`reconcile_stories` 整理相关历史经历并提出有来源的更新候选；`reconcile_preferences` 只用相关且获准的历史证据更新候选偏好。更新候选额外提供 `target_entry_id / expected_version`，目标类型与操作不匹配则拒绝。一次提取无内容可以正常返回空数组；一次推断不能修改本人已明确表达的事实。`context_prepare` 的索引／快照部分由程序执行，不必调用模型；可选语言变体沿用语言适配路径，不能作为另一条原始事实写入。

v0.5 的经历候选可带 `time_assertions`，字段见年谱设计。模型输出时间线索与锚点候选，确定性处理器负责计算年份范围及矛盾检查。`compose_chronicle` 只接收按用途过滤的已发布事件、时间结果与必要证据，返回 `{sentences: [{text, event_refs, evidence_refs, resolution_refs}], warnings}`；不是 extract_candidates 的结果结构。程序验证引用与版本，语义验收另查是否增加未支持事实。生成的年谱不是新的原始证据，不能通过回读自己扩大可信度。

适配器能力声明包括 `supports_structured_output`、`location`、`input_locales` 与 `output_locales`。不支持输入语言且没有已验证桥接路径时，后台任务报告 `LANGUAGE_UNSUPPORTED`，不强行分析。支持结构化输出时按服务能力使用；不支持时由适配器解析 JSON 并严格校验，失败返回 MODEL_OUTPUT_INVALID，不把未解析自然语言直接入库。每个新后端都运行相同的来源、错误与权限契约测试，中文／英文及跨语言语义表现分别评估。

## 6. 交接方需要准备的信息

主控需要提供稳定用户 ID、会话／轮次事件、本人许可与复核记录、会话结束通知及实际回复。DGX 部署方需要提供本地模型地址、协议类型、模型名和资源安排。HTTP 调用方只需固定本模块接口；更换模型由本模块配置或新增适配器完成。

语音和健康模块已有接口保持各自含义；本契约不会自动取代任务二的四字段协议。未审核前不得将这些示例当作已经上线的 API。
