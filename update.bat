@echo off
setlocal
chcp 65001 >nul
rem ============================================================
rem くりまポータル 更新（git pull + 依存環境の同期）
rem   1. uv が無ければインストール（winget → 公式スクリプトの順）
rem   2. git pull で最新コードを取得
rem   3. uv sync で依存を uv.lock どおりに同期
rem   4. Playwright ブラウザ（chromium）を最新化
rem ============================================================

cd /d "%~dp0"

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

echo [INFO] 最新のコードを取得します（git pull）...
git pull --ff-only
if errorlevel 1 (
    echo [ERROR] git pull に失敗しました。ローカル変更がある場合は退避してから再実行してください。
    pause
    exit /b 1
)

echo [INFO] 依存パッケージを同期します（uv sync）...
uv sync
if errorlevel 1 (
    echo [ERROR] uv sync に失敗しました。
    pause
    exit /b 1
)

echo [INFO] Playwright ブラウザ（chromium）を確認します...
uv run playwright install chromium

echo.
echo 更新が完了しました。サーバー起動中の場合は scripts\restart.bat で再起動してください。
pause
