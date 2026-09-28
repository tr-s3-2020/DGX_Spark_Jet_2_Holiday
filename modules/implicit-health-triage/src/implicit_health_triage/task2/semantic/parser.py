import json
import re

from ..schemas import HealthSignal


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class HealthSignalParser:
    def parse(self, raw: str) -> HealthSignal | None:
        if not isinstance(raw, str) or len(raw) > 16000:
            return None
        text = raw.strip()
        fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        try:
            payload = json.loads(text, object_pairs_hook=unique_object)
            return HealthSignal.model_validate(payload)
        except (ValueError, TypeError, RecursionError):
            return None
