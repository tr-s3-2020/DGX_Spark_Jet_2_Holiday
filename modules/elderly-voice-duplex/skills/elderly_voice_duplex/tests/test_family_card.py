#!/usr/bin/env python3
"""A→D 联调回归：skill1 的安全分级必须真的进得了 skill4 的卡片。

这条链路在 skill4 的 README 里长期标着"契约对齐、未联调"。接上之后要盯住两件事：
  1. 分级记录成功、卡片升级到对应 tier（否则家属永远看不到风险）
  2. skill4 不在时全部降级，通话链路不受影响（它是旁路，不是必需品）

用临时状态目录，不碰 .cache/voice-duplex 下真实那份额卡数据。

用法：
    python3 tests/test_family_card.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.elderly_voice_duplex import family_card  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    return cond


async def main() -> int:
    ok = True
    print("[a2d] skill1 安全分级 -> skill4 家属卡片")

    if not family_card.available():
        print("  SKIP skill4 源码不在，跳过（降级路径另行验证）")
        # 降级本身也要验：拿不到 skill4 时不能抛异常
        ok &= check("skill4 缺失时 build_card 不抛异常",
                    family_card.build_card("default", "老人").get("ok") is False)
        ok &= check("skill4 缺失时 record_safety 返回 False",
                    family_card.record_safety("default", "S", "T1", "P0",
                                              "测试") is False)
        print("[a2d] " + ("PASS（降级路径）" if ok else "FAIL"))
        return 0 if ok else 1

    # 换到临时状态目录，别污染真实卡片数据
    tmp = tempfile.mkdtemp(prefix="evd-a2d-")
    family_card.STATE_DIR = tmp
    family_card._svc = None
    family_card._tried = False
    try:
        svc = family_card._service()
        ok &= check("skill4 能接上", svc is not None)

        # 建卡前：今天没有任何信号 -> P2
        before = family_card.build_card("probe", "张奶奶")
        ok &= check("无信号时也能出卡", before.get("ok") is True,
                    str(before.get("reason")))
        ok &= check("无信号时是 P2",
                    (before.get("card") or {}).get("tier") == "P2",
                    str((before.get("card") or {}).get("tier")))

        # 记一条 P0：这是 A 侧唯一能产出的东西（normalize_a_safety）
        ok &= check("记录 P0 分级成功",
                    family_card.record_safety("probe", "S1", "T1", "P0",
                                              "我那个降压药今天能不能吃两颗？"))

        after = family_card.build_card("probe", "张奶奶")
        card = after.get("card") or {}
        routing = after.get("routing") or {}
        ok &= check("P0 分级把卡片顶到 P0", card.get("tier") == "P0",
                    str(card.get("tier")))
        ok &= check("触发原因是 voice_safety_p0",
                    "voice_safety_p0" in (routing.get("reasons") or []),
                    str(routing.get("reasons")))

        # 同一条重复记不该重复计数（skill4 自己的去重）
        family_card.record_safety("probe", "S1", "T1", "P0", "同一句")
        again = family_card.build_card("probe", "张奶奶")
        ok &= check("重复记录不会改错 tier",
                    (again.get("card") or {}).get("tier") == "P0")

        # 推送要 card_id：这是编排层踩过的坑（传 elder_id/date 会被拒）
        cid = card.get("card_id")
        ok &= check("卡片带上了 card_id", bool(cid), str(cid))
        if cid:
            sent = family_card.dispatch_card(cid)
            ok &= check("按 card_id 推送成功", sent.get("ok") is True,
                        str(sent.get("reason")))
        ok &= check("空 card_id 不会抛异常",
                    family_card.dispatch_card("").get("ok") is False)
    finally:
        family_card.STATE_DIR = os.path.join(
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                         "..", "..")), ".cache", "voice-duplex")
        family_card._svc = None
        family_card._tried = False
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("[a2d] " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
