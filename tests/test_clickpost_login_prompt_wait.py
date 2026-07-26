"""クリックポスト Yahoo!ログインのSPA描画待ち（_wait_yahoo_login_prompt）のテスト。

2026-07-24 実障害の回帰: Yahoo!ログイン画面はSPA描画で入力欄が遅れて現れるが、
旧実装の一発判定は locator.count()==0 のとき timeout を待たず即 False になる
（_first_visible_locator の仕様）ため、描画が遅い端末では自動入力が丸ごと
スキップされ、前面実行では人が手入力するまで止まって見えた。
仮想時計つき FakePage で「欄が◯ms後に現れる」画面を再現して検証する
（tests/test_yahoo_login_form_wait.py と同じ流儀。実ブラウザには触れない）。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from portal_app.services.clickpost import (
    _YAHOO_LOGIN_ID_SELECTOR,
    _YAHOO_LOGIN_PASSWORD_SELECTOR,
    _wait_yahoo_login_prompt,
)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    def _appears_at(self) -> int | None:
        return self._page.elements.get(self._selector)

    async def count(self) -> int:
        appears_at = self._appears_at()
        if appears_at is None:
            return 0
        return 1 if self._page.now >= appears_at else 0

    async def is_visible(self, timeout: float | None = None) -> bool:
        return await self.count() == 1


class FakePage:
    """仮想時計で「要素が◯ms後に描画される」SPA画面を再現する。"""

    def __init__(self, elements: dict[str, int], *, body_text: str = "") -> None:
        self.elements = elements
        self.body_text = body_text
        self.now = 0

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def wait_for_timeout(self, ms: float) -> None:
        self.now += ms

    async def wait_for_function(self, expression: str, arg=None, timeout: float = 0) -> None:
        if arg is not None and arg in self.body_text:
            return
        self.now += timeout
        raise PlaywrightTimeoutError("timeout")


class WaitYahooLoginPromptTest(unittest.TestCase):
    def test_id_field_rendered_after_delay_is_detected(self):
        """回帰: ID欄が3秒後に描画されても 'id' を返す（旧実装は即スキップ）。"""
        page = FakePage({_YAHOO_LOGIN_ID_SELECTOR: 3_000})
        state = asyncio.run(_wait_yahoo_login_prompt(page, timeout_ms=30_000))
        self.assertEqual(state, "id")
        self.assertGreaterEqual(page.now, 3_000)

    def test_password_only_reauth_screen(self):
        """ID記憶済みの再認証画面（パスワード欄のみ・遅延描画）で 'password' を返す。"""
        page = FakePage({_YAHOO_LOGIN_PASSWORD_SELECTOR: 2_000})
        state = asyncio.run(_wait_yahoo_login_prompt(page, timeout_ms=30_000))
        self.assertEqual(state, "password")

    def test_already_logged_in_returns_mypage_immediately(self):
        page = FakePage({}, body_text="マイページ ようこそ")
        state = asyncio.run(_wait_yahoo_login_prompt(page, timeout_ms=30_000))
        self.assertEqual(state, "mypage")
        self.assertEqual(page.now, 0)

    def test_unknown_screen_times_out_to_manual_fallback(self):
        """CAPTCHA等の未知画面は '' を返し、呼び出し元の手動ログイン待ちに委ねる。"""
        page = FakePage({})
        state = asyncio.run(_wait_yahoo_login_prompt(page, timeout_ms=5_000))
        self.assertEqual(state, "")
        self.assertGreaterEqual(page.now, 5_000)


if __name__ == "__main__":
    unittest.main()
