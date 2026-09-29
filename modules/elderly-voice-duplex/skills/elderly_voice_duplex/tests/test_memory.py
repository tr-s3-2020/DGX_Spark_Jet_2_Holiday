#!/usr/bin/env python3
"""语音链路的记忆（skill3）接线回归。

这条用例是冲着踩过的坑写的：`set_policy` 漏传 `user_id`，skill3 返回
VALIDATION_ERROR，而调用方只判断"有没有抛异常"——不抛，于是**授权从没生效**。
后果是挂断时 `close_session` 以 "no_eligible_content" 跳过提炼，表现是
"说了话但永远记不住"，而且一路都不报错。

所以这里不只验"能跑通"，还要验**每一步的 status**——静默失败是这条链路上
最容易发生、最难查的一类问题。

慢路径（真跑一遍提炼）要打真模型，默认跳过：
    EVD_MEMORY_E2E=1 python3 tests/test_memory.py

用法：
    python3 tests/test_memory.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.elderly_voice_duplex import memory  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    return cond


async def main() -> int:
    ok = True
    print("[memory] 语音链路的 skill3 接线")

    if not memory.available():
        print("  SKIP skill3 源码不在，跳过")
        # 降级本身也要验：接不上时不能抛异常
        ok &= check("skill3 缺失时 prepare_turn 返回空列表",
                    await memory.prepare_turn("s", "T1", "测试") == [])
        ok &= check("skill3 缺失时 chronicle 返回 None",
                    await memory.chronicle("default") is None)
        print("[memory] " + ("PASS（降级路径）" if ok else "FAIL"))
        return 0 if ok else 1

    tmp = tempfile.mkdtemp(prefix="evd-mem-")
    memory.STATE_DIR = tmp
    memory._svc = None
    memory._tried = False
    try:
        svc = memory._service()
        ok &= check("skill3 能接上", svc is not None)

        # ---- 授权必须真的生效 ----
        # 这是本次修的 bug：漏传 user_id 会让 set_policy 静默返回
        # VALIDATION_ERROR，而调用方只判断有没有抛异常——不抛。
        # 后果是 close_session 以 no_eligible_content 跳过提炼，
        # 表现是"说了话但永远记不住"，一路都不报错。
        elder = "polcheck"
        res = await memory._call(svc, elder, "set_policy", user_id=elder,
                                 expected_version=0, consent_ref="consent",
                                 grants={"long_term_memory": True,
                                         "profile_learning": True,
                                         "family_digest": True,
                                         "remote_analysis": False})
        ok &= check("set_policy 带 user_id 时成功",
                    res.get("status") == "ok",
                    f"status={res.get('status')} "
                    f"error={(res.get('error') or {}).get('code')}")
        grants = ((res.get("data") or {}).get("grants") or {})
        ok &= check("授权里 long_term_memory 为真（否则提炼会被跳过）",
                    grants.get("long_term_memory") is True, str(grants))

        # 漏传 user_id 必须失败——这条锁住"别再把 user_id 删了"
        bad = await memory._call(svc, elder, "set_policy",
                                 expected_version=0, consent_ref="consent",
                                 grants={"long_term_memory": True,
                                         "profile_learning": True,
                                         "family_digest": True,
                                         "remote_analysis": False})
        ok &= check("漏传 user_id 会被 skill3 拒绝（证明上面那条不是偶然成功）",
                    bad.get("status") == "error",
                    f"status={bad.get('status')}")

        # 第二次设同一份授权：skill3 报 VERSION_CONFLICT，适配层要幂等认下
        ok &= check("重复设授权被幂等接受",
                    await memory._set_policy(svc, elder))

        # ---- 开 session ----
        elder = "memtest"
        sid = "sess-" + str(os.getpid())
        ok &= check("开 session 成功", await memory.start(elder, sid))

        # ---- prepare_turn 必须返回可用的 observation ----
        items = await memory.prepare_turn(sid, "T1", "我最喜欢看的电影是泰坦尼克号",
                                          elder)
        ok &= check("prepare_turn 不抛异常且返回列表",
                    isinstance(items, list), str(type(items)))
        ok &= check("session_version 被记下（close_session 要用）",
                    sid in memory._versions, str(list(memory._versions)))

        # ---- chronicle 不能因为路径顺序问题炸掉 ----
        # memory.chronicle 自己挂 skill4 的 sys.path，不依赖 family_card
        # 先被导入（否则 ModuleNotFoundError，又静默返回 None）
        chrono = await memory.chronicle(elder)
        ok &= check("chronicle 不抛异常（拿不到内容也算过）",
                    chrono is None or isinstance(chrono, dict),
                    str(type(chrono)))

        # ---- 慢路径：真跑一遍提炼，验证记忆真的能存下来 ----
        if os.environ.get("EVD_MEMORY_E2E"):
            print("  （E2E）关 session 触发提炼，等它跑完…")
            await memory.close(elder, sid, drain_seconds=180)
            sid2 = "sess2-" + str(os.getpid())
            await memory.start(elder, sid2)
            got = await memory.prepare_turn(
                sid2, "T1", "我最喜欢看的电影是什么？", elder)
            print(f"  （E2E）取回 {len(got)} 条记忆")
            for m in got[:3]:
                print(f"      {str(m)[:90]}")
            ok &= check("E2E：跨会话能取回刚才那条记忆",
                        any("泰坦尼克" in str(m) for m in got), str(got)[:200])
            await memory.close(elder, sid2, drain_seconds=60)
        else:
            # 不跑 E2E 也要把 session 收掉，别留下 pending task
            await memory.close(elder, sid, drain_seconds=5)
    finally:
        memory.STATE_DIR = os.path.join(
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                         "..", "..")), ".cache", "voice-duplex")
        memory._svc = None
        memory._tried = False
        memory._versions.clear()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("[memory] " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
