"""クリックポスト決済ループ — 出口判定の再確認テスト（2026-08-17 実障害対策）。

実障害: 6件目の決済後、ウォレット(SBPS)からクリックポストへ戻る描画が通常探索
（3秒+1秒）より遅く、「支払いボタンなし＝全件完了」と誤判定して13件中7件が
未決済のままジョブ正常終了した（直後の残数カウントでは7件見えていた）。

固定する仕様:
  - 期待件数（インポート件数）に未達で支払いボタンが見えない場合、読み込み完了を
    待って1回だけ長めに再探索し、遅れて描画されたボタンも決済する
  - 再探索しても見つからなければ無限ループせずに打ち切る
  - expected_payments 未指定（従来呼び出し）では従来どおり即打ち切る
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import clickpost as cp


class FakeButtonLocator:
    """支払いボタン群。可視数は FakePage が管理する。"""

    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def count(self) -> int:
        return self._page.visible_buttons

    async def is_visible(self, timeout: int | None = None) -> bool:
        return self._page.visible_buttons > 0

    def nth(self, index: int) -> "FakeButtonLocator":
        return self

    async def click(self, **kwargs) -> None:
        self._page.visible_buttons = max(0, self._page.visible_buttons - 1)


class FakeEmptyLocator:
    """「次の支払い」リンク等、常に存在しない要素。"""

    async def count(self) -> int:
        return 0

    async def is_visible(self, timeout: int | None = None) -> bool:
        return False

    def nth(self, index: int) -> "FakeEmptyLocator":
        return self

    async def click(self, **kwargs) -> None:
        raise AssertionError("存在しない要素はクリックされないはず")


class FakePage:
    """決済ページの描画レースを再現する最小ページ。

    hidden_buttons は「サーバー側には存在するがまだ描画されていないボタン」。
    wait_for_load_state が呼ばれたタイミングで可視化される（＝読み込み完了待ちを
    してから再探索すれば見つかる、という実障害の状況を模す）。
    """

    def __init__(self, visible_buttons: int) -> None:
        self.visible_buttons = visible_buttons
        self.hidden_buttons = 0
        self.load_state_calls = 0

    def locator(self, selector: str):
        if "ywallet_button" in selector:
            return FakeButtonLocator(self)
        return FakeEmptyLocator()

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        self.load_state_calls += 1
        if self.hidden_buttons:
            self.visible_buttons += self.hidden_buttons
            self.hidden_buttons = 0

    async def wait_for_timeout(self, ms: int) -> None:
        await asyncio.sleep(0)


def _make_client(page: FakePage, *, reveal_after_first_payment: bool):
    """__init__（資格情報読込）を通さずにクライアントを作り、決済処理をスタブ化する。"""
    client = cp.ClickPostClient.__new__(cp.ClickPostClient)
    completed: list[int] = []

    async def fake_complete_wallet_payment(target_page) -> None:
        completed.append(1)
        if reveal_after_first_payment and len(completed) == 1:
            # 決済1件目の完了後、残ボタンは「未描画」状態になる（実障害の再現）。
            target_page.hidden_buttons = 1

    client._complete_wallet_payment = fake_complete_wallet_payment
    return client


def _fast_constants():
    """テストを高速化するための探索タイムアウト短縮（判定ロジックは不変）。"""
    return (
        mock.patch.object(cp, "PAYMENT_BUTTON_POLL_TIMEOUT_MS", 200),
        mock.patch.object(cp, "NEXT_PAYMENT_POLL_TIMEOUT_MS", 100),
        mock.patch.object(cp, "PAYMENT_BUTTON_RECHECK_TIMEOUT_MS", 500),
    )


class PaymentLoopRecheckTest(unittest.TestCase):
    def _run(self, client, page, *, max_payments: int = 20, expected: int | None = None):
        patches = _fast_constants()
        for patch in patches:
            patch.start()
        try:
            return asyncio.run(
                client._complete_available_wallet_payments(
                    page, max_payments, expected_payments=expected
                )
            )
        finally:
            for patch in patches:
                patch.stop()

    def test_late_rendered_button_is_paid_after_recheck(self) -> None:
        """2026-08-17 の再現: 描画遅延で消えたボタンを再確認して決済し切る。"""
        page = FakePage(visible_buttons=1)
        client = _make_client(page, reveal_after_first_payment=True)
        attempts, completed, remaining = self._run(client, page, expected=2)
        self.assertEqual(attempts, 2)
        self.assertEqual(completed, 2)
        self.assertEqual(remaining, 0)
        # 再確認前に読み込み完了待ちをしている（描画待ちを timeout 頼みにしない）。
        self.assertGreaterEqual(page.load_state_calls, 2)

    def test_recheck_does_not_loop_forever_when_buttons_never_appear(self) -> None:
        """期待件数に未達でも、再確認1回で見つからなければ打ち切る。"""
        page = FakePage(visible_buttons=0)
        client = _make_client(page, reveal_after_first_payment=False)
        attempts, completed, remaining = self._run(client, page, expected=5)
        self.assertEqual(attempts, 0)
        self.assertEqual(completed, 0)
        self.assertEqual(remaining, 0)
        # 再確認（読み込み完了待ち）は1回だけ行われる。
        self.assertEqual(page.load_state_calls, 1)

    def test_legacy_callers_without_expected_break_immediately(self) -> None:
        """expected_payments 未指定の従来呼び出しは従来どおり即終了する。"""
        page = FakePage(visible_buttons=0)
        client = _make_client(page, reveal_after_first_payment=False)
        attempts, completed, remaining = self._run(client, page, expected=None)
        self.assertEqual((attempts, completed, remaining), (0, 0, 0))
        self.assertEqual(page.load_state_calls, 0)

    def test_expected_reached_stops_without_recheck(self) -> None:
        """全件決済済みなら再確認せずに終了する（余計な30秒待ちを発生させない）。"""
        page = FakePage(visible_buttons=2)
        client = _make_client(page, reveal_after_first_payment=False)
        attempts, completed, remaining = self._run(client, page, expected=2)
        self.assertEqual(attempts, 2)
        self.assertEqual(completed, 2)
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
