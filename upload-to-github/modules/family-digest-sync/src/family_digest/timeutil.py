"""时间戳处理：不依赖 Python 3.11 的 ``fromisoformat`` 行为。

背景（联调编排层 2026-09-29 报的 bug）：

- ``pydantic`` 的 ``model_dump(mode="json")`` 会把 **UTC 时间**序列化成 ``"...Z"``；
- ``datetime.fromisoformat()`` 直到 **3.11** 才认 ``Z`` 后缀，**3.10 直接抛
  ``ValueError``**；
- 而 ``pyproject.toml`` 声明的是 ``requires-python = ">=3.10"``。

结果就是：调用方只要传 tz-aware 时间，写进 store 变成 ``Z``，再读回来解析就炸。
本模块把所有时间戳的进出收口到一处，**外部调用方怎么传都不会炸**。

另一个副作用一并解决：tz-aware 的 UTC 时间直接取 ``.date()`` 会按 UTC 算日期，
东八区的"今天早上七点"会被算成前一天——日报按天生成，这会让内容串天。
所以这里统一转成**本地墙钟时间**再落库。单地部署（主控与技能同机）下这是最符合直觉的口径。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = [
    "iso_z_to_offset",
    "parse_datetime",
    "to_local_naive",
    "normalize_stamp",
]


def iso_z_to_offset(value: str) -> str:
    """把 ISO 字符串尾部的 ``Z`` 换成 ``+00:00``。

    单独成函数是为了可测：即使跑在高版本 Python 上（其 ``fromisoformat``
    本来就认 ``Z``），也能验证替换逻辑确实执行了。
    """
    if value.endswith(("Z", "z")):
        return value[:-1] + "+00:00"
    return value


def parse_datetime(value: Any) -> datetime:
    """解析 ISO 时间戳，兼容 ``Z`` 后缀与已经是 datetime 的输入。"""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(iso_z_to_offset(str(value)))


def to_local_naive(dt: datetime) -> datetime:
    """tz-aware → 本地墙钟时间（去掉 tzinfo）；naive 原样返回。"""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def normalize_stamp(value: Any) -> datetime:
    """解析 + 统一成本地 naive 时间。

    落库后不再出现 ``Z``，``local_date`` 也按本地日期算。
    """
    return to_local_naive(parse_datetime(value))
