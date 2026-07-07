"""ブラウザ操作タイムアウトの設定一元化（S5: portal_app.settings）の単体テスト。

対象:
- env 未設定時は呼び出し元の現行既定値のまま（挙動を変えない）
- KURIMA_NAV_TIMEOUT_MS / KURIMA_DOWNLOAD_TIMEOUT_MS を設定すると反映される
- 不正値（数値でない・0 以下）は既定値へフォールバックする（起動を壊さない）
- LAN アクセス制御（O3: client_allowed）の許可判定
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.settings import (  # noqa: E402
    client_allowed,
    download_timeout_ms,
    nav_timeout_ms,
)


class TimeoutSettingsTest(unittest.TestCase):
    """S5: env によるタイムアウトの一元上書き。"""

    def test_defaults_match_current_hardcoded_values(self):
        # env 未設定なら現行値のまま（goto系 60000 / ダウンロード系は呼び出し元の値）
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(nav_timeout_ms(), 60000)
            self.assertEqual(download_timeout_ms(90000), 90000)
            self.assertEqual(download_timeout_ms(180000), 180000)

    def test_env_overrides_all_call_sites(self):
        env = {"KURIMA_NAV_TIMEOUT_MS": "120000", "KURIMA_DOWNLOAD_TIMEOUT_MS": "300000"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(nav_timeout_ms(), 120000)
            # サイト別の既定が異なっていても、env 設定時は一括で同じ値になる
            self.assertEqual(download_timeout_ms(90000), 300000)
            self.assertEqual(download_timeout_ms(180000), 300000)

    def test_invalid_values_fall_back_to_default(self):
        env = {"KURIMA_NAV_TIMEOUT_MS": "abc", "KURIMA_DOWNLOAD_TIMEOUT_MS": "-5"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(nav_timeout_ms(), 60000)
            self.assertEqual(download_timeout_ms(90000), 90000)


class ClientAllowedTest(unittest.TestCase):
    """O3: KURIMA_ALLOWED_CLIENTS による許可判定。"""

    def test_unset_allows_everyone(self):
        # 未設定は無制限（現行互換）
        self.assertTrue(client_allowed("192.168.1.50", []))

    def test_exact_ip_and_cidr_and_prefix(self):
        rules = ["192.168.1.10", "192.168.20.0/24", "10.0."]
        self.assertTrue(client_allowed("192.168.1.10", rules))  # 完全一致
        self.assertTrue(client_allowed("192.168.20.77", rules))  # CIDR
        self.assertTrue(client_allowed("10.0.5.6", rules))  # プレフィックス
        self.assertFalse(client_allowed("192.168.1.11", rules))
        self.assertFalse(client_allowed("172.16.0.1", rules))

    def test_loopback_always_allowed(self):
        # ホストPC自身は設定ミスでも締め出さない（フェイルセーフ）
        self.assertTrue(client_allowed("127.0.0.1", ["203.0.113.1"]))
        self.assertTrue(client_allowed("::1", ["203.0.113.1"]))

    def test_invalid_rule_is_ignored(self):
        # 不正な CIDR はそのルールだけ無視する（1つの typo で全員 403 にしない）
        rules = ["not-a-cidr/99", "192.168.1.10"]
        self.assertTrue(client_allowed("192.168.1.10", rules))
        self.assertFalse(client_allowed("192.168.1.11", rules))


if __name__ == "__main__":
    unittest.main()
