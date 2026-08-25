[CmdletBinding()]
param(
    [string]$OutputDirectory = "",
    [string]$Version = "0.4.3"
)

$ErrorActionPreference = "Stop"
$deploymentRoot = Split-Path -Parent $PSScriptRoot
$installerRoot = Join-Path $deploymentRoot "installer"
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $deploymentRoot "dist" }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    throw "Go не найден. Установите Go 1.22+ и повторите сборку."
}

$targets = @(
    @{ GOOS = "windows"; GOARCH = "amd64"; Name = "1c-consultant-installer-windows-x64.exe" },
    @{ GOOS = "linux";   GOARCH = "amd64"; Name = "1c-consultant-installer-linux-x64" },
    @{ GOOS = "linux";   GOARCH = "arm64"; Name = "1c-consultant-installer-linux-arm64" },
    @{ GOOS = "darwin";  GOARCH = "amd64"; Name = "1c-consultant-installer-macos-x64" },
    @{ GOOS = "darwin";  GOARCH = "arm64"; Name = "1c-consultant-installer-macos-arm64" }
)

Push-Location $installerRoot
try {
    & go test ./...
    if ($LASTEXITCODE -ne 0) { throw "go test завершился с кодом $LASTEXITCODE" }
    foreach ($target in $targets) {
        $env:GOOS = $target.GOOS
        $env:GOARCH = $target.GOARCH
        $env:CGO_ENABLED = "0"
        $output = Join-Path $OutputDirectory $target.Name
        & go build -trimpath -ldflags "-s -w" -o $output ./cmd/installer
        if ($LASTEXITCODE -ne 0) { throw "Сборка $($target.Name) завершилась с кодом $LASTEXITCODE" }
    }
}
finally {
    Remove-Item Env:GOOS, Env:GOARCH, Env:CGO_ENABLED -ErrorAction SilentlyContinue
    Pop-Location
}

Get-ChildItem -LiteralPath $OutputDirectory -File | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant(), $_.Name
} | Set-Content -Encoding ascii -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS")

Write-Host "Installer ${Version}: $OutputDirectory"
