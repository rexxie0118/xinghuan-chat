"""Policy documents: newest effective month should win over old versions/FAQ."""
from __future__ import annotations

import unittest

from kb.retrieval import (
    ChunkHit,
    doc_family,
    document_modified_at,
    effective_date,
    prefer_latest_policies,
)


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

    def test_general_document_versions_use_role_suffix(self):
        old = hit("c-v1", "d-v1", "合同_v1", "旧合同", "2026-01")
        new = hit("c-v2", "d-v2", "合同_v2", "新合同", "2026-06")
        out = prefer_latest_policies("合同条款", [old, new])
        self.assertEqual({h.doc_id for h in out}, {"d-v2"})

    def test_undated_general_versions_use_mtime(self):
        old = hit("c-old", "d-old", "会议纪要", "旧纪要")
        new = hit("c-new", "d-new", "会议纪要_2026-03", "新纪要")
        old.modified_at = "2026-01-10"
        new.modified_at = "2026-03-20"
        out = prefer_latest_policies("会议纪要", [old, new])
        self.assertEqual({h.doc_id for h in out}, {"d-new"})

    def test_modified_at_from_meta(self):
        self.assertEqual(
            document_modified_at('{"source_mtime":"2026-11-25T13:00:00+00:00"}'),
            "2026-11-25",
        )

    def test_file_mtime_tiebreaks_same_effective_month(self):
        old = hit("c-old", "d-old", "年假制度", "旧", "2026-10")
        new = hit("c-new", "d-new", "年假制度_2026", "新", "2026-10")
        old.modified_at = "2026-10-01"
        new.modified_at = "2026-11-25"
        out = prefer_latest_policies("年假有几天", [old, new])
        self.assertEqual({h.doc_id for h in out}, {"d-new"})

    def test_other_topic_keeps_faq(self):
        out = prefer_latest_policies("年假和病假", [self.faq, self.old, self.new])
        self.assertIn("d-faq", {h.doc_id for h in out})


if __name__ == "__main__":
    unittest.main()
