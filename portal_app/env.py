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


def load_env_file(path: Path | str | None = None, *, override: bool = False) -> None:
    """Load KEY=VALUE pairs from a local .env file without adding a dependency.

    .env.local は個人環境固有の値（gitignore対象）として .env より先に読み、
    override=False の既定では先勝ちのため .env.local の値が .env より優先される。
    """
    if path is None:
        for env_path in (DEFAULT_LOCAL_ENV_PATH, DEFAULT_ENV_PATH, *DEFAULT_EXTRA_ENV_PATHS):
            _load_env_path(env_path, override=override)
        return

    _load_env_path(Path(path), override=override)


def _load_env_path(env_path: Path, *, override: bool) -> None:
    if not env_path.exists():
        return

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
        if not key or (not override and key in os.environ):
            continue

        os.environ[key] = _strip_quotes(value.strip())


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
