param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$keyFiles = @(
    (Join-Path $projectRoot ".env"),
    (Join-Path $projectRoot "..\RAGAgent\.env")
)
$parentRoot = Split-Path -Parent $projectRoot
$consultantEnv = Get-ChildItem -LiteralPath $parentRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "*1C-Consultant*" } |
    ForEach-Object { Join-Path $_.FullName ".env" } |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
if ($consultantEnv) { $keyFiles += $consultantEnv }
if ([string]::IsNullOrWhiteSpace($env:WORMSOFT_API_KEY)) {
    foreach ($file in $keyFiles) {
        if (-not (Test-Path -LiteralPath $file)) { continue }
        foreach ($line in Get-Content -LiteralPath $file -Encoding UTF8) {
            if ($line -match '^\s*(?:NEWAGENT_API_KEY|WORMSOFT_API_KEY)\s*=\s*(.+?)\s*$') {
                $value = $Matches[1].Trim().Trim('"').Trim("'")
                if (-not [string]::IsNullOrWhiteSpace($value)) { $env:WORMSOFT_API_KEY = $value; break }
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($env:WORMSOFT_API_KEY)) { break }
    }
}
if ([string]::IsNullOrWhiteSpace($env:WORMSOFT_API_KEY)) {
    throw "WORMSOFT_API_KEY was not found. Add it to NewAgent/.env or keep it in RAGAgent/.env."
}
$env:PYTHONPATH = Join-Path $projectRoot "src"
Set-Location -LiteralPath $projectRoot
if ($Check) {
    & (Join-Path $projectRoot "consultant.ps1") --repo $projectRoot --json runtime-status
    exit $LASTEXITCODE
}
$pi = Get-Command "pi.cmd" -ErrorAction SilentlyContinue
if (-not $pi) { $pi = Get-Command "pi" -ErrorAction SilentlyContinue }
if (-not $pi) { throw "Pi was not found. Install @earendil-works/pi-coding-agent." }
& $pi.Source --approve --offline --model wormsoft-gateway/wormsoft/agent/medium --name "NewAgent ERP"
exit $LASTEXITCODE
