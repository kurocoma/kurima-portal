"""レターパック宛名PDFの住所はみ出し対策（2026-08-13 認識合わせ・案A）のテスト。

ユーザー報告のスクリーンショット2件（沖縄県北谷町・北海道札幌市厚別区)は、
最小フォント8.25ptまで縮小しても幅上限を超えて印刷領域からはみ出していた。
承認された仕様:
- はみ出す宛名だけ住所1+住所2を結合し、2行に収まる最大フォントサイズで組み直す
- 折返し位置は番地と建物名の境界（カタカナ/英字の頭）を優先する
- 収まっている宛名の出力は変えない（非破壊）
- 幅上限は裁ち線(用紙中央297.6pt)に食い込まない200ptとする
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from reportlab.pdfbase import pdfmetrics

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.letterpack_pdf import (  # noqa: E402
    ADDRESS_FONT_SIZE,
    ADDRESS_MAX_WIDTH,
    ADDRESS_MIN_FONT_SIZE,
    ADDRESS_REBALANCE_MIN_FONT_SIZE,
    BODY_FONT,
    _address_lines,
    _register_fonts,
)


def _row(address1: str, address2: str, name: str = "テスト太郎") -> dict[str, str]:
    return {"住所1": address1, "住所2": address2, "宛名2（氏名）": name}


class LetterpackPdfAddressFitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _register_fonts()

    def assert_all_lines_fit(self, lines: list[tuple[str, float]]) -> None:
        for text, size in lines:
            self.assertLessEqual(
                pdfmetrics.stringWidth(text, BODY_FONT, size),
                ADDRESS_MAX_WIDTH,
                msg=f"行が幅上限を超過: {text} ({size}pt)",
            )

    def test_screenshot_case_chatan_fits_after_rebalance(self) -> None:
        # スクショ左上（沖縄県北谷町）: 旧実装では8.25ptでも幅214ptではみ出していた
        warnings: list[str] = []
        lines = _address_lines(
            _row("沖縄県中頭郡北谷町", "上勢頭713番地1ペアーズコート北谷上勢頭ヒルズ1401"),
            warnings,
        )
        self.assertEqual(len(lines), 2)
        self.assert_all_lines_fit(lines)
        # 認識合わせ: 読めるサイズを保つ（極端な縮小警告なしで収まる）
        for _, size in lines:
            self.assertGreaterEqual(size, ADDRESS_MIN_FONT_SIZE)
        self.assertEqual(warnings, [])
        # 認識合わせ: 折返しは建物名（カタカナの頭）を優先する
        self.assertTrue(lines[1][0].startswith("ペアーズコート"), msg=str(lines))
        # 住所の文字は欠落しない
        self.assertEqual(
            "".join(text for text, _ in lines),
            "沖縄県中頭郡北谷町上勢頭713番地1ペアーズコート北谷上勢頭ヒルズ1401",
        )

    def test_screenshot_case_atsubetsu_fits_after_rebalance(self) -> None:
        # スクショ右下（北海道札幌市厚別区）: 旧実装では8.25ptでも幅212.6ptではみ出していた
        warnings: list[str] = []
        lines = _address_lines(
            _row("北海道札幌市厚別区", "大谷地東7丁目6-1パークシティ大谷地Aコート404号室"),
            warnings,
        )
        self.assertEqual(len(lines), 2)
        self.assert_all_lines_fit(lines)
        for _, size in lines:
            self.assertGreaterEqual(size, ADDRESS_MIN_FONT_SIZE)
        self.assertEqual(warnings, [])
        self.assertTrue(lines[1][0].startswith("パークシティ"), msg=str(lines))
        self.assertEqual(
            "".join(text for text, _ in lines),
            "北海道札幌市厚別区大谷地東7丁目6-1パークシティ大谷地Aコート404号室",
        )

    def test_short_address_is_unchanged(self) -> None:
        # 認識合わせ: 収まっている宛名（スクショ右上・江別市）は従来どおり無加工・標準サイズ
        warnings: list[str] = []
        lines = _address_lines(_row("北海道江別市", "東光町38-23"), warnings)
        self.assertEqual(
            lines,
            [("北海道江別市", ADDRESS_FONT_SIZE), ("東光町38-23", ADDRESS_FONT_SIZE)],
        )
        self.assertEqual(warnings, [])

    def test_per_line_shrink_case_is_unchanged(self) -> None:
        # 認識合わせ: 行ごとの縮小(8.25pt以上)で収まる宛名は組み直さない（非破壊）
        warnings: list[str] = []
        long_but_fitting = "あ" * 24  # 8.25ptなら幅上限内、11.04ptでは超過する長さ
        lines = _address_lines(_row("北海道江別市", long_but_fitting), warnings)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], ("北海道江別市", ADDRESS_FONT_SIZE))
        self.assertEqual(lines[1][0], long_but_fitting)
        self.assertGreaterEqual(lines[1][1], ADDRESS_MIN_FONT_SIZE)
        self.assertLess(lines[1][1], ADDRESS_FONT_SIZE)
        self.assert_all_lines_fit(lines)
        self.assertEqual(warnings, [])

    def test_single_long_line_is_rebalanced(self) -> None:
        # 住所1のみで長い場合も2行へ組み直して収める
        warnings: list[str] = []
        lines = _address_lines(
            _row("沖縄県中頭郡北谷町上勢頭713番地1ペアーズコート北谷上勢頭ヒルズ1401", ""),
            warnings,
        )
        self.assertEqual(len(lines), 2)
        self.assert_all_lines_fit(lines)
        self.assertEqual(warnings, [])

    def test_extremely_long_address_warns_and_uses_min_size(self) -> None:
        # 2行組み直しでも収まらない住所は、警告を残して最小サイズまで縮小する
        warnings: list[str] = []
        lines = _address_lines(_row("東京都", "あ" * 90, name="超長住所"), warnings)
        self.assertEqual(len(lines), 2)
        for _, size in lines:
            self.assertEqual(size, ADDRESS_REBALANCE_MIN_FONT_SIZE)
        self.assertTrue(any("超長住所" in warning for warning in warnings), msg=str(warnings))

    def test_empty_address_returns_no_lines(self) -> None:
        warnings: list[str] = []
        self.assertEqual(_address_lines(_row("", ""), warnings), [])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
