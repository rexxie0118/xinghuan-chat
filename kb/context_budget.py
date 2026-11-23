"""Token-aware context budgeting without a model-specific tokenizer.

The estimators are intentionally conservative: CJK characters count roughly
one token each and ASCII text uses ~3.5 chars/token. Exact tokenizers differ by
provider, but the budget remains useful and deterministic for prompt packing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_TRUNC_MARK = "\n…（上下文已压缩）"


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = _CJK_RE.sub(" ", text)
    ascii_chars = sum(1 for ch in rest if ch.isascii() and not ch.isspace())
    other_chars = sum(1 for ch in rest if not ch.isascii() and not ch.isspace())
    return max(1, int(math.ceil(cjk * 1.0 + ascii_chars / 3.5 + other_chars / 2.0)))


def estimate_messages(messages: Iterable[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += estimate_tokens(str(message.get("content") or ""))
        total += 8  # role / tool_calls / protocol overhead
    return total


def truncate_to_tokens(text: str, max_tokens: int, *, keep_tail: bool = False) -> tuple[str, bool]:
    if max_tokens <= 0:
        return "", bool(text)
    if estimate_tokens(text) <= max_tokens:
        return text, False
    marker_tokens = estimate_tokens(_TRUNC_MARK)
    target = max(1, max_tokens - marker_tokens)
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[-mid:] if keep_tail else text[:mid]
        if estimate_tokens(candidate) <= target:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    if not best:
        return _TRUNC_MARK.strip(), True
    return (best + _TRUNC_MARK), True


@dataclass
class BudgetReport:
    max_tokens: int
    reserve_tokens: int
    input_tokens: int
    used_tokens: int = 0
    truncated: bool = False
    dropped_sections: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "reserve_tokens": self.reserve_tokens,
            "input_tokens": self.input_tokens,
            "used_tokens": self.used_tokens,
            "truncated": self.truncated,
            "dropped_sections": self.dropped_sections,
        }


@dataclass
class Section:
    label: str
    text: str
    priority: int = 0
    required: bool = False


class ContextBudget:
    def __init__(self, max_tokens: int, *, reserve_tokens: int = 0) -> None:
        self.max_tokens = max(1, int(max_tokens))
        self.reserve_tokens = max(0, int(reserve_tokens))
        self.input_tokens = max(1, self.max_tokens - self.reserve_tokens)

    def fit_text(self, text: str, *, keep_tail: bool = False) -> tuple[str, BudgetReport]:
        report = BudgetReport(
            max_tokens=self.max_tokens,
            reserve_tokens=self.reserve_tokens,
            input_tokens=self.input_tokens,
        )
        trimmed, truncated = truncate_to_tokens(text, self.input_tokens, keep_tail=keep_tail)
        report.used_tokens = estimate_tokens(trimmed)
        report.truncated = truncated
        return trimmed, report

    def fit_sections(
        self, sections: list[Section], *, separator: str = "\n\n"
    ) -> tuple[str, BudgetReport]:
        """Fit sections by priority, preserving their original output order."""
        report = BudgetReport(
            max_tokens=self.max_tokens,
            reserve_tokens=self.reserve_tokens,
            input_tokens=self.input_tokens,
        )
        remaining = self.input_tokens
        selected: dict[int, str] = {}
        for index, section in sorted(
            enumerate(sections), key=lambda item: (item[1].priority, item[0])
        ):
            if remaining <= 0:
                report.dropped_sections.append(section.label)
                continue
            text, truncated = truncate_to_tokens(section.text, remaining)
            if not text:
                report.dropped_sections.append(section.label)
                continue
            selected[index] = text
            remaining -= estimate_tokens(text)
            if truncated:
                report.truncated = True
            if section.required and not text:
                report.dropped_sections.append(section.label)
        ordered = [selected[i] for i in sorted(selected)]
        output = separator.join(ordered)
        report.used_tokens = estimate_tokens(output)
        return output, report

    def fit_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], BudgetReport]:
        """Shrink context messages without breaking tool_call/tool pairing.

        System and the newest user message are preserved. Old tool outputs are
        shortened first, then older history text. We do not drop messages here;
        the caller can still see the exact conversation shape.
        """
        report = BudgetReport(
            max_tokens=self.max_tokens,
            reserve_tokens=self.reserve_tokens,
            input_tokens=self.input_tokens,
        )
        total = estimate_messages(messages)
        if total <= self.input_tokens:
            report.used_tokens = total
            return messages, report

        result = [dict(message) for message in messages]
        # Preserve system + last user message.
        protected = {0} if result and result[0].get("role") == "system" else set()
        last_user = -1
        for i, message in enumerate(result):
            if message.get("role") == "user":
                last_user = i
        if last_user >= 0:
            protected.add(last_user)

        # Shrink old tool outputs first, oldest first.
        for i, message in enumerate(result):
            if total <= self.input_tokens:
                break
            if i in protected or message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            target = 240 if estimate_tokens(content) > 240 else estimate_tokens(content)
            shrunk, did = truncate_to_tokens(content, target)
            if did:
                total -= estimate_tokens(content) - estimate_tokens(shrunk)
                result[i]["content"] = shrunk
                report.truncated = True

        # Then shorten older history/assistant content, but never tool protocol.
        for i, message in enumerate(result):
            if total <= self.input_tokens:
                break
            if i in protected or message.get("role") in {"tool", "system"}:
                continue
            content = str(message.get("content") or "")
            if estimate_tokens(content) <= 120:
                continue
            shrunk, did = truncate_to_tokens(content, 120)
            if did:
                total -= estimate_tokens(content) - estimate_tokens(shrunk)
                result[i]["content"] = shrunk
                report.truncated = True

        report.used_tokens = estimate_messages(result)
        if report.used_tokens > self.input_tokens:
            report.dropped_sections.append("messages_over_budget")
        return result, report
