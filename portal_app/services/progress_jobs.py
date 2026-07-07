from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Callable
from uuid import uuid4

from portal_app.services.error_hints import hint_for_exception, hint_for_message
from portal_app.services.execution_logger import APP_ROOT, ExecutionLogger, exception_payload


JobWorker = Callable[[str], None]


class JobCancelled(Exception):
    """ユーザーが「中止」を押したことを表す。worker はこれを工程境界で送出して安全に停止する。"""


class DuplicateJobError(RuntimeError):
    """同じ workflow のジョブが実行中のときに start() が送出する（S3: 二重実行ガード）。

    別タブ・別PC（LAN共有運用）から同じ /start を同時に叩いても、
    クリックポストの実決済・ヤマトB2の実取込が二重に走らないようにする。
    main.py の例外ハンドラが HTTP 409 とこの日本語メッセージに変換する。
    """

    def __init__(self, workflow: str, existing_job_id: str, existing_title: str) -> None:
        super().__init__(
            f"同じ処理（{existing_title}）が実行中のため、新しく開始しませんでした。"
            "完了を待ってから再実行してください。実行状況は /jobs（実行履歴）で確認できます。"
        )
        self.workflow = workflow
        self.existing_job_id = existing_job_id
        self.existing_title = existing_title


# ジョブ完了/失敗の履歴を1行1JSONで追記する永続ファイル。
# メモリ内の _jobs はプロセス終了で消えるため、「昨日の実行がどうなったか」を
# サーバー再起動後も追えるようにする。logs/ は gitignore 済み。
JOB_HISTORY_PATH = APP_ROOT / "logs" / "jobs" / "history.jsonl"

# history.jsonl の追記（_append_history）とコンパクション（compact_job_history）を
# 直列化するロック。読んで書き戻す最中の追記で行を取りこぼさないようにする（S6）。
_HISTORY_LOCK = Lock()


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
    # 失敗時の日本語対処ガイド（U1）。error_hints で例外から変換し、画面のエラー表示の下に出す。
    hint: str | None = None
    result: dict[str, object] | None = None
    log_dir: str | None = None
    log_events_path: str | None = None
    log_summary_path: str | None = None
    # events.jsonl の logs/ 相対パス（/logs/view へのリンク用。logs/ 外なら None）
    log_events_rel: str | None = None
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
        # 二重実行ガード（S3）: 同じ workflow の実行中（queued/running）ジョブがあれば開始しない。
        # チェック〜登録を同一ロック内で行い、同時 start の競合でも 2 本目が必ず拒否されるようにする
        # （ExecutionLogger の作成は軽いファイルIOのみで、ジョブ開始は低頻度のためロック内で許容）。
        with self._lock:
            active = next(
                (
                    job
                    for job in self._jobs.values()
                    if job.workflow == workflow and job.status in {"queued", "running"}
                ),
                None,
            )
            if active is not None:
                raise DuplicateJobError(
                    workflow=workflow,
                    existing_job_id=active.job_id,
                    existing_title=active.title,
                )
            logger = ExecutionLogger(
                workflow=workflow, run_id=job_id, title=title, metadata=metadata
            )
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
                log_events_rel=_logs_relative(log_paths["events_path"]),
            )
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
                # 例外オブジェクトから日本語の対処ガイドを引く（U1。未知のエラーは None のまま）
                self.fail(
                    job_id,
                    str(exc),
                    error_detail=exception_payload(exc),
                    hint=hint_for_exception(exc),
                )

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

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        error_detail: dict[str, object] | None = None,
        hint: str | None = None,
    ) -> None:
        self._mutate(
            job_id,
            status="failed",
            message="処理中にエラーが発生しました。",
            error=error,
            # worker が文字列だけで fail を呼ぶ経路でもガイドを出せるよう、メッセージからも引く（U1）
            hint=hint or hint_for_message(error),
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
            with _HISTORY_LOCK:
                JOB_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                with JOB_HISTORY_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def list_active(self) -> list[dict[str, object]]:
        """実行中（queued/running）ジョブの概要一覧を古い順で返す（U4: 再アタッチ用）。

        各画面はページロード時にこれを取得し、同じ workflow のジョブがあれば
        進捗ポーリングへ自動再接続する。ナビの実行中バッジもこの件数を使う。
        """
        with self._lock:
            active = [
                {
                    "job_id": job.job_id,
                    "workflow": job.workflow,
                    "title": job.title,
                    "status": job.status,
                    "created_at": job.created_at,
                }
                for job in self._jobs.values()
                if job.status in {"queued", "running"}
            ]
        active.sort(key=lambda item: str(item["created_at"]))
        return active

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
                "hint": job.hint,
                "result": job.result or {},
                "log_dir": job.log_dir,
                "log_events_path": job.log_events_path,
                "log_summary_path": job.log_summary_path,
                "log_events_rel": job.log_events_rel,
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


def _logs_relative(raw: str | None) -> str | None:
    """絶対パスをリポジトリ内 logs/ 相対（posix）へ変換する。logs/ 外・解決不能なら None。

    進捗スナップショットに載せて、画面側で「詳細ログを見る」リンク
    （/logs/view?path=...）を組み立てられるようにする（U1）。
    """
    if not raw:
        return None
    try:
        return Path(raw).resolve().relative_to((APP_ROOT / "logs").resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        delta = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return round(delta.total_seconds(), 1)


def compact_job_history(max_lines: int = 2000) -> int:
    """history.jsonl が max_lines を超えていたら、新しい側 max_lines 行だけ残す（S6）。

    read_job_history() は毎回全行を読むため、無限成長すると /jobs が遅くなる。
    ジョブ終了時の追記（_append_history）と同じ _HISTORY_LOCK で直列化し、
    読み→書き戻しの最中の追記で行を取りこぼさないようにする。
    戻り値は削除した行数（0 なら未実施）。失敗しても本体を壊さない（0 を返すだけ）。
    """
    if max_lines <= 0:
        return 0
    try:
        with _HISTORY_LOCK:
            if not JOB_HISTORY_PATH.is_file():
                return 0
            lines = JOB_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
            if len(lines) <= max_lines:
                return 0
            tmp_path = JOB_HISTORY_PATH.with_name(JOB_HISTORY_PATH.name + ".tmp")
            tmp_path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
            os.replace(tmp_path, JOB_HISTORY_PATH)
            return len(lines) - max_lines
    except OSError:
        return 0


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
