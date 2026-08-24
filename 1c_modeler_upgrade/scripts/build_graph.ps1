[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Raw,
    [Parameter(Mandatory = $true)][string]$Interface,
    [Parameter(Mandatory = $true)][Alias('Output')][string]$Destination,
    [string]$Configuration = '1С:ERP 2.5',
    [string]$Release = '[указать релиз]',
    [string]$ObjectGraph
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
foreach ($path in @($Raw, $Interface)) { if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "Каталог не найден: $path" } }

$nodes = @{}; $edges = [System.Collections.Generic.List[object]]::new()
function Add-Node($id, $label, $type, $properties) { if (-not $nodes.ContainsKey($id)) { $nodes[$id] = [ordered]@{ id=$id; label=$label; type=$type; properties=$properties } } }
function Add-Edge($source, $target, $relationship, $sourceRef) { $edges.Add([ordered]@{source=$source;target=$target;relationship=$relationship;source_ref=$sourceRef}) }
function Get-RelativeId($prefix, $base, $file) { "$prefix$(([IO.Path]::GetRelativePath($base, $file)).Replace('\','/'))" }

Add-Node 'ROOT_INTERFACE' 'Интерфейс 1С:ERP' 'Root' @{}
Get-ChildItem -LiteralPath $Interface -Filter *.md -File -Recurse | ForEach-Object {
    $id = Get-RelativeId 'IFACE_' $Interface $_.FullName
    $relative = [IO.Path]::GetRelativePath($Interface, $_.FullName)
    $type = if ($relative -match 'Функциональные_опции') {'FunctionalOption'} elseif ($relative -match 'Технические_метаданные') {'Metadata'} else {'InterfaceRoute'}
    $content = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8
    Add-Node $id $_.BaseName $type @{source_path=$_.FullName; path=$relative; search_text=$content}
    Add-Edge 'ROOT_INTERFACE' $id 'contains' $_.FullName
    foreach ($m in [regex]::Matches($content, '\[[^\]]+\]\(([^)]+\.md)\)')) {
        $targetFile = Join-Path $_.DirectoryName $m.Groups[1].Value
        $targetId = Get-RelativeId 'IFACE_' $Interface ([IO.Path]::GetFullPath($targetFile))
        if (Test-Path -LiteralPath $targetFile -PathType Leaf) { Add-Edge $id $targetId 'references' $_.FullName }
    }
}

Add-Node 'ROOT_RAW' 'Методология ИТС 1С:ERP' 'Root' @{}
Get-ChildItem -LiteralPath $Raw -Filter *.md -File -Recurse | ForEach-Object {
    $id = Get-RelativeId 'RAW_' $Raw $_.FullName; $content = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8
    Add-Node $id $_.BaseName 'MethodologyArticle' @{source_path=$_.FullName;path=[IO.Path]::GetRelativePath($Raw,$_.FullName);search_text=$content}
    Add-Edge 'ROOT_RAW' $id 'contains' $_.FullName
}

if ($ObjectGraph) {
    if (-not (Test-Path -LiteralPath $ObjectGraph -PathType Leaf)) { throw "Файл объектного графа не найден: $ObjectGraph" }
    $objects = Get-Content -LiteralPath $ObjectGraph -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($node in @($objects.nodes)) { Add-Node $node.id $node.label $node.type $node.properties }
    foreach ($edge in @($objects.edges)) { Add-Edge $edge.source $edge.target $edge.relationship $edge.source_ref }
}

[ordered]@{configuration=$Configuration;release=$Release;nodes=$nodes;edges=@($edges)} | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $Destination -Encoding UTF8
