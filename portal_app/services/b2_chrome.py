"""ヤマトB2取込＋印刷用の「実Chrome」を独立プロセスとして起動・管理する。

設計意図（[[portal-tool-yamato-b2]] の縮退問題 + 印刷までブラウザを開いたまま）:
- B2クラウドは Playwright が起動・所有するブラウザに縮退応答（system_error）を返すため、
  フル自動取込は不安定。一方ユーザーの実Chromeでは正規メニューが出て手動操作できる。
- 加えて B2 取込後に「印刷」の手動作業があるため、ブラウザは印刷が終わるまで開いたまま必要。

そこで本モジュールは実Chrome（無ければ Edge / Playwright chromium）を
`subprocess.Popen` で **detached** 起動する。Playwright が起動・所有しないため、
呼び出し元（asyncio.run のイベントループ）が終了してもブラウザは生存し続ける＝印刷まで開いたまま。

- モードA: CDP接続せず、実Chromeをそのまま手渡し（縮退リスクゼロ・取込/印刷は手動）。
- モードB: `--remote-debugging-port` 付きで起動し、別途 CDP 接続して取込まで自動を試みる
  （縮退時は手動フォールバック。実装は yamato_b2_import.run_b2_import_over_cdp）。

状態（PID/port/csv 等）は logs/b2_chrome_state.json に保存し、
サーバー再起動をまたいでも「閉じる」ボタンで終了できるようにする。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock

from portal_app.services.execution_logger import APP_ROOT
from portal_app.services.yamato_b2_import import DEFAULT_YAMATO_B2_MEMBER_LOGIN_URL

# --- 設定（環境変数で上書き可能。他PC対応） ---
CHROME_PATH_ENV = "KURIMA_B2_CHROME_PATH"
PROFILE_ENV = "KURIMA_B2_CHROME_PROFILE"
PORT_ENV = "KURIMA_B2_CHROME_PORT"
OPEN_URL_ENV = "KURIMA_B2_OPEN_URL"

# 実ブラウザ実行ファイルの探索順（実Google Chrome優先 → Edge → 最後にPlaywright chromium）。
_BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_STATE_PATH = APP_ROOT / "logs" / "b2_chrome_state.json"
_DEFAULT_PORT = 9333

_lock = Lock()


def default_profile_dir() -> Path:
    """B2専用の永続プロファイル。普段使いのChromeとは分離し、ログインを保持する。"""
    override = os.environ.get(PROFILE_ENV, "").strip()
    if override:
        return Path(override)
    return APP_ROOT / "data" / "b2_chrome_profile"


def _debug_port() -> int:
    raw = os.environ.get(PORT_ENV, "").strip()
    return int(raw) if raw.isdigit() else _DEFAULT_PORT


def _open_url() -> str:
    override = os.environ.get(OPEN_URL_ENV, "").strip()
    return override or DEFAULT_YAMATO_B2_MEMBER_LOGIN_URL


def _playwright_chromium() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if not base.is_dir():
        return None
    hits = sorted(base.glob("chromium-*/chrome-win/chrome.exe"))
    return hits[-1] if hits else None


def find_browser_executable() -> Path | None:
    override = os.environ.get(CHROME_PATH_ENV, "").strip()
    if override and Path(override).is_file():
        return Path(override)
    for candidate in _BROWSER_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return _playwright_chromium()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_state() -> dict | None:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(state: dict | None) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if state is None:
            if _STATE_PATH.exists():
                _STATE_PATH.unlink()
        else:
            _STATE_PATH.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except OSError:
        pass


def status() -> dict:
    """開いているB2 Chromeの状態を返す。PIDが死んでいれば状態をクリアする。"""
    with _lock:
        state = _read_state()
        if not state:
            return {"open": False}
        pid = int(state.get("pid", 0) or 0)
        if not _pid_alive(pid):
            _write_state(None)
            return {"open": False}
        return {"open": True, **state}


def close() -> dict:
    """記録されているB2 Chromeを終了する（印刷完了後に押す想定）。"""
    with _lock:
        state = _read_state()
        _write_state(None)
    if not state:
        return {"closed": False, "reason": "no_open_browser"}
    pid = int(state.get("pid", 0) or 0)
    killed = False
    if _pid_alive(pid):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
            killed = True
        except Exception:
            killed = False
    return {"closed": True, "killed": killed, "pid": pid}


def launch(*, csv_path: Path | None = None, enable_cdp: bool = False) -> dict:
    """実Chromeを専用プロファイルで独立起動し、B2画面を開く（印刷まで開いたまま）。

    enable_cdp=True のときのみ --remote-debugging-port を付け、CDP自動化（モードB）を可能にする。
    多重起動を避けるため、既存の開いているB2 Chromeがあれば先に閉じる。
    """
    exe = find_browser_executable()
    if exe is None:
        raise RuntimeError(
            "Chrome（または Edge / Playwright chromium）が見つかりませんでした。"
            f" {CHROME_PATH_ENV} で実行ファイルのパスを指定できます。"
            " 未導入なら `uv run playwright install chromium` でも代替できます。"
        )

    # 既存の開いているB2 Chromeを閉じる（多重起動・ポート競合の防止）。lock 外で実行。
    close()

    profile = default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    port = _debug_port()
    target_url = _open_url()

    args = [
        str(exe),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    if enable_cdp:
        args.append(f"--remote-debugging-port={port}")
    args.append(target_url)

    creationflags = 0
    if sys.platform == "win32":
        detached_process = 0x00000008
        create_new_process_group = 0x00000200
        creationflags = detached_process | create_new_process_group

    proc = subprocess.Popen(
        args,
        creationflags=creationflags,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    state = {
        "pid": proc.pid,
        "port": port if enable_cdp else None,
        "cdp_endpoint": f"http://127.0.0.1:{port}" if enable_cdp else None,
        "csv_path": str(csv_path) if csv_path else None,
        "open_url": target_url,
        "executable": str(exe),
        "profile_dir": str(profile),
        "enable_cdp": enable_cdp,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        _write_state(state)
    return state
