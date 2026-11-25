"""Policy documents: newest effective month should win over old versions/FAQ."""
from __future__ import annotations

import unittest

from kb.retrieval import ChunkHit, doc_family, effective_date, prefer_latest_policies


def hit(chunk_id: str, doc_id: str, title: str, text: str, date: str = "") -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_title=title,
        heading_path="",
        text=text,
        effective_date=date,
        doc_family=doc_family(title),
    )


class PolicyPriorityTest(unittest.TestCase):
    def setUp(self):
        self.old = hit("c-old", "d-old", "年假制度", "2025 年 12月 级别4 10天 补充3天", "2025-12")
        self.new = hit("c-new", "d-new", "年假制度_2026", "2026 年 10月 级别4 10天 补充5天", "2026-10")
        self.faq = hit("c-faq", "d-faq", "假期与考勤", "正式员工每年有 10 天带薪年假，病假需要医院证明")

    def test_extracts_effective_month(self):
        self.assertEqual(effective_date("年假制度_2026", "2026 年 10月"), "2026-10")
        self.assertEqual(doc_family("年假制度_2026"), doc_family("年假制度"))

    def test_current_policy_prefers_newest(self):
        out = prefer_latest_policies("年假有几天", [self.faq, self.old, self.new])
        self.assertEqual({h.doc_id for h in out}, {"d-new"})

    def test_history_query_keeps_versions(self):
        out = prefer_latest_policies("2025年年假制度", [self.faq, self.old, self.new])
        self.assertEqual({h.doc_id for h in out}, {"d-faq", "d-old", "d-new"})

    def test_other_topic_keeps_faq(self):
        out = prefer_latest_policies("年假和病假", [self.faq, self.old, self.new])
        self.assertIn("d-faq", {h.doc_id for h in out})


if __name__ == "__main__":
    unittest.main()
