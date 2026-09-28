import httpx

from .prompts import HEALTH_PROMPT


class QwenSemanticModel:
    """OpenAI-compatible local Qwen HTTP adapter; no hidden SDK retries."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "EMPTY",
        timeout: float = 30,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._owns_client = client is None

    async def analyze(self, text: str) -> str:
        response = await self.client.post(
            self.base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + self.api_key},
            json={
                "model": self.model,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": HEALTH_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Invalid Qwen response envelope") from exc
        if not isinstance(content, str):
            raise ValueError("Qwen content must be a string")
        return content

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
