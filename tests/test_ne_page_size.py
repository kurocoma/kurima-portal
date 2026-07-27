"""NE一覧の表示件数プルダウン(#page_sel)最大化のテスト。

2026-07-27 実障害の回帰: Playwright用プロファイルでは表示件数が100のまま起動する
ことがあり、100件超の受注で一覧に出ない行がスナップショット・全選択・ダウンロード
から丸ごと漏れた。ダウンロード前に必ず最大値（通常1000）へ変更する。
FakePage で select の状態を再現して検証する（実ブラウザには触れない）。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.next_engine_downloader import ensure_next_engine_page_size_max


class FakeSelectLocator:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def count(self) -> int:
        return 1 if self._page.options else 0

    async def evaluate(self, expression: str):
        return list(self._page.options)

    async def input_value(self) -> str:
        return self._page.value

    async def select_option(self, value=None, *, label=None) -> None:
        target = value if value is not None else label
        if target not in self._page.options:
            raise ValueError(f"option not found: {target}")
        self._page.value = target
        self._page.selected_values.append(target)


class FakePage:
    def __init__(self, options: list[str], value: str) -> None:
        self.options = options
        self.value = value
        self.selected_values: list[str] = []

    def locator(self, selector: str) -> FakeSelectLocator:
        assert selector == "select#page_sel"
        return FakeSelectLocator(self)

    async def wait_for_load_state(self, state: str, timeout: float = 0) -> None:
        return

    async def wait_for_function(self, expression: str, arg=None, timeout: float = 0) -> None:
        assert self.value == arg

    async def wait_for_timeout(self, ms: float) -> None:
        return


class EnsurePageSizeMaxTest(unittest.TestCase):
    def test_changes_100_to_1000(self):
        """表示件数100なら1000へ変更して True を返す。"""
        page = FakePage(["20", "50", "100", "300", "500", "1000"], "100")
        changed = asyncio.run(ensure_next_engine_page_size_max(page))
        self.assertTrue(changed)
        self.assertEqual(page.value, "1000")
        self.assertEqual(page.selected_values, ["1000"])

    def test_already_1000_is_noop(self):
        """既に1000なら何もしない（再読込を起こさない）。"""
        page = FakePage(["100", "300", "1000"], "1000")
        changed = asyncio.run(ensure_next_engine_page_size_max(page))
        self.assertFalse(changed)
        self.assertEqual(page.selected_values, [])

    def test_picks_largest_available_option(self):
        """1000が無い画面では存在する最大値を選ぶ。"""
        page = FakePage(["20", "50", "100", "500"], "100")
        changed = asyncio.run(ensure_next_engine_page_size_max(page))
        self.assertTrue(changed)
        self.assertEqual(page.value, "500")

    def test_no_pulldown_is_noop(self):
        """プルダウンが無い画面では何もしない。"""
        page = FakePage([], "")
        changed = asyncio.run(ensure_next_engine_page_size_max(page))
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
