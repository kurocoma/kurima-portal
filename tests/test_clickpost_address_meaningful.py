"""クリックポスト住所分割を「意味の区切り」で行う仕様のテスト。

2026-08-11 の運用要望:
    幅だけで機械的に詰めると、番地や建物名が語の途中で割れる。
        例) 神奈川県大和市林間2-6-1Dグラフォー / ト中央林間605
            北海道札幌市白石区南郷通17丁目南1-2 / 0
    番地の終わり（数字の並びが終わって建物名が始まる位置）と、
    元データの半角スペースを区切りとして扱い、語の途中では切らない。
    元データのスペースは行内に残す（宛名として読めるようにするため）。

上限は従来どおり 1行=全角20文字（40幅）・最大4行。
"""

import re
import unicodedata
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.clickpost import (  # noqa: E402
    CLICKPOST_ADDRESS_LINE_WIDTH,
    _clickpost_address_lines,
    _clickpost_text_width,
)


def _normalized(address: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", address).strip())


class MeaningfulAddressSplitTest(unittest.TestCase):
    """実際の受注データで、意味の区切りどおりに分割されること。"""

    # (ラベル, 元住所, 期待する4行)
    CASES = (
        (
            "8/10 伝票70437 堀江弘二",
            "東京都新宿区白銀町6-1 神楽坂トワイシアヒルサイドレジデンス421",
            ("東京都新宿区白銀町6-1", "神楽坂トワイシアヒルサイドレジデンス421", "", ""),
        ),
        (
            "8/10 伝票70397 小野香織（スペースを残す）",
            "福岡県北九州市八幡西区折尾四丁目28-14 103号室 Conforto Y 折尾",
            ("福岡県北九州市八幡西区折尾四丁目28-14", "103号室 Conforto Y 折尾", "", ""),
        ),
        (
            "8/10 伝票70407 坂本由美子（Dグラフォートを割らない）",
            "神奈川県大和市林間2-6-1 Dグラフォート中央林間605",
            ("神奈川県大和市林間2-6-1", "Dグラフォート中央林間605", "", ""),
        ),
        (
            "8/10 伝票70357 平本亮子（ヴェルディを割らない）",
            "広島県広島市南区大州3丁目 4ー12ヴェルディ天神川駅南1505",
            ("広島県広島市南区大州3丁目 4ー12", "ヴェルディ天神川駅南1505", "", ""),
        ),
        (
            "8/10 伝票70351 田島武（番地1-20を割らない）",
            "北海道札幌市白石区南郷通17丁目南1-20 ",
            ("北海道札幌市白石区南郷通17丁目南1-20", "", "", ""),
        ),
        (
            "8/10 伝票70335 石附秀太（村上寮を割らない）",
            "新潟県村上市二之町2-45 村上寮201",
            ("新潟県村上市二之町2-45 村上寮201", "", "", ""),
        ),
        (
            "8/10 伝票70454 小田茜（上限内なので1行）",
            "広島県 広島市安佐南区 緑井4-3-17",
            ("広島県 広島市安佐南区 緑井4-3-17", "", "", ""),
        ),
        (
            "8/10 伝票70378 中野円",
            "群馬県 伊勢崎市 茂呂町二丁目3548-2",
            ("群馬県 伊勢崎市 茂呂町二丁目3548-2", "", "", ""),
        ),
        (
            "8/10 伝票70317 小西康嗣（分割不要）",
            "奈良県奈良市中町2099-10 ",
            ("奈良県奈良市中町2099-10", "", "", ""),
        ),
        (
            "7/29 伝票69951 前田日和",
            "大阪府大阪市西区新町4-8-8 アーバネックス西長堀 805",
            ("大阪府大阪市西区新町4-8-8", "アーバネックス西長堀 805", "", ""),
        ),
        (
            "8/05 伝票70212 小林亘",
            "大阪府大阪市浪速区塩草3-7-5 ファステート難波グランプリ1508",
            ("大阪府大阪市浪速区塩草3-7-5", "ファステート難波グランプリ1508", "", ""),
        ),
        (
            "7/24 澤野寛子（旧実装でも取り込めていた住所）",
            "愛知県名古屋市名東区梅森坂3-5119 クレールメゾンEast102",
            ("愛知県名古屋市名東区梅森坂3-5119", "クレールメゾンEast102", "", ""),
        ),
    )

    def test_splits_at_meaningful_boundaries(self):
        for label, address, expected in self.CASES:
            with self.subTest(label):
                lines, overflow = _clickpost_address_lines(address)
                self.assertEqual(lines, expected)
                self.assertEqual(overflow, "")

    def test_every_line_is_within_limit(self):
        for label, address, _expected in self.CASES:
            with self.subTest(label):
                lines, _ = _clickpost_address_lines(address)
                for index, line in enumerate(lines, start=1):
                    self.assertLessEqual(
                        _clickpost_text_width(line),
                        CLICKPOST_ADDRESS_LINE_WIDTH,
                        f"住所{index}行目が上限を超えている: {line}",
                    )

    def test_no_information_is_lost(self):
        for label, address, _expected in self.CASES:
            with self.subTest(label):
                lines, overflow = _clickpost_address_lines(address)
                restored = re.sub(r"\s+", "", "".join(lines) + overflow)
                self.assertEqual(restored, _normalized(address))


class AddressNumberBoundaryTest(unittest.TestCase):
    """番地の途中で切らないこと（丁目・号室・方角を含む）。"""

    def test_keeps_chome_and_direction_together(self):
        lines, _ = _clickpost_address_lines("北海道札幌市白石区南郷通17丁目南1-20")
        self.assertEqual(lines[0], "北海道札幌市白石区南郷通17丁目南1-20")

    def test_breaks_after_banchi_when_building_follows(self):
        lines, _ = _clickpost_address_lines(
            "東京都千代田区丸の内1-2-3グランドメゾン丸の内タワーレジデンス1203"
        )
        self.assertEqual(lines[0], "東京都千代田区丸の内1-2-3")
        self.assertEqual(lines[1], "グランドメゾン丸の内タワーレジデンス1203")

    def test_room_number_atom_is_not_split(self):
        # 「908号室」は数字で始まるが、ここで切ると意味を成さないので1つの塊のまま扱う
        lines, _ = _clickpost_address_lines("大阪府大阪市北区梅田1-1-1 サンプルビル 908号室")
        self.assertEqual(lines, ("大阪府大阪市北区梅田1-1-1 サンプルビル", "908号室", "", ""))


class AddressEdgeCaseTest(unittest.TestCase):
    """空・空白のみ・上限ちょうど・4行に収まらない住所。"""

    def test_empty_address(self):
        self.assertEqual(_clickpost_address_lines(""), (("", "", "", ""), ""))

    def test_whitespace_only_address(self):
        self.assertEqual(_clickpost_address_lines("　  "), (("", "", "", ""), ""))

    def test_exactly_at_limit_stays_on_one_line(self):
        text = "あ" * 20  # 40幅ちょうど
        lines, overflow = _clickpost_address_lines(text)
        self.assertEqual(lines[0], text)
        self.assertEqual(lines[1], "")
        self.assertEqual(overflow, "")

    def test_one_over_limit_is_split(self):
        text = "あ" * 21  # 42幅
        lines, overflow = _clickpost_address_lines(text)
        self.assertEqual(lines[0], "あ" * 20)
        self.assertEqual(lines[1], "あ")
        self.assertEqual(overflow, "")

    def test_address_longer_than_four_lines_reports_overflow(self):
        text = "あ" * 100  # 200幅 = 5行ぶん
        lines, overflow = _clickpost_address_lines(text)
        self.assertEqual(lines, ("あ" * 20, "あ" * 20, "あ" * 20, "あ" * 20))
        self.assertEqual(overflow, "あ" * 20)

    def test_full_width_space_is_treated_as_separator(self):
        # 全角スペースはNFKCで半角化され、区切りとして扱われる（行内には半角で残る）
        lines, _ = _clickpost_address_lines("東京都新宿区西新宿1-1-1　ハイツ203")
        self.assertEqual(lines, ("東京都新宿区西新宿1-1-1 ハイツ203", "", "", ""))


if __name__ == "__main__":
    unittest.main()
