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
