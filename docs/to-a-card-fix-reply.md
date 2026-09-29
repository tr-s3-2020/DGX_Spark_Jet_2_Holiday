# 回复 Zoe：A→D 卡片修复已合入，三件事回你

**报告人**：子扬（skill1 `elderly-voice-duplex`）
**日期**：2026-09-29
**对应来文**：`to-a-card-fix.md`（2026-09-29）

已把你 `12940af` 合进集成分支，**84 项测试全过**，并用我这边的一体化探针
（真实音频走 WebSocket → skill1 判 P0 → skill4 建卡）复验通过。

```
通话前  tier=P2  标题=【今日摘要】小明 2026-09-29
                sources=[]  健康观察=· 今天没有需要特别说明的健康观察。

说"我那个降压药今天能不能吃两颗？"（skill1 判 P0）

通话后  tier=P0  标题=【需要立即关注】用药/安全风险，建议尽快联系小明
                sources=['8069738bdd68:T1']
                健康观察=· 本次通话中出现需要立即关注的表述，已按最高等级通知家属。
```

三处全对上了，谢谢。下面回你那三件事。

---

## 1. `session_id` / `turn_id`：我本来就在传，现在能溯源了

我的 `family_card.py` 一直按你文档里的报文形状传这两个字段：

```python
event = normalize_a_safety(
    {"type": "final", "safety": level, "session_id": session_id,
     "turn_id": turn_id, "text": text},
    elder_id=elder_id, occurred_at=stamp)
```

以前适配器读出来了但没塞进模型，所以 `sources` 是空的；你补上之后
`sources=['<session>:T1']` 直接能跳到触发那句话。**不用改我这边任何代码。**

`text` 字段：确认你只做进程内调试、不落盘不进卡片，那我继续传——排错时要用。
我这边 `turn_id` 形如 `T1`/`T2`（按本次通话的收轮次序），不是全局唯一，
配上 `session_id` 才唯一，溯源时两个一起看。

## 2. P1 口径：确认「需要留意」，你的映射是对的

我这边 P1 的判定是症状级表述（头晕/头痛/恶心/肚子痛/腿沉/睡不着），
回复话术是"您先坐下歇会儿，缓缓神……回头告诉{kin}"。语义就是
**今天需要留意、要同步给家属**，和 D 的 P1 一致。

所以 `voice_safety_p1` → D 的 P1 **照你实现的保持，不用改 P2**。
我这边 P1 的量比 P0 多（老人抱怨身体不舒服是常事），但每条都会配上具体的
话术和时间线，家属侧不至于嫌吵——真嫌吵我们再一起调。

## 3. `routing.reasons` 已从家属可见区域撤下

按你说的降级了：现在只进 `title` tooltip 和 `console.debug`，家属看到的
`cardMeta` 只剩「来源 / 已脱敏 / 采集状态」。修复前它是家属可见的一行，
专门为了兜住"今天没有需要特别说明的健康观察"那句误导——现在你修好了，
不需要了。

---

## 另外提醒一件事：`upload-to-github/` 被误提交了

`282d378` 里有一个 `upload-to-github/` 目录，48 个文件，是
`modules/family-digest-sync` 和 `docs` 的**完整副本**。合并时一起进来了，
我在集成分支上补了一个提交把它删掉（`1a04077`）。

**建议你在自己分支上也删掉远端那份**，否则后面每次合并都会再把这份重复内容
带一遍。你的 `.gitignore` 没挡它——项目根的 `.gitignore` 是白名单制
（只放行 `README.md` / `.gitignore` / `modules/` / `团队协作指南.md`），
对**已跟踪**的文件不起作用，所以得显式 `git rm -r --cached`。

## 当前集成分支状态

| 套件 | 结果 |
|---|---|
| skill4（含你新增的 test_voice_safety） | 84/84 |
| skill1 barge_in | 22/22 |
| skill1 ws_protocol | 12/12 |
| skill1 family_card（A→D） | 11/11 |
| skill1 web_framing | 24/24 |
| skill1 duplex_live / voice_loop / ws_audio | 4/4 / 4/4 / 2 场景 |
| orchestrator | 20/20 |
