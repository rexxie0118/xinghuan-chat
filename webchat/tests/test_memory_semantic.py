"""Tests for the optional LLM semantic duplicate judge."""
from __future__ import annotations

import unittest

from webchat.memory_semantic import LLMSemanticJudge


class FakeChat:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def complete(self, system, prompt, max_tokens=80):
        self.calls += 1
        return self.answer


class SemanticJudgeTest(unittest.TestCase):
    def test_parses_json_yes(self):
        chat = FakeChat('{"same": true, "reason": "同义"}')
        judge = LLMSemanticJudge(chat)
        self.assertTrue(judge.same("回答简洁", "尽量简短", "preference"))
        self.assertEqual(chat.calls, 1)

    def test_parses_no(self):
        judge = LLMSemanticJudge(FakeChat('{"same": false, "reason": "不同"}'))
        self.assertFalse(judge.same("喜欢蓝色", "喜欢红色", "preference"))


if __name__ == "__main__":
    unittest.main()
