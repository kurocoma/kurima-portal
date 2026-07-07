"""logs/ 保持期間クリーンアップ（S6: log_retention）の単体テスト。

対象:
- 保持日数を超えた execution_runs の run フォルダが削除され、新しいものは残ること
- 保持日数を超えた B2 デバッグ出力（スクショ・HTML）が削除されること
- 保持日数 0 以下でクリーンアップが無効になること（何も消さない）
- history.jsonl のコンパクション（行数上限超過分を古い側から削る）

実フォルダへは書き込まず、一時フォルダを logs ルートとして実行する。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import progress_jobs as progress_jobs_module  # noqa: E402
from portal_app.services.log_retention import cleanup_old_logs  # noqa: E402
from portal_app.services.progress_jobs import compact_job_history  # noqa: E402


def _make_file(path: Path, *, age_days: float | None = None, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if age_days is not None:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))


def _set_dir_age(path: Path, age_days: float) -> None:
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))


class CleanupOldLogsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs_root = Path(self._tmp.name)

    def test_old_run_dirs_are_removed_and_new_ones_kept(self) -> None:
        old_run = self.logs_root / "execution_runs" / "yamato" / "20250101_000000_old"
        new_run = self.logs_root / "execution_runs" / "yamato" / "20260707_000000_new"
        _make_file(old_run / "events.jsonl", age_days=40)
        _make_file(old_run / "summary.json", age_days=40)
        _set_dir_age(old_run, 40)
        _make_file(new_run / "events.jsonl", age_days=1)

        result = cleanup_old_logs(self.logs_root, days=30)

        self.assertTrue(result["enabled"])
        self.assertEqual(result["removed_run_dirs"], 1)
        self.assertFalse(old_run.exists(), "30日超の run フォルダは削除されること")
        self.assertTrue(new_run.exists(), "保持期間内の run フォルダは残ること")

    def test_old_debug_files_are_removed(self) -> None:
        debug_dir = self.logs_root / "next_engine_yamato" / "b2_import_debug"
        _make_file(debug_dir / "old_screenshot.png", age_days=45)
        _make_file(debug_dir / "old_page.html", age_days=45)
        _make_file(debug_dir / "new_screenshot.png", age_days=2)

        result = cleanup_old_logs(self.logs_root, days=30)

        self.assertEqual(result["removed_debug_files"], 2)
        self.assertFalse((debug_dir / "old_screenshot.png").exists())
        self.assertFalse((debug_dir / "old_page.html").exists())
        self.assertTrue((debug_dir / "new_screenshot.png").exists())

    def test_zero_days_disables_cleanup(self) -> None:
        old_run = self.logs_root / "execution_runs" / "yamato" / "20250101_000000_old"
        _make_file(old_run / "events.jsonl", age_days=400)
        _set_dir_age(old_run, 400)

        result = cleanup_old_logs(self.logs_root, days=0)

        self.assertFalse(result["enabled"])
        self.assertTrue(old_run.exists(), "無効化時は何も削除しないこと")

    def test_days_from_env_when_not_specified(self) -> None:
        old_run = self.logs_root / "execution_runs" / "inventory" / "20250101_000000_old"
        _make_file(old_run / "events.jsonl", age_days=10)
        _set_dir_age(old_run, 10)
        with mock.patch.dict(os.environ, {"KURIMA_LOG_RETENTION_DAYS": "7"}):
            result = cleanup_old_logs(self.logs_root)
        self.assertEqual(result["days"], 7)
        self.assertFalse(old_run.exists(), "env の保持日数（7日）が効くこと")


class CompactJobHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.history_path = Path(self._tmp.name) / "jobs" / "history.jsonl"

    def test_compaction_keeps_newest_lines(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f'{{"job_id": "{index}"}}' for index in range(10)]
        self.history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with mock.patch.object(progress_jobs_module, "JOB_HISTORY_PATH", self.history_path):
            removed = compact_job_history(max_lines=4)

        self.assertEqual(removed, 6)
        kept = self.history_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(kept, lines[-4:], "新しい側（末尾）の行だけが残ること")

    def test_no_compaction_when_under_limit(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text('{"job_id": "1"}\n', encoding="utf-8")
        with mock.patch.object(progress_jobs_module, "JOB_HISTORY_PATH", self.history_path):
            self.assertEqual(compact_job_history(max_lines=100), 0)
        self.assertEqual(
            self.history_path.read_text(encoding="utf-8"), '{"job_id": "1"}\n'
        )

    def test_missing_file_and_disabled_limit(self) -> None:
        with mock.patch.object(progress_jobs_module, "JOB_HISTORY_PATH", self.history_path):
            self.assertEqual(compact_job_history(max_lines=100), 0)
            self.assertEqual(compact_job_history(max_lines=0), 0)


if __name__ == "__main__":
    unittest.main()
