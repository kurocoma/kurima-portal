@echo off
setlocal
chcp 65001 >nul
rem ============================================================
rem くりまポータル 自動起動・自動復旧の登録 (O2)
rem   register_autostart.bat            ログオン時の自動起動を登録
rem   register_autostart.bat /watchdog  自動起動＋5分毎の死活監視も登録
rem   register_autostart.bat /delete    登録したタスクをすべて解除
rem   register_autostart.bat /status    登録状態を表示
rem 補足:
rem - 登録は現在のユーザーのタスクとして行う
rem   (アクセス拒否になる環境では管理者のコマンドプロンプトで実行)
rem - 起動は serve.ps1 -Mode lan (ポートは KURIMA_PORT または 8006)
rem - 死活監視は watchdog.ps1 が /health を確認し、
rem   無応答なら restart.bat で再起動 (記録は logs\watchdog.log)
rem ============================================================

set "SCRIPT_DIR=%~dp0"
set "TASK_SERVE=kurima-portal-autostart"
set "TASK_WATCH=kurima-portal-watchdog"

if /i "%~1"=="/delete" goto :delete
if /i "%~1"=="/status" goto :status

echo [autostart] ログオン時の自動起動タスクを登録します (LAN公開モード)
schtasks /create /f /tn "%TASK_SERVE%" /sc onlogon /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File \"%SCRIPT_DIR%serve.ps1\" -Mode lan"
if errorlevel 1 goto :fail
echo [autostart] 登録しました: %TASK_SERVE% (次回ログオンから有効)

if /i not "%~1"=="/watchdog" goto :done

echo [autostart] 5分毎の死活監視タスクを登録します (無応答なら自動復旧)
schtasks /create /f /tn "%TASK_WATCH%" /sc minute /mo 5 /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%SCRIPT_DIR%watchdog.ps1\""
if errorlevel 1 goto :fail
echo [autostart] 登録しました: %TASK_WATCH%

:done
echo.
echo [autostart] 登録内容の確認:  register_autostart.bat /status
echo [autostart] いますぐ起動:    schtasks /run /tn "%TASK_SERVE%"
echo [autostart] 解除:            register_autostart.bat /delete
goto :eof

:status
echo [autostart] 登録状態を表示します (見つからない場合は未登録)
schtasks /query /tn "%TASK_SERVE%" 2>nul
if errorlevel 1 echo [autostart] %TASK_SERVE%: 未登録です
schtasks /query /tn "%TASK_WATCH%" 2>nul
if errorlevel 1 echo [autostart] %TASK_WATCH%: 未登録です
goto :eof

:delete
echo [autostart] 登録済みタスクを解除します
schtasks /delete /f /tn "%TASK_SERVE%" >nul 2>&1
if errorlevel 1 (echo [autostart] %TASK_SERVE%: 未登録のためスキップ) else (echo [autostart] %TASK_SERVE%: 解除しました)
schtasks /delete /f /tn "%TASK_WATCH%" >nul 2>&1
if errorlevel 1 (echo [autostart] %TASK_WATCH%: 未登録のためスキップ) else (echo [autostart] %TASK_WATCH%: 解除しました)
goto :eof

:fail
echo.
echo [autostart][ERROR] タスク登録に失敗しました。
rem 重要な対処案内は表示崩れが起きないよう ASCII でも出す
echo   Access denied: run this bat from an elevated (admin) command prompt.
echo   (管理者のコマンドプロンプトから実行してください)
exit /b 1
