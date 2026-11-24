"""Office parser smoke tests for PPTX/XLSX ingestion."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from kb.parsers import parse_pptx, parse_xlsx, supported_suffixes


class OfficeParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-parse-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pptx_extracts_slide_text_and_tables(self):
        from pptx import Presentation
        from pptx.util import Inches

        path = self.tmp / "deck.pptx"
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = "季度规划"
        table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
        table.cell(0, 0).text = "目标"
        table.cell(0, 1).text = "负责人"
        table.cell(1, 0).text = "增长"
        table.cell(1, 1).text = "Alice"
        prs.save(path)
        parsed = parse_pptx(path)
        self.assertIn("季度规划", parsed.text)
        self.assertIn("Alice", parsed.text)
        self.assertEqual(parsed.doc_type, "slides")

    def test_xlsx_extracts_sheet_rows(self):
        from openpyxl import Workbook

        path = self.tmp / "report.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "汇总"
        ws.append(["项目", "数量"])
        ws.append(["北极星", 3])
        wb.save(path)
        parsed = parse_xlsx(path)
        self.assertIn("汇总", parsed.text)
        self.assertIn("北极星", parsed.text)
        self.assertIn("3", parsed.text)
        self.assertEqual(parsed.doc_type, "sheet")

    def test_supported_suffixes_include_office(self):
        suffixes = supported_suffixes()
        self.assertIn(".pptx", suffixes)
        self.assertIn(".xlsx", suffixes)
        self.assertIn(".xls", suffixes)


if __name__ == "__main__":
    unittest.main()
