"""レターパック配送番号反映（依頼4）— スキャン判定とCSV出力のテスト。

認識合わせ対応（2026-07-20 依頼4・ユーザー回答）:
  - 入力欄1つの自動判定: 「D始まり=納品書」「12桁数字（A接頭辞可）=送り状番号」
    （既存Excelマクロの VBA: 伝票番号はD除去・送り状はA除去、を実機解析で確認）
  - 出力はExcel互換の2列CSV（伝票番号,送り状番号・cp932・yyyyMMddhhmmレターパック.csv）。
    既存の出荷確定カード（shipment_confirmation._load_tracking_maps）が
    ファイル名に「レターパック」を含むCSVとして自動読込できる形式であること
  - 同一伝票番号は上書き（読込側が最初の1件を採用するため、書き出しで一意化）
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.letterpack_tracking import (
    classify_scan_value,
    letterpack_csv_filename,
    write_letterpack_csv,
)
from portal_app.services.shipment_confirmation import normalize_barcode_value


class ClassifyScanValueTest(unittest.TestCase):
    def test_denpyo_barcode_forms(self):
        """納品書バーコード（D+ゼロ埋め）と手入力の伝票番号を判定できる。"""
        cases = {
            "D0000068531": "68531",  # 実機のプレースホルダ例と同形式
            "d0000068531": "68531",
            "68531": "68531",
            " D0000069589 ": "69589",
        }
        for raw, expected in cases.items():
            result = classify_scan_value(raw)
            self.assertEqual(result.kind, "denpyo", raw)
            self.assertEqual(result.value, expected, raw)

    def test_tracking_barcode_forms(self):
        """レターパック送り状番号（12桁。A接頭辞はExcel VBA同様に除去）を判定できる。"""
        cases = {
            "123456789012": "123456789012",
            "A123456789012": "123456789012",
            "a123456789012": "123456789012",
            " 123456789012 ": "123456789012",
        }
        for raw, expected in cases.items():
            result = classify_scan_value(raw)
            self.assertEqual(result.kind, "tracking", raw)
            self.assertEqual(result.value, expected, raw)

    def test_unknown_values(self):
        """判定できない値は unknown（理由付き）になり、勝手にどちらかへ寄せない。"""
        for raw in ("", "ABC", "1234567890", "A12345678901", "1234567890123"):
            self.assertEqual(classify_scan_value(raw).kind, "unknown", raw)

    def test_normalization_matches_shipment_confirmation(self):
        """伝票番号の正規化は出荷確定カードの読込側と同一関数を使う（突合保証）。"""
        raw = "D0000068531"
        self.assertEqual(classify_scan_value(raw).value, normalize_barcode_value(raw))


class WriteLetterpackCsvTest(unittest.TestCase):
    def test_excel_compatible_output(self):
        """Excel版と同じ 2列・cp932・命名 で出力される。"""
        now = datetime(2026, 7, 21, 11, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_letterpack_csv(
                [
                    {"denpyo": "68531", "tracking": "123456789012"},
                    {"denpyo": "D0000068532", "tracking": "A123456789013"},
                ],
                output_dir=Path(tmp),
                now=now,
            )
            self.assertEqual(path.name, "202607211101レターパック.csv")
            self.assertEqual(letterpack_csv_filename(now), path.name)
            raw = path.read_bytes()
            text = raw.decode("cp932")
            lines = [line for line in text.split("\r\n") if line]
            self.assertEqual(lines[0], "伝票番号,送り状番号")
            self.assertEqual(lines[1], "68531,123456789012")
            # D/A接頭辞付きで渡しても正規化されて出力される
            self.assertEqual(lines[2], "68532,123456789013")

    def test_duplicate_denpyo_keeps_last(self):
        """同一伝票番号は最後のペア（貼り直し）を採用する。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_letterpack_csv(
                [
                    {"denpyo": "68531", "tracking": "111111111111"},
                    {"denpyo": "68531", "tracking": "222222222222"},
                ],
                output_dir=Path(tmp),
                now=datetime(2026, 7, 21, 11, 2),
            )
            with path.open("r", encoding="cp932", newline="") as fp:
                rows = list(csv.DictReader(fp))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["送り状番号"], "222222222222")

    def test_same_minute_does_not_overwrite(self):
        """同じ「分」に2回作成しても、既存ファイルを上書きせず秒付き別名になる。"""
        now = datetime(2026, 7, 21, 11, 3, 45)
        with tempfile.TemporaryDirectory() as tmp:
            first = write_letterpack_csv(
                [{"denpyo": "1", "tracking": "111111111111"}], output_dir=Path(tmp), now=now
            )
            second = write_letterpack_csv(
                [{"denpyo": "2", "tracking": "222222222222"}], output_dir=Path(tmp), now=now
            )
            self.assertNotEqual(first, second)
            self.assertIn("レターパック", second.name)
            self.assertTrue(first.exists() and second.exists())

    def test_invalid_pairs_rejected(self):
        """不正なペア（桁違い・空）はエラーになり、CSVを出力しない。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                write_letterpack_csv(
                    [{"denpyo": "68531", "tracking": "123"}], output_dir=Path(tmp)
                )
            with self.assertRaises(ValueError):
                write_letterpack_csv([], output_dir=Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
