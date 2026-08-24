[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][Alias('Input')][string]$SourceGraph,
    [Parameter(Mandatory = $true)][Alias('Output')][string]$Destination,
    [string]$Configuration = '1С:ERP 2.5',
    [string]$Release = '[релиз не подтвержден]'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$graph = Get-Content -LiteralPath $SourceGraph -Raw -Encoding UTF8 | ConvertFrom-Json
$nodeIds = @{}; foreach ($node in @($graph.nodes.psobject.Properties | ForEach-Object { $_.Value })) { $nodeIds[$node.id] = $true }
$validEdges = @($graph.edges | Where-Object { $nodeIds.ContainsKey([string]$_.source) -and $nodeIds.ContainsKey([string]$_.target) -and -not [string]::IsNullOrWhiteSpace([string]$_.relationship) })
[ordered]@{configuration=$Configuration;release=$Release;nodes=$graph.nodes;edges=$validEdges} | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $Destination -Encoding UTF8
