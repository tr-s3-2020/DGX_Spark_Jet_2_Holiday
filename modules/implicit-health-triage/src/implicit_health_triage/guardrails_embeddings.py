"""Dependency-free embedding provider for the Colang flow index.

Why this exists
---------------
When a Colang 2.x ``main`` flow activates ``llm continuation``, the runtime
builds a semantic index over the configured flows
(``nemoguardrails.actions.v2_x.generation._init_colang_flows_index``) so the LLM
can pick a relevant flow.  That index uses the ``fastembed`` engine by default,
which pulls in **onnxruntime**.

The implicit-health-triage skill does not need semantic flow search at all: its
only Colang behaviour is one deterministic ``input rails`` flow that calls a
pure-Python action.  Making the safety fence depend on a large native ONNX
runtime is both unnecessary and, as measured on the development machine, fatal —
``import onnxruntime`` aborts the process with an access violation
(``0xC0000005``), which took the whole service down on every non-medication turn.

This module registers a deterministic hashing embedder instead.  It is pure
Python, has no third-party dependencies, and produces stable vectors, so the
flow index is built without loading any native library.  Accuracy of flow
selection is irrelevant here because the medication rail never relies on it.

On DGX Spark, where onnxruntime works normally, this component is still the
right default for this skill: it removes a ~50 MB native dependency from a
safety path that gains nothing from it.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from nemoguardrails.embeddings.providers import register_embedding_provider
from nemoguardrails.embeddings.providers.base import EmbeddingModel

#: Engine name referenced from ``guardrails/config.yml``.
ENGINE_NAME = "iht_hashing"

#: Vector width.  Small is fine — this only ranks short Colang snippets.
DIMENSIONS = 256

_WHITESPACE_RE = re.compile(r"\s+")


def embed_text(text: str, dimensions: int = DIMENSIONS) -> list[float]:
    """Deterministic character-bigram hashing embedder, L2 normalised."""

    compact = _WHITESPACE_RE.sub("", text or "")
    vector = [0.0] * dimensions

    if not compact:
        return vector

    tokens = (
        [compact[i : i + 2] for i in range(len(compact) - 1)]
        if len(compact) > 1
        else [compact]
    )

    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        vector[int.from_bytes(digest, "big") % dimensions] += 1.0

    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]

    return vector


class HashingEmbeddingModel(EmbeddingModel):
    """Offline, deterministic replacement for the default fastembed engine."""

    engine_name = ENGINE_NAME

    def __init__(self, embedding_model: str = "iht-hashing", **_: Any) -> None:
        self.embedding_model = embedding_model
        self._dimensions = DIMENSIONS

    def encode(self, documents: list[str]) -> list[list[float]]:
        return [embed_text(document, self._dimensions) for document in documents]

    async def encode_async(self, documents: list[str]) -> list[list[float]]:
        return self.encode(documents)


_registered = False


def register() -> None:
    """Register the provider.

    Idempotent: the registry raises ``ValueError`` when a name already exists,
    which happens whenever a second ``GuardrailsRuntime`` is created in the same
    process (the CLI demo, the FastAPI app under pytest, re-entrant tests).
    """

    global _registered

    if _registered:
        return

    try:
        register_embedding_provider(HashingEmbeddingModel, ENGINE_NAME)
    except ValueError:
        # Already present in the process-wide registry.
        pass

    _registered = True


__all__ = [
    "DIMENSIONS",
    "ENGINE_NAME",
    "HashingEmbeddingModel",
    "embed_text",
    "register",
]
