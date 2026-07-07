@echo off
setlocal
chcp 65001 >nul
rem ============================================================
rem くりまポータル 更新（git pull + 依存環境の同期）
rem   1. uv が無ければインストール（winget → 公式スクリプトの順）
rem   2. git pull で最新コードを取得
rem   3. uv sync で依存を uv.lock どおりに同期
rem   4. Playwright ブラウザ（chromium）を最新化
rem   5. サーバー再起動（O1: 更新→再起動の一括化。再起動しないと旧コードのまま）
rem
rem 使い方:
rem   update.bat                更新のみ。最後に再起動するか確認（30秒無応答なら再起動しない）
rem   update.bat /restart       更新後に scripts\restart.bat で自動再起動（確認なし）
rem   update.bat /restart 8006  ポートを指定して自動再起動
rem ============================================================

cd /d "%~dp0"

set "AUTO_RESTART="
set "RESTART_PORT="
if /i "%~1"=="/restart" (
    set "AUTO_RESTART=1"
    set "RESTART_PORT=%~2"
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
echo 更新が完了しました。
if defined AUTO_RESTART goto do_restart

rem O1: 更新→再起動の一括化。無応答 30 秒で N（従来どおり案内のみ）なので無人実行でも安全。
choice /c YN /t 30 /d N /m "サーバーを再起動しますか (Y=いま再起動 / N=あとで)"
if errorlevel 2 (
    echo サーバー起動中の場合は scripts\restart.bat で再起動すると反映されます。
    pause
    exit /b 0
)

:do_restart
echo [INFO] サーバーを再起動します（scripts\restart.bat）...
rem restart.bat は cp932 のため、表示化けを避けてコードページを戻してから呼ぶ
chcp 932 >nul
call "%~dp0scripts\restart.bat" %RESTART_PORT%
exit /b 0
