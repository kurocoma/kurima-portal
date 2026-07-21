@echo off
setlocal EnableExtensions
rem ============================================================
rem  くりまポータルツール 再起動スクリプト (cp932/Shift-JIS)
rem  使い方:
rem    restart.bat              ポート 8006 (または KURIMA_PORT) / local
rem    restart.bat lan          LAN 公開モード
rem    restart.bat 8010         ポート指定 (local)
rem    restart.bat 8010 lan     ポート + LAN
rem    restart.bat lan 8010     同上 (順不同可)
rem    restart.bat /nobrowser   ブラウザを開かない (watchdog 用)
rem  黒画面を出さず起動するとき:
rem    restart.vbs をダブルクリック
rem    または: wscript //nologo scripts\restart.vbs lan
rem  サーバー本体は restart_serve.vbs 経由で完全非表示起動する。
rem ============================================================

set "PORT="
set "MODE=local"
set "NO_BROWSER="

for %%a in (%*) do (
  if /i "%%~a"=="/nobrowser" (
    set "NO_BROWSER=1"
  ) else if /i "%%~a"=="lan" (
    set "MODE=lan"
  ) else if /i "%%~a"=="local" (
    set "MODE=local"
  ) else (
    echo %%~a| findstr /r "^[0-9][0-9]*$" >nul
    if not errorlevel 1 set "PORT=%%~a"
  )
)

if not defined PORT if defined KURIMA_PORT set "PORT=%KURIMA_PORT%"
if not defined PORT set "PORT=8006"

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

echo [restart] サーバーを非表示で起動します (mode=%MODE% port=%PORT%)
wscript //nologo "%~dp0restart_serve.vbs" "%MODE%" "%PORT%"
if errorlevel 1 (
    echo [restart][ERROR] restart_serve.vbs の起動に失敗しました。
    exit /b 1
)

if defined NO_BROWSER (
    echo [restart] /nobrowser のためブラウザは開きません。
    echo [restart] 起動しました: http://127.0.0.1:%PORT%/
    endlocal
    exit /b 0
)

echo [restart] 起動待ちのあとブラウザを開きます...
ping -n 4 127.0.0.1 >nul
start "" "http://127.0.0.1:%PORT%/"
echo [restart] ブラウザを開きました: http://127.0.0.1:%PORT%/
echo [restart] .py の変更はこの再起動で反映されます。
endlocal