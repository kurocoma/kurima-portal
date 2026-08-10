"""クリックポスト申込CSVの住所4分割の単体テスト。

背景（2026-08-10 の実障害）:
クリックポストは住所を4行で受け取るが、1行あたり全角20文字（半角換算40幅）を
超える行があるとその明細を取り込まない。取込件数がCSV件数に足りず
「ClickPostインポート後の支払い対象件数がCSV件数と一致しません」で停止した。

ここでは分割の純関数だけを対象にし、
- どの分割経路を通っても4行すべてが上限幅（40）以内に収まること
- 分割で文字が欠落・重複・入れ替わらないこと（空白除去した元住所を復元できること）
- 4行に収まらない住所が黙って切り捨てられず、呼び出し側へ残りが返ること
- これまで正常に取り込めていた住所の分割結果が変わっていないこと
を検証する。実データ（実際に落ちた4件・実際に通った5件）を回帰ケースとして持つ。
"""

from __future__ import annotations

import re
import sys
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.clickpost import (  # noqa: E402
    CLICKPOST_ADDRESS_LINE_COUNT,
    CLICKPOST_ADDRESS_LINE_WIDTH,
    ContentRule,
    _clickpost_address_lines,
    _clickpost_text_width,
    _convert_clickpost_csv,
    _split_clickpost_address_power_query,
)

# 2026-08-10 / 08-05 / 07-29 に実際に取込から漏れた住所と、修正後の期待分割。
# 旧実装の住所1行目は 43〜47 幅あり、クリックポスト側で弾かれていた。
REAL_FAILURES = (
    (
        "2026-08-10 伝票70437 堀江弘二",
        "東京都新宿区白銀町6-1 神楽坂トワイシアヒルサイドレジデンス421",
        ("東京都新宿区白銀町6-1", "神楽坂トワイシアヒルサイドレジデンス421", "", ""),
    ),
    (
        "2026-08-10 伝票70397 小野香織",
        "福岡県北九州市八幡西区折尾四丁目28-14 103号室 Conforto Y 折尾",
        ("福岡県北九州市八幡西区折尾四丁目28-14", "103号室 Conforto Y 折尾", "", ""),
    ),
    (
        "2026-07-29 伝票69951 前田日和",
        "大阪府大阪市西区新町4-8-8 アーバネックス西長堀 805",
        ("大阪府大阪市西区新町4-8-8", "アーバネックス西長堀 805", "", ""),
    ),
    (
        "2026-08-05 伝票70212 小林亘",
        "大阪府大阪市浪速区塩草3-7-5 ファステート難波グランプリ1508",
        ("大阪府大阪市浪速区塩草3-7-5", "ファステート難波グランプリ1508", "", ""),
    ),
)

# 実際にクリックポストが取り込めていた住所。
# 2026-08-11 の仕様変更（意味の区切りで分ける）で、上限は満たしたまま分割位置が変わった。
# 語の途中で割れていたもの（…村上/寮201、…1-2/0）が解消されている。
PREVIOUSLY_ACCEPTED = (
    (
        "40幅ちょうどで通った実績（建物名を割らない）",
        "愛知県名古屋市名東区梅森坂3-5119 クレールメゾンEast102",
        ("愛知県名古屋市名東区梅森坂3-5119", "クレールメゾンEast102", "", ""),
    ),
    (
        "丁目と方角を挟む番地を割らない",
        "北海道札幌市白石区南郷通17丁目南1-20 ",
        ("北海道札幌市白石区南郷通17丁目南1-20", "", "", ""),
    ),
    (
        "分割不要",
        "奈良県奈良市中町2099-10 ",
        ("奈良県奈良市中町2099-10", "", "", ""),
    ),
    (
        "建物名「村上寮」を割らない",
        "新潟県村上市二之町2-45 村上寮201",
        ("新潟県村上市二之町2-45 村上寮201", "", "", ""),
    ),
    (
        "空白2区切りだが短い",
        "広島県広島市安佐南区 緑井4-3-17",
        ("広島県広島市安佐南区 緑井4-3-17", "", "", ""),
    ),
)

# 上限・情報保存を横断的に確認するための入力一式（異常系・境界値を含む）。
ALL_INPUTS = (
    [address for _, address, _ in REAL_FAILURES]
    + [address for _, address, _ in PREVIOUSLY_ACCEPTED]
    + [
        "",
        "   ",
        "　",  # 全角空白のみ（NFKCで半角空白になる）
        "沖縄県那覇市 首里1-2-3 コーポ栄 101",
        "沖縄県那覇市首里1-2-3コーポ栄101",
        "あ" * 20,  # ちょうど40幅
        "あ" * 20 + "A",  # 41幅
        "あ" * 25,
        "あ" * 100,  # 4行(160幅)に収まらない
        "東京都" + "a" * 60,  # 半角のみの長い建物名
        "京都府京都市中京区 " + "サンシャインコーポラス" * 4,
    ]
)


def _compact(address: str) -> str:
    """空白を除いた正規化済み住所（分割で失われてはいけない情報）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", address).strip())


class ClickPostTextWidthTest(unittest.TestCase):
    """幅の数え方（全角=2・半角=1）。"""

    def test_width_counts_full_width_as_two(self):
        self.assertEqual(_clickpost_text_width(""), 0)
        self.assertEqual(_clickpost_text_width("a"), 1)
        self.assertEqual(_clickpost_text_width("1-2"), 3)
        self.assertEqual(_clickpost_text_width("あ"), 2)
        self.assertEqual(_clickpost_text_width("号室"), 4)
        # 上限は「全角20文字＝40幅」。文字数20でも半角混在だと幅は40未満になりうる。
        self.assertEqual(CLICKPOST_ADDRESS_LINE_WIDTH, 40)
        self.assertEqual(_clickpost_text_width("あ" * 20), CLICKPOST_ADDRESS_LINE_WIDTH)
        self.assertEqual(_clickpost_text_width("東京都新宿区白銀町6-1"), 21)


class ClickPostAddressLineLimitTest(unittest.TestCase):
    """全4行が上限幅に収まること（実障害の再発防止）。"""

    def test_all_lines_within_limit(self):
        for address in ALL_INPUTS:
            with self.subTest(address=address):
                lines, _ = _clickpost_address_lines(address)
                self.assertEqual(len(lines), CLICKPOST_ADDRESS_LINE_COUNT)
                for index, line in enumerate(lines, start=1):
                    self.assertLessEqual(
                        _clickpost_text_width(line),
                        CLICKPOST_ADDRESS_LINE_WIDTH,
                        f"住所{index}行目が上限超過: {line}",
                    )

    def test_real_failures_are_fixed(self):
        for label, address, expected in REAL_FAILURES:
            with self.subTest(label=label):
                lines, overflow = _clickpost_address_lines(address)
                self.assertEqual(lines, expected)
                self.assertEqual(overflow, "")

    def test_legacy_entry_point_is_also_enforced(self):
        # 従来の分割意図を使う関数（申込CSVでは未使用）も、同じ上限強制を通る。
        # 分割位置は旧仕様のままなので、ここでは上限だけを固定する。
        for label, address, _expected in REAL_FAILURES:
            with self.subTest(label=label):
                for line in _split_clickpost_address_power_query(address):
                    self.assertLessEqual(
                        _clickpost_text_width(line), CLICKPOST_ADDRESS_LINE_WIDTH
                    )


class ClickPostAddressLosslessTest(unittest.TestCase):
    """情報が失われないこと。"""

    def test_lines_plus_overflow_restore_original(self):
        # 行内には元データの区切り空白が残るため、比較は空白を除いて行う
        for address in ALL_INPUTS:
            with self.subTest(address=address):
                lines, overflow = _clickpost_address_lines(address)
                restored = re.sub(r"\s+", "", "".join(lines) + overflow)
                self.assertEqual(restored, _compact(address))

    def test_overflow_is_empty_for_normal_addresses(self):
        for _, address, _ in REAL_FAILURES + PREVIOUSLY_ACCEPTED:
            with self.subTest(address=address):
                self.assertEqual(_clickpost_address_lines(address)[1], "")

    def test_too_long_address_reports_leftover(self):
        # 4行(全角80文字)に収まらない分は黙って捨てず、残りとして返す
        lines, overflow = _clickpost_address_lines("あ" * 100)
        self.assertEqual(lines, ("あ" * 20,) * 4)
        self.assertEqual(overflow, "あ" * 20)


class ClickPostAddressRegressionTest(unittest.TestCase):
    """これまで通っていた住所の分割結果を変えていないこと。"""

    def test_previously_accepted_addresses_unchanged(self):
        for label, address, expected in PREVIOUSLY_ACCEPTED:
            with self.subTest(label=label):
                lines, overflow = _clickpost_address_lines(address)
                self.assertEqual(lines, expected)
                self.assertEqual(overflow, "")

    def test_space_separated_address_keeps_existing_grouping(self):
        # 空白3区切り以上: 1+2番目を結合、3番目・4番目以降を2/3行目へ（従来通り）
        self.assertEqual(
            _split_clickpost_address_power_query("沖縄県那覇市 首里1-2-3 コーポ栄 101"),
            ("沖縄県那覇市首里1-2-3", "コーポ栄", "101", ""),
        )

    def test_building_keyword_split_kept(self):
        self.assertEqual(
            _split_clickpost_address_power_query("沖縄県那覇市首里1-2-3コーポ栄101"),
            ("沖縄県那覇市首里1-2-3", "コーポ栄101", "", ""),
        )


class ClickPostAddressBoundaryTest(unittest.TestCase):
    """境界値・異常系。"""

    def test_empty_and_blank(self):
        for address in ("", "   ", "　", None):
            with self.subTest(address=address):
                self.assertEqual(_clickpost_address_lines(address), (("", "", "", ""), ""))

    def test_exactly_at_limit_stays_on_one_line(self):
        lines, overflow = _clickpost_address_lines("あ" * 20)
        self.assertEqual(lines, ("あ" * 20, "", "", ""))
        self.assertEqual(overflow, "")

    def test_one_width_over_limit_moves_to_next_line(self):
        lines, overflow = _clickpost_address_lines("あ" * 20 + "A")
        self.assertEqual(_clickpost_text_width(lines[0]), CLICKPOST_ADDRESS_LINE_WIDTH)
        self.assertEqual(lines[1], "A")
        self.assertEqual(overflow, "")

    def test_single_token_without_spaces_is_chunked_by_width(self):
        # 空白境界が無く、建物キーワードでの分割が上限を超える場合は幅で機械分割する
        lines, overflow = _clickpost_address_lines("あ" * 25 + "コーポ101")
        self.assertEqual(lines, ("あ" * 20, "あ" * 5 + "コーポ101", "", ""))
        self.assertEqual(overflow, "")

    def test_halfwidth_only_building_is_chunked_by_width(self):
        # 空白も番地の切れ目も無い場合は、最後の手段として幅40ごとに機械分割する
        lines, overflow = _clickpost_address_lines("東京都" + "a" * 60)
        self.assertEqual(lines, ("東京都" + "a" * 34, "a" * 26, "", ""))
        self.assertEqual(overflow, "")


class ClickPostConversionWarningTest(unittest.TestCase):
    """4行に収まらない住所が変換結果の警告として呼び出し側に伝わること。"""

    def _convert(self, address: str):
        buyer_rows = [{"受注番号": "1", "発送方法": "クリックポスト"}]
        product_rows = [
            {
                "受注番号": "1",
                "明細行": "1",
                "送り先〒": "9040013",
                "送り先名": "検証太郎",
                "送り先住所": address,
                "商品ｺｰﾄﾞ": "m-1",
                "受注数": "1",
            }
        ]
        with mock.patch("portal_app.services.clickpost.find_clickpost_paths"), mock.patch(
            "portal_app.services.clickpost._read_csv", side_effect=[buyer_rows, product_rows]
        ), mock.patch(
            "portal_app.services.clickpost._load_content_rules",
            return_value={"m-1": ContentRule(prefix="もずく(", default_quantity=None)},
        ):
            return _convert_clickpost_csv(
                buyer_csv=Path("buyer.csv"),
                product_csv=Path("product.csv"),
                write=False,
                preview_limit=10,
            )

    def test_no_warning_for_fitting_address(self):
        result = self._convert(REAL_FAILURES[0][1])
        self.assertEqual(result.output_rows, 1)
        self.assertFalse([w for w in result.warnings if "収まらない" in w])
        row = result.preview_rows[0]
        self.assertEqual(row["お届け先住所1行目"], "東京都新宿区白銀町6-1")
        self.assertEqual(row["お届け先住所2行目"], "神楽坂トワイシアヒルサイドレジデンス421")

    def test_warns_when_address_does_not_fit_in_four_lines(self):
        result = self._convert("あ" * 100)
        leftover_warnings = [w for w in result.warnings if "収まらない" in w]
        self.assertEqual(len(leftover_warnings), 1)
        self.assertIn("受注番号 1", leftover_warnings[0])
        self.assertIn("あ" * 20, leftover_warnings[0])


if __name__ == "__main__":
    unittest.main()
