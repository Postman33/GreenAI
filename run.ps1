param(
    [string]$InputDxf = '',
    [string]$OutputDirectory = '.\output\latest',
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not $InputDxf) {
        $InputDxf = Join-Path $PSScriptRoot 'Пилотный проект 20 улиц\input_10001759_bound.dxf'
    }
    if (-not (Test-Path -LiteralPath $InputDxf -PathType Leaf)) {
        throw 'Input DXF not found. Run .\init.ps1 first, or pass -InputDxf with your DXF path.'
    }
    & (Join-Path $PSScriptRoot 'scripts\run_pipeline.ps1') `
        -InputDxf $InputDxf -OutputDirectory $OutputDirectory `
        -PipelineMode full -PlantingPreset dense_mixed -ValidateOnly:$ValidateOnly
} finally {
    Pop-Location
}
