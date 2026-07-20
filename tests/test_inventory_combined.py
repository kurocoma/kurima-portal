"""在庫明細確認 — 数量合算表（依頼2）のテスト。

認識合わせ対応（2026-07-20 依頼2「合算のバージョンを作成し、PDFを出せるようにする」）:
  - ユーザー回答: 合算 =「商品単位で数量を合算した1つの表」（表の連結ではない）
  - 列は 商品コード/商品名/必要数/備考 の4列。必要数 = 通常の受注数 + セットの発注数量
  - 引当数はセット側に対応値が無いため載せない
  - 備考は由来（通常のみ / セット含む / セットのみ）
  - 対応付けはJANコード（通常商品のNEコードは商品マスタでJANに解決）
あわせて、既存の選べるセット表（build_choice_products）の出力形式が
リファクタリング後も変わっていないことを確認する。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.inventory import (
    MasterTables,
    ORDER_COLUMNS,
    _build_choice_products_detail,
    build_choice_products,
    build_combined_products,
    build_normal_products,
)


def _orders(rows: list[dict[str, object]]) -> pd.DataFrame:
    records = []
    for row in rows:
        record = {column: "" for column in ORDER_COLUMNS}
        record.update(row)
        records.append(record)
    frame = pd.DataFrame(records, columns=ORDER_COLUMNS)
    for column in ("受注数", "引当数"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(int)
    return frame


def _masters() -> MasterTables:
    product_master = pd.DataFrame(
        [
            {"NEコード": "n001", "JANコード": "4900000000011", "商品名": "泡盛 紙パック"},
            {"NEコード": "n002", "JANコード": "4900000000022", "商品名": "さんぴん茶"},
            # JAN未登録の通常商品（合算キーに解決できないケース）
            {"NEコード": "n003", "JANコード": "", "商品名": "島とうがらし"},
        ]
    )
    choice_master = pd.DataFrame(
        [
            {
                "NEコード": "c001",
                "項目選択肢項目名": "選択1",
                "項目選択肢": "泡盛",
                "JANコード": "4900000000011",
                "数量": "2",
            },
            {
                "NEコード": "c001",
                "項目選択肢項目名": "選択2",
                "項目選択肢": "ちんすこう",
                "JANコード": "4900000000033",
                "数量": "1",
            },
        ]
    )
    # 内訳JAN 4900000000033 は商品マスタに商品名が無い想定 → 名前解決のため追加しない
    shimanoya_master = pd.DataFrame(columns=["商品コード"])
    return MasterTables(
        product_master=product_master,
        choice_master=choice_master,
        shimanoya_master=shimanoya_master,
    )


def _sample_frames():
    masters = _masters()
    orders = _orders(
        [
            # 通常商品: 泡盛×10（セット内訳と同一JANに合算されるケース）
            {"商品ｺｰﾄﾞ": "n001", "受注数": 10, "引当数": 10},
            # 通常商品のみ: さんぴん茶×5
            {"商品ｺｰﾄﾞ": "n002", "受注数": 5, "引当数": 5},
            # JAN未登録の通常商品×4
            {"商品ｺｰﾄﾞ": "n003", "受注数": 4, "引当数": 4},
            # 選べるセット×3: 泡盛2個 + ちんすこう1個
            {
                "商品ｺｰﾄﾞ": "c001",
                "商品ｵﾌﾟｼｮﾝ": "選択1:泡盛 選択2:ちんすこう",
                "受注数": 3,
                "引当数": 3,
            },
        ]
    )
    warnings: list[str] = []
    normal = build_normal_products(orders, masters)
    choice_detail = _build_choice_products_detail(orders, masters, warnings)
    return masters, orders, normal, choice_detail, warnings


class CombinedProductsTest(unittest.TestCase):
    def test_combined_merges_by_jan(self):
        """通常10 + セット内訳(2×3=6) が同一JANで合算され 必要数16 になる。"""
        masters, _orders_df, normal, choice_detail, _ = _sample_frames()
        combined = build_combined_products(normal, choice_detail, masters)
        by_code = {row["商品コード"]: row for row in combined.to_dict(orient="records")}

        awamori = by_code["n001"]
        self.assertEqual(awamori["必要数"], 16)
        self.assertEqual(awamori["備考"], "セット含む")
        self.assertEqual(awamori["商品名"], "泡盛 紙パック")

    def test_normal_only_and_choice_only_rows(self):
        """通常のみ・セットのみの行が由来付きで残る。"""
        masters, _orders_df, normal, choice_detail, _ = _sample_frames()
        combined = build_combined_products(normal, choice_detail, masters)
        by_code = {row["商品コード"]: row for row in combined.to_dict(orient="records")}

        # 通常のみ（JAN解決可）
        self.assertEqual(by_code["n002"]["必要数"], 5)
        self.assertEqual(by_code["n002"]["備考"], "通常のみ")
        # 通常のみ（JAN未登録 → NEコードのまま独立行）
        self.assertEqual(by_code["n003"]["必要数"], 4)
        self.assertEqual(by_code["n003"]["備考"], "通常のみ")
        # セットのみ（ちんすこう 1×3=3。商品コード列は内訳JAN）
        self.assertEqual(by_code["4900000000033"]["必要数"], 3)
        self.assertEqual(by_code["4900000000033"]["備考"], "セットのみ")

    def test_combined_columns_and_no_allocation_column(self):
        """列は 商品コード/商品名/必要数/備考 の4列で、引当数は含まれない。"""
        masters, _orders_df, normal, choice_detail, _ = _sample_frames()
        combined = build_combined_products(normal, choice_detail, masters)
        self.assertEqual(list(combined.columns), ["商品コード", "商品名", "必要数", "備考"])

    def test_empty_inputs(self):
        """通常・セットとも0件でも空の合算表を返し例外にならない。"""
        masters = _masters()
        empty_orders = _orders([])
        warnings: list[str] = []
        normal = build_normal_products(empty_orders, masters)
        choice_detail = _build_choice_products_detail(empty_orders, masters, warnings)
        combined = build_combined_products(normal, choice_detail, masters)
        self.assertEqual(len(combined), 0)


class ChoiceViewCompatibilityTest(unittest.TestCase):
    def test_choice_public_view_unchanged(self):
        """既存の選べるセット表は従来どおり 商品名/発注数量/備考 の3列のまま。"""
        masters, orders, _normal, _detail, _ = _sample_frames()
        warnings: list[str] = []
        choice = build_choice_products(orders, masters, warnings)
        self.assertEqual(list(choice.columns), ["商品名", "発注数量", "備考"])
        self.assertEqual(int(choice["発注数量"].sum()), 9)  # 2×3 + 1×3
        self.assertTrue((choice["備考"] == "選べるセット").all())


if __name__ == "__main__":
    unittest.main()
