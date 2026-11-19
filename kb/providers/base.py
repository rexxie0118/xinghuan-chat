"""Provider 抽象：Embedding 与 Chat 两类能力。

上层只依赖这里的 Protocol，因此替换后端（mock / ARK / OpenAI / 本地服务）
不需要改任何检索、本体或记忆代码。
"""

from __future__ import annotations

from typing import Protocol, Sequence

import numpy as np


class ProviderError(RuntimeError):
    """LLM / Embedding 调用失败。"""


class EmbeddingProvider(Protocol):
    model: str

    @property
    def dim(self) -> int | None: ...

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """返回 (n, dim) 的 float32 矩阵，已 L2 归一化。"""
        ...


class ChatProvider(Protocol):
    model: str

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 1200,
    ) -> str: ...


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
