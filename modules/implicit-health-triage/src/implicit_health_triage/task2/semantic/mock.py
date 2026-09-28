import re

from ..schemas import HealthSignal, none_signal


class MockSemanticModel:
    """Deterministic demo fixtures, not a general language model."""

    async def analyze(self, text: str) -> str:
        patterns = [
            (r"腿(?:有点)?沉|腿没劲", "symptom", "下肢沉重/乏力", "moderate"),
            (r"睡不着|失眠|睡不好", "sleep", "睡眠不佳", "moderate"),
            (r"头疼|头痛", "pain", "头痛", "moderate"),
            (r"走路不稳", "mobility", "行走不稳", "moderate"),
            (r"没胃口|吃不下饭", "appetite", "食欲不佳", "moderate"),
            (r"按时吃药|药.*按时吃", "medication", "已按时服药", "low"),
        ]
        for pattern, kind, detail, severity in patterns:
            if re.search(pattern, text):
                return HealthSignal(type=kind, detail=detail, severity=severity).model_dump_json()
        return none_signal().model_dump_json()
