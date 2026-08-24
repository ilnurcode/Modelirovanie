[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,
    [string]$ProjectRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$OutputDirectory = "",
    [string]$Python = "py",
    [string[]]$PythonArguments = @("-3")
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $ProjectRoot "release" }
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$packageManifest = Get-Content -Raw -LiteralPath (Join-Path $ProjectRoot "PACKAGE_MANIFEST.json") | ConvertFrom-Json
$version = [string]$packageManifest.application_version
$configurationVersion = [string]$packageManifest.configuration_pack.release
$graphVersion = [string]$packageManifest.configuration_pack.graph_version
if (-not $graphVersion) { $graphVersion = $version }
$appName = "1c-consultant-$version-windows-x64.zip"
$graphName = "erp-$configurationVersion-graph-$graphVersion.zip"
$appArchive = Join-Path $OutputDirectory $appName
$graphArchive = Join-Path $OutputDirectory $graphName
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("1c-consultant-release-" + [guid]::NewGuid().ToString("N"))

try {
    & (Join-Path $ProjectRoot "consultant.exe") --version | ForEach-Object {
        if ($_ -ne $version) { throw "consultant.exe имеет версию $_, ожидалась $version" }
    }
    & $Python @PythonArguments (Join-Path $ProjectRoot "scripts\validate_repository.py")
    if ($LASTEXITCODE -ne 0) { throw "validate_repository.py завершился с кодом $LASTEXITCODE" }

    $appStage = Join-Path $stage "application"
    $graphStage = Join-Path $stage "graph"
    New-Item -ItemType Directory -Force -Path $appStage, $graphStage | Out-Null
    $excludedTopLevel = @(".git", ".venv", "build", "dist", "release", "results", "deployment", "tests")
    Get-ChildItem -LiteralPath $ProjectRoot -Force | Where-Object { $_.Name -notin $excludedTopLevel } | ForEach-Object {
        if ($_.Name -eq "1c_modeler_upgrade") {
            $modelerStage = Join-Path $appStage $_.Name
            New-Item -ItemType Directory -Force -Path $modelerStage | Out-Null
            Get-ChildItem -LiteralPath $_.FullName -Force | Where-Object Name -ne "graphs" |
                Copy-Item -Destination $modelerStage -Recurse -Force
        } else {
            Copy-Item -LiteralPath $_.FullName -Destination $appStage -Recurse -Force
        }
    }
    $stagedIntegrity = Join-Path $appStage "FILES.sha256"
    Remove-Item -LiteralPath $stagedIntegrity -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $appStage -File -Recurse | ForEach-Object {
        $relative = [IO.Path]::GetRelativePath($appStage, $_.FullName).Replace('\', '/')
        [pscustomobject]@{ Path = $relative; Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant() }
    } | Sort-Object Path | ForEach-Object { "{0}  {1}" -f $_.Hash, $_.Path } |
        Set-Content -Encoding utf8NoBOM -LiteralPath $stagedIntegrity
    Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "1c_modeler_upgrade\graphs") -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $graphStage -Recurse -Force
    }

    if (Test-Path -LiteralPath $appArchive) { Remove-Item -LiteralPath $appArchive -Force }
    if (Test-Path -LiteralPath $graphArchive) { Remove-Item -LiteralPath $graphArchive -Force }
    Compress-Archive -Path (Join-Path $appStage "*") -DestinationPath $appArchive -CompressionLevel Optimal
    Compress-Archive -Path (Join-Path $graphStage "*") -DestinationPath $graphArchive -CompressionLevel Optimal

    $appFile = Get-Item -LiteralPath $appArchive
    $graphFile = Get-Item -LiteralPath $graphArchive
    $base = $BaseUrl.TrimEnd('/')
    $manifest = [ordered]@{
        schema_version = 1
        application = [ordered]@{
            version = $version
            artifacts = @([ordered]@{
                os = "windows"; arch = "x64"; url = "$base/$appName"
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $appArchive).Hash.ToLowerInvariant()
                size = $appFile.Length; executable = "consultant.exe"; health_check_args = @("--version")
            })
        }
        graphs = @([ordered]@{
            id = "erp-$configurationVersion"; name = "1С:ERP Управление предприятием 2"
            configuration_version = $configurationVersion; graph_version = $graphVersion; url = "$base/$graphName"
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $graphArchive).Hash.ToLowerInvariant()
            size = $graphFile.Length; minimum_application_version = $version
        })
    }
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8NoBOM -LiteralPath (Join-Path $OutputDirectory "manifest.json")
}
finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}

Write-Host "Release подготовлен: $OutputDirectory"
