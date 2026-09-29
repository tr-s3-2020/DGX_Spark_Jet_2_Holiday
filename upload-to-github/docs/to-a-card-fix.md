# 回复子扬：A→D 卡片两个问题（已修）

**日期**：2026-09-29
**对应来文**：`to-d-a2d-findings.md`（2026-09-29）
**状态**：✅ 已修复，测试 68 → **84**（新增 `tests/test_voice_safety.py`，16 项）

## 1. 结论：你报的两个都成立，根因就是你写的那句

> 只认 health record、不认 safety event。

D 是**双输入**（B 的 `health_signal` + A 的 `safety_level`），但**渲染层写出来的时候只当自己有一条输入**。
纯语音场景（A 抛 P0、B 一条 record 都没有）正好把这个洞踩满：路由是对的（`voice_safety_p0`），
卡片内容是错的。你的路由复现没问题 —— 问题在 `build_card` 之后。

顺带说一句，这不是 A 的问题也不是契约问题，是 D 自己没闭环：**路由看得懂 safety event，卡片看不懂。**

## 2. 改了什么

| 位置 | 改动 |
|---|---|
| `models.py` · `SafetyEventRecord` | 新增 `session_id` / `turn_id` 两个**可选**字段（缺省空串），加 `source_ref()` 与 `has_signal()` 两个方法 |
| `adapters/upstream.py` · `normalize_a_safety` | 把 A 报文里的 `session_id` / `turn_id` 填进记录 —— **以前读出来了但没塞进模型**，白读 |
| `routing.py` · `summarize_acquisition` | 签名加 `safety=()`；只要今天有 P0/P1 的 safety event，直接判 `ACQUIRED`（三处调用全部传入） |
| `routing.py` · `route` | **新增**语音 P1 → D 的 P1 映射（`voice_safety_p1`），见第 3 节 |
| `cards.py` · `build_card` | 新增 `safety=()` 参数；P0 标题条件加 `or voice_p0`；`sources` 收 safety 的 `session:turn` |
| `cards.py` · `_no_signal_line` | health 段为空时的兜底句按情形分三档（见下） |
| `service.py` | `build_card(...)` 传 `safety=today_safety` |

### 关于你没提到的第三处：`render_health_section` 的兜底句

同一个根因还有第三处泄漏：health 段一条都没有时，原文硬写
「**今天没有需要特别说明的健康观察**」。

也就是说，**就算标题修对了，同一张卡片的正文中段还在说"没事"** —— 家属读到的是自相矛盾的一张卡。
现在按三种情形分开写：

| 情形 | 文案 |
|---|---|
| 今天真的一条都没有 | 保持原句 |
| 有语音 P0/P1，家属有 `medication_safety` 授权 | 「本次通话中出现需要立即关注的表述，已按最高等级通知家属。」 |
| 有语音 P0/P1，**但没给** `medication_safety` 授权 | 「有内容超出当前授权范围，本次未展示。」 |

第三种必须兜住：没授权时**不能退回"今天没事"那句**，那是把「不给你看」说成「没有」。

## 3.5 你也没提到的第四处：A 的 P1 在 D 里被整个丢掉了

顺手自查时发现：`route()` 里 P0 映射只看 `safety_level == P0`，而 P1 的四个来源
（`single_day_high` / `trend` / `sameday_count` / `baseline_deviation`）**全部只看 B 的 health record**。

结果就是：**A 判定 P1 的话，在 D 里既不进 P0 分支，也不产生任何 P1 reason，直接掉到 P2。**
老人明确说了需要留意的话，家属侧收到的是一张"今日摘要"，零感知。

现在补上：

```python
if any(s.safety_level is SafetyLevel.P1 for s in today_safety):
    reasons.append("voice_safety_p1")
```

这符合原有的边界纪律 —— P1 也是**映射**（来源就是 A 的分级），不是 D 自创；
不写这一条才是对上游判定的二次静默修改。**如果你那边 P1 的含义不是「需要留意」，
或者你觉得 P1 太吵，回我一句，改成 P2 + 高亮也行**，这是个产品口径问题，我按最保守的「映射即通知」实现了。

## 4. 需要你配合一件事

`sources` 要能溯源到具体哪句话，**前提是 A 的 WebSocket 消息里带上 `session_id` / `turn_id`**。
你的文档里 safe-level 消息形如 `{"type": "final", "safety": "P0", "text": "..."}`，没有这两个字段。

适配器已经**支持且向下兼容**（没有就留空，`sources` 不出现脏数据），但只要你在消息里补上：

```json
{"type": "final", "safety": "P0", "text": "...", "session_id": "s1", "turn_id": "t3"}
```

家属侧就能直接跳到触发那句话。**这是可选的**，没有也能跑，只是溯源为空。

另外确认一下：`text` 字段**只做进程内调试**（模型的原文），D 不落盘、不进卡片、不进 `sources`。
如果你那边怕残留，其实可以直接不传 `text`。

## 4. 你这边的临时措施可以撤了

> 我这边先把 `routing.reasons` 显示在网页卡片上

建议**保留但降级**：现在标题已经是「用药/安全风险，建议尽快联系王奶奶」，`reasons` 属于内部调试信息，
给家属看 `voice_safety_p0` 这种英文 id 反而是噪音。留着做你们自己的 debug 面板就好。

## 5. 验证

新增 `tests/test_voice_safety.py`（15 项），覆盖你报的两个问题 + 上面第三处 + A 适配器字段 + 端到端：

```python
# 端到端：只有 safety event、一条 health record 都没有
res = svc.execute("build_daily_digest", {"elder_id": "e1", "date": "2026-09-29"})
assert res["status"] == "ok"          # 以前是 degraded
assert data["tier"] == "P0"
assert data["acquisition"] == "acquired"   # 以前是 not_acquired
assert data["warnings"] == []              # 以前有「本次未能获取健康信息」
assert "用药/安全风险" in data["title"]
assert data["sources"] == ["s1:t3"]
```

`pytest -q` → **83 passed**。

## 6. 复现（你 merge 之后）

```bash
pytest modules/family-digest-sync/tests -q
```

你的探针 `.cache/tmp-work/a2d_probe.py` 不用改，直接再跑一遍：
卡片标题应变成「【需要立即关注】用药/安全风险，建议尽快联系小明」，`acquisition` 应为 `acquired`，
健康观察段应出现「本次通话中出现需要立即关注的表述」。
