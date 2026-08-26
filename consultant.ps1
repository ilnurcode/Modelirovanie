$installedExecutable = Join-Path $PSScriptRoot $(if ($env:OS -eq "Windows_NT") { "consultant.exe" } else { "consultant" })
if (Test-Path -LiteralPath $installedExecutable -PathType Leaf) {
    if (-not $env:CONSULTANT_DATA_DIR) {
        $env:CONSULTANT_DATA_DIR = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    }
    & $installedExecutable --repo $PSScriptRoot @args
    exit $LASTEXITCODE
}
$env:PYTHONPATH = Join-Path $PSScriptRoot "src"
$env:PYTHONUTF8 = "1"
& py -3 -m consultant_cli @args
exit $LASTEXITCODE
