<#
.SYNOPSIS
くりまポータルの死活監視（O2）。タスクスケジューラから5分毎に実行される想定。

.DESCRIPTION
  http://127.0.0.1:<ポート>/health を確認し、応答しない場合だけ scripts\restart.bat で
  サーバーを再起動する（LAN公開モード）。ポートは -Port > 環境変数 KURIMA_PORT > 8006。
  実行結果は logs\watchdog.log に追記する（正常時は記録しない＝ログ肥大防止）。
  登録・解除は scripts\register_autostart.bat（/watchdog・/delete）で行う。
#>
param(
    [int]$Port = 0
)

if ($Port -le 0) {
    if ($env:KURIMA_PORT) { $Port = [int]$env:KURIMA_PORT } else { $Port = 8006 }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$logFile = Join-Path $repoRoot "logs\watchdog.log"

function Write-WatchdogLog([string]$Text) {
    # 監視ログの書き込み失敗で監視自体を止めない（フェイルセーフ）
    try {
        $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    } catch { }
}

try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 10
    if ($response.StatusCode -eq 200) {
        exit 0  # 正常。何もしない
    }
    Write-WatchdogLog "health NG (status=$($response.StatusCode)) -> restart.bat を実行します (port=$Port)"
} catch {
    Write-WatchdogLog "health 無応答 ($($_.Exception.Message)) -> restart.bat を実行します (port=$Port)"
}

# 無応答: 既存プロセスの停止と再起動は restart.bat に任せる（本番ホストは LAN 公開モード）
& cmd.exe /c "`"$PSScriptRoot\restart.bat`" $Port lan"
Write-WatchdogLog "restart.bat 呼び出し完了 (port=$Port mode=lan exitcode=$LASTEXITCODE)"
