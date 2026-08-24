param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$entryPoint = Join-Path $repoRoot "src\consultant_cli\__main__.py"

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name consultant `
    --paths (Join-Path $repoRoot "src") `
    --distpath (Join-Path $repoRoot "dist") `
    --workpath (Join-Path $repoRoot "build\pyinstaller") `
    --specpath (Join-Path $repoRoot "build") `
    $entryPoint

& (Join-Path $repoRoot "dist\consultant.exe") --version
