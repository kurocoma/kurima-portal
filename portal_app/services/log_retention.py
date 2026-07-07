"""logs/ の保持期間管理と自動クリーンアップ（S6）。

リポジトリ内 logs/ は追記のみで無限成長する（実測 28MB）。サーバー起動時に
バックグラウンドスレッドで 1 回だけ掃除し、/jobs・/logs の表示速度を長期運用でも保つ。

対象（削除はリポジトリ内 logs/ 配下限定）:
- ``logs/execution_runs/<workflow>/<run>/`` … 保持日数を超えた run フォルダを削除
- ``logs/next_engine_yamato/b2_import_debug/`` … 保持日数を超えたスクショ・デバッグHTMLを削除
- ``logs/jobs/history.jsonl`` … 行数上限を超えたら新しい側だけ残す（progress_jobs 側でロック共有）

設定（env）:
- ``KURIMA_LOG_RETENTION_DAYS`` … 保持日数（既定 30。0 以下でクリーンアップ無効）
- ``KURIMA_JOB_HISTORY_MAX_LINES`` … history.jsonl の行数上限（既定 2000。0 以下で無効）

フェイルセーフ: 失敗しても本体を止めない（例外は握りつぶし、結果を共有ログへ記録するのみ）。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from threading import Thread

from portal_app.env import env_int
from portal_app.services.progress_jobs import compact_job_history

APP_ROOT = Path(__file__).resolve().parents[2]
LOGS_ROOT = APP_ROOT / "logs"

EXECUTION_RUNS_DIRNAME = "execution_runs"
B2_DEBUG_RELATIVE_PARTS = ("next_engine_yamato", "b2_import_debug")

RETENTION_DAYS_ENV = "KURIMA_LOG_RETENTION_DAYS"
HISTORY_MAX_LINES_ENV = "KURIMA_JOB_HISTORY_MAX_LINES"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_HISTORY_MAX_LINES = 2000


def retention_days() -> int:
    # 0 以下は「無効化」を意味する有効値のため、下限検査なしで読む（portal_app.env に一元化）。
    return env_int(RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS)


def history_max_lines() -> int:
    return env_int(HISTORY_MAX_LINES_ENV, DEFAULT_HISTORY_MAX_LINES)


def cleanup_old_logs(
    logs_root: Path | None = None,
    *,
    days: int | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """保持日数を超えた実行ログ・デバッグ出力を削除する（削除は logs_root 配下限定）。

    days が 0 以下なら何もしない（無効化）。個々の削除失敗（同期ロック等）は
    握りつぶして続行するベストエフォート。戻り値は削除件数のサマリ。
    """
    root = (logs_root or LOGS_ROOT).resolve()
    keep_days = retention_days() if days is None else days
    if keep_days <= 0:
        return {"enabled": False, "days": keep_days, "removed_run_dirs": 0, "removed_debug_files": 0}
    cutoff = (time.time() if now is None else now) - keep_days * 86400

    removed_run_dirs = 0
    removed_debug_files = 0

    # 1) ジョブ詳細ログ（execution_runs/<workflow>/<run>/）の期限切れ run フォルダ
    runs_root = root / EXECUTION_RUNS_DIRNAME
    if runs_root.is_dir():
        for workflow_dir in _iterdir_safe(runs_root):
            if not workflow_dir.is_dir():
                continue
            for run_dir in _iterdir_safe(workflow_dir):
                if not run_dir.is_dir():
                    continue
                if _newest_mtime(run_dir) >= cutoff:
                    continue
                if not _is_inside(run_dir, root):
                    continue  # 念のための保険（logs/ 配下以外は絶対に消さない）
                shutil.rmtree(run_dir, ignore_errors=True)
                if not run_dir.exists():
                    removed_run_dirs += 1

    # 2) B2取込デバッグ出力（スクショ PNG・HTML）の期限切れファイル
    debug_dir = root.joinpath(*B2_DEBUG_RELATIVE_PARTS)
    if debug_dir.is_dir():
        for path in _iterdir_safe(debug_dir):
            try:
                if not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
                if not _is_inside(path, root):
                    continue
                path.unlink(missing_ok=True)
                removed_debug_files += 1
            except OSError:
                continue

    return {
        "enabled": True,
        "days": keep_days,
        "removed_run_dirs": removed_run_dirs,
        "removed_debug_files": removed_debug_files,
    }


def run_cleanup() -> dict[str, object]:
    """クリーンアップ＋履歴コンパクションを実行し、結果を共有ログへ記録する。

    どこで失敗しても例外を外へ出さない（クリーンアップの失敗で本体を巻き込まない）。
    """
    try:
        result = cleanup_old_logs()
        result["history_compacted_lines"] = compact_job_history(history_max_lines())
        _log_result(result)
        return result
    except Exception as exc:  # フェイルセーフ: 掃除の失敗はログのみ
        _log_error(exc)
        return {"enabled": True, "error": str(exc)}


def start_background_cleanup() -> Thread:
    """サーバー起動をブロックしないよう、バックグラウンドスレッドで 1 回実行する。"""
    thread = Thread(target=run_cleanup, name="kurima-log-retention", daemon=True)
    thread.start()
    return thread


def _iterdir_safe(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _newest_mtime(directory: Path) -> float:
    """フォルダ配下で最も新しい mtime を返す（判定不能なら現在時刻＝削除しない側に倒す）。"""
    try:
        newest = directory.stat().st_mtime
    except OSError:
        return time.time()
    try:
        for path in directory.rglob("*"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return time.time()
    return newest


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _log_result(result: dict[str, object]) -> None:
    try:
        from portal_app.log_paths import get_portal_logger

        get_portal_logger().info("ログクリーンアップ完了: %s", result)
    except Exception:
        pass


def _log_error(exc: Exception) -> None:
    try:
        from portal_app.log_paths import get_portal_logger

        get_portal_logger().error("ログクリーンアップに失敗しました: %s", exc, exc_info=exc)
    except Exception:
        pass
