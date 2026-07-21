"""Yahoo!自動ログイン `_attempt_yahoo_login` のSPA描画待ちの単体テスト。

2026-07-14 の実行（run_id=84feb6bb…）が「Yahoo!ログインフォームのID欄を
特定できません。」で起動4秒で失敗した回帰の再現。ログイン画面
（account.line.biz チューザー / login.yahoo.co.jp）は domcontentloaded 後に
SPA描画されるため、描画を待たずにDOMを1回走査するだけの実装では必ず失敗する。

FakePage は仮想時計（wait_for_timeout で進む）で「要素が◯ms後に描画される」
画面遷移を再現する。実サイト・実ブラウザ・ファイルシステムには一切触れない。
access_analytics_yahoo / billing_statements_yahoo は同一実装のコピーのため
両方を同じシナリオで検証する。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402

from portal_app.services import access_analytics_yahoo  # noqa: E402
from portal_app.services import billing_statements_yahoo  # noqa: E402

_ENV = {
    "KURIMA_YAHOO_LOGIN_ID": "test-id@example.com",
    "KURIMA_YAHOO_LOGIN_PASSWORD": "test-pass",
}

STORE_URL = "https://pro.store.yahoo.co.jp/pro.test-store/sales_manage/item_report"
LOGIN_URL = "https://login.yahoo.co.jp/config/login"
PASSWORD_URL = "https://login.yahoo.co.jp/config/login/password"
CHOOSER_URL = "https://account.line.biz/login?redirect=..."


class FakeElement:
    """appears_at（画面表示からの経過ms）以降にだけ可視になる要素。"""

    def __init__(self, *, appears_at: int = 0, on_click=None, text: str = "") -> None:
        self.appears_at = appears_at
        self.on_click = on_click
        self.text = text
        self.filled: list[str] = []


class FakeScreen:
    def __init__(self, url: str, elements: dict) -> None:
        self.url = url
        self.elements = dict(elements)
        self.elements.setdefault("body", FakeElement())


class FakeLocator:
    def __init__(self, page: "FakePage", key) -> None:
        self._page = page
        self._key = key

    @property
    def first(self) -> "FakeLocator":
        return self

    @property
    def last(self) -> "FakeLocator":
        return self

    def nth(self, index: int) -> "FakeLocator":
        return self

    def _visible_element(self) -> FakeElement | None:
        element = self._page.current_screen.elements.get(self._key)
        if element is None:
            return None
        if self._page.now - self._page.entered_at < element.appears_at:
            return None
        return element

    async def count(self) -> int:
        return 1 if self._visible_element() is not None else 0

    async def is_visible(self) -> bool:
        return self._visible_element() is not None

    async def inner_text(self) -> str:
        element = self._visible_element()
        return element.text if element is not None else ""

    async def fill(self, value: str) -> None:
        element = self._visible_element()
        assert element is not None, f"fill対象 {self._key} が不可視です"
        element.filled.append(value)

    async def click(self) -> None:
        element = self._visible_element()
        assert element is not None, f"click対象 {self._key} が不可視です"
        if element.on_click is not None:
            element.on_click()

    async def wait_for(self, *, state: str = "visible", timeout: float = 30_000) -> None:
        waited = 0
        while waited <= timeout:
            if self._visible_element() is not None:
                return
            await self._page.wait_for_timeout(100)
            waited += 100
        raise PlaywrightTimeoutError(f"timeout: {self._key}")


class FakePage:
    """仮想時計つきの画面遷移モック。schedule で時刻起点の自動遷移も再現する。"""

    def __init__(
        self,
        screens: dict[str, FakeScreen],
        initial: str,
        schedule: list[tuple[int, str]] | None = None,
    ) -> None:
        self.screens = screens
        self.state = initial
        self.now = 0
        self.entered_at = 0
        self.schedule = sorted(schedule or [])
        self.goto_urls: list[str] = []

    @property
    def current_screen(self) -> FakeScreen:
        return self.screens[self.state]

    @property
    def url(self) -> str:
        return self.current_screen.url

    def switch(self, state: str) -> None:
        self.state = state
        self.entered_at = self.now

    async def wait_for_timeout(self, ms: float) -> None:
        self.now += ms
        while self.schedule and self.schedule[0][0] <= self.now:
            _, state = self.schedule.pop(0)
            self.switch(state)

    async def wait_for_load_state(self, state: str = "load") -> None:
        return None

    async def goto(self, url: str, wait_until: str | None = None, **_: object) -> None:
        self.goto_urls.append(url)
        for name, screen in self.screens.items():
            if screen.url == url:
                self.switch(name)
                return

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_role(self, role: str, *, name=None, **_: object) -> FakeLocator:
        pattern = getattr(name, "pattern", name)
        return FakeLocator(self, ("role", role, pattern))

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, ("text", text))


def _build_login_screens(page_holder: dict, *, id_appears_at: int) -> dict[str, FakeScreen]:
    """login.yahoo.co.jp のID→パスワード2画面＋ストア画面を組み立てる。"""

    def to_password() -> None:
        page_holder["page"].switch("password")

    def to_store() -> None:
        page_holder["page"].switch("store")

    id_field = FakeElement(appears_at=id_appears_at)
    password_field = FakeElement(appears_at=1_000)
    return {
        "login": FakeScreen(
            LOGIN_URL,
            {
                'input[name="handle"]': id_field,
                'button[type="submit"]': FakeElement(
                    appears_at=id_appears_at, on_click=to_password
                ),
            },
        ),
        "password": FakeScreen(
            PASSWORD_URL,
            {
                'input[name="password"]': password_field,
                'button[type="submit"]': FakeElement(appears_at=1_000, on_click=to_store),
            },
        ),
        "store": FakeScreen(STORE_URL, {}),
    }


class YahooLoginFormWaitTest(unittest.TestCase):
    """SPA描画が遅いログイン画面でも自動ログインが完走することを検証する。"""

    modules = (access_analytics_yahoo, billing_statements_yahoo)

    def _run(self, module, page: FakePage) -> None:
        with mock.patch.dict(os.environ, _ENV):
            asyncio.run(module._attempt_yahoo_login(page, target_url=STORE_URL))

    def test_login_form_rendered_after_delay(self) -> None:
        """回帰: ID欄が2.5秒後に描画されてもログインを完走する。"""
        for module in self.modules:
            with self.subTest(module=module.__name__):
                holder: dict = {}
                screens = _build_login_screens(holder, id_appears_at=2_500)
                page = FakePage(screens, "login")
                holder["page"] = page
                self._run(module, page)
                self.assertEqual(
                    screens["login"].elements['input[name="handle"]'].filled,
                    [_ENV["KURIMA_YAHOO_LOGIN_ID"]],
                )
                self.assertEqual(
                    screens["password"].elements['input[name="password"]'].filled,
                    [_ENV["KURIMA_YAHOO_LOGIN_PASSWORD"]],
                )
                self.assertEqual(page.url, STORE_URL)

    def test_line_biz_chooser_rendered_after_delay(self) -> None:
        """account.line.bizチューザーのリンクが2秒後に描画されても選択できる。"""
        for module in self.modules:
            with self.subTest(module=module.__name__):
                holder: dict = {}
                screens = _build_login_screens(holder, id_appears_at=1_500)

                def to_login() -> None:
                    holder["page"].switch("login")

                screens["chooser"] = FakeScreen(
                    CHOOSER_URL,
                    {
                        ':is(a, button):has-text("Yahoo! JAPAN ID")': FakeElement(
                            appears_at=2_000, on_click=to_login
                        ),
                    },
                )
                page = FakePage(screens, "chooser")
                holder["page"] = page
                self._run(module, page)
                self.assertEqual(page.url, STORE_URL)

    def test_password_only_reauth_screen(self) -> None:
        """ID記憶済み・再認証画面（パスワード欄のみ）でも完走する。"""
        for module in self.modules:
            with self.subTest(module=module.__name__):
                holder: dict = {}
                password_field = FakeElement(appears_at=2_000)

                def to_store() -> None:
                    holder["page"].switch("store")

                screens = {
                    "reauth": FakeScreen(
                        PASSWORD_URL,
                        {
                            'input[name="password"]': password_field,
                            'button[type="submit"]': FakeElement(
                                appears_at=2_000, on_click=to_store
                            ),
                        },
                    ),
                    "store": FakeScreen(STORE_URL, {}),
                }
                page = FakePage(screens, "reauth")
                holder["page"] = page
                self._run(module, page)
                self.assertEqual(password_field.filled, [_ENV["KURIMA_YAHOO_LOGIN_PASSWORD"]])
                self.assertEqual(page.url, STORE_URL)

    def test_sso_passthrough_returns_without_error(self) -> None:
        """settle待ちの間にSSOでストアへ戻った場合はフォーム探索せず戻る。"""
        for module in self.modules:
            with self.subTest(module=module.__name__):
                screens = {
                    "interstitial": FakeScreen(CHOOSER_URL, {}),
                    "store": FakeScreen(STORE_URL, {}),
                }
                page = FakePage(screens, "interstitial", schedule=[(3_000, "store")])
                self._run(module, page)
                self.assertEqual(page.goto_urls, [])
                self.assertEqual(page.url, STORE_URL)

    def test_form_never_appears_raises_with_url(self) -> None:
        """フォームが描画されないままなら従来どおり手動ログイン誘導で失敗する。"""
        for module, error_type in (
            (access_analytics_yahoo, access_analytics_yahoo.YahooAccessAnalyticsError),
            (billing_statements_yahoo, billing_statements_yahoo.YahooBillingStatementsError),
        ):
            with self.subTest(module=module.__name__):
                page = FakePage({"login": FakeScreen(LOGIN_URL, {})}, "login")
                with self.assertRaises(error_type) as captured:
                    self._run(module, page)
                self.assertEqual(captured.exception.state, "AUTH_REQUIRED_MANUAL")
                self.assertIn("ID欄を特定できません", str(captured.exception))
                self.assertIn(LOGIN_URL, str(captured.exception))

    def test_missing_credentials_returns_immediately(self) -> None:
        """環境変数未設定なら何もせず戻る（従来挙動の維持）。"""
        for module in self.modules:
            with self.subTest(module=module.__name__):
                page = FakePage({"login": FakeScreen(LOGIN_URL, {})}, "login")
                empty = {
                    "KURIMA_YAHOO_LOGIN_ID": "",
                    "KURIMA_YAHOO_LOGIN_PASSWORD": "",
                }
                with mock.patch.dict(os.environ, empty):
                    asyncio.run(module._attempt_yahoo_login(page, target_url=STORE_URL))
                self.assertEqual(page.now, 0)
                self.assertEqual(page.goto_urls, [])


if __name__ == "__main__":
    unittest.main()
