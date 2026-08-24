$env:PYTHONPATH = Join-Path $PSScriptRoot "src"
$env:PYTHONUTF8 = "1"
& py -3 -m consultant_cli @args
exit $LASTEXITCODE
