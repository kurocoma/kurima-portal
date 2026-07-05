@echo off
setlocal
rem ============================================================
rem  くりまポータルツール 再起動スクリプト (cp932/Shift-JIS)
rem  使い方:
rem    restart.bat              ポート 8006 (または KURIMA_PORT) で再起動
rem    restart.bat 8010         ポートを指定して再起動
rem    restart.bat 8006 lan     LAN 公開モードで再起動
rem  日本語を含むパスでも動くよう、パスはすべて引用符で囲み %~dp0 相対で参照する。
rem ============================================================

set "PORT=%~1"
if not defined PORT if defined KURIMA_PORT set "PORT=%KURIMA_PORT%"
if not defined PORT set "PORT=8006"

set "MODE=%~2"
if not defined MODE set "MODE=local"

echo [restart] ポート %PORT% で稼働中のサーバーを探しています...
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /c:"LISTENING" ^| findstr /c:":%PORT% "') do (
    set "FOUND=1"
    echo [restart] PID %%p を停止します
    taskkill /f /pid %%p >nul 2>&1
)
if not defined FOUND (
    echo [restart] 稼働中のサーバーは見つかりませんでした。そのまま起動します。
) else (
    rem ポート解放を約2秒待つ
    ping -n 3 127.0.0.1 >nul
)

echo [restart] サーバーを起動します (mode=%MODE% port=%PORT%)
start "kurima-portal" powershell -NoExit -NoProfile -ExecutionPolicy Bypass -File "%~dp0serve.ps1" -Mode %MODE% -Port %PORT%

echo [restart] 新しいウィンドウで起動しました: http://127.0.0.1:%PORT%/
echo [restart] .py の変更はこの再起動で反映されます。
endlocal
