#!/usr/bin/env python3
"""终端 REPL：把编排层包成能持续对话的界面。

每句话都会完整过一遍四方链路，并把轨迹打出来——这样"用药安全有没有拦"、
"记忆有没有取回"、"家属侧记了什么"都是可见的，不用改代码跑场景。

用法：
    ../../.venv-v019/bin/python repl.py              # 默认 elder=e001
    ../../.venv-v019/bin/python repl.py --elder e002 --kin 小明
    ../../.venv-v019/bin/python repl.py --no-llm      # 不调 Qwen，只验链路

会话内命令：
    /digest     生成并推送今日家属卡片（skill4）
    /memories   看 skill3 当前记住了什么
    /trace      切换详细/简洁输出
    /quit       挂断（会触发 skill3 的后台提炼）后退出

依赖：LLM 服务在 127.0.0.1:8000（scripts/serve_v019.sh）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from orchestrator import Orchestrator  # noqa: E402


def _today() -> str:
    """skill4 建卡用的日期。本地时区即可——卡片是给今天看的。"""
    from datetime import date
    return date.today().isoformat()

BANNER = """\
┌──────────────────────────────────────────────────────────────┐
│  老人陪伴 · 四方联调 REPL                                      │
│  skill1 语音围栏 → skill3 记忆 → Qwen3.6 → skill2 分诊 → skill4 家属 │
└──────────────────────────────────────────────────────────────┘
输入老人说的话开始；/digest 看家属卡片，/memories 看记忆，/quit 挂断
"""


def show(out, verbose: bool) -> None:
    """把一轮的链路轨迹打出来。"""
    # skill1 正则围栏
    if out.safety_level:
        print(f"  [skill1 围栏] {out.safety_level}")
    # skill3 记忆
    for ev in out.events:
        if ev.startswith("skill3_memories"):
            n = ev.split("=")[1]
            status = f"（{out.match_status}）" if out.match_status else "（未命中）"
            print(f"  [skill3 记忆] {n} 条 {status}")
            for m in out.memories[:3]:
                text = m.get("summary") or m.get("text") or str(m)
                print(f"      · {str(text)[:60]}")
    # skill2 分诊
    for ev in out.events:
        if ev.startswith("skill2="):
            body = ev[len("skill2="):]
            print(f"  [skill2 分诊] {body}")
    if out.triage and verbose:
        hs = out.triage["health_signal"]
        if hs.get("type") not in (None, "none"):
            print(f"      signal: {hs['type']} / {hs['detail']} / {hs['severity']}")
    # skill4 家属
    for ev in out.events:
        if ev.startswith("skill4="):
            body = ev[len("skill4="):]
            if body == "ok" and out.digest:
                d = out.digest.get("data") or {}
                bits = [f"{k}={v}" for k, v in d.items()
                        if isinstance(v, int)]
                print(f"  [skill4 家属] 已记录" + (f"（{' '.join(bits)}）" if bits else ""))
            else:
                print(f"  [skill4 家属] {body}")
    # 降级警告
    if out.degraded:
        print(f"  [warn] 降级: {', '.join(out.degraded)}")
    # 回复
    tag = {"voice": "Qwen", "triage_safety": "skill2标准话术",
           "voice_p0_guard": "skill1标准话术", "fallback": "兜底",
           "stub": "stub"}.get(out.reply_source, out.reply_source)
    print(f"  助手[{tag}]> {out.reply}")
    if verbose:
        timings = " ".join(f"{k}={v:.0f}ms" for k, v in out.timings.items())
        print(f"  ({timings})")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--elder", default="e001")
    ap.add_argument("--kin", default="your son")
    ap.add_argument("--locale", default="zh-CN")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    orch = Orchestrator(elder_id=args.elder, kin=args.kin,
                        locale=args.locale, use_llm=not args.no_llm)
    verbose = args.verbose
    print(BANNER)
    print(f"  elder={args.elder}  kin={args.kin}  llm="
          f"{'off' if args.no_llm else 'on'}")
    try:
        await orch.start()
        print(f"  通话已接通 session={orch.session_id}\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 起不来：{type(exc).__name__}: {exc}")
        print("       确认 LLM 服务在跑：PROFILE=full bash scripts/serve_v019.sh")
        return 1

    try:
        while True:
            try:
                text = input("老人> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n挂断")
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                print("挂断")
                break
            if text == "/trace":
                verbose = not verbose
                print(f"[ok] {'详细' if verbose else '简洁'}输出\n")
                continue
            if text == "/memories":
                try:
                    res = await orch._mem_call("list_entries",
                                               user_id=args.elder, limit=20)
                    items = (res.get("data") or {}).get("items") or []
                    print(f"[skill3] 共 {len(items)} 条记忆")
                    for it in items[:10]:
                        print(f"    · {str(it.get('summary') or it.get('text'))[:70]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[skill3] 读取失败: {exc}")
                print()
                continue
            if text == "/digest":
                try:
                    built = orch.digest().execute("build_daily_digest", {
                        "elder_id": args.elder, "date": _today(),
                        "elder_name": args.elder})
                    if built.get("status") == "error":
                        msg = (built.get("error") or {}).get("message", "")
                        print(f"[skill4] 生成失败: {msg}")
                        print()
                        continue
                    card = built.get("data") or {}
                    routing = (built.get("meta") or {}).get("routing") or {}
                    print(f"[skill4] 今日家属卡片  tier={card.get('tier')}"
                          f"  触发原因={routing.get('reasons')}")
                    print("    " + (card.get("title") or "").strip())
                    for s in card.get("sections") or []:
                        print(f"    [{s['heading']}]")
                        for line in str(s.get("body") or "").splitlines():
                            print("      " + line)
                    # dispatch_digest 要的是 card_id，不是 elder_id/date
                    cid = card.get("card_id")
                    if not cid:
                        print("    （没有卡片可推送）")
                    else:
                        sent = orch.digest().execute("dispatch_digest",
                                                     {"card_id": cid})
                        print(f"[skill4] 推送结果: {sent.get('status')}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[skill4] 失败: {exc}")
                print()
                continue

            try:
                out = await orch.turn(text)
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] 这一轮出错: {type(exc).__name__}: {exc}\n")
                continue
            show(out, verbose)
            print()
    finally:
        try:
            await orch.close()
            print("[skill3] session 已关闭，后台提炼已触发")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
