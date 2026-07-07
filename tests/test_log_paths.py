"""共有ログのローテーション（S1）と PC 別ファイル名（S2）の単体テスト。

対象:
- ファイル名にコンピュータ名（または KURIMA_LOG_SUFFIX）が入ること（S2）
- 小さいサイズ上限（0.01MB）でローテーションが実際に起き、世代ファイルができること（S1）
- 上限・世代数の env が不正値のとき既定値へフォールバックすること
- ローテーション失敗（他プロセスがファイルを開いているケース）でも書き込みが継続すること
  （SharePoint 同期フォルダ向けのフェイルセーフ）

グローバルロガー（setup_file_logging のシングルトン）には触れず、
ハンドラ生成部品（_build_file_handler）を直接検証する。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.log_paths import (  # noqa: E402
    _build_file_handler,
    error_log_file_name,
    log_file_suffix,
    run_log_file_name,
)


class LogFileNameTest(unittest.TestCase):
    """S2: 共有ログのファイル名に PC 識別子が入る。"""

    def test_computer_name_in_file_name(self) -> None:
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "TEST-PC-01", "KURIMA_LOG_SUFFIX": ""}):
            self.assertEqual(run_log_file_name(), "portal-run-TEST-PC-01.log")
            self.assertEqual(error_log_file_name(), "portal-error-TEST-PC-01.log")

    def test_suffix_env_overrides_computer_name(self) -> None:
        with mock.patch.dict(
            os.environ, {"COMPUTERNAME": "TEST-PC-01", "KURIMA_LOG_SUFFIX": "kensho-2"}
        ):
            self.assertEqual(run_log_file_name(), "portal-run-kensho-2.log")

    def test_unsafe_characters_are_sanitized(self) -> None:
        with mock.patch.dict(os.environ, {"KURIMA_LOG_SUFFIX": "HOST A/B"}):
            self.assertEqual(log_file_suffix(), "HOST_A_B")

    def test_fallback_when_no_computer_name(self) -> None:
        with mock.patch.dict(os.environ, {"COMPUTERNAME": "", "KURIMA_LOG_SUFFIX": ""}):
            self.assertEqual(log_file_suffix(), "pc")


class RotationTest(unittest.TestCase):
    """S1: サイズ上限でローテーションが起き、世代ファイルが残る。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = Path(self._tmp.name)
        self.formatter = logging.Formatter("%(message)s")

    def _make_logger(self, name: str, handler: logging.Handler) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        self.addCleanup(handler.close)
        self.addCleanup(lambda: logger.removeHandler(handler))
        return logger

    def test_rollover_creates_backup_files(self) -> None:
        path = self.log_dir / "portal-run-TEST.log"
        with mock.patch.dict(
            os.environ, {"KURIMA_LOG_MAX_MB": "0.01", "KURIMA_LOG_BACKUP_COUNT": "2"}
        ):
            handler = _build_file_handler(path, logging.INFO, self.formatter)
        logger = self._make_logger("kurima_test_rotation", handler)

        for index in range(400):  # 100 文字 × 400 行 ≒ 40KB ≫ 上限 10KB
            logger.info("ローテーション検証メッセージ %04d %s", index, "x" * 80)

        backup = path.with_name(path.name + ".1")
        self.assertTrue(path.is_file(), "ベースのログファイルが存在すること")
        self.assertTrue(backup.is_file(), "ローテーションで .1 世代が作られること")
        max_bytes = int(0.01 * 1024 * 1024)
        self.assertLessEqual(path.stat().st_size, max_bytes + 200, "ベースは上限近辺で切られること")

    def test_invalid_env_falls_back_to_defaults(self) -> None:
        path = self.log_dir / "portal-run-DEFAULTS.log"
        with mock.patch.dict(
            os.environ, {"KURIMA_LOG_MAX_MB": "abc", "KURIMA_LOG_BACKUP_COUNT": "-1"}
        ):
            handler = _build_file_handler(path, logging.INFO, self.formatter)
        self.addCleanup(handler.close)
        self.assertEqual(handler.maxBytes, 5 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)

    @unittest.skipUnless(sys.platform == "win32", "Windows のファイルロック挙動に依存するため")
    def test_rollover_failure_does_not_stop_logging(self) -> None:
        """他プロセス相当がファイルを開いていて rename できなくても、書き込みは継続する。"""
        path = self.log_dir / "portal-run-LOCKED.log"
        with mock.patch.dict(
            os.environ, {"KURIMA_LOG_MAX_MB": "0.01", "KURIMA_LOG_BACKUP_COUNT": "2"}
        ):
            handler = _build_file_handler(path, logging.INFO, self.formatter)
        logger = self._make_logger("kurima_test_locked", handler)

        # 別ハンドルで開いたまま（Windows では rename が PermissionError になる）
        with path.open("r", encoding="utf-8"):
            for index in range(400):
                logger.info("ロック中の書き込み %04d %s", index, "x" * 80)

        # ローテはできなくても例外にならず、ログ本体は上限を超えて追記され続けている
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, int(0.01 * 1024 * 1024))


if __name__ == "__main__":
    unittest.main()
