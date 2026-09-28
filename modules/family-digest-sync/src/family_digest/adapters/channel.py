"""渠道适配器。v0 只给 Mock / Console / 可控失败三种，正式版换实现不改上层。"""

from __future__ import annotations

import uuid
from typing import Dict, List


class Channel:
    name = "base"

    def send(self, to: str, title: str, body: str) -> Dict[str, str]:
        raise NotImplementedError


class ConsoleChannel(Channel):
    """打印到标准输出，用于 CLI 演示。"""

    name = "console"

    def send(self, to: str, title: str, body: str) -> Dict[str, str]:
        print(f"--- 推送给 {to} ---\n{title}\n\n{body}\n")
        return {"receipt_id": "console-" + uuid.uuid4().hex[:8], "status": "sent"}


class MockChannel(Channel):
    """内存渠道，测试可用，可查已发内容。"""

    name = "mock"

    def __init__(self) -> None:
        self.sent: List[Dict[str, str]] = []

    def send(self, to: str, title: str, body: str) -> Dict[str, str]:
        receipt = {"receipt_id": "mock-" + uuid.uuid4().hex[:8], "status": "sent"}
        self.sent.append({"to": to, "title": title, "body": body, **receipt})
        return receipt


class FlakyChannel(Channel):
    """前 n 次失败，用来验证重试与死信。"""

    name = "flaky"

    def __init__(self, fail_times: int = 2) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def send(self, to: str, title: str, body: str) -> Dict[str, str]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("channel unavailable")
        return {"receipt_id": "flaky-" + uuid.uuid4().hex[:8], "status": "sent"}
