param(
    [string]$PipelineOutput = ".\output\latest",
    [string]$VisualizationOutput = ".\output\visualization",
    [double]$FocusRadius = 60,
    [ValidateSet("draft", "final")]
    [string]$Quality = "draft",
    [string]$Blender = ""
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$prepare = Join-Path $PSScriptRoot "..\visualizer\prepare_scene.py"
$render = Join-Path $PSScriptRoot "..\visualizer\render.py"
$scene = Join-Path $VisualizationOutput "scene.json"
$preview = Join-Path $VisualizationOutput "scene_preview.png"
$renders = Join-Path $VisualizationOutput "renders"

& $python $prepare `
    (Join-Path $PipelineOutput "normalized_objects.geojsonl") `
    (Join-Path $PipelineOutput "constraint_map.geojsonl") `
    (Join-Path $PipelineOutput "planting_plan.geojsonl") `
    --output $scene `
    --preview $preview `
    --focus-radius $FocusRadius
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$arguments = @($render, $scene, "--output", $renders, "--quality", $Quality)
if ($Blender) { $arguments += @("--blender", $Blender) }
& $python @arguments
exit $LASTEXITCODE
