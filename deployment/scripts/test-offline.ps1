[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$BundlePath
)

$ErrorActionPreference = "Stop"
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("1c-consultant-install-test-" + [guid]::NewGuid().ToString("N"))
try {
    & $Installer install --offline-path $BundlePath --application --non-interactive --data-dir $testRoot
    if ($LASTEXITCODE -ne 0) { throw "install завершился с кодом $LASTEXITCODE" }
    & $Installer status --data-dir $testRoot
    if ($LASTEXITCODE -ne 0) { throw "status завершился с кодом $LASTEXITCODE" }
    $state = Get-Content -Raw -LiteralPath (Join-Path $testRoot "config\installed.json") | ConvertFrom-Json
    if (-not $state.active_application) { throw "active_application не записан" }
    & (Join-Path $testRoot "1C-Consultant.cmd") --version
    if ($LASTEXITCODE -ne 0) { throw "launcher завершился с кодом $LASTEXITCODE" }
    Write-Host "Offline self-check пройден."
}
finally {
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
