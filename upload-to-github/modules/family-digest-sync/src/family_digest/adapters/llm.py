"""可选的模型润色适配器。

默认关闭：家属侧文案走确定性模板，可预期性优先。
打开后也只是「润色」，不能引入新事实，也不能覆盖降级说明。
"""

from __future__ import annotations

from typing import Protocol


class Summarizer(Protocol):
    name: str

    def polish(self, text: str) -> str: ...


class NullSummarizer:
    """默认：不动一个字。"""

    name = "none"

    def polish(self, text: str) -> str:
        return text


class MockSummarizer:
    """演示用：只做无信息增量的规范化，绝不自造内容。"""

    name = "mock"

    def polish(self, text: str) -> str:
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()
