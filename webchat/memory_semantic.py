"""Optional LLM adjudication for semantically equivalent user memories.

Lexical similarity is cheap and deterministic, but misses paraphrases such as
"回答简洁" vs "尽量简短". This module asks the configured chat provider only
for borderline, same-kind candidates; the caller keeps the lexical fast path.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

LOG = logging.getLogger("webchat.memory_semantic")

_SYSTEM = """你是记忆去重判定器。判断两条用户长期记忆是否表达同一个可合并的偏好、事实或任务。
只判断语义，不要因为措辞不同就判为不同；如果一条更具体、一条是上位概念，也视为可合并。
只输出 JSON：{"same": true|false, "reason": "不超过20字"}"""


class LLMSemanticJudge:
    def __init__(self, chat: Any) -> None:
        self.chat = chat
        self.calls = 0

    def same(self, left: str, right: str, kind: str = "fact") -> bool:
        if not left.strip() or not right.strip():
            return False
        self.calls += 1
        prompt = (
            f"类型：{kind}\n"
            f"记忆 A：{left[:300]}\n"
            f"记忆 B：{right[:300]}\n"
            "是否同一语义？"
        )
        try:
            raw = self.chat.complete(_SYSTEM, prompt, max_tokens=80) or ""
        except Exception as exc:  # noqa: BLE001
            LOG.warning("semantic memory judge failed: %s", exc)
            return False
        text = str(raw).strip()
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(text[start:end + 1])
                return bool(data.get("same"))
        except (ValueError, TypeError):
            pass
        return bool(re.search(r"\b(yes|true|是|相同|可合并)\b", text, re.I))
