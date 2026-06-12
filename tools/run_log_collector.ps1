# run_log_collector.ps1 — スタンドアロン EA 用ログ収集サーバを起動する。
#
# このサーバは「ログ受け取り」専用。トレード判断には一切関与しない。
# EA (strategies/standalone/mtf_pullback_v2.js) が POST してくる
# entry / outcome / skip ログを data/fto_mtf_pb_v2_live/<SYMBOL>/<session>.jsonl に保存する。
#
# 使い方:
#   PS> ./tools/run_log_collector.ps1
#   (別ポートや別保存先にしたいとき)
#   PS> ./tools/run_log_collector.ps1 -Port 8443 -LogDir data/fto_mtf_pb_v2_live
#
# 前提: server/certs/localhost.pem / localhost-key.pem が存在し、ブラウザが
#       https://localhost:<Port> の自己署名証明書を信頼済み (前回 wss で承認済みのはず)。
#       未承認なら一度 https://localhost:8443/ping をブラウザで開いて許可する。

param(
  [int]$Port = 8443,
  [string]$LogDir = "data/fto_mtf_pb_v2_live"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:LOG_DIR = $LogDir

$key = Join-Path $root "server/certs/localhost-key.pem"
$crt = Join-Path $root "server/certs/localhost.pem"
if (-not (Test-Path $key) -or -not (Test-Path $crt)) {
  Write-Error "証明書が見つかりません: $crt / $key"
}

Write-Host "[log_collector] port=$Port  out=$LogDir  (Ctrl+C で停止)" -ForegroundColor Cyan
python -m uvicorn server.log_collector:app `
  --host 0.0.0.0 --port $Port `
  --ssl-keyfile $key --ssl-certfile $crt
