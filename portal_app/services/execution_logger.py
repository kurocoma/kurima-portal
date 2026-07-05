from __future__ import annotations

import json
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_LOG_ROOT = APP_ROOT / "logs" / "execution_runs"
SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|security|token|cookie|authorization|credential|login_id)",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): "***" if SENSITIVE_KEY_PATTERN.search(str(key)) else json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def exception_payload(exc: BaseException) -> dict[str, object]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }


class ExecutionLogger:
    def __init__(self, *, workflow: str, run_id: str, title: str, metadata: dict[str, Any] | None = None) -> None:
        self.workflow = _safe_segment(workflow)
        self.run_id = _safe_segment(run_id)
        self.title = title
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = EXECUTION_LOG_ROOT / self.workflow / f"{timestamp}_{self.run_id}"
        self.events_path = self.run_dir / "events.jsonl"
        self.summary_path = self.run_dir / "summary.json"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.write_event(
            "job_created",
            status="queued",
            data={
                "title": title,
                "metadata": metadata or {},
                "run_dir": self.run_dir,
                "events_path": self.events_path,
                "summary_path": self.summary_path,
            },
        )

    def write_event(
        self,
        event: str,
        *,
        status: str | None = None,
        step: str | None = None,
        detail: str | None = None,
        data: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        payload = {
            "logged_at": now_iso(),
            "workflow": self.workflow,
            "run_id": self.run_id,
            "level": level,
            "event": event,
            "status": status,
            "step": step,
            "detail": detail,
            "data": json_safe(data or {}),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _mirror_to_portal_log(payload)

    def write_summary(self, summary: dict[str, Any]) -> None:
        payload = {
            "logged_at": now_iso(),
            "workflow": self.workflow,
            "run_id": self.run_id,
            **json_safe(summary),
            "run_dir": str(self.run_dir),
            "events_path": str(self.events_path),
            "summary_path": str(self.summary_path),
        }
        self.summary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def paths_payload(self) -> dict[str, str]:
        return {
            "run_dir": str(self.run_dir),
            "events_path": str(self.events_path),
            "summary_path": str(self.summary_path),
        }


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe.strip("._") or "run"


def _mirror_to_portal_log(payload: dict[str, Any]) -> None:
    """ジョブの実行イベントを共有ログ（SharePoint 同期フォルダ）へもミラーする。

    - 全イベント → portal-run.log（実行ログ）
    - level=error のイベント → portal-error.log にも出る（traceback 込みのエラーログ）

    ミラーはベストエフォート。ここでの失敗（同期フォルダのロック等）で
    ジョブ本体や logs/ 配下の既存ログ書き込みを壊さないよう例外は握りつぶす。
    """
    try:
        from portal_app.log_paths import get_portal_logger

        logger = get_portal_logger()
        parts = [
            f"workflow={payload.get('workflow')}",
            f"run_id={payload.get('run_id')}",
            f"event={payload.get('event')}",
        ]
        if payload.get("status"):
            parts.append(f"status={payload['status']}")
        if payload.get("step"):
            parts.append(f"step={payload['step']}")
        if payload.get("detail"):
            parts.append(f"detail={payload['detail']}")
        message = " ".join(parts)
        if payload.get("level") == "error":
            data = payload.get("data") or {}
            logger.error("%s data=%s", message, json.dumps(data, ensure_ascii=False))
        elif payload.get("level") == "warn":
            logger.warning(message)
        else:
            logger.info(message)
    except Exception:
        pass
