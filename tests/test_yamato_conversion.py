"""変換ロジックの単体テスト（S7）。

配布（update.bat = git pull）で全PCへ即波及する「業務の心臓部」の純ロジックを対象にする:
- yamato_conversion: 住所補正（「様方」「方」の扱い・A1/A-1 形式・1桁建物名の結合・
  正規化・幅超過や CP932 不可文字の要確認判定）と電話番号のB2向け補正
- paths.latest_order_csv: 受注明細CSV（data*）の最新選択規則
- clickpost: 申込CSV整形の純関数（住所4分割・郵便番号整形・内容品文字列・明細行1の選択）

ファイルシステムを使うのは latest_order_csv のみ（一時フォルダ）。ブラウザ・実データには
一切触れない。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.clickpost import (  # noqa: E402
    ContentRule,
    _content_for_clickpost_line,
    _first_detail_line,
    _format_zip,
    _split_clickpost_address_power_query,
)
from portal_app.services.paths import latest_order_csv  # noqa: E402
from portal_app.services.yamato_conversion import (  # noqa: E402
    looks_like_building,
    looks_like_care_of,
    normalize_b2_phone_number,
    split_b2_address,
)


class SplitB2AddressTest(unittest.TestCase):
    """住所補正（split_b2_address）: B2の住所欄・建物名欄への振り分け規則。"""

    def test_care_of_suffix_moves_to_building(self):
        # 「様方」で終わる末尾は建物名欄へ移す（空白区切り）
        address, building, reasons, requires_review = split_b2_address(
            "沖縄県那覇市首里1-2-3 田中様方", ""
        )
        self.assertEqual(address, "沖縄県那覇市首里1-2-3")
        self.assertEqual(building, "田中様方")
        self.assertIn("住所内の空白以降を建物名欄へ分割", reasons)
        self.assertFalse(requires_review)

    def test_single_kata_moves_to_building(self):
        # 「方」で終わる末尾（様方でない care-of）も建物名欄へ移す
        address, building, _, _ = split_b2_address("那覇市おもろまち1-1 山田方", "")
        self.assertEqual(address, "那覇市おもろまち1-1")
        self.assertEqual(building, "山田方")

    def test_care_of_exclusions(self):
        # 「地方」「平方」「方向」で終わる語は care-of とみなさない
        self.assertTrue(looks_like_care_of("山田方"))
        self.assertTrue(looks_like_care_of("田中様方"))
        self.assertFalse(looks_like_care_of("阿蘇地方"))
        self.assertFalse(looks_like_care_of("2平方"))
        self.assertFalse(looks_like_care_of("北方向"))

    def test_room_like_a1_and_a_hyphen_1(self):
        # A1 / A-1 形式は建物名（部屋番号）らしい末尾として扱う
        self.assertTrue(looks_like_building("A1"))
        self.assertTrue(looks_like_building("A-1"))
        self.assertTrue(looks_like_building("101号室"))
        address, building, _, _ = split_b2_address("那覇市金城2-3 A-1", "")
        self.assertEqual(address, "那覇市金城2-3")
        self.assertEqual(building, "A-1")

    def test_single_digit_building_joins_address(self):
        # 1桁数字だけの建物名欄は住所末尾へ戻す（NE側の誤分割の復元）
        address, building, reasons, _ = split_b2_address("那覇市金城2-3", "5")
        self.assertEqual(address, "那覇市金城2-35")
        self.assertEqual(building, "")
        self.assertIn("1桁の建物名欄を住所末尾に結合", reasons)

    def test_normalizes_fullwidth_digits_and_hyphens(self):
        # 全角数字・全角ハイフンは半角へ正規化する
        address, building, reasons, _ = split_b2_address("那覇市おもろまち１−２−３", "")
        self.assertEqual(address, "那覇市おもろまち1-2-3")
        self.assertEqual(building, "")
        self.assertIn("数字・英字・ハイフン・空白をB2向けに正規化", reasons)

    def test_existing_building_keeps_split(self):
        # 建物名欄がすでにある行は、住所の建物名らしい末尾を建物名欄の先頭へ寄せる
        address, building, _, _ = split_b2_address("那覇市壺屋3-4コーポ栄", "202")
        self.assertEqual(address, "那覇市壺屋3-4")
        self.assertEqual(building, "コーポ栄202")

    def test_overwidth_address_requires_review(self):
        # B2上限（住所64幅）を超える行は要確認になる
        long_address = "沖縄県那覇市" + "あ" * 40
        _, _, reasons, requires_review = split_b2_address(long_address, "")
        self.assertTrue(requires_review)
        self.assertTrue(any("超過" in reason for reason in reasons))

    def test_environment_dependent_char_requires_review(self):
        _, _, reasons, requires_review = split_b2_address("那覇市①-2-3", "")
        self.assertTrue(requires_review)
        self.assertIn("環境依存文字の可能性", reasons)

    def test_cp932_unencodable_requires_review(self):
        _, _, reasons, requires_review = split_b2_address("那覇市€1-2-3", "")
        self.assertTrue(requires_review)
        self.assertIn("CP932で出力できない文字を含む", reasons)

    def test_empty_address_requires_review(self):
        _, _, reasons, requires_review = split_b2_address("", "")
        self.assertTrue(requires_review)
        self.assertIn("届け先住所が空欄", reasons)


class NormalizeB2PhoneNumberTest(unittest.TestCase):
    """電話番号のB2向け補正（国際表記→国内表記・全角→半角・記号除去）。"""

    def test_plus81_becomes_domestic(self):
        self.assertEqual(normalize_b2_phone_number("+81-90-1234-5678"), "09012345678")

    def test_0081_becomes_domestic(self):
        self.assertEqual(normalize_b2_phone_number("00819012345678"), "09012345678")

    def test_fullwidth_digits(self):
        self.assertEqual(normalize_b2_phone_number("０９０-１２３４-５６７８"), "09012345678")

    def test_empty_stays_empty(self):
        self.assertEqual(normalize_b2_phone_number(""), "")


class LatestOrderCsvTest(unittest.TestCase):
    """paths.latest_order_csv: 「data で始まるファイルのうち更新日時が最新」を選ぶ。"""

    def test_picks_newest_data_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = directory / "data240701.csv"
            new = directory / "data240707.csv"
            other = directory / "other240708.csv"  # data 始まりでないため対象外
            for path in (old, new, other):
                path.write_text("x", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 7200, now - 7200))
            os.utime(new, (now - 60, now - 60))
            os.utime(other, (now, now))  # 最新でも data 始まりでなければ選ばれない
            self.assertEqual(latest_order_csv(directory), new)

    def test_prefix_match_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            upper = directory / "DATA240707.csv"
            upper.write_text("x", encoding="utf-8")
            self.assertEqual(latest_order_csv(directory), upper)

    def test_raises_when_no_data_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "note.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                latest_order_csv(directory)


class ClickpostCsvFormattingTest(unittest.TestCase):
    """クリックポスト申込CSV整形の純関数。"""

    def test_address_split_with_spaces(self):
        # 空白3区切り以上: 1+2番目を結合、3番目・4番目以降を2/3行目へ
        self.assertEqual(
            _split_clickpost_address_power_query("沖縄県那覇市 首里1-2-3 コーポ栄 101"),
            ("沖縄県那覇市首里1-2-3", "コーポ栄", "101", ""),
        )

    def test_address_split_compact_building_keyword(self):
        # 空白が無い住所は建物キーワードの位置で分割する
        self.assertEqual(
            _split_clickpost_address_power_query("沖縄県那覇市首里1-2-3コーポ栄101"),
            ("沖縄県那覇市首里1-2-3", "コーポ栄101", "", ""),
        )

    def test_address_split_compact_long_without_keyword(self):
        # 建物キーワードが無く20文字を超える場合は20文字で機械分割する
        text = "あ" * 25
        first, second, third, fourth = _split_clickpost_address_power_query(text)
        self.assertEqual(first, "あ" * 20)
        self.assertEqual(second, "あ" * 5)
        self.assertEqual((third, fourth), ("", ""))

    def test_format_zip(self):
        self.assertEqual(_format_zip("9040013"), "904-0013")
        self.assertEqual(_format_zip("９０４−００１３"), "904-0013")  # 全角もNFKCで半角化
        self.assertEqual(_format_zip("904-0013"), "904-0013")
        self.assertEqual(_format_zip("12345"), "12345")  # 7桁でなければそのまま

    def test_content_for_clickpost_line(self):
        rules = {"m-1": ContentRule(prefix="もずく(", default_quantity=None)}
        warnings: list[str] = []
        text = _content_for_clickpost_line(
            "68300", {"商品ｺｰﾄﾞ": "M-1", "受注数": "2"}, rules, warnings
        )
        self.assertEqual(text, "もずく(2)")
        self.assertEqual(warnings, [])

    def test_content_for_unknown_code_warns(self):
        warnings: list[str] = []
        text = _content_for_clickpost_line(
            "68301", {"商品ｺｰﾄﾞ": "X-9", "受注数": "3"}, {}, warnings
        )
        self.assertEqual(text, "3)")
        self.assertEqual(len(warnings), 1)
        self.assertIn("X-9".lower(), warnings[0].lower())

    def test_first_detail_line_picks_line_one(self):
        warnings: list[str] = []
        rows = [
            {"明細行": "2", "商品ｺｰﾄﾞ": "B"},
            {"明細行": "1", "商品ｺｰﾄﾞ": "A"},
        ]
        line = _first_detail_line("68302", rows, warnings)
        self.assertIsNotNone(line)
        self.assertEqual(line["商品ｺｰﾄﾞ"], "A")
        self.assertEqual(warnings, [])

    def test_first_detail_line_missing_rows_warns(self):
        warnings: list[str] = []
        self.assertIsNone(_first_detail_line("68303", [], warnings))
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
