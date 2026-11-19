"""离线 mock 后端：特征哈希向量 + 占位 Chat。

存在的意义：没有 API Key 也能完整跑通摄取 → 检索 → 引用 的全链路，
方便验证数据流与结构，而不是为了“假装有语义能力”。
它的相似度本质是词法重叠（中文二元组），所以对同义改写不敏感，
这恰恰是真实 embedding 才能补上的部分（见 DESIGN.md 的评测章节）。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from ..textutil import embed_tokens
from ..util import hash_bucket
from .base import l2_normalize


class MockEmbedder:
    def __init__(self, dim: int = 256) -> None:
        self._dim = dim
        self.model = f"mock-hash-{dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            vec = np.zeros(self._dim, dtype=np.float32)
            toks = embed_tokens(text)
            if not toks:
                vec[0] = 1.0
            for tok, weight in toks:
                bucket, sign = hash_bucket(tok, self._dim)
                vec[bucket] += sign * weight
            out[i] = vec
        return l2_normalize(out)


class MockChat:
    """不产生幻觉：不调用模型时明确返回空串，由上层决定降级策略。"""

    model = "mock-chat"

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 1200,
    ) -> str:
        return ""
