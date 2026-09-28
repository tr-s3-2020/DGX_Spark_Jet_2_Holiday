"""授权范围校验 + 脱敏。

原则：最小披露。没有授权就一个字都不给；有授权也要先脱敏。
"""

from __future__ import annotations

import re
from typing import Iterable, List

from .models import ConsentScope

# 顺序有讲究：身份证 18 位必须先于手机号匹配，否则手机号正则会吃掉身份证中间一段
PATTERNS = [
    ("id_card", re.compile(r"\d{17}[\dXx]")),
    ("phone", re.compile(r"1[3-9]\d{9}")),
    ("bank_card", re.compile(r"\d{16,19}")),
    ("address_unit", re.compile(r"(?:栋|号楼|单元|室)\s*\d+")),
]

MASK = {
    "id_card": "[身份证已隐去]",
    "phone": "[手机号已隐去]",
    "bank_card": "[卡号已隐去]",
    "address_unit": "[门牌已隐去]",
}

MAX_CHARS = 400


def redact(text: str) -> str:
    """规则级脱敏。正式版可叠加 NER，但规则层必须先兜住。"""
    if not text:
        return ""
    out = text
    for name, pattern in PATTERNS:
        out = pattern.sub(MASK[name], out)
    if len(out) > MAX_CHARS:
        out = out[: MAX_CHARS - 2] + "……"
    return out


def is_redaction_applied(text: str) -> bool:
    return any(pattern.search(text or "") for _, pattern in PATTERNS)


def check_scope(granted: Iterable[str], required: ConsentScope) -> bool:
    return required.value in set(granted or [])


def visible_scopes(granted: Iterable[str]) -> List[str]:
    return [s for s in (granted or []) if s in {m.value for m in ConsentScope}]
