"""卡片发送状态机。

失败不静默：重试到上限进死信，且死信必须能被查到 —— 家属侧漏发比迟到严重。
"""

from __future__ import annotations

from typing import Dict, List

from .models import CardStatus

TRANSITIONS: Dict[str, List[str]] = {
    CardStatus.DRAFT: [CardStatus.VALIDATED],
    CardStatus.VALIDATED: [CardStatus.SCHEDULED],
    CardStatus.SCHEDULED: [CardStatus.SENT, CardStatus.RETRYING, CardStatus.DEAD_LETTER],
    CardStatus.RETRYING: [CardStatus.SENT, CardStatus.RETRYING, CardStatus.DEAD_LETTER],
    CardStatus.SENT: [CardStatus.ACKNOWLEDGED],
    CardStatus.DEAD_LETTER: [],
    CardStatus.ACKNOWLEDGED: [],
}


class InvalidTransition(Exception):
    pass


def can_transit(current: CardStatus, target: CardStatus) -> bool:
    return target.value in TRANSITIONS.get(current.value, [])


class CardStateMachine:
    def __init__(self, card_id: str, state: CardStatus = CardStatus.DRAFT,
                 retry_limit: int = 3) -> None:
        self.card_id = card_id
        self.state = state
        self.retry_limit = retry_limit
        self.attempts = 0
        self.history: List[str] = [state.value]

    def transition(self, target: CardStatus, reason: str = "") -> CardStatus:
        if not can_transit(self.state, target):
            raise InvalidTransition(
                f"{self.card_id}: {self.state.value} -> {target.value} 不允许"
            )
        self.state = target
        self.history.append(target.value + (f"({reason})" if reason else ""))
        return self.state

    def on_failure(self, reason: str = "") -> CardStatus:
        """一次发送失败：还能重试就 retrying，否则死信。"""
        self.attempts += 1
        if self.attempts < self.retry_limit:
            return self.transition(CardStatus.RETRYING, reason or "send_failed")
        return self.transition(CardStatus.DEAD_LETTER, reason or "retry_exhausted")
