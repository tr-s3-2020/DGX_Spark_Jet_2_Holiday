"""幂等三件套：event_id / alert_key / (elder_id, date, type)。

上游重放、网络重试、日报重复生成，都靠这一层挡住。
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Set, Tuple

from .models import HealthSignalRecord, SafetyEventRecord, SignalType


def alert_key(record: HealthSignalRecord) -> str:
    """同一老人、同一天、同一类信号的去重键。"""
    subtype = record.rule_id or record.signal_type.value
    return f"{record.elder_id}|{record.local_date.isoformat()}|{subtype}"


def daily_key(elder_id: str, for_date: date) -> str:
    return f"{elder_id}|{for_date.isoformat()}"


def safety_key(record: SafetyEventRecord) -> str:
    return f"{record.elder_id}|{record.local_date.isoformat()}|{record.safety_level.value}"


class DedupIndex:
    """内存 + 可持久化的已见键集合。v0 用 JSON 存，正式版换数据库。"""

    def __init__(self, seen: Iterable[str] = ()) -> None:
        self._seen: Set[str] = set(seen)

    def seen(self, key: str) -> bool:
        return key in self._seen

    def mark(self, key: str) -> None:
        self._seen.add(key)

    def first_time(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def export(self) -> List[str]:
        return sorted(self._seen)


def dedup_records(
    records: List[HealthSignalRecord], index: DedupIndex
) -> Tuple[List[HealthSignalRecord], List[str]]:
    """按 event_id 去重，返回（新记录，被跳过的 event_id）。"""
    fresh: List[HealthSignalRecord] = []
    skipped: List[str] = []
    for r in records:
        if index.first_time(f"evt:{r.event_id}"):
            fresh.append(r)
        else:
            skipped.append(r.event_id)
    return fresh, skipped


def collapse_repeats(records: List[HealthSignalRecord]) -> Dict[str, HealthSignalRecord]:
    """同一 alert_key 只保留最重的一条，避免日报里刷屏。"""
    worst: Dict[str, HealthSignalRecord] = {}
    order = {"low": 1, "moderate": 2, "high": 3}
    for r in records:
        key = alert_key(r)
        cur = worst.get(key)
        if cur is None or order[r.severity.value] > order[cur.severity.value]:
            worst[key] = r
    return worst


SIGNAL_LABEL = {
    SignalType.NONE: "未识别",
    SignalType.SYMPTOM: "身体不适",
    SignalType.SLEEP: "睡眠",
    SignalType.PAIN: "疼痛",
    SignalType.MEDICATION: "用药",
    SignalType.MOBILITY: "行动能力",
    SignalType.APPETITE: "食欲",
    SignalType.OTHER: "其他体征",
}
