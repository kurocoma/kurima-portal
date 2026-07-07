from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from portal_app.env import env_int
from portal_app.services.paths import candidate_portal_roots

APP_ROOT = Path(__file__).resolve().parents[1]
FALLBACK_LOG_DIR = APP_ROOT / "logs"

# SharePoint「くりまポータル」ライブラリ内のログ出力先（ライブラリルートからの相対）。
# 例: %USERPROFILE%\株式会社しまのや\くりまポータル - ドキュメント\神里\くりまポータルエラーログ
LOG_RELATIVE_PARTS = ("神里", "くりまポータルエラーログ")

# 実行ログ（起動・ジョブ実行の記録）とエラーログ（例外・traceback）のベース名。
# 実ファイル名は S2（SharePoint 同期競合対策）で PC 名サフィックス付きになる
# （例: portal-run-KURIMA-PC1.log）。run_log_file_name() / error_log_file_name() で解決する。
RUN_LOG_NAME = "portal-run.log"
ERROR_LOG_NAME = "portal-error.log"

# ローテーション設定（S1）。上限サイズと世代数は env で調整できる。
# 既定は 5MB × 3 世代（同期フォルダ上の rename 負荷を抑えるため世代数は小さめ）。
LOG_MAX_MB_ENV = "KURIMA_LOG_MAX_MB"
LOG_BACKUP_COUNT_ENV = "KURIMA_LOG_BACKUP_COUNT"
LOG_SUFFIX_ENV = "KURIMA_LOG_SUFFIX"
DEFAULT_LOG_MAX_MB = 5.0
DEFAULT_LOG_BACKUP_COUNT = 3

_LOGGER_NAME = "kurima_portal"
_configured_dir: Path | None = None


def _env_float(name: str, default: float) -> float:
    """env を float として読む。未設定・数値でない・負値は既定値（設定ミスで起動を壊さない）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def log_file_suffix() -> str:
    """共有ログのファイル名に埋め込む PC 識別サフィックスを返す（S2）。

    既定はコンピュータ名（%COMPUTERNAME%）。KURIMA_LOG_SUFFIX で明示上書きできる。
    全 PC が同じファイルへ追記すると OneDrive の同期競合（競合コピー・ログ割れ）が
    起こるため、ファイル名を PC ごとに分離して構造的に回避する。
    """
    raw = os.environ.get(LOG_SUFFIX_ENV, "").strip() or os.environ.get("COMPUTERNAME", "").strip() or "pc"
    # ファイル名に使えない文字は "_" に寄せる（PC 名に日本語や空白が入る環境の保険）
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._")
    return safe or "pc"


def run_log_file_name() -> str:
    """実行ログの実ファイル名（PC 名サフィックス付き。例: portal-run-KURIMA-PC1.log）。"""
    return f"portal-run-{log_file_suffix()}.log"


def error_log_file_name() -> str:
    """エラーログの実ファイル名（PC 名サフィックス付き。例: portal-error-KURIMA-PC1.log）。"""
    return f"portal-error-{log_file_suffix()}.log"


class _SafeRotatingFileHandler(RotatingFileHandler):
    """ローテーション失敗を握りつぶして書き込みを継続する RotatingFileHandler（S1）。

    出力先は SharePoint 同期フォルダのため、ロールオーバー時の rename が
    OneDrive 同期や他プロセス（ログを開いているエディタ等）のロックで失敗し得る。
    その場合はローテーションを諦めて既存ファイルへの追記を続ける
    （ログが書けなくなって本体が止まる事態を避けるフェイルセーフ）。
    """

    def doRollover(self) -> None:  # noqa: N802 (logging の命名に合わせる)
        try:
            super().doRollover()
        except OSError:
            # rename 失敗時は stream が閉じられたままになり得るが、
            # FileHandler.emit が次回書き込み時に再オープンするため出力は継続する。
            pass


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


def _build_file_handler(path: Path, level: int, formatter: logging.Formatter) -> logging.Handler:
    """サイズローテーション付きのファイルハンドラを作る（S1）。

    - 上限サイズ: ``KURIMA_LOG_MAX_MB``（既定 5。0 でローテーション無効＝従来どおり追記のみ）
    - 世代数: ``KURIMA_LOG_BACKUP_COUNT``（既定 3、最小 1。portal-run-<PC>.log.1 の形で保持）
    """
    max_bytes = int(_env_float(LOG_MAX_MB_ENV, DEFAULT_LOG_MAX_MB) * 1024 * 1024)
    backup_count = env_int(LOG_BACKUP_COUNT_ENV, DEFAULT_LOG_BACKUP_COUNT, minimum=1)
    handler = _SafeRotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def setup_file_logging() -> Path:
    """実行ログ・エラーログのファイルハンドラを構成し、出力先フォルダを返す。

    - ``portal-run-<PC>.log`` … INFO 以上（起動・ジョブ・CLI 実行の実行ログ）
    - ``portal-error-<PC>.log`` … ERROR 以上（例外・traceback のみ）

    ファイル名の PC サフィックスは S2（SharePoint 同期競合対策）、
    サイズローテーションは S1（ログ無限成長の防止）。
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

    logger.addHandler(_build_file_handler(log_dir / run_log_file_name(), logging.INFO, formatter))
    logger.addHandler(
        _build_file_handler(log_dir / error_log_file_name(), logging.ERROR, formatter)
    )

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
