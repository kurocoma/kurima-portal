from __future__ import annotations

import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = APP_ROOT / ".env"
DEFAULT_EXTRA_ENV_PATHS = (
    APP_ROOT / ".env.yamato-b2",
    APP_ROOT / "yamato-b2.env",
)


def load_env_file(path: Path | str | None = None, *, override: bool = False) -> None:
    """Load KEY=VALUE pairs from a local .env file without adding a dependency."""
    if path is None:
        for env_path in (DEFAULT_ENV_PATH, *DEFAULT_EXTRA_ENV_PATHS):
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
