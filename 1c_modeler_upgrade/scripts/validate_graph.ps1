[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Graph
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Graph -PathType Leaf)) { throw "Граф не найден: $Graph" }

$data = Get-Content -LiteralPath $Graph -Raw -Encoding UTF8 | ConvertFrom-Json
$errors = [System.Collections.Generic.List[string]]::new()
if ($null -eq $data.nodes) { $errors.Add('Отсутствует nodes.') }
if ($null -eq $data.edges) { $errors.Add('Отсутствует edges.') }

$index = @{}
if ($null -ne $data.nodes) {
    foreach ($node in @($data.nodes.psobject.Properties | ForEach-Object { $_.Value })) {
        if ([string]::IsNullOrWhiteSpace([string]$node.id)) { $errors.Add('Узел без id.'); continue }
        if ($index.ContainsKey($node.id)) { $errors.Add("Дублирующийся id узла: $($node.id)") }
        $index[$node.id] = $true
        foreach ($field in 'label','type') { if ([string]::IsNullOrWhiteSpace([string]$node.$field)) { $errors.Add("Узел $($node.id) без $field.") } }
    }
}

$unresolved = @()
if ($null -ne $data.edges) {
    foreach ($edge in @($data.edges)) {
        if (-not $index.ContainsKey([string]$edge.source) -or -not $index.ContainsKey([string]$edge.target)) { $unresolved += $edge }
        if ([string]::IsNullOrWhiteSpace([string]$edge.relationship)) { $errors.Add("Связь $($edge.source) -> $($edge.target) без relationship.") }
    }
}

[pscustomobject]@{
    Nodes = $index.Count; Edges = @($data.edges).Count; UnresolvedEdges = $unresolved.Count
    Valid = ($errors.Count -eq 0 -and $unresolved.Count -eq 0); Errors = @($errors)
    UnresolvedSamples = @($unresolved | Select-Object -First 10)
} | ConvertTo-Json -Depth 6
if ($errors.Count -gt 0 -or $unresolved.Count -gt 0) { exit 1 }

