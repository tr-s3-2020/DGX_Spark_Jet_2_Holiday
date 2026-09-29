"""统一入口：宿主/Agent 只用 `execute(operation, request)` 调 D。

操作列表（v0.1）
- set_consent            登记家属可见范围
- record_signals         消费 B 的 health_signal 与 A 的 safety_level
- build_daily_digest     生成当日家属卡片（幂等）
- dispatch_digest        推送（含重试与死信）
- get_digest             查询卡片与发送状态
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Sequence

from .adapters.channel import Channel, MockChannel
from .adapters.llm import NullSummarizer
from .cards import build_card, render_plain_text
from .dedup import DedupIndex, dedup_records
from .models import (
    Acquisition,
    CardStatus,
    ChronicleInput,
    DigestCard,
    DispatchResult,
    DispatchStatus,
    HealthSignalRecord,
    Policy,
    SafetyEventRecord,
    Tier,
)
from .routing import route
from .state import CardStateMachine
from .store import JsonStore
from .timeutil import normalize_stamp

OPERATIONS = (
    "set_consent",
    "record_signals",
    "build_daily_digest",
    "dispatch_digest",
    "get_digest",
)


def _ok(data: Any, meta: Dict[str, Any] | None = None) -> dict:
    return {"status": "ok", "data": data, "meta": meta or {}, "error": None}


def _degraded(data: Any, warnings: List[str], meta: Dict[str, Any] | None = None) -> dict:
    return {
        "status": "degraded",
        "data": data,
        "meta": {**(meta or {}), "warnings": warnings},
        "error": None,
    }


def _ignored(reason: str) -> dict:
    return {"status": "ignored", "data": None, "meta": {"reason": reason}, "error": None}


def _error(code: str, message: str) -> dict:
    return {
        "status": "error",
        "data": None,
        "meta": {},
        "error": {"code": code, "message": message, "retryable": False},
    }


def _load_health(rows: Sequence[Dict[str, Any]]) -> List[HealthSignalRecord]:
    out = []
    for r in rows:
        payload = dict(r)
        if payload.get("occurred_at") is not None:
            # 兼容 Z 后缀（3.10 不认）+ 统一本地墙钟，落库后不会再产生 Z
            payload["occurred_at"] = normalize_stamp(payload["occurred_at"])
        out.append(HealthSignalRecord(**payload))
    return out


def _load_safety(rows: Sequence[Dict[str, Any]]) -> List[SafetyEventRecord]:
    out = []
    for r in rows:
        payload = dict(r)
        if payload.get("occurred_at") is not None:
            payload["occurred_at"] = normalize_stamp(payload["occurred_at"])
        out.append(SafetyEventRecord(**payload))
    return out


class FamilyDigestService:
    def __init__(
        self,
        store: JsonStore | None = None,
        store_path: str | None = None,
        channel: Channel | None = None,
        summarizer=None,
        policy: Policy | None = None,
    ) -> None:
        self.store = store or JsonStore(store_path)
        self.channel = channel or MockChannel()
        self.summarizer = summarizer or NullSummarizer()
        self.policy = policy or Policy()
        self.dedup = DedupIndex(self.store.dedup_keys())

    # ------------------------------------------------------------------
    def execute(self, operation: str, request: Dict[str, Any]) -> dict:
        if operation not in OPERATIONS:
            return _error("UNKNOWN_OPERATION", f"未知操作：{operation}")
        try:
            handler = getattr(self, "_op_" + operation)
            return handler(request or {})
        except Exception as exc:  # noqa: BLE001 —— 统一包成 envelope，不抛给宿主
            return _error("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    def _op_set_consent(self, req: Dict[str, Any]) -> dict:
        elder_id = req.get("elder_id")
        scopes = req.get("scopes", [])
        if not elder_id:
            return _error("VALIDATION_ERROR", "缺少 elder_id")
        self.store.set_consent(elder_id, list(scopes))
        return _ok({"elder_id": elder_id, "scopes": list(scopes)})

    def _op_record_signals(self, req: Dict[str, Any]) -> dict:
        raw_health = req.get("health_signals", [])
        raw_safety = req.get("safety_events", [])
        if not raw_health and not raw_safety:
            return _ignored("empty_payload")

        health = _load_health(raw_health)
        safety = _load_safety(raw_safety)

        fresh, skipped = dedup_records(health, self.dedup)
        for r in fresh:
            self.store.append_health(r.model_dump(mode="json"))
        for s in safety:
            if self.dedup.first_time(f"evt:{s.event_id}"):
                self.store.append_safety(s.model_dump(mode="json"))
        self.store.set_dedup(self.dedup.export())

        return _ok(
            {"accepted": len(fresh), "skipped": skipped, "safety": len(safety)},
            {"deduped": bool(skipped)},
        )

    def _op_build_daily_digest(self, req: Dict[str, Any]) -> dict:
        elder_id = req.get("elder_id")
        day_raw = req.get("date")
        if not elder_id or not day_raw:
            return _error("VALIDATION_ERROR", "缺少 elder_id 或 date")
        for_date = date.fromisoformat(day_raw) if isinstance(day_raw, str) else day_raw

        granted = req.get("consents") or self.store.consent(elder_id)
        if not granted:
            return _ignored("no_family_digest_consent")

        history = _load_health(
            [r for r in self.store.health_records() if r.get("elder_id") == elder_id]
        )
        today = [r for r in history if r.local_date == for_date]
        safety_all = _load_safety(
            [r for r in self.store.safety_records() if r.get("elder_id") == elder_id]
        )
        today_safety = [s for s in safety_all if s.local_date == for_date]

        decision = route(today, today_safety, history, elder_id, for_date, self.policy)
        chronicle = ChronicleInput(**(req.get("chronicle") or {}))

        card_id = f"card:{elder_id}:{for_date.isoformat()}:{decision.tier.value}"
        existing = self.store.get_card(card_id)
        if existing:
            return _ok(DigestCard(**existing).model_dump(mode="json"),
                      {"idempotent": True, "routing": decision.as_dict()})

        card = build_card(
            card_id=card_id,
            elder_id=elder_id,
            elder_name=req.get("elder_name", "老人"),
            for_date=for_date,
            tier=decision.tier,
            records=today,
            chronicle=chronicle,
            granted=granted,
            degraded_suspected=decision.degraded_suspected,
            acquisition=decision.acquisition,
        )
        card.status = CardStatus.VALIDATED
        self.store.put_card(card.model_dump(mode="json"))

        payload = card.model_dump(mode="json")
        if decision.acquisition is not Acquisition.ACQUIRED:
            return _degraded(payload, card.warnings, {"routing": decision.as_dict()})
        return _ok(payload, {"routing": decision.as_dict()})

    def _op_dispatch_digest(self, req: Dict[str, Any]) -> dict:
        card_id = req.get("card_id")
        to = req.get("to", "family")
        if not card_id:
            return _error("VALIDATION_ERROR", "缺少 card_id")
        raw = self.store.get_card(card_id)
        if not raw:
            return _error("NOT_FOUND", f"卡片不存在：{card_id}")

        card = DigestCard(**raw)
        fsm = CardStateMachine(card_id, card.status, self.policy.retry_limit)

        # 已发过：幂等返回，不重复打扰家属
        if fsm.state in (CardStatus.SENT, CardStatus.ACKNOWLEDGED):
            return _ok(DispatchResult(
                card_id=card_id, channel=self.channel.name,
                status=DispatchStatus.SENT, receipt_id="already-sent",
                attempts=fsm.attempts, reason="idempotent",
            ).model_dump(mode="json"), {"idempotent": True})
        if fsm.state is CardStatus.DEAD_LETTER:
            return _degraded(
                DispatchResult(card_id=card_id, channel=self.channel.name,
                               status=DispatchStatus.DEAD_LETTER,
                               reason="上一轮已进死信，需人工处理").model_dump(mode="json"),
                ["上一轮已进死信，需人工处理"],
            )
        if fsm.state is CardStatus.DRAFT:
            fsm.transition(CardStatus.VALIDATED, "auto_validate")
        if fsm.state is CardStatus.VALIDATED:
            fsm.transition(CardStatus.SCHEDULED, "queued")

        text = self.summarizer.polish(render_plain_text(card))

        result = DispatchResult(card_id=card_id, channel=self.channel.name,
                                status=DispatchStatus.RETRYING)
        while True:
            try:
                receipt = self.channel.send(to, card.title, text)
            except Exception as exc:  # noqa: BLE001
                state = fsm.on_failure(str(exc))
                card.status = state
                result.attempts = fsm.attempts
                if state is CardStatus.RETRYING:
                    continue
                result.status = DispatchStatus.DEAD_LETTER
                result.reason = str(exc)
                break
            else:
                fsm.transition(CardStatus.SENT, receipt["receipt_id"])
                card.status = CardStatus.SENT
                result.status = DispatchStatus.SENT
                result.receipt_id = receipt["receipt_id"]
                result.attempts = fsm.attempts + 1
                break

        self.store.put_card(card.model_dump(mode="json"))
        self.store.append_dispatch(result.model_dump(mode="json"))
        if result.status is DispatchStatus.DEAD_LETTER:
            return _degraded(result.model_dump(mode="json"), [result.reason or "发送失败"])
        return _ok(result.model_dump(mode="json"))

    def _op_get_digest(self, req: Dict[str, Any]) -> dict:
        card_id = req.get("card_id")
        if not card_id:
            return _error("VALIDATION_ERROR", "缺少 card_id")
        raw = self.store.get_card(card_id)
        if not raw:
            return _error("NOT_FOUND", f"卡片不存在：{card_id}")
        return _ok(raw)
