"""打到本地 vLLM 服务的 LLM 后端（就是现在跑着的 Qwen3.6-35B-A3B）。

三个实测踩过的坑，都写在这里：
  1. vLLM 0.19 的思考内容在 `reasoning` 字段，不是 OpenAI 的 `reasoning_content`
  2. 这个模型思考很长（上千字），max_tokens 给小了会只吐思考、content 是 null
  3. 语音链路必须关思考：开着思考首个"答案" token 要 2.3s+，关掉只要 ~100ms、
     整句 ~400ms。老人那边 500ms 内听不到声音就会以为断了。
     做法是 chat_template_kwargs={"enable_thinking": false}（Qwen 模板原生支持）。
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from .. import config
from .base import LLMBackend


class VLLMBackend(LLMBackend):
    def __init__(self, base: str | None = None, model: str | None = None):
        self.base = (base or config.LLM_BASE).rstrip("/")
        self.model = model or config.LLM_MODEL

    # ------------------------------------------------------------ 非流式

    async def complete(self, *, system: str, history: list[dict], user: str,
                       max_tokens: int | None = None,
                       temperature: float | None = None,
                       think: bool = True) -> str:
        chunks: list[str] = []
        async for delta in self.stream(system=system, history=history,
                                       user=user, max_tokens=max_tokens,
                                       temperature=temperature, think=think):
            chunks.append(delta)
        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError(
                f"LLM 返回空 content（think={think}）。这个模型的思考很长，"
                f"额度不够时答案会被挤成 null——试着调大 max_tokens"
                f"（当前 {max_tokens or config.LLM_MAX_TOKENS}）")
        return text

    # ------------------------------------------------------------ 流式

    async def stream(self, *, system: str, history: list[dict], user: str,
                     max_tokens: int | None = None,
                     temperature: float | None = None,
                     think: bool = True):
        """异步产出**答案**增量（思考内容被跳过）。

        首 token 延迟由调用方在收到第一段时计时——注意要计到第一个答案 token，
        不是第一个 token：开着思考时第一个 token 是思考，对老人没有意义。

        读 socket 放在工作线程里、用队列递回来：否则阻塞事件循环，
        LLM 生成期间就收不到音频帧，打断（barge-in）会失灵。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        def worker():
            try:
                for chunk in self._stream_sync(system, history, user,
                                               max_tokens, temperature, think):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except BaseException as exc:  # noqa: BLE001  原样递回事件循环
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        loop.run_in_executor(None, worker)

        while True:
            item = await queue.get()
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def _stream_sync(self, system: str, history: list[dict], user: str,
                     max_tokens: int | None, temperature: float | None,
                     think: bool):
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(history)
        if user:
            messages.append({"role": "user", "content": user})

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
            "temperature": (config.LLM_TEMPERATURE if temperature is None
                            else temperature),
            "stream": True,
        }
        if not think:
            body["chat_template_kwargs"] = {"enable_thinking": False}

        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc

        def gen():
            with resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        text = delta.get("content")
                        if text:
                            yield text
        return gen()
