<#
.SYNOPSIS
  マルチプラットフォーム EA 開発基盤のステージング/バンドル・スクリプト。

.DESCRIPTION
  1) config/profiles.yaml から各言語の Profiles.* を再生成（tools/gen_profiles.py）
  2) 指定プラットフォーム向けに framework + strategy を配備
     - mt5/mt4: framework/<plat>/*.mqh を <Terminal>/MQL{5,4}/Include/PropKit/ へ、
                strategy を Experts/ へコピー（-Bundle 指定時は 1 ファイルにインライン）
     - ctrader: PropKit.cs + Profiles.cs + strategy を 1 つの .cs にバンドル
                （using をホイスト）。-TerminalPath 指定で cAlgo Sources へコピー
  コンパイルは各 GUI（MetaEditor F7 / cTrader Build）で行う（本スクリプトは配備のみ）。

.PARAMETER Platform
  mt5 | mt4 | ctrader | all

.PARAMETER Strategy
  strategies/<name>/ 配下のディレクトリ名（既定: breakout_h1）

.PARAMETER TerminalPath
  配備先。mt5/mt4 は端末データフォルダ（…\MQL5 や …\MQL4 の親）。
  ctrader は cAlgo Sources\Robots フォルダ。未指定なら build/ にのみ出力。

.PARAMETER Bundle
  単一ファイルにインライン化（mt は任意、ctrader は常にバンドル）。

.EXAMPLE
  pwsh tools/deploy.ps1 -Platform all
  pwsh tools/deploy.ps1 -Platform mt5 -TerminalPath "$env:APPDATA\MetaQuotes\Terminal\<hash>"
  pwsh tools/deploy.ps1 -Platform ctrader -Bundle
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('mt5', 'mt4', 'ctrader', 'all')][string]$Platform,
    [string]$Strategy = 'breakout_h1',
    [string]$TerminalPath = '',
    [switch]$Bundle
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$BuildDir = Join-Path $Root 'build'
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

# --- 1) プロファイル再生成 -------------------------------------------------
Write-Host "[deploy] gen_profiles..." -ForegroundColor Cyan
python (Join-Path $Root 'tools/gen_profiles.py')
if ($LASTEXITCODE -ne 0) { throw "gen_profiles.py 失敗" }

# --- ヘルパー: ローカル include を再帰インライン（MQL）---------------------
function Expand-MqlIncludes {
    param([string]$Path, [hashtable]$LocalMap)
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*#include\s+[<"](.+?)[>"]') {
            $leaf = Split-Path $Matches[1] -Leaf
            if ($LocalMap.ContainsKey($leaf)) {
                $out.Add("// >>> inlined: $leaf")
                foreach ($l in (Expand-MqlIncludes -Path $LocalMap[$leaf] -LocalMap $LocalMap)) { $out.Add($l) }
                continue
            }
        }
        $out.Add($line)
    }
    return $out
}

# --- ヘルパー: C# を 1 ファイルにバンドル（using をホイスト）--------------
function Bundle-CSharp {
    param([string[]]$Files)
    $usings = New-Object System.Collections.Generic.SortedSet[string]
    $body = New-Object System.Collections.Generic.List[string]
    foreach ($f in $Files) {
        $body.Add("// >>> from: $(Split-Path $f -Leaf)")
        foreach ($line in Get-Content -LiteralPath $f) {
            if ($line -match '^\s*using\s+[A-Za-z0-9_.]+\s*;\s*$') { [void]$usings.Add($line.Trim()); continue }
            $body.Add($line)
        }
        $body.Add('')
    }
    $res = New-Object System.Collections.Generic.List[string]
    foreach ($u in $usings) { $res.Add($u) }
    $res.Add('')
    foreach ($b in $body) { $res.Add($b) }
    return $res
}

# --- MT (mt5/mt4) 配備 -----------------------------------------------------
function Deploy-MT {
    param([string]$Plat)   # 'mt5' or 'mt4'
    $lang = if ($Plat -eq 'mt5') { 'mql5' } else { 'mql4' }   # framework/strategy のフォルダ名
    $ext = if ($Plat -eq 'mt5') { 'mq5' } else { 'mq4' }
    $mqlDir = if ($Plat -eq 'mt5') { 'MQL5' } else { 'MQL4' }
    $fwDir = Join-Path $Root "framework/$lang"
    $stratFile = Join-Path $Root "strategies/$Strategy/$lang.$ext"
    $outName = "$($Strategy)_$lang.$ext"
    if (-not (Test-Path $stratFile)) { throw "戦略ファイルが無い: $stratFile" }

    $localMap = @{
        'PropKit.mqh'  = (Join-Path $fwDir 'PropKit.mqh')
        'Profiles.mqh' = (Join-Path $fwDir 'Profiles.mqh')
    }

    if ($Bundle) {
        $bundle = Expand-MqlIncludes -Path $stratFile -LocalMap $localMap
        $outFile = Join-Path $BuildDir $outName
        Set-Content -LiteralPath $outFile -Value $bundle -Encoding UTF8
        Write-Host "[deploy] bundled -> $outFile" -ForegroundColor Green
        if ($TerminalPath) {
            $expDir = Join-Path $TerminalPath "$mqlDir/Experts"
            New-Item -ItemType Directory -Force -Path $expDir | Out-Null
            Copy-Item $outFile (Join-Path $expDir $outName) -Force
            Write-Host "[deploy] copied bundle -> $expDir" -ForegroundColor Green
        }
    }
    else {
        $outFile = Join-Path $BuildDir $outName
        Copy-Item $stratFile $outFile -Force
        Write-Host "[deploy] staged -> $outFile (要 Include/PropKit/)" -ForegroundColor Green
        if ($TerminalPath) {
            $incDir = Join-Path $TerminalPath "$mqlDir/Include/PropKit"
            $expDir = Join-Path $TerminalPath "$mqlDir/Experts"
            New-Item -ItemType Directory -Force -Path $incDir, $expDir | Out-Null
            Copy-Item (Join-Path $fwDir 'PropKit.mqh') $incDir -Force
            Copy-Item (Join-Path $fwDir 'Profiles.mqh') $incDir -Force
            Copy-Item $stratFile (Join-Path $expDir $outName) -Force
            Write-Host "[deploy] copied include+expert -> $TerminalPath\$mqlDir" -ForegroundColor Green
        }
    }
    Write-Host "[deploy] ${Plat}: MetaEditor で F7 コンパイルしてください。" -ForegroundColor Yellow
}

# --- cTrader 配備（常にバンドル）------------------------------------------
function Deploy-CTrader {
    $fwDir = Join-Path $Root 'framework/ctrader'
    $stratFile = Join-Path $Root "strategies/$Strategy/ctrader.cs"
    if (-not (Test-Path $stratFile)) { throw "戦略ファイルが無い: $stratFile" }
    $files = @(
        (Join-Path $fwDir 'Profiles.cs'),
        (Join-Path $fwDir 'PropKit.cs'),
        $stratFile
    )
    $bundle = Bundle-CSharp -Files $files
    $outFile = Join-Path $BuildDir "$($Strategy)_ctrader.cs"
    Set-Content -LiteralPath $outFile -Value $bundle -Encoding UTF8
    Write-Host "[deploy] bundled -> $outFile" -ForegroundColor Green
    if ($TerminalPath) {
        $dst = Join-Path $TerminalPath "$Strategy"
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item $outFile (Join-Path $dst "$($Strategy).cs") -Force
        Write-Host "[deploy] copied -> $dst" -ForegroundColor Green
    }
    Write-Host "[deploy] ctrader: cTrader Automate でこの .cs を貼って Build してください。" -ForegroundColor Yellow
}

switch ($Platform) {
    'mt5' { Deploy-MT 'mt5' }
    'mt4' { Deploy-MT 'mt4' }
    'ctrader' { Deploy-CTrader }
    'all' { Deploy-MT 'mt5'; Deploy-MT 'mt4'; Deploy-CTrader }
}
Write-Host "[deploy] 完了." -ForegroundColor Cyan
