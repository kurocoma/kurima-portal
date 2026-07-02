from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock, Thread
from typing import Callable
from uuid import uuid4

from portal_app.services.execution_logger import APP_ROOT, ExecutionLogger, exception_payload


JobWorker = Callable[[str], None]


class JobCancelled(Exception):
    """ユーザーが「中止」を押したことを表す。worker はこれを工程境界で送出して安全に停止する。"""

# ジョブ完了/失敗の履歴を1行1JSONで追記する永続ファイル。
# メモリ内の _jobs はプロセス終了で消えるため、「昨日の実行がどうなったか」を
# サーバー再起動後も追えるようにする。logs/ は gitignore 済み。
JOB_HISTORY_PATH = APP_ROOT / "logs" / "jobs" / "history.jsonl"


def _ensure_subprocess_event_loop_policy() -> None:
    """Windows の worker スレッドで Playwright のサブプロセス起動を可能にする。

    uvicorn プロセスでは event loop policy が SelectorEventLoop 系になっており、
    worker スレッドの asyncio.run が作るループが subprocess 非対応のため、
    Playwright のブラウザ起動が NotImplementedError になる。ProactorEventLoop を保証する。
    （既存ループには影響しない＝新規に作られる worker のループにのみ効く）
    """
    if sys.platform != "win32":
        return
    proactor_policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if proactor_policy is None:
        return
    if not isinstance(asyncio.get_event_loop_policy(), proactor_policy):
        asyncio.set_event_loop_policy(proactor_policy())


@dataclass
class ProgressStep:
    key: str
    label: str
    status: str = "pending"
    detail: str | None = None


@dataclass
class ProgressJob:
    job_id: str
    title: str
    status: str
    message: str
    steps: list[ProgressStep]
    workflow: str = "progress"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, object] | None = None
    log_dir: str | None = None
    log_events_path: str | None = None
    log_summary_path: str | None = None
    cancel_requested: bool = False
    stopped_at_step: str | None = None


class ProgressJobStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[str, ProgressJob] = {}
        self._loggers: dict[str, ExecutionLogger] = {}

    def start(
        self,
        *,
        title: str,
        steps: list[tuple[str, str]],
        worker: JobWorker,
        workflow: str = "progress",
        metadata: dict[str, object] | None = None,
    ) -> str:
        job_id = uuid4().hex
        logger = ExecutionLogger(workflow=workflow, run_id=job_id, title=title, metadata=metadata)
        log_paths = logger.paths_payload()
        job = ProgressJob(
            job_id=job_id,
            title=title,
            status="queued",
            message="開始待ちです。",
            steps=[ProgressStep(key=key, label=label) for key, label in steps],
            workflow=workflow,
            log_dir=log_paths["run_dir"],
            log_events_path=log_paths["events_path"],
            log_summary_path=log_paths["summary_path"],
        )
        with self._lock:
            self._jobs[job_id] = job
            self._loggers[job_id] = logger

        thread = Thread(target=self._run_worker, args=(job_id, worker), daemon=True)
        thread.start()
        return job_id

    def _run_worker(self, job_id: str, worker: JobWorker) -> None:
        _ensure_subprocess_event_loop_policy()
        self.set_running(job_id, "処理を開始しました。")
        try:
            worker(job_id)
        except Exception as exc:
            # 「中止」が要求されていれば、例外（ブラウザ強制終了によるもの等）を失敗ではなく中止として扱う。
            if self.is_cancel_requested(job_id):
                self.mark_cancelled(job_id)
            else:
                self.fail(job_id, str(exc), error_detail=exception_payload(exc))

    def request_cancel(self, job_id: str) -> str | None:
        """中止を要求する。実行中の工程ラベル（＝どこで止めたか）を返す。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in {"completed", "failed", "cancelled"}:
                return None
            job.cancel_requested = True
            running = next((s for s in job.steps if s.status == "running"), None)
            job.stopped_at_step = running.label if running else None
            label = job.stopped_at_step
        self._write_event(job_id, "cancel_requested", status="running", detail=label)
        return label

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_requested)

    def mark_cancelled(self, job_id: str, message: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            where = job.stopped_at_step if job else None
            # 実行中だった工程を failed 表示にして「どこで止めたか」を明示する。
            if job is not None:
                for step in job.steps:
                    if step.status == "running":
                        step.status = "failed"
                        step.detail = "中止"
        msg = message or (f"「{where}」で中止しました。" if where else "処理を中止しました。")
        self._mutate(
            job_id,
            status="cancelled",
            message=msg,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        snapshot = self.snapshot(job_id) or {}
        self._write_event(job_id, "job_cancelled", status="cancelled", detail=msg, level="warn")
        self._write_summary(job_id, snapshot)
        self._append_history(job_id)

    def set_running(self, job_id: str, message: str) -> None:
        self._mutate(job_id, status="running", message=message)
        self._write_event(job_id, "job_started", status="running", detail=message)

    def update_step(self, job_id: str, key: str, *, status: str, detail: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for step in job.steps:
                if step.key == key:
                    step.status = status
                    step.detail = detail
                    break
            job.updated_at = datetime.now().isoformat(timespec="seconds")
        self._write_event(job_id, "step_updated", status=status, step=key, detail=detail)

    def finish(self, job_id: str, *, message: str, result: dict[str, object] | None = None) -> None:
        self._mutate(
            job_id,
            status="completed",
            message=message,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            result=result or {},
        )
        snapshot = self.snapshot(job_id) or {}
        self._write_event(job_id, "job_completed", status="completed", detail=message, data=result or {})
        self._write_summary(job_id, snapshot)
        self._append_history(job_id)

    def fail(self, job_id: str, error: str, *, error_detail: dict[str, object] | None = None) -> None:
        self._mutate(
            job_id,
            status="failed",
            message="処理中にエラーが発生しました。",
            error=error,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        snapshot = self.snapshot(job_id) or {}
        self._write_event(
            job_id,
            "job_failed",
            status="failed",
            detail=error,
            data={"error": error_detail or {"message": error}},
            level="error",
        )
        self._write_summary(job_id, snapshot)
        self._append_history(job_id)

    def _append_history(self, job_id: str) -> None:
        """ジョブ終了時に logs/jobs/history.jsonl へ1行追記する。

        履歴の書き込み失敗でジョブ本体の完了/失敗処理を壊さないよう、
        例外は握りつぶす（履歴はベストエフォート）。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            record = {
                "job_id": job.job_id,
                "title": job.title,
                "workflow": job.workflow,
                "status": job.status,
                "started_at": job.created_at,
                "finished_at": job.finished_at,
                "duration_sec": _duration_seconds(job.created_at, job.finished_at),
                "error": job.error,
                "steps": [
                    {"key": step.key, "label": step.label, "status": step.status}
                    for step in job.steps
                ],
                "log_events_path": job.log_events_path,
            }
        try:
            JOB_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with JOB_HISTORY_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def snapshot(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "job_id": job.job_id,
                "title": job.title,
                "status": job.status,
                "message": job.message,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "finished_at": job.finished_at,
                "error": job.error,
                "result": job.result or {},
                "log_dir": job.log_dir,
                "log_events_path": job.log_events_path,
                "log_summary_path": job.log_summary_path,
                "steps": [
                    {
                        "key": step.key,
                        "label": step.label,
                        "status": step.status,
                        "detail": step.detail,
                    }
                    for step in job.steps
                ],
            }

    def _mutate(self, job_id: str, **changes: object) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = datetime.now().isoformat(timespec="seconds")

    def _write_event(
        self,
        job_id: str,
        event: str,
        *,
        status: str | None = None,
        step: str | None = None,
        detail: str | None = None,
        data: dict[str, object] | None = None,
        level: str = "info",
    ) -> None:
        with self._lock:
            logger = self._loggers.get(job_id)
        if logger is not None:
            logger.write_event(event, status=status, step=step, detail=detail, data=data, level=level)

    def _write_summary(self, job_id: str, summary: dict[str, object]) -> None:
        with self._lock:
            logger = self._loggers.get(job_id)
        if logger is not None:
            logger.write_summary(summary)


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        delta = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return round(delta.total_seconds(), 1)


def read_job_history(limit: int = 200) -> list[dict[str, object]]:
    """history.jsonl を新しい順に読み出す。

    ファイルが無ければ空、壊れた行（不完全なJSON等）は無視する
    （追記型JSONLの性質上、プロセス強制終了などで欠けた行が混ざり得るため）。
    """
    if not JOB_HISTORY_PATH.is_file():
        return []
    records: list[dict[str, object]] = []
    try:
        lines = JOB_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    records.reverse()
    return records[:limit]


progress_jobs = ProgressJobStore()
