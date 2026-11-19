"""Provider 工厂：把配置翻译成具体的 Embedding + Chat 实例。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from ..config import api_key
from .base import ChatProvider, EmbeddingProvider, ProviderError
from .mock import MockChat, MockEmbedder
from .openai_compat import OpenAICompatProvider


@dataclass
class Provider:
    kind: str
    embedder: EmbeddingProvider
    chat: ChatProvider

    @property
    def is_mock(self) -> bool:
        return self.kind == "mock"

    @property
    def embedding_model(self) -> str:
        return getattr(self.embedder, "model", "unknown")

    @property
    def dim(self) -> int | None:
        return getattr(self.embedder, "dim", None)


def build_provider(config: Mapping[str, Any]) -> Provider:
    prov = dict(config.get("provider") or {})
    kind = str(prov.get("kind") or "mock").lower()

    if kind == "mock":
        return Provider("mock", MockEmbedder(), MockChat())

    if kind in {"ark", "openai", "openai-compatible"}:
        key = api_key(config)
        if not key:
            env_name = prov.get("api_key_env") or "ARK_API_KEY"
            raise ProviderError(
                f"provider={kind} 需要 API Key，但环境变量 {env_name} 未设置。\n"
                f"可选：export {env_name}=<key>，或先改用离线模式 KB_PROVIDER=mock"
            )
        impl = OpenAICompatProvider(
            base_url=str(prov.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3"),
            api_key=key,
            chat_model=str(prov.get("chat_model") or "doubao-seed-1-6-250615"),
            embedding_model=str(prov.get("embedding_model") or "doubao-embedding-large-text-250515"),
            timeout=float(prov.get("timeout") or 60),
            max_retries=int(prov.get("max_retries") or 3),
            batch_size=int(prov.get("batch_size") or 16),
            embedding_dim=prov.get("embedding_dim"),
        )
        return Provider(kind, impl, impl)

    raise ProviderError(f"未知 provider.kind={kind}（支持 mock / ark / openai / openai-compatible）")


def embed_vectors(provider: Provider, texts: list[str]) -> np.ndarray:
    """统一入口：返回 L2 归一化的 float32 矩阵。"""
    if not texts:
        return np.zeros((0, provider.dim or 0), dtype=np.float32)
    return provider.embedder.embed(texts)
