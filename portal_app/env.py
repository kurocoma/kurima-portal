from __future__ import annotations

import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_ENV_PATH = APP_ROOT / ".env.local"
DEFAULT_ENV_PATH = APP_ROOT / ".env"
DEFAULT_EXTRA_ENV_PATHS = (
    APP_ROOT / ".env.yamato-b2",
    APP_ROOT / "yamato-b2.env",
)

# SharePoint同期フォルダの共有設定（2026-07-26）。認証情報などの共通値を
# 「くりまポータル - ドキュメント/kurimaportal-app/.env」に集約し、全PCで共用する。
SHARED_ENV_DIR_ENV = "KURIMA_SHARED_ENV_DIR"
SHARED_ENV_DIR_NAME = "kurimaportal-app"
SHARED_ENV_FILE_NAMES = (".env", "kurima-portal.env")


def load_env_file(path: Path | str | None = None, *, override: bool = False) -> None:
    """Load KEY=VALUE pairs from .env files without adding a dependency.

    読み込み順（override=False の既定では先勝ち＝先に読んだ値が優先）:
      1. OS環境変数（既に設定済みの値が常に最優先）
      2. .env.local（端末固有の上書き。gitignore対象）
      3. 共有 .env（SharePoint同期の kurimaportal-app/。会社共通の認証情報を集約）
      4. .env / yamato拡張（従来のローカル既定値）
    """
    if path is None:
        _load_env_path(DEFAULT_LOCAL_ENV_PATH, override=override)
        load_shared_env_file(override=override)
        for env_path in (DEFAULT_ENV_PATH, *DEFAULT_EXTRA_ENV_PATHS):
            _load_env_path(env_path, override=override)
        return

    _load_env_path(Path(path), override=override)


def load_shared_env_file(*, override: bool = False) -> Path | None:
    """SharePoint同期フォルダの共有 .env を読み込み、読んだファイルのパスを返す。

    置き場所は KURIMA_SHARED_ENV_DIR で明示指定するか、未指定なら
    ポータル同期フォルダ候補（KURIMA_PORTAL_ROOT / 既定候補）直下の
    kurimaportal-app/ を自動探索する。フォルダ名は SharePoint 上で見える
    名前に合わせ .env のほか kurima-portal.env も受け付ける。
    見つからなければ何もしない（共有未導入のPCでも従来どおり動く）。
    """
    for directory in _shared_env_dir_candidates():
        for file_name in SHARED_ENV_FILE_NAMES:
            shared_path = directory / file_name
            if shared_path.is_file():
                _load_env_path(shared_path, override=override)
                return shared_path
    return None


def _shared_env_dir_candidates() -> list[Path]:
    explicit = os.environ.get(SHARED_ENV_DIR_ENV, "").strip()
    if explicit:
        return [Path(explicit).expanduser()]

    candidates: list[Path] = []

    # 共有 .env はローカル .env より先に読むため、KURIMA_PORTAL_ROOT 等を
    # ローカル .env にだけ書いているPCでは通常の候補探索で見つからない。
    # ローカル .env から場所のキーだけ先読みして候補に足す（os.environ は汚さない）。
    peeked_dir = _peek_env_value(DEFAULT_ENV_PATH, SHARED_ENV_DIR_ENV)
    if peeked_dir:
        candidates.append(Path(peeked_dir).expanduser())

    # portal_app.services.paths は execution_logger 経由の依存があるため、
    # モジュール初期化順の問題を避けて関数内で読み込む。
    from portal_app.services.paths import candidate_portal_roots

    candidates.extend(root / SHARED_ENV_DIR_NAME for root in candidate_portal_roots())

    for key in ("KURIMA_PORTAL_ROOT", "PORTAL_ROOT"):
        peeked_root = _peek_env_value(DEFAULT_ENV_PATH, key)
        if peeked_root:
            candidates.append(Path(peeked_root).expanduser() / SHARED_ENV_DIR_NAME)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _peek_env_value(env_path: Path, key: str) -> str | None:
    """envファイルから指定キーの値だけ読む（os.environ には反映しない）。"""
    if not env_path.exists():
        return None
    for pair_key, value in _iter_env_pairs(env_path):
        if pair_key == key and value:
            return value
    return None


def _load_env_path(env_path: Path, *, override: bool) -> None:
    if not env_path.exists():
        return

    for key, value in _iter_env_pairs(env_path):
        if not override and key in os.environ:
            continue
        os.environ[key] = value


def _iter_env_pairs(env_path: Path):
    """envファイルの KEY=VALUE を順に返す（コメント・export・引用符を処理）。"""
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        yield key, _strip_quotes(value.strip())


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """env を int として読む。未設定・数値でない・下限未満は既定値（設定ミスで起動を壊さない）。

    log_paths / log_retention / settings で個別定義されていた読み取りをここへ一元化する。
    minimum=None なら下限検査なし（0 や負値を「無効化フラグ」として使うキー向け）。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value
