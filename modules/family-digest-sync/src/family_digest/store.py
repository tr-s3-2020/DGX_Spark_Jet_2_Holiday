"""v0 用 JSON 文件持久化，正式版换数据库时只需替换本文件。"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List

DEFAULT_FILENAME = "family_digest_store.json"


class JsonStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))))),
            "var",
            DEFAULT_FILENAME,
        )
        self._lock = threading.Lock()
        self.data: Dict[str, Any] = {
            "health_signals": [],
            "safety_events": [],
            "cards": {},
            "dispatches": [],
            "consents": {},
            "dedup": [],
        }
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ---- 便捷读写 ----
    def append_health(self, payload: Dict[str, Any]) -> None:
        self.data["health_signals"].append(payload)
        self.save()

    def append_safety(self, payload: Dict[str, Any]) -> None:
        self.data["safety_events"].append(payload)
        self.save()

    def health_records(self) -> List[Dict[str, Any]]:
        return list(self.data["health_signals"])

    def safety_records(self) -> List[Dict[str, Any]]:
        return list(self.data["safety_events"])

    def set_consent(self, elder_id: str, scopes: List[str]) -> None:
        self.data["consents"][elder_id] = scopes
        self.save()

    def consent(self, elder_id: str) -> List[str]:
        return list(self.data["consents"].get(elder_id, []))

    def put_card(self, card: Dict[str, Any]) -> None:
        self.data["cards"][card["card_id"]] = card
        self.save()

    def get_card(self, card_id: str) -> Dict[str, Any] | None:
        return self.data["cards"].get(card_id)

    def append_dispatch(self, payload: Dict[str, Any]) -> None:
        self.data["dispatches"].append(payload)
        self.save()

    def set_dedup(self, keys: List[str]) -> None:
        self.data["dedup"] = sorted(set(keys))
        self.save()

    def dedup_keys(self) -> List[str]:
        return list(self.data["dedup"])
