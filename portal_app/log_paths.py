from __future__ import annotations

import logging
import os
from pathlib import Path

from portal_app.services.paths import candidate_portal_roots

APP_ROOT = Path(__file__).resolve().parents[1]
FALLBACK_LOG_DIR = APP_ROOT / "logs"

# SharePoint「くりまポータル」ライブラリ内のログ出力先（ライブラリルートからの相対）。
# 例: %USERPROFILE%\株式会社しまのや\くりまポータル - ドキュメント\神里\くりまポータルエラーログ
LOG_RELATIVE_PARTS = ("神里", "くりまポータルエラーログ")

# 実行ログ（起動・ジョブ実行の記録）とエラーログ（例外・traceback）のファイル名。
RUN_LOG_NAME = "portal-run.log"
ERROR_LOG_NAME = "portal-error.log"

_LOGGER_NAME = "kurima_portal"
_configured_dir: Path | None = None


def resolve_log_dir() -> Path:
    """実行ログ・エラーログの出力先フォルダを解決する（無ければ自動作成）。

    解決順:
    1. 環境変数 ``KURIMA_LOG_DIR``（明示上書き用）
    2. SharePoint 同期ライブラリ（``candidate_portal_roots()`` が返す既存フォルダ）
       配下の ``神里\\くりまポータルエラーログ``
    3. リポジトリ内 ``logs/``（同期フォルダが無い PC でも起動不能にならない fallback）

    ユーザー名部分は ``Path.home()``（= ``%USERPROFILE%``）と環境変数で解決するため、
    特定ユーザー名のハードコードなしで SharePoint 同期済みのどの PC でも同じ場所に出力される。
    """
    explicit = os.environ.get("KURIMA_LOG_DIR")
    if explicit:
        target = Path(explicit).expanduser()
        if _ensure_dir(target):
            return target
    for root in candidate_portal_roots():
        if root.is_dir():
            target = root.joinpath(*LOG_RELATIVE_PARTS)
            if _ensure_dir(target):
                return target
    _ensure_dir(FALLBACK_LOG_DIR)
    return FALLBACK_LOG_DIR


def setup_file_logging() -> Path:
    """実行ログ・エラーログのファイルハンドラを構成し、出力先フォルダを返す。

    - ``portal-run.log`` … INFO 以上（起動・ジョブ・CLI 実行の実行ログ）
    - ``portal-error.log`` … ERROR 以上（例外・traceback のみ）

    何度呼んでも 2 重にハンドラが付かない（初回のみ構成）。
    """
    global _configured_dir
    if _configured_dir is not None:
        return _configured_dir

    log_dir = resolve_log_dir()
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    # ファイル出力専用ロガーとして root へ伝播させない（コンソール二重出力・他ライブラリ設定の影響を避ける）。
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    run_handler = logging.FileHandler(log_dir / RUN_LOG_NAME, encoding="utf-8")
    run_handler.setLevel(logging.INFO)
    run_handler.setFormatter(formatter)
    logger.addHandler(run_handler)

    error_handler = logging.FileHandler(log_dir / ERROR_LOG_NAME, encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    _configured_dir = log_dir
    return log_dir


def get_portal_logger() -> logging.Logger:
    """ポータル共通ロガーを返す（未構成なら先にファイル出力を構成する）。"""
    setup_file_logging()
    return logging.getLogger(_LOGGER_NAME)


def _ensure_dir(path: Path) -> bool:
    """フォルダを作成できたら True。権限・同期エラー等で作れない場合は False（次候補へ）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return path.is_dir()
