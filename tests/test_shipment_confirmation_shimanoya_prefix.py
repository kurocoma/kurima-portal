"""しまのや出荷データの「お客様管理番号」店舗記号（prefix）除去の単体テスト。

背景:
- しまのやCSV作成ツールは受注NOに店舗記号を付ける（r=楽天, a=Amazon, y=Yahoo）。
  y は 2026-09-10 の Yahoo 受注対応で追加された。
- 出荷確定はこの記号を除去した7桁を、購入者データの受注番号末尾7桁と突合する。
- 従来は a/r/R しか除去しておらず、Yahoo 受注（y）が突合されなかった。

合成データのみ使用。実フォルダへは書き込まない（一時フォルダを portal_root にする）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.shipment_confirmation import (
    _load_tracking_maps,
    _normalize_shimanoya_order_suffix,
)


class NormalizeShimanoyaOrderSuffixTest(unittest.TestCase):
    def test_strips_yahoo_prefix_lowercase(self) -> None:
        self.assertEqual(_normalize_shimanoya_order_suffix("y1234567"), "1234567")

    def test_strips_yahoo_prefix_uppercase(self) -> None:
        self.assertEqual(_normalize_shimanoya_order_suffix("Y1234567"), "1234567")

    def test_keeps_rakuten_and_amazon_behavior(self) -> None:
        """既存の r/a/R 除去は変えない（回帰ガード）。"""
        for raw in ("r1234567", "a1234567", "R1234567", " r1234567 "):
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_shimanoya_order_suffix(raw), "1234567")

    def test_no_prefix_is_unchanged(self) -> None:
        self.assertEqual(_normalize_shimanoya_order_suffix("1234567"), "1234567")


class LoadTrackingMapsYahooRowTest(unittest.TestCase):
    """出荷データCSVに y 行があれば、7桁キーで shimanoya マップに載ること。"""

    def test_yahoo_row_is_keyed_by_seven_digits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            portal_root = Path(tmp)
            shipment_dir = portal_root / "しまのやさん" / "csv格納" / "出荷データ"
            shipment_dir.mkdir(parents=True)
            csv_path = shipment_dir / "20260911164501_出荷完了データ_楽天.csv"
            csv_path.write_text(
                "お客様管理番号,伝票番号\r\n"
                "r1111111,300000000001\r\n"
                "y2222222,300000000002\r\n",
                encoding="utf-8-sig",
            )
            warnings: list[str] = []
            maps, source_files, row_count = _load_tracking_maps(portal_root, warnings)

        self.assertEqual(row_count, 2)
        self.assertEqual(source_files, [csv_path])
        self.assertEqual(
            maps["shimanoya"],
            {"1111111": "300000000001", "2222222": "300000000002"},
        )
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
