<#
.SYNOPSIS
くりまポータルツールの起動スクリプト。

.DESCRIPTION
  .\scripts\serve.ps1                  ローカルのみ (127.0.0.1) で起動
  .\scripts\serve.ps1 -Mode lan        LAN 公開 (0.0.0.0) で起動 — 社内LAN限定！
  .\scripts\serve.ps1 -Port 8010       ポート指定

ポートは -Port > 環境変数 KURIMA_PORT > 8006 の順で決まる。

注意: このツールには実決済（クリックポスト）・実取込（ヤマトB2）ボタンが含まれる。
LAN モードは社内LAN の信頼できる端末からのみ使い、
公開インターネットには絶対に露出させないこと。
#>
param(
    [ValidateSet("local", "lan")]
    [string]$Mode = "local",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

if ($Port -le 0) {
    if ($env:KURIMA_PORT) { $Port = [int]$env:KURIMA_PORT } else { $Port = 8006 }
}

$bindHost = if ($Mode -eq "lan") { "0.0.0.0" } else { "127.0.0.1" }

# リポジトリルート（このスクリプトの1つ上）で実行する
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if ($Mode -eq "lan") {
    Write-Host "[serve] LAN公開モードで起動します: http://<このPCの名前またはIP>:$Port/" -ForegroundColor Yellow
    Write-Host "[serve] 実決済・実取込ボタンを含むため、社内LAN限定で使用してください。" -ForegroundColor Yellow
    Write-Host "[serve] 他のPCから届かない場合は、Windowsファイアウォールの受信許可が必要です（README参照）。" -ForegroundColor Yellow
} else {
    Write-Host "[serve] ローカルモードで起動します: http://127.0.0.1:$Port/"
}

uv run uvicorn portal_app.main:app --host $bindHost --port $Port
