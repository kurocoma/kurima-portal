"""レターパック配送番号反映 — ゼロ埋めバーコード値の判定（2026-07-24 実障害の回帰）。

利用者PCでのスキャンが「D」の落ちた 10桁ゼロ埋め値（0000069677）になり、
「桁数が伝票番号にも送り状番号にも一致しません」で弾かれた。先頭ゼロを除いた
桁数で伝票番号判定するのが正。ゼロ埋めでない10桁など曖昧な値は従来どおり
unknown のまま（勝手にどちらかへ寄せない）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.letterpack_tracking import classify_scan_value


class ZeroPaddedScanTest(unittest.TestCase):
    def test_zero_padded_barcode_without_d_is_denpyo(self):
        """2026-07-24 実障害の入力値がそのまま伝票番号になる。"""
        result = classify_scan_value("0000069677")
        self.assertEqual(result.kind, "denpyo")
        self.assertEqual(result.value, "69677")

    def test_zero_padded_variants(self):
        cases = {
            " 0000069589 ": "69589",
            "000123": "123",
            "0000000000": "0",
        }
        for raw, expected in cases.items():
            result = classify_scan_value(raw)
            self.assertEqual(result.kind, "denpyo", raw)
            self.assertEqual(result.value, expected, raw)

    def test_non_zero_padded_long_digits_stay_unknown(self):
        """先頭ゼロなしの9〜11桁は従来どおり unknown（誤読を伝票番号に寄せない）。"""
        for raw in ("1234567890", "123456789", "12345678901"):
            self.assertEqual(classify_scan_value(raw).kind, "unknown", raw)

    def test_twelve_digits_with_leading_zeros_stay_tracking(self):
        """12桁は先頭ゼロがあっても送り状番号を優先する。"""
        result = classify_scan_value("001234567890")
        self.assertEqual(result.kind, "tracking")
        self.assertEqual(result.value, "001234567890")


if __name__ == "__main__":
    unittest.main()
