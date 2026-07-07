"""エラーメッセージの日本語対処ガイド変換（U1: error_hints）の単体テスト。

対象:
- Playwright の時間切れ（例外型 / メッセージ文字列の両経路）が時間切れガイドになること
- ヤマトB2ログイン失敗（B2LoginError 相当）が state 別のガイドになること
- 保存先未検出（find_portal_paths の FileNotFoundError）が KURIMA_PORTAL_ROOT 案内になること
- ブラウザ実体なし / 接続エラー / NotImplementedError / CP932 変換不能のガイド
- 未知のエラー・空メッセージは None（画面は従来どおり生メッセージのみ）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.error_hints import hint_for_exception, hint_for_message  # noqa: E402


class B2LoginError(RuntimeError):
    """yamato_b2_import.B2LoginError と同じ形（型名 + state 属性）を持つテスト用クラス。

    error_hints は Playwright を import しないよう duck-typing（型名と state 属性）で
    判定するため、本物を import せずに同じ形で検証できる。
    """

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


class HintForExceptionTest(unittest.TestCase):
    def test_timeout_error(self) -> None:
        hint = hint_for_exception(TimeoutError("Timeout 60000ms exceeded."))
        self.assertIsNotNone(hint)
        self.assertIn("時間切れ", hint)
        self.assertIn("もう一度実行", hint)

    def test_b2_login_error_states(self) -> None:
        expectations = {
            "still_on_login": "YAMATO_B2_LOGIN_ID",
            "needs_2fa": "ワンタイムパスワード",
            "system_error": "システムエラー",
            "time_outside": "サービス時間外",
        }
        for state, keyword in expectations.items():
            with self.subTest(state=state):
                hint = hint_for_exception(B2LoginError(state, "B2ログインに失敗しました。"))
                self.assertIsNotNone(hint)
                self.assertIn(keyword, hint)

    def test_not_implemented_error(self) -> None:
        hint = hint_for_exception(NotImplementedError())
        self.assertIsNotNone(hint)
        self.assertIn("再起動", hint)

    def test_portal_root_not_found(self) -> None:
        exc = FileNotFoundError(
            "くりまポータルの同期フォルダを検出できませんでした。"
            "KURIMA_PORTAL_ROOT（または PORTAL_ROOT）を設定するか、"
            "SharePoint ライブラリを同期してください。"
        )
        hint = hint_for_exception(exc)
        self.assertIsNotNone(hint)
        self.assertIn("KURIMA_PORTAL_ROOT", hint)

    def test_generic_file_not_found(self) -> None:
        exc = FileNotFoundError("data で始まる受注明細 CSV が見つかりません: C:/tmp")
        hint = hint_for_exception(exc)
        self.assertIsNotNone(hint)
        self.assertIn("同期", hint)

    def test_unicode_encode_error(self) -> None:
        exc = UnicodeEncodeError("cp932", "髙", 0, 1, "illegal multibyte sequence")
        hint = hint_for_exception(exc)
        self.assertIsNotNone(hint)
        self.assertIn("環境依存文字", hint)

    def test_unknown_exception_returns_none(self) -> None:
        self.assertIsNone(hint_for_exception(ValueError("something unexpected")))
        self.assertIsNone(hint_for_exception(None))


class HintForMessageTest(unittest.TestCase):
    def test_playwright_timeout_text(self) -> None:
        hint = hint_for_message("Timeout 60000ms exceeded.")
        self.assertIsNotNone(hint)
        self.assertIn("時間切れ", hint)

    def test_browser_executable_missing(self) -> None:
        hint = hint_for_message(
            "BrowserType.launch: Executable doesn't exist at C:\\ms-playwright\\chrome.exe"
        )
        self.assertIsNotNone(hint)
        self.assertIn("playwright install chromium", hint)

    def test_connection_error(self) -> None:
        hint = hint_for_message("page.goto: net::ERR_CONNECTION_REFUSED at https://example.com")
        self.assertIsNotNone(hint)
        self.assertIn("接続", hint)

    def test_login_failure_text(self) -> None:
        hint = hint_for_message("Next Engine のログインに失敗しました。")
        self.assertIsNotNone(hint)
        self.assertIn("認証情報", hint)

    def test_unknown_or_empty_returns_none(self) -> None:
        self.assertIsNone(hint_for_message("予期しないエラー"))
        self.assertIsNone(hint_for_message(""))
        self.assertIsNone(hint_for_message(None))


if __name__ == "__main__":
    unittest.main()
