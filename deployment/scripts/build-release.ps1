[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^https://')]
    [string]$BaseUrl,
    [string]$ProjectRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$ApplicationDirectory = "application-packages",
    [string]$OutputDirectory = "",
    [string]$GraphDatabase = "",
    [string]$GraphDatabaseSha256 = "",
    [string]$Python = "py",
    [string[]]$PythonArguments = @("-3")
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$ApplicationDirectory = [IO.Path]::GetFullPath($ApplicationDirectory)
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $ProjectRoot "release" }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

if (-not $GraphDatabase) { $GraphDatabase = $env:ERP_GRAPH_DATABASE }
if (-not $GraphDatabase) {
    throw "Укажите -GraphDatabase или ERP_GRAPH_DATABASE: полный graph ZIP обязан содержать erp_graph_mcp.sqlite"
}
$GraphDatabase = (Resolve-Path -LiteralPath $GraphDatabase -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $GraphDatabase -PathType Leaf)) {
    throw "Не найден SQLite-граф: $GraphDatabase"
}
$stream = [IO.File]::OpenRead($GraphDatabase)
try {
    $header = New-Object byte[] 16
    if ($stream.Read($header, 0, $header.Length) -ne $header.Length -or
        [Text.Encoding]::ASCII.GetString($header) -ne "SQLite format 3`0") {
        throw "Файл графа не является SQLite 3: $GraphDatabase"
    }
}
finally {
    $stream.Dispose()
}

if ($GraphDatabaseSha256) {
    $actualGraphSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $GraphDatabase).Hash.ToLowerInvariant()
    if ($actualGraphSha256 -ne $GraphDatabaseSha256.ToLowerInvariant()) {
        throw "SHA-256 SQLite-графа не совпадает с ожидаемым значением"
    }
}

$package = Get-Content -Raw -LiteralPath (Join-Path $ProjectRoot "PACKAGE_MANIFEST.json") | ConvertFrom-Json
$version = [string]$package.application_version
$installerVersion = [string]$package.installer_version
$piVersion = [string]$package.pi_version
$piPackage = [string]$package.pi_package
$nodeVersion = [string]$package.node_version
$configurationVersion = [string]$package.configuration_pack.release
$graphVersion = [string]$package.configuration_pack.graph_version
if (-not $graphVersion) { $graphVersion = $version }

& $Python @PythonArguments (Join-Path $ProjectRoot "scripts\validate_repository.py")
if ($LASTEXITCODE -ne 0) { throw "validate_repository.py завершился с кодом $LASTEXITCODE" }

$expected = @("windows-x64", "linux-x64", "linux-arm64", "macos-x64", "macos-arm64")
$artifacts = @()
foreach ($platform in $expected) {
    $name = "1c-consultant-$version-$platform.zip"
    $source = Join-Path $ApplicationDirectory $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Не найден пакет приложения: $name" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $OutputDirectory $name) -Force
    $parts = $platform.Split("-")
    $artifacts += [ordered]@{
        os = $parts[0]
        arch = $parts[1]
        url = "$($BaseUrl.TrimEnd('/'))/$name"
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash.ToLowerInvariant()
        size = (Get-Item -LiteralPath $source).Length
        executable = $(if ($parts[0] -eq "windows") { "consultant.exe" } else { "consultant" })
        health_check_args = @("--version")
    }
}

$nodeBaseUrl = "https://nodejs.org/download/release/v$nodeVersion"
$nodeArtifacts = @(
    [ordered]@{ os = "windows"; arch = "x64"; url = "$nodeBaseUrl/node-v$nodeVersion-win-x64.zip"; sha256 = "1177b4137ba5adaa56354ae40f1080c7450e8ae09cecb47da459d1c52ac99f97"; node = "node-v$nodeVersion-win-x64/node.exe"; npm = "node-v$nodeVersion-win-x64/node_modules/npm/bin/npm-cli.js" },
    [ordered]@{ os = "linux"; arch = "x64"; url = "$nodeBaseUrl/node-v$nodeVersion-linux-x64.tar.gz"; sha256 = "b294a556e639d64338823920e5866c21c02741742d2e1529ee1a225c1ec9252a"; node = "node-v$nodeVersion-linux-x64/bin/node"; npm = "node-v$nodeVersion-linux-x64/lib/node_modules/npm/bin/npm-cli.js" },
    [ordered]@{ os = "linux"; arch = "arm64"; url = "$nodeBaseUrl/node-v$nodeVersion-linux-arm64.tar.gz"; sha256 = "013b59cfd2819703a6f4a14ab891fc46fc2a4e3f5bcd92de3fb4929b43e35b30"; node = "node-v$nodeVersion-linux-arm64/bin/node"; npm = "node-v$nodeVersion-linux-arm64/lib/node_modules/npm/bin/npm-cli.js" },
    [ordered]@{ os = "macos"; arch = "x64"; url = "$nodeBaseUrl/node-v$nodeVersion-darwin-x64.tar.gz"; sha256 = "58e99022c2ff89395576cc7fd4d98cea24bb68081475d5f88b801ee8729fb026"; node = "node-v$nodeVersion-darwin-x64/bin/node"; npm = "node-v$nodeVersion-darwin-x64/lib/node_modules/npm/bin/npm-cli.js" },
    [ordered]@{ os = "macos"; arch = "arm64"; url = "$nodeBaseUrl/node-v$nodeVersion-darwin-arm64.tar.gz"; sha256 = "61130f394c1630d211dd50aecc4353d379480f36d3ac913cd85dbba1aed585c6"; node = "node-v$nodeVersion-darwin-arm64/bin/node"; npm = "node-v$nodeVersion-darwin-arm64/lib/node_modules/npm/bin/npm-cli.js" }
)

$installerArtifacts = @()
$installerNames = [ordered]@{
    "windows-x64" = "1c-consultant-installer-windows-x64.exe"
    "linux-x64" = "1c-consultant-installer-linux-x64"
    "linux-arm64" = "1c-consultant-installer-linux-arm64"
    "macos-x64" = "1c-consultant-installer-macos-x64"
    "macos-arm64" = "1c-consultant-installer-macos-arm64"
}
foreach ($platform in $expected) {
    $name = $installerNames[$platform]
    $source = Join-Path $OutputDirectory $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Не найден installer: $name" }
    $parts = $platform.Split("-")
    $installerArtifacts += [ordered]@{
        os = $parts[0]
        arch = $parts[1]
        url = "$($BaseUrl.TrimEnd('/'))/$name"
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash.ToLowerInvariant()
        size = (Get-Item -LiteralPath $source).Length
        filename = $name
    }
}

$graphName = "erp-$configurationVersion-graph-$graphVersion.zip"
$graphArchive = Join-Path $OutputDirectory $graphName
$graphStage = Join-Path ([System.IO.Path]::GetTempPath()) ("1c-consultant-graph-" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Force -Path $graphStage | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "1c_modeler_upgrade\graphs") -Force |
        Copy-Item -Destination $graphStage -Recurse -Force
    $databaseStage = Join-Path $graphStage "graph_rag_data"
    New-Item -ItemType Directory -Force -Path $databaseStage | Out-Null
    Copy-Item -LiteralPath $GraphDatabase -Destination (Join-Path $databaseStage "erp_graph_mcp.sqlite") -Force
    if (Test-Path -LiteralPath $graphArchive) { Remove-Item -LiteralPath $graphArchive -Force }
    Compress-Archive -Path (Join-Path $graphStage "*") -DestinationPath $graphArchive -CompressionLevel Optimal
}
finally {
    if (Test-Path -LiteralPath $graphStage) { Remove-Item -LiteralPath $graphStage -Recurse -Force }
}

$manifest = [ordered]@{
    schema_version = 1
    application = [ordered]@{ version = $version; artifacts = $artifacts }
    installer = [ordered]@{ version = $installerVersion; artifacts = $installerArtifacts }
    pi = [ordered]@{ version = $piVersion; package = $piPackage; node_version = $nodeVersion; node_artifacts = $nodeArtifacts }
    graphs = @([ordered]@{
        id = "erp-$configurationVersion"
        name = "1С:ERP Управление предприятием 2"
        configuration_version = $configurationVersion
        graph_version = $graphVersion
        url = "$($BaseUrl.TrimEnd('/'))/$graphName"
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $graphArchive).Hash.ToLowerInvariant()
        size = (Get-Item -LiteralPath $graphArchive).Length
        minimum_application_version = $version
    })
}
$manifest | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8NoBOM -LiteralPath (Join-Path $OutputDirectory "manifest.json")
Write-Host "Release подготовлен: $OutputDirectory"
