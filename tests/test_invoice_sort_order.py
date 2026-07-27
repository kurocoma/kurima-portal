"""納品書・出荷指示書ダウンロードの並び順固定のテスト。

2026-07-28 ユーザー要望: 並び順プルダウン(select[name="sort"])はNE側に前回値が
保存され、空白や別の並びになっていることがある。ダウンロード前に毎回確認し、
「店舗コード、伝票番号順」以外なら選択し直す。
FakePage で select の状態を再現して検証する（実ブラウザには触れない）。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.clickpost import _ensure_invoice_sort_order

SORT_OPTIONS = [
    {"value": "", "label": ""},
    {"value": "1", "label": "受注日順"},
    {"value": "2", "label": "店舗コード、伝票番号順"},
    {"value": "3", "label": "伝票番号順"},
]


class FakeSortSelect:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, expression: str):
        return {"value": self._page.value, "options": self._page.options}

    async def select_option(self, value) -> None:
        self._page.value = value
        self._page.selected_values.append(value)


class FakePage:
    def __init__(self, value: str, options=None) -> None:
        self.value = value
        self.options = options if options is not None else SORT_OPTIONS
        self.selected_values: list[str] = []

    def locator(self, selector: str) -> FakeSortSelect:
        assert selector == 'select[name="sort"]'
        return FakeSortSelect(self)


class EnsureInvoiceSortOrderTest(unittest.TestCase):
    def test_blank_value_is_fixed(self):
        """空白（未選択）なら「店舗コード、伝票番号順」を選択する。"""
        page = FakePage("")
        asyncio.run(_ensure_invoice_sort_order(page))
        self.assertEqual(page.selected_values, ["2"])

    def test_other_value_is_fixed(self):
        """他の並び順になっていたら「店舗コード、伝票番号順」へ直す。"""
        page = FakePage("1")
        asyncio.run(_ensure_invoice_sort_order(page))
        self.assertEqual(page.selected_values, ["2"])

    def test_correct_value_is_noop(self):
        """既に「店舗コード、伝票番号順」なら何もしない。"""
        page = FakePage("2")
        asyncio.run(_ensure_invoice_sort_order(page))
        self.assertEqual(page.selected_values, [])

    def test_missing_target_option_is_noop(self):
        """該当ラベルの選択肢が無い画面では何もしない（既定順のまま）。"""
        page = FakePage("1", options=[{"value": "1", "label": "受注日順"}])
        asyncio.run(_ensure_invoice_sort_order(page))
        self.assertEqual(page.selected_values, [])

    def test_exact_label_wins_over_partial_match(self):
        """実機の選択肢: 部分一致する別の並び（value=7）より完全一致（value=3）を選ぶ。"""
        page = FakePage(
            "",
            options=[
                {"value": "7", "label": "店舗コード、発送方法、支払方法、伝票番号順"},
                {"value": "3", "label": "店舗コード、伝票番号順"},
            ],
        )
        asyncio.run(_ensure_invoice_sort_order(page))
        self.assertEqual(page.selected_values, ["3"])


if __name__ == "__main__":
    unittest.main()
