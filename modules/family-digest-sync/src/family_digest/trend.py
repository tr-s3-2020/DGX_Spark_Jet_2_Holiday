"""跨日趋势与基线。

B 每次只看单句、不保存历史；C 不存健康序列。所以「连续几天」「偏离基线」
这类判断天然只能由 D 来做 —— 这是 D 不可替代的地方。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Sequence

from .models import (
    HealthSignalRecord,
    Policy,
    SEVERITY_ORDER,
    Severity,
    SignalType,
    signal_group,
)


def _is_evidence(r: HealthSignalRecord, min_severity: Severity) -> bool:
    return (
        r.signal_type is not SignalType.NONE
        and SEVERITY_ORDER[r.severity] >= SEVERITY_ORDER[min_severity]
    )


def daily_evidence_counts(
    history: Sequence[HealthSignalRecord], min_severity: Severity
) -> Dict[date, int]:
    counts: Dict[date, int] = defaultdict(int)
    for r in history:
        if _is_evidence(r, min_severity):
            counts[r.local_date] += 1
    return dict(counts)


def consecutive_days(
    history: Sequence[HealthSignalRecord],
    elder_id: str,
    for_date: date,
    signal_type: SignalType,
    min_severity: Severity,
    max_lookback: int = 14,
    group: str | None = None,
) -> int:
    """从 for_date 往前数，同类信号连续达到 min_severity 的天数。

    ``group`` 非空时按**趋势分组**匹配而非精确 type。B 的 type 是浅层次结构
    （疼痛/睡眠/食欲/行动能力 都是「身体不适」的子类），按精确值匹配会把
    「今天说腿沉、昨天说睡不好」这种真趋势算成中断，所以默认按组。
    """
    by_date: Dict[date, List[HealthSignalRecord]] = defaultdict(list)
    for r in history:
        if r.elder_id != elder_id:
            continue
        hit = (
            signal_group(r.signal_type) == group
            if group
            else r.signal_type is signal_type
        )
        if hit:
            by_date[r.local_date].append(r)

    streak = 0
    cursor = for_date
    for _ in range(max_lookback):
        day_hits = [r for r in by_date.get(cursor, []) if _is_evidence(r, min_severity)]
        if not day_hits:
            break
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


def baseline_mean(
    history: Sequence[HealthSignalRecord],
    elder_id: str,
    for_date: date,
    policy: Policy,
) -> float:
    """不含当天的历史窗口内，日均「中度及以上」条数。"""
    start = for_date - timedelta(days=policy.baseline_window_days)
    per_day: Dict[date, int] = defaultdict(int)
    for r in history:
        if r.elder_id != elder_id:
            continue
        d = r.local_date
        if d >= for_date or d < start:
            continue
        if _is_evidence(r, Severity.MODERATE):
            per_day[d] += 1
    if not per_day:
        return 0.0
    return sum(per_day.values()) / policy.baseline_window_days


TREND_GROUPS = ("bodily", "medication", "other")


def trending_groups(
    history: Sequence[HealthSignalRecord],
    elder_id: str,
    for_date: date,
    policy: Policy,
) -> List[str]:
    """哪些趋势分组已经连续达标。按组而不是按精确 type —— 见 signal_group。"""
    out = []
    for grp in TREND_GROUPS:
        # 组内任一具体 type 都算，取该组最长的连续天数
        best = 0
        for st in SignalType:
            if st is SignalType.NONE or signal_group(st) != grp:
                continue
            best = max(
                best,
                consecutive_days(
                    history, elder_id, for_date, st, policy.trend_min_severity,
                    group=grp,
                ),
            )
        if best >= policy.trend_days:
            out.append(grp)
    return out


def trending_types(
    history: Sequence[HealthSignalRecord],
    elder_id: str,
    for_date: date,
    policy: Policy,
) -> List[str]:
    """兼容旧名：返回趋势分组（语义已按 B 的浅层次结构修正）。"""
    return trending_groups(history, elder_id, for_date, policy)
