from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock, Thread
from typing import Callable
from uuid import uuid4

from portal_app.services.execution_logger import ExecutionLogger, exception_payload


JobWorker = Callable[[str], None]


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
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, object] | None = None
    log_dir: str | None = None
    log_events_path: str | None = None
    log_summary_path: str | None = None


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
        self.set_running(job_id, "処理を開始しました。")
        try:
            worker(job_id)
        except Exception as exc:
            self.fail(job_id, str(exc), error_detail=exception_payload(exc))

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


progress_jobs = ProgressJobStore()
