from typing import Protocol


class SemanticModelPort(Protocol):
    async def analyze(self, text: str) -> str: ...
