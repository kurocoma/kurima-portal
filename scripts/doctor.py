"""環境自己診断スクリプト。

新しいPCでくりまポータルツールを動かす前に、必要なツール類が
入っているかを ○/× で検査する。不足があればインストールコマンドを表示する。

実行:
    uv run python scripts/doctor.py
    （uv 未導入時は .venv\\Scripts\\python.exe scripts/doctor.py でも可）

終了コード = 失敗した必須チェックの数（0 なら全て OK）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OK = "[OK]"
NG = "[NG]"
INFO = "[--]"

# 認証系キー（未設定でも起動はできるが、対応機能が動かない）
OPTIONAL_ENV_KEYS = (
    ("NEXT_ENGINE_LOGIN_ID", "Next Engine 自動操作（在庫明細取得など）"),
    ("NEXT_ENGINE_PASSWORD", "Next Engine 自動操作（在庫明細取得など）"),
    ("YAMATO_B2_LOGIN_ID", "ヤマトB2取込"),
    ("YAMATO_B2_PASSWORD", "ヤマトB2取込"),
    ("CLICKPOST_YAHOO_LOGIN_ID", "クリックポスト取込・決済"),
    ("CLICKPOST_YAHOO_PASSWORD", "クリックポスト取込・決済"),
)

SYSTEM_BROWSER_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)


def check_python() -> bool:
    version = sys.version_info
    ok = version >= (3, 11)
    print(f"{OK if ok else NG} Python バージョン: {sys.version.split()[0]} (必要: 3.11 以上)")
    if not ok:
        print("     -> https://www.python.org/downloads/ か `uv python install 3.13` で導入してください。")
    return ok


def check_uv() -> bool:
    uv_path = shutil.which("uv")
    if uv_path:
        try:
            version = subprocess.run(
                ["uv", "--version"], capture_output=True, text=True, timeout=30
            ).stdout.strip()
        except Exception:
            version = "(バージョン取得失敗)"
        print(f"{OK} uv: {version}")
        return True
    print(f"{NG} uv が見つかりません。")
    print("     -> PowerShell で: winget install astral-sh.uv")
    print("        または: powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"")
    return False


def _playwright_browsers_dir() -> Path:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ms-playwright"
    return Path.home() / "AppData" / "Local" / "ms-playwright"


def check_playwright_browser() -> bool:
    # 優先1: 明示指定された実行ファイル
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if configured and Path(configured).is_file():
        print(f"{OK} ブラウザ: PLAYWRIGHT_CHROMIUM_EXECUTABLE = {configured}")
        return True

    # 優先2: Playwright 同梱 chromium の実体
    browsers_dir = _playwright_browsers_dir()
    bundled = []
    if browsers_dir.is_dir():
        for pattern in ("chromium-*/chrome-win/chrome.exe", "chromium_headless_shell-*/chrome-win/headless_shell.exe"):
            bundled.extend(browsers_dir.glob(pattern))
    if bundled:
        print(f"{OK} ブラウザ: Playwright 同梱 chromium ({bundled[0]})")
        return True

    # 優先3: システムの Chrome/Edge（アプリが自動検出して使う）
    for candidate in SYSTEM_BROWSER_CANDIDATES:
        if candidate.is_file():
            print(f"{OK} ブラウザ: システムの Chrome/Edge を使用 ({candidate})")
            return True

    print(f"{NG} 自動操作に使えるブラウザが見つかりません。")
    print("     -> uv run playwright install chromium")
    print("        （または Chrome / Edge をインストールしてください）")
    return False


def check_playwright_package() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print(f"{NG} playwright パッケージが未インストールです。")
        print("     -> uv sync を実行してください。")
        return False
    print(f"{OK} playwright パッケージ: インストール済み")
    return True


def check_portal_paths() -> bool:
    try:
        from portal_app.env import load_env_file
        from portal_app.services.paths import find_portal_paths

        load_env_file()
        paths = find_portal_paths()
    except FileNotFoundError as exc:
        print(f"{NG} ポータル同期フォルダ: 未検出")
        for line in str(exc).splitlines():
            print(f"     {line}")
        print("     -> SharePoint「くりまポータル」ライブラリを同期するか、")
        print("        .env に KURIMA_PORTAL_ROOT を設定してください（.env.example 参照）。")
        return False
    except Exception as exc:  # 想定外でも診断は続行する
        print(f"{NG} ポータル同期フォルダ: 検査中にエラー ({exc})")
        return False

    print(f"{OK} ポータルルート: {paths.portal_root}")
    print(f"{OK} 商品管理シート: {paths.master_book}")
    print(f"{OK} 受注明細フォルダ: {paths.order_csv_dir}")
    return True


def report_env_keys() -> None:
    print("--- 認証系 環境変数（未設定でも起動可。対応機能を使う場合に設定） ---")
    for key, purpose in OPTIONAL_ENV_KEYS:
        mark = OK if os.environ.get(key) else INFO
        state = "設定済み" if os.environ.get(key) else "未設定"
        print(f"{mark} {key}: {state}（{purpose}）")


def main() -> int:
    print("=== くりまポータルツール 環境診断 (doctor) ===")
    print(f"リポジトリ: {REPO_ROOT}")
    print()

    failures = 0
    print("--- 必須チェック ---")
    for check in (check_python, check_uv, check_playwright_package, check_playwright_browser, check_portal_paths):
        if not check():
            failures += 1
    print()
    report_env_keys()

    print()
    if failures == 0:
        print("結果: 必須チェックはすべて OK です。scripts/serve.ps1 で起動できます。")
    else:
        print(f"結果: 必須チェックの失敗が {failures} 件あります。上の -> の手順で解消してください。")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
