"""選べるセット内訳マスタ未一致の明細・診断のテスト。

2026-07-29 要望: 「未一致が N 行あります」だけでは何を直せばよいか分からない。
未一致の商品リストと、NEオプション一覧（紐づけマスタ）のどのレベルで
外れたか（NEコード未登録／項目名未登録／選択肢未登録）を表示する。
実障害例: ビレリー選べるセット(a009-2215-c01)の受注が4本刻み6区分に変わったが、
マスタは6本刻み4区分のままで30行が未一致になった。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from portal_app.services.inventory import _diagnose_missing_choice


def _missing_frame(rows):
    return pd.DataFrame(rows, columns=["商品ｺｰﾄﾞ", "商品ｵﾌﾟｼｮﾝ.1", "商品ｵﾌﾟｼｮﾝ.2", "受注数"])


CHOICE_MASTER = pd.DataFrame(
    [
        {"NEコード": "a009-2215-c01", "項目選択肢項目名": "1本目から6本目", "項目選択肢": "グァバ", "JANコード": "4514603342116", "数量": "6"},
        {"NEコード": "a009-2215-c01", "項目選択肢項目名": "1本目から6本目", "項目選択肢": "オレンジ", "JANコード": "4514603390513", "数量": "6"},
        {"NEコード": "a009-2215-c01", "項目選択肢項目名": "7本目から12本目", "項目選択肢": "グァバ", "JANコード": "4514603342116", "数量": "6"},
    ]
)


class DiagnoseMissingChoiceTest(unittest.TestCase):
    def test_unregistered_option_name(self):
        """実障害の形: 項目名の刻みが変わった（4本刻み）はマスタ側項目名を提示する。"""
        rows = _diagnose_missing_choice(
            _missing_frame([("a009-2215-c01", "1本目から4本目", "グァバ", 2)]),
            CHOICE_MASTER,
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("項目名「1本目から4本目」が未登録", rows[0]["診断"])
        self.assertIn("1本目から6本目", rows[0]["診断"])
        self.assertEqual(rows[0]["受注数"], 2)

    def test_unregistered_option_value(self):
        """項目名は一致・選択肢だけ無い場合は登録済み選択肢を提示する。"""
        rows = _diagnose_missing_choice(
            _missing_frame([("a009-2215-c01", "1本目から6本目", "島レモネード", 1)]),
            CHOICE_MASTER,
        )
        self.assertIn("選択肢「島レモネード」が未登録", rows[0]["診断"])
        self.assertIn("グァバ", rows[0]["診断"])

    def test_unregistered_ne_code(self):
        """NEコード自体が紐づけマスタに無い場合はその旨を表示する。"""
        rows = _diagnose_missing_choice(
            _missing_frame([("zzz-999", "1本目から6本目", "グァバ", 1)]),
            CHOICE_MASTER,
        )
        self.assertIn("NEコード自体が未登録", rows[0]["診断"])

    def test_groups_and_sums_order_counts(self):
        """同じ組み合わせは1組に集約し受注数を合算する。"""
        rows = _diagnose_missing_choice(
            _missing_frame(
                [
                    ("a009-2215-c01", "1本目から4本目", "グァバ", 2),
                    ("a009-2215-c01", "1本目から4本目", "グァバ", 3),
                ]
            ),
            CHOICE_MASTER,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["受注数"], 5)


if __name__ == "__main__":
    unittest.main()
