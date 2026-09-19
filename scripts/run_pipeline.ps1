param(
    [Parameter(Mandatory = $false)]
    [string]$InputDxf = "",

    [Parameter(Mandatory = $false)]
    [string]$OutputDirectory = ".\output",

    [Parameter(Mandatory = $false)]
    [double]$DxfUnitsPerMeter = 1.0,

    [Parameter(Mandatory = $false)]
    [string]$UtilityDetectorModel = "",

    [Parameter(Mandatory = $false)]
    [switch]$SkipDatabaseStart
)

$ErrorActionPreference = "Stop"

$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $workspace ".venv\Scripts\python.exe"
$extractor = Join-Path $workspace "dxf_extract_go.exe"
$inputPath = if ([string]::IsNullOrWhiteSpace($InputDxf)) {
    $inputMatches = @(
        Get-ChildItem -LiteralPath $workspace -Filter "input_10001759_bound.dxf" -File -Recurse
    )
    if ($inputMatches.Count -ne 1) {
        throw "Expected exactly one input_10001759_bound.dxf in the workspace, found $($inputMatches.Count). Pass -InputDxf explicitly."
    }
    $inputMatches[0].FullName
} else {
    $inputCandidate = if ([IO.Path]::IsPathRooted($InputDxf)) {
        $InputDxf
    } else {
        Join-Path $workspace $InputDxf
    }
    (Resolve-Path -LiteralPath $inputCandidate).Path
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment was not found: $python. Create .venv and install requirements.txt first."
}
if (-not (Test-Path -LiteralPath $extractor)) {
    throw "Go extractor was not found: $extractor. Build parser/dxf_extract_go first."
}
if (
    [double]::IsNaN($DxfUnitsPerMeter) -or
    [double]::IsInfinity($DxfUnitsPerMeter) -or
    $DxfUnitsPerMeter -le 0
) {
    throw "DxfUnitsPerMeter must be greater than zero."
}

$outputPath = [IO.Path]::GetFullPath((Join-Path $workspace $OutputDirectory))
if (-not $outputPath.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDirectory must be inside the workspace: $workspace"
}
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null

$objects = Join-Path $outputPath "extracted_objects.jsonl"
$surfaces = Join-Path $outputPath "surface_candidates_raw.jsonl"
$normalized = Join-Path $outputPath "normalized_objects.geojsonl"
$normalizationReport = Join-Path $outputPath "normalization_report.json"
$cleanedUtilities = Join-Path $outputPath "cleaned_utilities.geojsonl"
$reviewUtilities = Join-Path $outputPath "review_utility_graphics.geojsonl"
$rejectedUtilities = Join-Path $outputPath "rejected_utility_graphics.geojsonl"
$utilityCleaningReport = Join-Path $outputPath "utility_cleaning_report.json"
$utilityDebugDxf = Join-Path $outputPath "utility_cleaning_debug.dxf"
$utilityDebugPng = Join-Path $outputPath "utility_cleaning_debug.png"
$reconstructedUtilities = Join-Path $outputPath "reconstructed_utilities.geojsonl"
$inferredUtilityConnections = Join-Path $outputPath "inferred_utility_connections.geojsonl"
$reviewUtilityConnections = Join-Path $outputPath "review_utility_connections.geojsonl"
$networkReconstructionReport = Join-Path $outputPath "network_reconstruction_report.json"
$networkReconstructionDebugDxf = Join-Path $outputPath "network_reconstruction_debug.dxf"
$surfaceDxf = Join-Path $outputPath "surface_candidates.dxf"
$surfacePng = Join-Path $outputPath "surface_candidates.png"
$surfaceReport = Join-Path $outputPath "surface_candidates_report.json"
$constraints = Join-Path $outputPath "constraint_map.geojsonl"
$constraintReport = Join-Path $outputPath "constraint_report.json"
$zones = Join-Path $outputPath "plant_allow_zones.geojsonl"
$zoneReport = Join-Path $outputPath "plant_allow_zones_report.json"
$debugDxf = Join-Path $outputPath "plant_allow_zones_debug.dxf"
$debugPng = Join-Path $outputPath "plant_allow_zones_debug.png"
$verificationReport = Join-Path $outputPath "verification_report.json"
$resultDxf = Join-Path $outputPath "result_with_plant_zones.dxf"

$detectorModelPath = if ([string]::IsNullOrWhiteSpace($UtilityDetectorModel)) {
    $defaultBundle = Join-Path $workspace "models\utility_detector\latest"
    if (Test-Path -LiteralPath $defaultBundle) {
        (Resolve-Path -LiteralPath $defaultBundle).Path
    } else {
        $null
    }
} else {
    $modelCandidate = if ([IO.Path]::IsPathRooted($UtilityDetectorModel)) {
        $UtilityDetectorModel
    } else {
        Join-Path $workspace $UtilityDetectorModel
    }
    (Resolve-Path -LiteralPath $modelCandidate).Path
}

Push-Location $workspace
try {
    if (-not $SkipDatabaseStart) {
        Write-Host "[1/11] Starting PostGIS"
        docker compose up -d --wait
    } else {
        Write-Host "[1/11] PostGIS start skipped"
    }

    Write-Host "[2/11] Extracting semantic DXF objects"
    & $extractor --config .\src\core\config.yaml --output $objects $inputPath
    if ($LASTEXITCODE -ne 0) { throw "Semantic extraction failed" }

    Write-Host "[3/11] Extracting surface candidates"
    & $extractor --config .\src\core\surface_inspector_config.yaml --output $surfaces $inputPath
    if ($LASTEXITCODE -ne 0) { throw "Surface extraction failed" }

    Write-Host "[4/11] Normalizing semantic geometry"
    & $python .\src\normalizer.py $objects --output $normalized --report $normalizationReport
    if ($LASTEXITCODE -ne 0) { throw "Normalization failed" }

    Write-Host "[5/11] Cleaning engineering utility geometry"
    if ($null -ne $detectorModelPath) {
        Write-Host "       Using supervised ONNX utility detector: $detectorModelPath"
        & $python .\utility_detector\detector.py predict $objects `
            --model $detectorModelPath `
            --output $cleanedUtilities `
            --review-output $reviewUtilities `
            --rejected-output $rejectedUtilities `
            --report $utilityCleaningReport `
            --debug-dxf $utilityDebugDxf `
            --debug-png $utilityDebugPng
    } else {
        & $python .\utility_cleaner\clean_utilities.py $objects `
            --work-boundary $normalized `
            --output $cleanedUtilities `
            --review-output $reviewUtilities `
            --rejected-output $rejectedUtilities `
            --report $utilityCleaningReport `
            --debug-dxf $utilityDebugDxf `
            --debug-png $utilityDebugPng `
            --dxf-units-per-meter $DxfUnitsPerMeter
    }
    if ($LASTEXITCODE -ne 0) { throw "Utility cleaning failed" }

    Write-Host "[6/11] Reconstructing utility gaps and junctions"
    & $python .\src\network_reconstructor.py $cleanedUtilities `
        --output $reconstructedUtilities `
        --inferred-output $inferredUtilityConnections `
        --review-output $reviewUtilityConnections `
        --report $networkReconstructionReport `
        --debug-dxf $networkReconstructionDebugDxf `
        --dxf-units-per-meter $DxfUnitsPerMeter
    if ($LASTEXITCODE -ne 0) { throw "Utility network reconstruction failed" }

    Write-Host "[7/11] Rendering source surface diagnostics"
    & $python .\src\surface_inspector.py $surfaces $normalized `
        --dxf-output $surfaceDxf `
        --png-output $surfacePng `
        --report $surfaceReport
    if ($LASTEXITCODE -ne 0) { throw "Surface inspection failed" }

    Write-Host "[8/11] Building common constraints and reconstructed road"
    & $python .\src\constraint_builder.py $normalized $surfaces `
        --output $constraints `
        --report $constraintReport
    if ($LASTEXITCODE -ne 0) { throw "Constraint building failed" }

    Write-Host "[9/11] Applying plant rules to reconstructed utility geometry"
    & $python .\src\plant_allow_zone.py $constraints $normalized `
        --output $zones `
        --report $zoneReport `
        --utility-geometries $reconstructedUtilities `
        --dxf-units-per-meter $DxfUnitsPerMeter
    if ($LASTEXITCODE -ne 0) { throw "Plant allow-zone calculation failed" }

    Write-Host "[10/11] Rendering and verifying calculated zones"
    & $python .\src\plant_allow_zone_debug.py $zones $constraints $normalized `
        --dxf-output $debugDxf `
        --png-output $debugPng `
        --utility-geometries $reconstructedUtilities `
        --raw-objects $objects
    if ($LASTEXITCODE -ne 0) { throw "Debug export failed" }
    & $python .\scripts\verify_outputs.py $constraints $zones --output $verificationReport
    if ($LASTEXITCODE -ne 0) { throw "Spatial verification failed" }

    Write-Host "[11/11] Writing result layers into a copy of the source DXF"
    & $python .\src\dxf_exporter.py $inputPath $zones `
        --constraint-map $constraints `
        --output $resultDxf
    if ($LASTEXITCODE -ne 0) { throw "Final DXF export failed" }

    Write-Host ""
    Write-Host "Pipeline completed"
    Write-Host "Final DXF: $resultDxf"
    Write-Host "Preview PNG: $debugPng"
    Write-Host "Rule report: $zoneReport"
    Write-Host "Network reconstruction: $networkReconstructionReport"
    Write-Host "Verification: $verificationReport"
} finally {
    Pop-Location
}
