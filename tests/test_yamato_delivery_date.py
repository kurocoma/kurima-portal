"""ヤマト伝票CSV — 配達指定日の自動削除テスト（依頼3）。

認識合わせ対応（2026-07-20 依頼3「配達希望日が翌日の場合は出力エラーになるので削除する」）:
  - ユーザー回答: 翌日ちょうどだけでなく「翌日以前すべて」（当日・過去日含む）を削除
  - 判定基準日は行の「出荷予定日」列（B2は取込CSVの出荷予定日を出荷日として扱うため）
  - 空欄・解釈不能な配達指定日は触らない（不正値の判定はB2に委ねる）
  - 削除時は受注番号（お客様管理番号）付きの警告を出す

既存テストファイル（test_yamato_conversion.py）は保護ルールにより改変しないため、
新規ファイルとして追加する。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.yamato_conversion import (
    YAMATO_INPUT_HEADERS,
    clear_undeliverable_delivery_dates,
    transform_ne_to_yamato,
)


def _frame(rows: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "お客様管理番号": row.get("order", ""),
                "出荷予定日": row.get("ship", ""),
                "配達指定日": row.get("delivery", ""),
            }
            for row in rows
        ]
    )


class ClearUndeliverableDeliveryDatesTest(unittest.TestCase):
    def test_next_day_and_earlier_are_cleared(self):
        """翌日・当日・過去日の指定は削除し、翌々日以降は残す（境界値）。"""
        df = _frame(
            [
                {"order": "69001", "ship": "2026/07/21", "delivery": "2026/07/22"},  # 翌日 → 削除
                {"order": "69002", "ship": "2026/07/21", "delivery": "2026/07/21"},  # 当日 → 削除
                {"order": "69003", "ship": "2026/07/21", "delivery": "2026/07/20"},  # 過去日 → 削除
                {"order": "69004", "ship": "2026/07/21", "delivery": "2026/07/23"},  # 翌々日 → 残す
                {"order": "69005", "ship": "2026/07/21", "delivery": "2026/08/01"},  # 先日付 → 残す
            ]
        )
        count, orders = clear_undeliverable_delivery_dates(df)
        self.assertEqual(count, 3)
        self.assertEqual(orders, ["69001", "69002", "69003"])
        self.assertEqual(list(df["配達指定日"]), ["", "", "", "2026/07/23", "2026/08/01"])

    def test_blank_and_invalid_values_are_untouched(self):
        """空欄・解釈不能な配達指定日、出荷予定日欠損の行は変更しない。"""
        df = _frame(
            [
                {"order": "69011", "ship": "2026/07/21", "delivery": ""},           # 空欄
                {"order": "69012", "ship": "2026/07/21", "delivery": "あさって"},    # 解釈不能
                {"order": "69013", "ship": "", "delivery": "2026/07/22"},           # 出荷予定日なし
                {"order": "69014", "ship": "2026/07/21", "delivery": " 2026/07/22 "},  # 空白付き翌日 → 削除
            ]
        )
        count, orders = clear_undeliverable_delivery_dates(df)
        self.assertEqual(count, 1)
        self.assertEqual(orders, ["69014"])
        self.assertEqual(list(df["配達指定日"]), ["", "あさって", "2026/07/22", ""])

    def test_month_boundary(self):
        """月またぎの翌日（7/31出荷→8/1指定）も削除される。"""
        df = _frame([{"order": "69021", "ship": "2026/07/31", "delivery": "2026/08/01"}])
        count, _ = clear_undeliverable_delivery_dates(df)
        self.assertEqual(count, 1)


class TransformIntegrationTest(unittest.TestCase):
    def test_transform_clears_date_and_warns_with_order_number(self):
        """変換本体を通したとき、指定日が空欄化され受注番号付き警告が出る。"""
        row = {header: "" for header in YAMATO_INPUT_HEADERS}
        row["お客様管理番号"] = "69589"
        row["届け先名（漢字）"] = "テスト太郎"
        row["出荷予定日"] = "2026/07/21"
        row["配達指定日"] = "2026/07/22"
        source_df = pd.DataFrame([row], columns=YAMATO_INPUT_HEADERS)

        converted, warnings_out, _reviews, _adjusted = transform_ne_to_yamato(source_df, {})

        self.assertEqual(converted.iloc[0]["配達指定日"], "")
        self.assertEqual(converted.iloc[0]["出荷予定日"], "2026/07/21")
        delivery_warnings = [w for w in warnings_out if "配達指定日を削除" in w]
        self.assertEqual(len(delivery_warnings), 1)
        self.assertIn("69589", delivery_warnings[0])

    def test_transform_keeps_valid_date_without_warning(self):
        """翌々日以降の指定日は変更されず、削除警告も出ない。"""
        row = {header: "" for header in YAMATO_INPUT_HEADERS}
        row["お客様管理番号"] = "69586"
        row["届け先名（漢字）"] = "テスト次郎"
        row["出荷予定日"] = "2026/07/21"
        row["配達指定日"] = "2026/07/24"
        source_df = pd.DataFrame([row], columns=YAMATO_INPUT_HEADERS)

        converted, warnings_out, _reviews, _adjusted = transform_ne_to_yamato(source_df, {})

        self.assertEqual(converted.iloc[0]["配達指定日"], "2026/07/24")
        self.assertFalse(any("配達指定日を削除" in w for w in warnings_out))


if __name__ == "__main__":
    unittest.main()
