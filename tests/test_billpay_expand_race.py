"""BillPay精算回の展開（_ensure_settlement_expanded）のテスト。

2026-10-01 実障害の回帰: 最新の精算回は描画の少し後にページ側が自動で展開する。
展開ボタンはトグルなので、描画直後に「非表示」と判定して即クリックすると自動展開と
打ち消し合って閉じたままになり、「BillPay精算回の展開完了を確認できません。」で
失敗した。クリック前に自動展開を待ち、閉じ直されたら押し直すことを検証する。
FakeRow で仮想時計つきの開閉状態を再現する（実ブラウザには触れない）。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from portal_app.services import billing_statements_rakuten as billpay


class FakeRow:
    """精算回1件の開閉状態。auto_toggle_at（秒）にページ側の自動トグルが1回入る。"""

    def __init__(
        self,
        *,
        opened: bool = False,
        auto_toggle_at: float | None = None,
        click_works: bool = True,
        has_target: bool = True,
        has_collapse: bool = True,
    ) -> None:
        self.opened = opened
        self.auto_toggle_at = auto_toggle_at
        self.click_works = click_works
        self.has_target = has_target
        self.has_collapse = has_collapse
        self.now = 0.0
        self.clicks = 0

    def advance(self, seconds: float) -> None:
        self.now += seconds
        if self.auto_toggle_at is not None and self.now >= self.auto_toggle_at:
            self.auto_toggle_at = None
            self.opened = not self.opened

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class FakeTarget:
    def __init__(self, row: FakeRow) -> None:
        self._row = row
        self.first = self

    async def count(self) -> int:
        return 1 if self._row.has_target else 0

    async def is_visible(self) -> bool:
        return self._row.opened

    async def wait_for(self, *, state: str, timeout: float) -> None:
        remaining = timeout / 1000
        while not self._row.opened:
            if remaining <= 0:
                raise PlaywrightTimeoutError("timeout")
            self._row.advance(0.1)
            remaining -= 0.1


class FakeCollapse:
    def __init__(self, row: FakeRow) -> None:
        self._row = row
        self.first = self

    async def count(self) -> int:
        return 1 if self._row.has_collapse else 0

    async def click(self) -> None:
        self._row.clicks += 1
        if self._row.click_works:
            self._row.opened = not self._row.opened


def _run(row: FakeRow) -> None:
    with mock.patch.object(billpay.asyncio, "sleep", row.sleep):
        asyncio.run(billpay._ensure_settlement_expanded(FakeTarget(row), FakeCollapse(row)))


class EnsureSettlementExpandedTest(unittest.TestCase):
    def test_auto_expanding_latest_row_is_not_clicked(self) -> None:
        # 障害の再現条件: 描画直後は閉じていて、0.3秒後に自動で開く。
        row = FakeRow(opened=False, auto_toggle_at=0.3)
        _run(row)
        self.assertTrue(row.opened)
        self.assertEqual(row.clicks, 0)

    def test_already_expanded_row_is_left_alone(self) -> None:
        row = FakeRow(opened=True)
        _run(row)
        self.assertTrue(row.opened)
        self.assertEqual(row.clicks, 0)

    def test_collapsed_row_is_clicked_once(self) -> None:
        # 過去の精算回: 自動展開されないのでクリックで開く。
        row = FakeRow(opened=False)
        _run(row)
        self.assertTrue(row.opened)
        self.assertEqual(row.clicks, 1)

    def test_reclicks_when_page_closes_row_after_click(self) -> None:
        # 遅い端末: 自動トグルがクリックの後に来て閉じ直される → 押し直して開く。
        row = FakeRow(opened=False, auto_toggle_at=billpay._EXPAND_WAIT_MS / 1000 + 0.5)
        _run(row)
        self.assertTrue(row.opened)
        self.assertEqual(row.clicks, 2)

    def test_raises_when_row_never_expands(self) -> None:
        row = FakeRow(opened=False, click_works=False)
        with self.assertRaises(billpay.BillPayError) as caught:
            _run(row)
        self.assertEqual(caught.exception.state, "PAGE_CONTRACT_CHANGED")
        self.assertEqual(row.clicks, billpay._EXPAND_MAX_CLICKS)

    def test_no_target_button_skips_expansion(self) -> None:
        # 未発行の精算回など: 判定は後段の _document_button に任せる。
        row = FakeRow(opened=False, has_target=False)
        _run(row)
        self.assertEqual(row.clicks, 0)


if __name__ == "__main__":
    unittest.main()
