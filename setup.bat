@echo off
setlocal
chcp 65001 >nul
rem ============================================================
rem くりまポータル 初回セットアップ（新しいPC用）
rem   1. uv が無ければインストール（winget → 公式スクリプトの順）
rem   2. リポジトリ外で単体実行された場合は git clone
rem   3. uv sync で依存を uv.lock どおりに再現
rem   4. Playwright ブラウザ（chromium）を導入
rem   5. .env を作成し、環境診断（scripts\doctor.py）を実行
rem 前提: SharePoint「くりまポータル」ライブラリが OneDrive で同期済みであること。
rem ============================================================

cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] git が見つかりません。先に Git for Windows を導入してください:
    echo     winget install --id Git.Git -e
    pause
    exit /b 1
)

where uv >nul 2>nul
if errorlevel 1 (
    echo [INFO] uv が見つからないため winget でインストールします...
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
    echo [INFO] winget で導入できなかったため公式スクリプトでインストールします...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv を導入できませんでした。https://docs.astral.sh/uv/ を参照して手動で導入してください。
    pause
    exit /b 1
)

rem この bat がリポジトリ内（pyproject.toml あり）で実行されていれば clone は不要。
rem 単体で配布された bat から実行された場合は %USERPROFILE%\kurima-portal へ clone する。
if not exist "%~dp0pyproject.toml" (
    if not exist "%USERPROFILE%\kurima-portal\pyproject.toml" (
        echo [INFO] リポジトリを取得します（git clone）...
        git clone https://github.com/kurocoma/kurima-portal.git "%USERPROFILE%\kurima-portal"
        if errorlevel 1 (
            echo [ERROR] git clone に失敗しました。ネットワークと GitHub へのアクセス権を確認してください。
            pause
            exit /b 1
        )
    )
    cd /d "%USERPROFILE%\kurima-portal"
)

echo [INFO] 依存パッケージを同期します（uv sync）...
uv sync
if errorlevel 1 (
    echo [ERROR] uv sync に失敗しました。
    pause
    exit /b 1
)

echo [INFO] Playwright ブラウザ（chromium）を導入します...
uv run playwright install chromium

if not exist ".env" (
    copy .env.example .env >nul
    echo [INFO] .env を作成しました。あとで notepad .env で認証情報を設定してください。
)

echo [INFO] 環境診断を実行します（scripts\doctor.py）...
uv run python scripts/doctor.py

echo.
echo セットアップが完了しました。起動方法:
echo     powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
echo     または: uv run uvicorn portal_app.main:app --host 127.0.0.1 --port 8006
echo ブラウザで http://127.0.0.1:8006/ を開いてください。
pause
