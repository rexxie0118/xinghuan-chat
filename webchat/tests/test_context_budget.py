"""Tests for token estimation and context packing."""
from __future__ import annotations

import unittest

from kb.context_budget import (
    ContextBudget,
    Section,
    estimate_tokens,
    truncate_to_tokens,
)


class ContextBudgetTest(unittest.TestCase):
    def test_token_estimate_and_truncation(self):
        self.assertGreater(estimate_tokens("你好，世界"), 0)
        self.assertLess(estimate_tokens("hello world"), len("hello world"))
        text, truncated = truncate_to_tokens("这是一个很长的上下文。" * 100, 20)
        self.assertTrue(truncated)
        self.assertLessEqual(estimate_tokens(text), 20)
        self.assertIn("上下文已压缩", text)

    def test_sections_prioritize_required_question_and_evidence(self):
        budget = ContextBudget(35, reserve_tokens=0)
        output, report = budget.fit_sections(
            [
                Section("graph", "相关实体：" + "实体，" * 80, priority=3),
                Section("evidence", "证据：" + "证据内容。" * 80, priority=1),
                Section("question", "问题：测试", priority=0, required=True),
                Section("history", "历史：" + "历史内容。" * 80, priority=2),
            ]
        )
        self.assertIn("问题：测试", output)
        self.assertIn("证据：", output)
        self.assertGreater(report.used_tokens, 0)
        self.assertTrue(report.truncated or report.dropped_sections)

    def test_messages_keep_system_and_latest_user(self):
        budget = ContextBudget(80, reserve_tokens=0)
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old " * 100},
            {"role": "assistant", "content": "old answer " * 100},
            {"role": "tool", "content": "tool output " * 200, "tool_call_id": "x"},
            {"role": "user", "content": "latest question"},
        ]
        result, report = budget.fit_messages(messages)
        self.assertEqual(result[0]["content"], "system")
        self.assertEqual(result[-1]["content"], "latest question")
        self.assertTrue(report.truncated)


if __name__ == "__main__":
    unittest.main()
