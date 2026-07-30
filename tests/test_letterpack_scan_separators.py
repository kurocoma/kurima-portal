"""レターパック配送番号反映 — スキャン値の区切り文字正規化のテスト。

2026-07-31 送り状スキャン検証（実物なしのCODE39ラウンドトリップ）で判明した
取りこぼしの回帰:
- CODE39 のスタート/ストップ(*)まで送信するスキャナ設定 → *A123456789012*
- シール表記どおりの手入力 → 1234-5678-9012（半角/全角ハイフン・長音）
どちらも明確に送り状番号の形なので、判定前に * とハイフン類を取り除く。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.letterpack_tracking import classify_scan_value


class ScanSeparatorNormalizationTest(unittest.TestCase):
    def test_code39_start_stop_asterisks(self):
        """CODE39のスタート/ストップ(*)送信設定でも送り状番号と判定する。"""
        result = classify_scan_value("*A123456789012*")
        self.assertEqual(result.kind, "tracking")
        self.assertEqual(result.value, "123456789012")

    def test_hyphenated_manual_input(self):
        """シール表記どおり（4-4-4区切り）の手入力を送り状番号と判定する。"""
        for raw in ("1234-5678-9012", "1234－5678－9012", "1234ー5678ー9012"):
            result = classify_scan_value(raw)
            self.assertEqual(result.kind, "tracking", raw)
            self.assertEqual(result.value, "123456789012", raw)

    def test_denpyo_with_asterisks(self):
        """伝票番号側も * 付き送信設定で読める。"""
        result = classify_scan_value("*D0000069998*")
        self.assertEqual(result.kind, "denpyo")
        self.assertEqual(result.value, "69998")

    def test_ambiguous_values_stay_unknown(self):
        """正規化後も曖昧な値は従来どおり unknown のまま（送り状へ寄せない）。"""
        for raw in ("*ABC*", "A12345678901", "1234-5678-90123"):
            self.assertEqual(classify_scan_value(raw).kind, "unknown", raw)

    def test_hyphenated_denpyo_manual_input_is_forgiven(self):
        """伝票番号の手入力にハイフンが混ざっても除去されて通る（正規化の副次効果）。"""
        result = classify_scan_value("6-9998")
        self.assertEqual(result.kind, "denpyo")
        self.assertEqual(result.value, "69998")


if __name__ == "__main__":
    unittest.main()
