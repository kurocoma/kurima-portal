"""NE納品書PDFのページ順解析（ne_invoice_pdf）のテスト。

reportlab で合成した納品書相当PDFに対して、
- ページ順どおりに既知の伝票番号が返ること
- 番号のないページ（明細続き）は無視されること
- 既知番号が複数載る曖昧ページは無視されること
- 受注番号断片・桁続きの数字（例: 16967700）を誤検知しないこと
を検証する。実PDF（NE出力）は cp932 CMap を含むため pdfminer.six を使う。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reportlab.pdfgen import canvas

from portal_app.services.ne_invoice_pdf import extract_invoice_denpyo_order


def _build_pdf(path: Path, pages: list[list[str]]) -> None:
    pdf = canvas.Canvas(str(path))
    for index, lines in enumerate(pages):
        if index:
            pdf.showPage()
        y = 800
        for line in lines:
            pdf.drawString(72, y, line)
            y -= 20
    pdf.save()


class ExtractInvoiceDenpyoOrderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf_path = Path(self._tmp.name) / "invoice.pdf"

    def test_page_order_with_continuation_and_ambiguous_pages(self):
        _build_pdf(
            self.pdf_path,
            [
                ["Invoice", "69707", "235545-20260723-0988441950"],
                ["69677", "some item line"],
                ["continuation page without number"],
                ["ambiguous page", "69423", "69720"],
                ["69720 only here"],
            ],
        )
        known = ["69423", "69677", "69707", "69720"]
        self.assertEqual(
            extract_invoice_denpyo_order(self.pdf_path, known),
            ["69707", "69677", "69720"],
        )

    def test_digit_adjacent_numbers_are_not_matched(self):
        """16967700 の内側の 69677 を伝票番号と誤認しない。"""
        _build_pdf(self.pdf_path, [["16967700"], ["69677"]])
        self.assertEqual(
            extract_invoice_denpyo_order(self.pdf_path, ["69677"]),
            ["69677"],
        )

    def test_empty_known_numbers_returns_empty(self):
        _build_pdf(self.pdf_path, [["69707"]])
        self.assertEqual(extract_invoice_denpyo_order(self.pdf_path, []), [])

    def test_duplicate_appearance_keeps_first_position(self):
        _build_pdf(self.pdf_path, [["69707"], ["69677"], ["69707"]])
        self.assertEqual(
            extract_invoice_denpyo_order(self.pdf_path, ["69677", "69707"]),
            ["69707", "69677"],
        )


class SortCandidatesByInvoiceOrderTest(unittest.TestCase):
    """clickpost._sort_candidates_by_invoice_order の並び替え・フォールバックの検証。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf_path = Path(self._tmp.name) / "invoice.pdf"

    def _rows(self, numbers: list[str]) -> list[dict[str, str]]:
        return [{"伝票番号": number} for number in numbers]

    def test_candidates_follow_invoice_page_order(self):
        from portal_app.services.clickpost import _sort_candidates_by_invoice_order

        _build_pdf(self.pdf_path, [["69707"], ["69677"], ["69423"]])
        candidates = self._rows(["69423", "69677", "69707"])
        warnings: list[str] = []
        _sort_candidates_by_invoice_order(candidates, self.pdf_path, None, warnings)
        self.assertEqual(
            [row["伝票番号"] for row in candidates], ["69707", "69677", "69423"]
        )
        self.assertEqual(warnings, [])

    def test_unmatched_candidates_fall_back_to_tail_with_warning(self):
        from portal_app.services.clickpost import _sort_candidates_by_invoice_order

        _build_pdf(self.pdf_path, [["69707"]])
        candidates = self._rows(["69423", "69677", "69707"])
        warnings: list[str] = []
        _sort_candidates_by_invoice_order(candidates, self.pdf_path, None, warnings)
        self.assertEqual(
            [row["伝票番号"] for row in candidates], ["69707", "69423", "69677"]
        )
        self.assertTrue(any("末尾に回しました" in warning for warning in warnings))

    def test_missing_pdf_keeps_order_with_warning(self):
        from portal_app.services.clickpost import _sort_candidates_by_invoice_order

        candidates = self._rows(["69423", "69677"])
        warnings: list[str] = []
        _sort_candidates_by_invoice_order(
            candidates, Path(self._tmp.name) / "missing.pdf", None, warnings
        )
        self.assertEqual([row["伝票番号"] for row in candidates], ["69423", "69677"])
        self.assertTrue(any("納品書PDF" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
