"""OpenAI 兼容后端：一份实现覆盖火山方舟 ARK、OpenAI 及任何兼容网关。

只依赖标准库 urllib，避免为 MVP 引入 HTTP 客户端依赖。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

import numpy as np

from .base import ProviderError, l2_normalize


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        chat_model: str,
        embedding_model: str,
        timeout: float = 60.0,
        max_retries: int = 3,
        batch_size: int = 16,
        embedding_dim: int | None = None,
    ) -> None:
        if not api_key:
            raise ProviderError(
                "缺少 API Key：请设置环境变量 ARK_API_KEY（或配置 provider.api_key）"
            )
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.chat_model = chat_model
        self.model = embedding_model
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.batch_size = max(1, batch_size)
        self._dim = embedding_dim

    # ------------------------------------------------------------ 基础 HTTP

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001 - 读取失败不应掩盖原始错误
                    pass
                last_err = ProviderError(f"HTTP {exc.code} {url}: {body}")
                if exc.code in (408, 409, 429) or exc.code >= 500:
                    time.sleep(min(8.0, 0.8 * 2**attempt))
                    continue
                raise last_err from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_err = ProviderError(f"请求失败 {url}: {exc}")
                time.sleep(min(8.0, 0.8 * 2**attempt))
        raise last_err or ProviderError("请求失败且无错误信息")

    # ------------------------------------------------------------ Embedding

    @property
    def dim(self) -> int | None:
        return self._dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        texts = [t if t and t.strip() else " " for t in texts]
        if not texts:
            return np.zeros((0, self._dim or 0), dtype=np.float32)
        chunks: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            payload = {"model": self.model, "input": batch, "encoding_format": "float"}
            try:
                resp = self._post("/embeddings", payload)
            except ProviderError:
                payload.pop("encoding_format", None)
                resp = self._post("/embeddings", payload)
            data = resp.get("data") or []
            if len(data) != len(batch):
                raise ProviderError(
                    f"embedding 返回条数不匹配：期望 {len(batch)}，实际 {len(data)}"
                )
            data = sorted(data, key=lambda d: d.get("index", 0))
            vecs = np.array([d["embedding"] for d in data], dtype=np.float32)
            if self._dim is None:
                self._dim = int(vecs.shape[1])
            chunks.append(vecs)
        return l2_normalize(np.vstack(chunks))

    # ------------------------------------------------------------ Chat

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 1200,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = self._post("/chat/completions", payload)
        except ProviderError:
            # 部分模型/网关不支持 response_format：去掉后重试一次
            if not json_mode:
                raise
            payload.pop("response_format", None)
            resp = self._post("/chat/completions", payload)
        choices = resp.get("choices") or []
        if not choices:
            raise ProviderError(f"chat 返回为空：{json.dumps(resp)[:300]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 少数网关返回分段 content
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return (content or "").strip()

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 1200,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        """OpenAI-compatible function calling (DeepSeek/ARK/OpenAI)."""
        payload: dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        resp = self._post("/chat/completions", payload)
        choices = resp.get("choices") or []
        if not choices:
            raise ProviderError(f"chat 返回为空：{json.dumps(resp)[:300]}")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name")
            if not name:
                continue
            calls.append(
                {
                    "id": str(call.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(name),
                        "arguments": str(function.get("arguments") or "{}"),
                    },
                }
            )
        return {
            "content": (content or "").strip(),
            "tool_calls": calls,
            "finish_reason": choice.get("finish_reason") or "",
            "raw": message,
        }

    def health(self) -> tuple[bool, str]:
        try:
            vec = self.embed(["健康检查"])
            return True, f"embedding ok, dim={vec.shape[1]}"
        except Exception as exc:  # noqa: BLE001 - 健康检查需要兜住一切
            return False, f"{type(exc).__name__}: {exc}"
