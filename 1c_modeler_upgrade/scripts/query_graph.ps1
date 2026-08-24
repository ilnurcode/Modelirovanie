[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Graph,
    [Parameter(Mandatory = $true)][string]$Query,
    [ValidateRange(0, 5)][int]$Depth = 1,
    [string[]]$Type
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Graph -PathType Leaf)) { throw "Граф не найден: $Graph" }
$data = Get-Content -LiteralPath $Graph -Raw -Encoding UTF8 | ConvertFrom-Json
$nodes = @($data.nodes.psobject.Properties | ForEach-Object { $_.Value })
$byId = @{}; foreach ($node in $nodes) { $byId[$node.id] = $node }
$configuration = if ($null -ne $data.psobject.Properties['configuration']) { $data.configuration } else { $null }
$release = if ($null -ne $data.psobject.Properties['release']) { $data.release } else { $null }
$needle = [regex]::Escape($Query.Trim())
$matches = @($nodes | Where-Object {
    $searchText = ''
    if ($null -ne $_.properties -and $null -ne $_.properties.psobject.Properties['search_text']) {
        $searchText = [string]$_.properties.search_text
    }
    ($null -eq $Type -or $Type.Count -eq 0 -or $_.type -in $Type) -and
    (([string]$_.label -match $needle) -or ([string]$_.id -match $needle) -or ($searchText -match $needle))
})

$visited = @{}; $frontier = @($matches.id); foreach ($id in $frontier) { $visited[$id] = $true }
$edges = [System.Collections.Generic.List[object]]::new()
for ($level = 0; $level -lt $Depth; $level++) {
    $next = [System.Collections.Generic.List[string]]::new()
    foreach ($edge in @($data.edges)) {
        if ($frontier -contains $edge.source -or $frontier -contains $edge.target) {
            $edges.Add($edge)
            foreach ($id in @([string]$edge.source, [string]$edge.target)) { if (-not $visited.ContainsKey($id)) { $visited[$id] = $true; $next.Add($id) } }
        }
    }
    $frontier = @($next); if ($frontier.Count -eq 0) { break }
}

[pscustomobject]@{
    query = $Query; configuration = $configuration; release = $release
    matched_nodes = @($matches); related_nodes = @($visited.Keys | Where-Object { $byId.ContainsKey($_) } | ForEach-Object { $byId[$_] })
    related_edges = @($edges | Select-Object -Unique)
} | ConvertTo-Json -Depth 10
