"""同一ワークフローの二重実行ガード（S3: progress_jobs 層）の単体テスト。

対象:
- 同じ workflow の実行中（queued/running）ジョブがあると start() が DuplicateJobError を送出すること
- 拒否メッセージが日本語で、実行中ジョブの job_id が例外に載ること
- 異なる workflow は並行して開始できること
- ジョブ完了後は同じ workflow を再び開始できること（ガードが解除される）
- 失敗ジョブの snapshot に日本語対処ガイド hint が入ること（U1 の結線確認）

実フォルダへ書き込まないよう、ジョブ詳細ログ（execution_runs）と履歴（history.jsonl）、
共有ログ出力先（KURIMA_LOG_DIR）をすべて一時フォルダへ向けて実行する。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 共有ログ（portal-run/error）が実際の SharePoint 同期フォルダへ書かれないよう、
# ロガー初回構成より前に出力先を一時フォルダへ固定する。
_LOG_DIR_FOR_TESTS = tempfile.mkdtemp(prefix="kurima-test-logs-")
os.environ.setdefault("KURIMA_LOG_DIR", _LOG_DIR_FOR_TESTS)

from portal_app.services import execution_logger as execution_logger_module  # noqa: E402
from portal_app.services import progress_jobs as progress_jobs_module  # noqa: E402
from portal_app.services.progress_jobs import DuplicateJobError, ProgressJobStore  # noqa: E402


def _wait_for(condition, timeout_sec: float = 10.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


class ProgressJobsGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        # worker スレッドの終了直後にログ書き込みが残ることがあるため、
        # 消し損ねを許容する（Windows の rmtree 競合でテストを落とさない）
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        tmp_root = Path(self._tmp.name)
        # ジョブ詳細ログと履歴を一時フォルダへ（実 logs/ を汚さない）
        patch_runs = mock.patch.object(
            execution_logger_module, "EXECUTION_LOG_ROOT", tmp_root / "execution_runs"
        )
        patch_history = mock.patch.object(
            progress_jobs_module, "JOB_HISTORY_PATH", tmp_root / "jobs" / "history.jsonl"
        )
        patch_runs.start()
        patch_history.start()
        self.addCleanup(patch_runs.stop)
        self.addCleanup(patch_history.stop)

        self.store = ProgressJobStore()
        self._release_events: list[Event] = []
        self._started_job_ids: list[str] = []

    def tearDown(self) -> None:
        # ブロック中の worker を必ず解放し、終了処理（ログ書き込み）まで待ってから片付ける
        for event in self._release_events:
            event.set()
        for job_id in self._started_job_ids:
            _wait_for(
                lambda job_id=job_id: (self.store.snapshot(job_id) or {}).get("status")
                in {"completed", "failed", "cancelled"}
            )

    def _start_blocking_job(self, workflow: str, title: str = "テストジョブ") -> tuple[str, Event]:
        release = Event()
        self._release_events.append(release)

        def worker(job_id: str) -> None:
            release.wait(timeout=30)
            self.store.finish(job_id, message="完了しました。")

        job_id = self.store.start(
            title=title,
            steps=[("run", "実行")],
            worker=worker,
            workflow=workflow,
        )
        self._started_job_ids.append(job_id)
        return job_id, release

    def test_second_start_of_same_workflow_is_rejected(self) -> None:
        first_id, release = self._start_blocking_job("clickpost_full_run", title="クリックポスト実行")

        with self.assertRaises(DuplicateJobError) as ctx:
            self._start_blocking_job("clickpost_full_run")

        message = str(ctx.exception)
        self.assertIn("同じ処理", message, "日本語の拒否メッセージであること")
        self.assertIn("実行中", message)
        self.assertIn("/jobs", message, "確認先（実行履歴）への導線があること")
        self.assertEqual(ctx.exception.existing_job_id, first_id)
        self.assertEqual(ctx.exception.workflow, "clickpost_full_run")

        release.set()

    def test_different_workflow_can_run_in_parallel(self) -> None:
        _, release_a = self._start_blocking_job("workflow_a")
        job_b, release_b = self._start_blocking_job("workflow_b")
        self.assertIsNotNone(job_b, "別 workflow は排他対象外で開始できること")
        release_a.set()
        release_b.set()

    def test_guard_is_released_after_completion(self) -> None:
        job_id, release = self._start_blocking_job("inventory_fetch")
        release.set()
        self.assertTrue(
            _wait_for(lambda: (self.store.snapshot(job_id) or {}).get("status") == "completed"),
            "先行ジョブが完了すること",
        )

        second_id, release2 = self._start_blocking_job("inventory_fetch")
        self.assertNotEqual(second_id, job_id, "完了後は同じ workflow を再実行できること")
        release2.set()

    def test_failed_job_snapshot_contains_japanese_hint(self) -> None:
        def worker(job_id: str) -> None:
            raise TimeoutError("Timeout 60000ms exceeded.")

        job_id = self.store.start(
            title="失敗するジョブ",
            steps=[("run", "実行")],
            worker=worker,
            workflow="failing_workflow",
        )
        self.assertTrue(
            _wait_for(lambda: (self.store.snapshot(job_id) or {}).get("status") == "failed"),
            "ジョブが失敗として確定すること",
        )
        snapshot = self.store.snapshot(job_id) or {}
        self.assertEqual(snapshot.get("error"), "Timeout 60000ms exceeded.")
        self.assertIn("時間切れ", snapshot.get("hint") or "", "対処ガイド（U1）が snapshot に載ること")


if __name__ == "__main__":
    unittest.main()
