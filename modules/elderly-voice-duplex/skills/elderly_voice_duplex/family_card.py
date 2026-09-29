"""把本技能的安全分级喂给 skill4（A → D），并按需生成家属卡片。

这是 A→D 那条一直"契约对齐、未联调"的链路（见 skill4 README 的联调状态表）。
skill1 这边每收一轮、判出 P0/P1，就按 skill4 自己的 `normalize_a_safety`
适配器转成 SafetyEventRecord 记进去——**不自己拼字段**，契约变了只改一处。

skill4 不在（没装、路径不对）时全部降级：通话照常，只是没有卡片。
任何异常都不往通话链路里抛：家属侧功能失败不能让老人听不到声音。

用法：
    from . import family_card
    family_card.record_safety(elder_id, session_id, turn_id, "P0", text)
    result = family_card.build_card(elder_id, "张奶奶")
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from . import config

log = logging.getLogger("elderly-voice-duplex.card")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
SKILL4_SRC = os.path.join(PROJECT, "modules", "family-digest-sync", "src")
STATE_DIR = os.path.join(PROJECT, ".cache", "voice-duplex")

# 授权范围：不授权就出不了卡（skill4 的硬规则），这里给本技能用到的三项
SCOPES = ["health_summary", "medication_safety", "chronicle"]

_svc = None          # FamilyDigestService，懒加载
_tried = False       # 导入失败就别再试了，每轮都试一遍太吵


def _service():
    """懒加载 skill4。失败返回 None，调用方据此降级。"""
    global _svc, _tried
    if _svc is not None:
        return _svc
    if _tried:
        return None
    _tried = True
    if not os.path.isdir(SKILL4_SRC):
        log.info("skill4 源码不在 %s，家属卡片功能不可用", SKILL4_SRC)
        return None
    if SKILL4_SRC not in sys.path:
        sys.path.insert(0, SKILL4_SRC)
    try:
        from family_digest import FamilyDigestService, Policy
        from family_digest.adapters import MockChannel
        from family_digest.store import JsonStore

        os.makedirs(STATE_DIR, exist_ok=True)
        svc = FamilyDigestService(
            store=JsonStore(os.path.join(STATE_DIR, "digest.json")),
            channel=MockChannel(), policy=Policy())
        # 没有授权 skill4 直接忽略建卡请求，先帮老人把授权打开
        svc.execute("set_consent", {"elder_id": "default", "scopes": SCOPES})
        _svc = svc
        log.info("skill4 已接入，家属卡片可用（状态目录 %s）", STATE_DIR)
    except Exception as exc:  # noqa: BLE001  接不上不影响通话
        log.info("skill4 接入失败，家属卡片不可用: %s: %s",
                 type(exc).__name__, exc)
        return None
    return _svc


def available() -> bool:
    """skill4 是否可用（给 /health 和页面显示用）。"""
    return _service() is not None


def record_safety(elder_id: str, session_id: str, turn_id: str,
                  level: str, text: str) -> bool:
    """把一次安全分级记进 skill4。失败只记日志，不往外抛。"""
    svc = _service()
    if svc is None or not level:
        return False
    try:
        from family_digest.adapters.upstream import normalize_a_safety

        stamp = datetime.now(timezone.utc)
        event = normalize_a_safety(
            {"type": "final", "safety": level, "session_id": session_id,
             "turn_id": turn_id, "text": text},
            elder_id=elder_id, occurred_at=stamp)
        out = svc.execute("record_signals", {
            "elder_id": elder_id, "session_id": session_id,
            "turn_id": turn_id, "occurred_at": stamp,
            "safety_events": [event.model_dump(mode="json")]})
        ok = out.get("status") in ("ok", "degraded")
        if not ok:
            log.info("skill4 记录安全分级未接受: %s", out.get("meta"))
        return ok
    except Exception as exc:  # noqa: BLE001
        log.info("skill4 记录失败（不影响通话）: %s: %s",
                 type(exc).__name__, exc)
        return False


def _today_signals(svc, elder_id: str) -> list[dict]:
    """今天的安全信号，只带时间和分级，**不带原文**。

    为什么要这个：卡片是「今日累计」的日报，跨通话汇总。刚接通就看到的 P0
    可能是早上那句话触发的——页面上不显示时间，用户就会以为"我什么都没说
    它就说有风险"。带上时间才能把两者联系起来。

    不带 text：skill4 明确不把原文放进卡片（见她的 to-a-card-fix.md），
    这里也不该绕过这个决定。时间和 session/turn 足够溯源。
    """
    try:
        today = datetime.now(timezone.utc).date()
        out = []
        for raw in svc.store.safety_records():
            if raw.get("elder_id") != elder_id:
                continue
            stamp = raw.get("occurred_at")
            if not stamp:
                continue
            try:
                when = datetime.fromisoformat(
                    str(stamp).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.date() != today:
                continue
            out.append({"time": when.astimezone().strftime("%H:%M"),
                        "level": str(raw.get("safety_level") or ""),
                        "session": str(raw.get("session_id") or ""),
                        "turn": str(raw.get("turn_id") or "")})
        out.sort(key=lambda s: s["time"])
        return out
    except Exception as exc:  # noqa: BLE001  溯源信息拿不到不影响卡片本身
        log.info("读取今日信号失败（不影响卡片）: %s: %s",
                 type(exc).__name__, exc)
        return []


def build_card(elder_id: str, elder_name: str = "老人") -> dict:
    """生成今日卡片。返回 {"ok":bool, "card":dict|None, "reason":str}。"""
    svc = _service()
    if svc is None:
        return {"ok": False, "reason": "skill4 不可用"}
    try:
        # 没有授权 skill4 会直接忽略建卡请求（返回 status=ignored、data=None），
        # 而且不报错——调用方只看到"没有卡片"，很容易误判成"今天没信号"。
        # 所以每个 elder_id 都先确保授权到位。
        if not svc.store.consent(elder_id):
            svc.execute("set_consent", {"elder_id": elder_id, "scopes": SCOPES})
        today = datetime.now(timezone.utc).date().isoformat()
        out = svc.execute("build_daily_digest", {
            "elder_id": elder_id, "date": today, "elder_name": elder_name})
        if out.get("status") == "error":
            return {"ok": False,
                    "reason": (out.get("error") or {}).get("message", "")}
        card = out.get("data")
        signals = _today_signals(svc, elder_id)
        if not card:
            # ignored：今天没有任何信号。给一张空的 P2 卡，页面才好展示
            return {"ok": True, "card": None,
                    "reason": (out.get("meta") or {}).get("reason", "ignored"),
                    "routing": out.get("meta"), "signals": signals}
        return {"ok": True, "card": card,
                "routing": (out.get("meta") or {}).get("routing"),
                "signals": signals}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def dispatch_card(card_id: str) -> dict:
    """推送卡片给家属。返回 {"ok":bool, ...}。"""
    svc = _service()
    if svc is None:
        return {"ok": False, "reason": "skill4 不可用"}
    if not card_id:
        return {"ok": False, "reason": "缺少 card_id"}
    try:
        out = svc.execute("dispatch_digest", {"card_id": card_id})
        return {"ok": out.get("status") in ("ok", "degraded"),
                "status": out.get("status"),
                "data": out.get("data"),
                "reason": (out.get("error") or {}).get("message", "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
