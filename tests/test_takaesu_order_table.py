"""高江洲発注表（編集・メール貼り付け用）の保存・読込のテスト。

2026-07-28 使用者要望: 合算/発注書はPDFだとメールにコピペできず、
高江洲のExcelを開いてコピペしていた。ポータル上の編集可能な表（自動保存）と
表コピーで置き換える。ベースは高江洲発注書、作り直しは手動のみ（ユーザー回答）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import takaesu_order_table as mod


def _fake_sheet(rows):
    return SimpleNamespace(source_csv=Path("data2607281200.csv"), preview_rows=tuple(rows))


class TakaesuOrderTableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.table_path = Path(self._tmp.name) / "order_table.json"
        patcher = patch.object(mod, "ORDER_TABLE_PATH", self.table_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_returns_none_when_missing(self):
        self.assertIsNone(mod.load_order_table())

    def test_load_returns_none_on_broken_json(self):
        self.table_path.parent.mkdir(parents=True, exist_ok=True)
        self.table_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(mod.load_order_table())

    def test_reset_builds_rows_from_order_sheet(self):
        sheet_rows = [
            {"JANコード": "4900000000001", "仕入先CD": "T1", "商品名": "サプリA",
             "発注数": 12, "受注数": 10, "備考": ""},
        ]
        with patch.object(mod, "preview_takaesu_order_sheet", return_value=_fake_sheet(sheet_rows)):
            table = mod.reset_order_table()
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0]["商品名"], "サプリA")
        self.assertEqual(table.rows[0]["発注数"], "12")
        self.assertEqual(table.base_source, "data2607281200.csv")
        loaded = mod.load_order_table()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.rows, table.rows)

    def test_save_normalizes_and_keeps_base_info(self):
        """保存は空白除去・未知キー除去を行い、reset時のベース情報を保持する。"""
        with patch.object(mod, "preview_takaesu_order_sheet", return_value=_fake_sheet([])):
            mod.reset_order_table()
        table = mod.save_order_table_rows(
            [
                {"JANコード": " 4900000000002 ", "商品名": "紙パック", "発注数": " 0 ",
                 "受注数": "", "備考": "発注しない", "unknown": "x"},
            ]
        )
        self.assertEqual(table.rows[0]["JANコード"], "4900000000002")
        self.assertEqual(table.rows[0]["発注数"], "0")
        self.assertNotIn("unknown", table.rows[0])
        self.assertEqual(table.base_source, "data2607281200.csv")
        self.assertIsNotNone(table.updated_at)

    def test_save_survives_reload(self):
        """自動保存の内容が再読込（別リクエスト相当）でも残る。"""
        mod.save_order_table_rows([{"商品名": "追加行", "発注数": "5"}])
        loaded = mod.load_order_table()
        self.assertEqual(loaded.rows[0]["商品名"], "追加行")
        doc = json.loads(self.table_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["rows"][0]["発注数"], "5")

    def test_save_rejects_too_many_rows(self):
        rows = [{"商品名": f"r{i}"} for i in range(mod.MAX_ROWS + 1)]
        with self.assertRaises(ValueError):
            mod.save_order_table_rows(rows)

    def test_ensure_uses_saved_table_without_rebuild(self):
        """保存済みがあれば集計を呼ばずにそれを返す（手動作り直し方式）。"""
        mod.save_order_table_rows([{"商品名": "編集済み", "発注数": "3"}])
        with patch.object(mod, "preview_takaesu_order_sheet", side_effect=AssertionError("呼ばれてはいけない")):
            table = mod.ensure_order_table()
        self.assertEqual(table.rows[0]["商品名"], "編集済み")


if __name__ == "__main__":
    unittest.main()
