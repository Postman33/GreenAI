param(
    [Parameter(Mandatory = $false)]
    [string]$InputDxf = "",

    [Parameter(Mandatory = $false)]
    [string]$OutputDirectory = ".\output\latest",

    [Parameter(Mandatory = $false)]
    [string]$SemanticConfig = ".\src\core\config.yaml",

    [Parameter(Mandatory = $false)]
    [string]$SurfaceConfig = ".\src\core\surface_inspector_config.yaml",

    [Parameter(Mandatory = $false)]
    [double]$DxfUnitsPerMeter = 1.0,

    [Parameter(Mandatory = $false)]
    [string]$UtilityDetectorModel = "",

    [Parameter(Mandatory = $false)]
    [string]$PlantingRequest = "",

    [Parameter(Mandatory = $false)]
    [ValidateSet("balanced_mixed", "dense_mixed", "tree_lawn", "trees_only", "shrub_lawn", "shrubs_only", "lawn_only")]
    [string]$PlantingPreset = "dense_mixed",

    [Parameter(Mandatory = $false)]
    [double]$TreeSpacingM = 0.0,

    [Parameter(Mandatory = $false)]
    [int]$TreeMaxCount = 0,

    [Parameter(Mandatory = $false)]
    [int]$DiagnosticRejectedMaxCount = -1,

    [Parameter(Mandatory = $false)]
    [ValidateSet("auto", "full", "lean", "fast")]
    [string]$PipelineMode = "auto",

    [Parameter(Mandatory = $false)]
    [switch]$SkipDatabaseStart
)

$ErrorActionPreference = "Stop"

$stageTimings = [Collections.Generic.List[object]]::new()
$pipelineStopwatch = [Diagnostics.Stopwatch]::StartNew()

function Invoke-TimedPipelineStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,

        [Parameter(Mandatory = $false)]
        [string[]]$Artifacts = @()
    )

    $startedAt = Get-Date
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $status = "passed"
    $errorMessage = $null
    try {
        & $Action
    }
    catch {
        $status = "failed"
        $errorMessage = $_.Exception.Message
        throw
    }
    finally {
        $stopwatch.Stop()
        $artifactDetails = @(
            foreach ($artifact in $Artifacts) {
                if ([string]::IsNullOrWhiteSpace($artifact)) { continue }
                $item = Get-Item -LiteralPath $artifact -ErrorAction SilentlyContinue
                [pscustomobject][ordered]@{
                    path = $artifact
                    exists = $null -ne $item
                    bytes = if ($null -ne $item -and -not $item.PSIsContainer) { [long]$item.Length } else { $null }
                }
            }
        )
        $artifactBytes = [long](($artifactDetails | ForEach-Object {
            if ($null -ne $_.bytes) { [long]$_.bytes }
        } | Measure-Object -Sum).Sum)
        $stageTimings.Add([pscustomobject][ordered]@{
            id = $Id
            name = $Name
            status = $status
            started_at = $startedAt.ToString("o")
            elapsed_seconds = [math]::Round($stopwatch.Elapsed.TotalSeconds, 6)
            artifact_bytes = $artifactBytes
            artifacts = $artifactDetails
            error = $errorMessage
        })
    }
}

function Add-SkippedPipelineStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [ValidateSet("cached", "skipped")]
        [string]$Status
    )
    $stageTimings.Add([pscustomobject][ordered]@{
        id = $Id
        name = $Name
        status = $Status
        started_at = $null
        elapsed_seconds = 0.0
        artifact_bytes = 0
        artifacts = @()
        error = $null
    })
}

function Write-PipelineTimingReports {
    param(
        [Parameter(Mandatory = $true)]
        [string]$OutputDirectory,

        [Parameter(Mandatory = $true)]
        [string]$Mode,

        [Parameter(Mandatory = $true)]
        [string]$InputFile,

        [Parameter(Mandatory = $false)]
        [string]$FinalStatus = "passed"
    )

    if (-not (Test-Path -LiteralPath $OutputDirectory)) {
        New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    }
    $pipelineStopwatch.Stop()
    $executedSeconds = [double](
        ($stageTimings | Where-Object { $_.status -in @("passed", "failed") } |
            Measure-Object -Property elapsed_seconds -Sum).Sum
    )
    $totalSeconds = $pipelineStopwatch.Elapsed.TotalSeconds
    $rows = @(
        foreach ($stage in $stageTimings) {
            $share = if ($totalSeconds -gt 0 -and $stage.status -in @("passed", "failed")) {
                100.0 * [double]$stage.elapsed_seconds / $totalSeconds
            } else { 0.0 }
            [pscustomobject][ordered]@{
                id = $stage.id
                name = $stage.name
                status = $stage.status
                elapsed_seconds = [math]::Round([double]$stage.elapsed_seconds, 3)
                share_percent = [math]::Round($share, 2)
                artifact_megabytes = [math]::Round([double]$stage.artifact_bytes / 1MB, 3)
            }
        }
    )
    $report = [ordered]@{
        schema_version = 1
        generated_at = (Get-Date).ToString("o")
        status = $FinalStatus
        mode = $Mode
        input_dxf = $InputFile
        total_elapsed_seconds = [math]::Round($totalSeconds, 6)
        measured_stage_seconds = [math]::Round($executedSeconds, 6)
        orchestration_seconds = [math]::Round([math]::Max(0.0, $totalSeconds - $executedSeconds), 6)
        stages = @($stageTimings)
    }
    $jsonPath = Join-Path $OutputDirectory "pipeline_stage_timings.json"
    $csvPath = Join-Path $OutputDirectory "pipeline_stage_timings.csv"
    $markdownPath = Join-Path $OutputDirectory "pipeline_stage_timings.md"
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $jsonPath -Encoding UTF8
    $rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8

    $markdown = [Collections.Generic.List[string]]::new()
    $markdown.Add("# Pipeline stage timings")
    $markdown.Add("")
    $markdown.Add("- Status: ``$FinalStatus``")
    $markdown.Add("- Mode: ``$Mode``")
    $markdown.Add(("- Total: {0:N3} s" -f $totalSeconds))
    $markdown.Add(("- Measured stages: {0:N3} s" -f $executedSeconds))
    $markdown.Add("")
    $markdown.Add("| Stage | Status | Seconds | Share | Artifacts, MB |")
    $markdown.Add("|---|---:|---:|---:|---:|")
    foreach ($row in $rows) {
        $markdown.Add(("| {0} {1} | {2} | {3:N3} | {4:N2}% | {5:N3} |" -f `
            $row.id, $row.name, $row.status, $row.elapsed_seconds, $row.share_percent, $row.artifact_megabytes))
    }
    $markdown.Add("")
    $markdown.Add("Artifact size is the sum of files produced by the stage; it is not peak memory usage.")
    $markdown | Set-Content -LiteralPath $markdownPath -Encoding UTF8

    return [ordered]@{
        json = $jsonPath
        csv = $csvPath
        markdown = $markdownPath
    }
}

$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $workspace ".venv\Scripts\python.exe"
$extractorDirectory = Join-Path $workspace ".gotmp"
$extractor = Join-Path $extractorDirectory "dxf_extract_go.exe"
$semanticConfigPath = (Resolve-Path -LiteralPath (Join-Path $workspace $SemanticConfig)).Path
$surfaceConfigPath = (Resolve-Path -LiteralPath (Join-Path $workspace $SurfaceConfig)).Path
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
    $go = Get-Command go -ErrorAction SilentlyContinue
    if (-not $go) {
        throw "Go was not found. Install Go to build parser/dxf_extract_go."
    }
    New-Item -ItemType Directory -Path $extractorDirectory -Force | Out-Null
    & $go.Source build -buildvcs=false -o $extractor (Join-Path $workspace "parser\dxf_extract_go")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build the Go DXF extractor."
    }
}
if (
    [double]::IsNaN($DxfUnitsPerMeter) -or
    [double]::IsInfinity($DxfUnitsPerMeter) -or
    $DxfUnitsPerMeter -le 0
) {
    throw "DxfUnitsPerMeter must be greater than zero."
}
if (
    [double]::IsNaN($TreeSpacingM) -or
    [double]::IsInfinity($TreeSpacingM) -or
    $TreeSpacingM -lt 0
) {
    throw "TreeSpacingM must be zero (preset default) or a positive finite number."
}
if ($TreeMaxCount -lt 0) {
    throw "TreeMaxCount must be zero (no override) or a positive integer."
}
if ($DiagnosticRejectedMaxCount -lt -1) {
    throw "DiagnosticRejectedMaxCount must be -1 (config default), zero, or a positive integer."
}
if (-not [string]::IsNullOrWhiteSpace($PlantingRequest)) {
    foreach ($parameterName in @("PlantingPreset", "TreeSpacingM", "TreeMaxCount")) {
        if ($PSBoundParameters.ContainsKey($parameterName)) {
            throw "PlantingRequest cannot be combined with $parameterName. Put the override into the request JSON."
        }
    }
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
$unitReport = Join-Path $outputPath "dxf_units_report.json"
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
$overheadPowerReview = Join-Path $outputPath "overhead_power_review.geojsonl"
$overheadPowerReport = Join-Path $outputPath "overhead_power_reconstruction_report.json"
$overheadPowerDebugDxf = Join-Path $outputPath "overhead_power_reconstruction_debug.dxf"
$surfaceDxf = Join-Path $outputPath "surface_candidates.dxf"
$surfacePng = Join-Path $outputPath "surface_candidates.png"
$surfaceReport = Join-Path $outputPath "surface_candidates_report.json"
$constraints = Join-Path $outputPath "constraint_map.geojsonl"
$constraintReport = Join-Path $outputPath "constraint_report.json"
$zones = Join-Path $outputPath "plant_allow_zones.geojsonl"
$zoneReport = Join-Path $outputPath "plant_allow_zones_report.json"
$plantingPlan = Join-Path $outputPath "planting_plan.geojsonl"
$plantingDecisions = Join-Path $outputPath "planting_decisions.geojsonl"
$plantingPlanReport = Join-Path $outputPath "planting_plan_report.json"
$plantingExplanations = Join-Path $outputPath "planting_explanations.md"
$debugDxf = Join-Path $outputPath "plant_allow_zones_debug.dxf"
$debugPng = Join-Path $outputPath "plant_allow_zones_debug.png"
$zoneVerificationReport = Join-Path $outputPath "zone_verification_report.json"
$verificationReport = Join-Path $outputPath "verification_report.json"
$fullResultDxf = Join-Path $outputPath "result_with_planting_plan.dxf"
$overlayDxf = Join-Path $outputPath "planting_overlay.dxf"
$runParametersReport = Join-Path $outputPath "pipeline_run_parameters.json"
$cacheManifest = Join-Path $outputPath "preprocessing_cache.json"
$cacheStatusReport = Join-Path $outputPath "preprocessing_cache_status.json"

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
$plantingRequestPath = if ([string]::IsNullOrWhiteSpace($PlantingRequest)) {
    $null
} else {
    $requestCandidate = if ([IO.Path]::IsPathRooted($PlantingRequest)) {
        $PlantingRequest
    } else {
        Join-Path $workspace $PlantingRequest
    }
    (Resolve-Path -LiteralPath $requestCandidate).Path
}

$unitsArgumentForCache = if ($PSBoundParameters.ContainsKey("DxfUnitsPerMeter")) {
    $DxfUnitsPerMeter.ToString("R", [Globalization.CultureInfo]::InvariantCulture)
} else {
    "auto"
}
$cacheArguments = @(
    ".\scripts\pipeline_cache.py", "check",
    "--workspace", $workspace,
    "--input-dxf", $inputPath,
    "--output-directory", $outputPath,
    "--manifest", $cacheManifest,
    "--dxf-units-argument", $unitsArgumentForCache,
    "--extra-dependency", $semanticConfigPath,
    "--extra-dependency", $surfaceConfigPath,
    "--status-output", $cacheStatusReport
)
if ($null -ne $detectorModelPath) {
    $cacheArguments += @("--detector-model", $detectorModelPath)
}
Invoke-TimedPipelineStage -Id "00" -Name "Preprocessing cache validation" `
    -Artifacts @($cacheStatusReport) -Action {
        Push-Location $workspace
        try {
            & $python @cacheArguments
            if ($LASTEXITCODE -ne 0) { throw "Preprocessing cache validation failed" }
        } finally {
            Pop-Location
        }
    }
$cacheStatus = Get-Content -LiteralPath $cacheStatusReport -Raw -Encoding UTF8 | ConvertFrom-Json
$reusePreprocessing = switch ($PipelineMode) {
    "full" { $false }
    "lean" { $false }
    "fast" {
        if (-not [bool]$cacheStatus.hit) {
            throw "Fast mode requires a valid preprocessing cache: $($cacheStatus.reason). Run once with -PipelineMode full or auto."
        }
        $true
    }
    default { [bool]$cacheStatus.hit }
}
$actualPipelineMode = if ($reusePreprocessing) {
    "fast"
} elseif ($PipelineMode -eq "lean") {
    "lean"
} else {
    "full"
}
$resultDxf = if ($actualPipelineMode -in @("fast", "lean")) { $overlayDxf } else { $fullResultDxf }

[ordered]@{
    generated_at = (Get-Date).ToString("o")
    input_dxf = $inputPath
    output_directory = $outputPath
    semantic_config = $semanticConfigPath
    surface_config = $surfaceConfigPath
    requested_dxf_units_per_meter = $DxfUnitsPerMeter
    utility_detector_model = $detectorModelPath
    planting_request = $plantingRequestPath
    planting_preset = if ($null -eq $plantingRequestPath) { $PlantingPreset } else { $null }
    tree_spacing_m = if ($TreeSpacingM -gt 0) { $TreeSpacingM } else { $null }
    tree_max_count = if ($TreeMaxCount -gt 0) { $TreeMaxCount } else { $null }
    diagnostic_rejected_max_count = if ($DiagnosticRejectedMaxCount -ge 0) {
        $DiagnosticRejectedMaxCount
    } else {
        $null
    }
    requested_pipeline_mode = $PipelineMode
    actual_pipeline_mode = $actualPipelineMode
    preprocessing_cache_hit = [bool]$cacheStatus.hit
    preprocessing_cache_reason = [string]$cacheStatus.reason
    result_dxf = $resultDxf
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $runParametersReport -Encoding UTF8

Push-Location $workspace
$pipelineSucceeded = $false
try {
    if ($reusePreprocessing) {
        Write-Host "[1/14] Preprocessing cache hit; PostGIS and stages 2-11 are not needed"
        $unitMetadata = Get-Content -LiteralPath $unitReport -Raw -Encoding UTF8 | ConvertFrom-Json
        $DxfUnitsPerMeter = [double]$unitMetadata.dxf_units_per_meter
        Add-SkippedPipelineStage -Id "01" -Name "PostGIS startup" -Status "cached"
    }
    elseif (-not $SkipDatabaseStart) {
        Write-Host "[1/14] Starting PostGIS"
        Invoke-TimedPipelineStage -Id "01" -Name "PostGIS startup" -Action {
            docker compose up -d --wait
            if ($LASTEXITCODE -ne 0) { throw "PostGIS startup failed" }
        }
    } else {
        Write-Host "[1/14] PostGIS start skipped"
        Add-SkippedPipelineStage -Id "01" -Name "PostGIS startup" -Status "skipped"
    }

    if (-not $reusePreprocessing) {
    Write-Host "[2/14] Detecting and confirming DXF units"
    $unitArguments = @(".\scripts\detect_dxf_units.py", $inputPath, "--output", $unitReport)
    if ($PSBoundParameters.ContainsKey("DxfUnitsPerMeter")) {
        $unitArguments += @("--dxf-units-per-meter", $DxfUnitsPerMeter)
    }
    Invoke-TimedPipelineStage -Id "02" -Name "DXF unit detection" `
        -Artifacts @($unitReport) -Action {
            & $python @unitArguments
            if ($LASTEXITCODE -ne 0) { throw "DXF unit detection failed" }
        }
    $unitMetadata = Get-Content -LiteralPath $unitReport -Raw -Encoding UTF8 | ConvertFrom-Json
    $DxfUnitsPerMeter = [double]$unitMetadata.dxf_units_per_meter

    Write-Host "[3/14] Extracting semantic DXF objects"
    Invoke-TimedPipelineStage -Id "03" -Name "Semantic DXF extraction (Go)" `
        -Artifacts @($objects) -Action {
            & $extractor --config $semanticConfigPath --output $objects $inputPath
            if ($LASTEXITCODE -ne 0) { throw "Semantic extraction failed" }
        }

    Write-Host "[4/14] Extracting surface candidates"
    Invoke-TimedPipelineStage -Id "04" -Name "Surface DXF extraction (Go)" `
        -Artifacts @($surfaces) -Action {
            & $extractor --config $surfaceConfigPath --output $surfaces $inputPath
            if ($LASTEXITCODE -ne 0) { throw "Surface extraction failed" }
        }

    Write-Host "[5/14] Normalizing semantic geometry"
    Invoke-TimedPipelineStage -Id "05" -Name "Geometry normalization" `
        -Artifacts @($normalized, $normalizationReport) -Action {
            & $python .\src\normalizer.py $objects --output $normalized --report $normalizationReport
            if ($LASTEXITCODE -ne 0) { throw "Normalization failed" }
        }

    Write-Host "[6/14] Cleaning engineering utility geometry"
    Invoke-TimedPipelineStage -Id "06" -Name "Engineering utility cleaning and ONNX inference" `
        -Artifacts @($cleanedUtilities, $reviewUtilities, $rejectedUtilities, $utilityCleaningReport, $utilityDebugDxf, $utilityDebugPng) -Action {
            if ($null -ne $detectorModelPath) {
                Write-Host "       Using supervised ONNX utility detector: $detectorModelPath"
                $detectorArguments = @(
                    ".\utility_detector\detector.py", "predict", $objects,
                    "--model", $detectorModelPath,
                    "--output", $cleanedUtilities,
                    "--review-output", $reviewUtilities,
                    "--rejected-output", $rejectedUtilities,
                    "--report", $utilityCleaningReport
                )
                if ($actualPipelineMode -ne "lean") {
                    $detectorArguments += @(
                        "--debug-dxf", $utilityDebugDxf,
                        "--debug-png", $utilityDebugPng
                    )
                }
                & $python @detectorArguments
            } else {
                $cleanerArguments = @(
                    ".\utility_cleaner\clean_utilities.py", $objects,
                    "--work-boundary", $normalized,
                    "--output", $cleanedUtilities,
                    "--review-output", $reviewUtilities,
                    "--rejected-output", $rejectedUtilities,
                    "--report", $utilityCleaningReport,
                    "--dxf-units-per-meter", $DxfUnitsPerMeter
                )
                if ($actualPipelineMode -ne "lean") {
                    $cleanerArguments += @(
                        "--debug-dxf", $utilityDebugDxf,
                        "--debug-png", $utilityDebugPng
                    )
                }
                & $python @cleanerArguments
            }
            if ($LASTEXITCODE -ne 0) { throw "Utility cleaning failed" }
        }

    Write-Host "[7/14] Reconstructing utility gaps and junctions"
    Invoke-TimedPipelineStage -Id "07" -Name "Utility network reconstruction" `
        -Artifacts @($reconstructedUtilities, $inferredUtilityConnections, $reviewUtilityConnections, $networkReconstructionReport, $networkReconstructionDebugDxf) -Action {
            $networkArguments = @(
                ".\src\network_reconstructor.py", $cleanedUtilities,
                "--output", $reconstructedUtilities,
                "--inferred-output", $inferredUtilityConnections,
                "--review-output", $reviewUtilityConnections,
                "--report", $networkReconstructionReport,
                "--dxf-units-per-meter", $DxfUnitsPerMeter
            )
            if ($actualPipelineMode -ne "lean") {
                $networkArguments += @("--debug-dxf", $networkReconstructionDebugDxf)
            }
            & $python @networkArguments
            if ($LASTEXITCODE -ne 0) { throw "Utility network reconstruction failed" }
        }

    Write-Host "[7b/14] Reconstructing overhead power-line hypotheses from arrows"
    Invoke-TimedPipelineStage -Id "07b" -Name "Overhead power reconstruction" `
        -Artifacts @($reconstructedUtilities, $overheadPowerReview, $overheadPowerReport, $overheadPowerDebugDxf) -Action {
            $overheadArguments = @(
                ".\src\overhead_power_reconstructor.py", $inputPath,
                "--objects", $objects,
                "--base-utilities", $reconstructedUtilities,
                "--output", $reconstructedUtilities,
                "--review-output", $overheadPowerReview,
                "--report", $overheadPowerReport,
                "--dxf-units-per-meter", $DxfUnitsPerMeter
            )
            if ($actualPipelineMode -ne "lean") {
                $overheadArguments += @("--debug-dxf", $overheadPowerDebugDxf)
            }
            & $python @overheadArguments
            if ($LASTEXITCODE -ne 0) { throw "Overhead power-line reconstruction failed" }
        }

    if ($actualPipelineMode -eq "lean") {
        Write-Host "[8/14] Source surface diagnostics skipped in lean mode"
        Add-SkippedPipelineStage -Id "08" -Name "Surface diagnostics rendering" -Status "skipped"
    } else {
        Write-Host "[8/14] Rendering source surface diagnostics"
        Invoke-TimedPipelineStage -Id "08" -Name "Surface diagnostics rendering" `
            -Artifacts @($surfaceDxf, $surfacePng, $surfaceReport) -Action {
                & $python .\src\surface_inspector.py $surfaces $normalized `
                    --dxf-output $surfaceDxf `
                    --png-output $surfacePng `
                    --report $surfaceReport
                if ($LASTEXITCODE -ne 0) { throw "Surface inspection failed" }
            }
    }

    Write-Host "[9/14] Building common constraints and reconstructed road"
    Invoke-TimedPipelineStage -Id "09" -Name "Constraint and road construction" `
        -Artifacts @($constraints, $constraintReport) -Action {
            & $python .\src\constraint_builder.py $normalized $surfaces `
                --output $constraints `
                --report $constraintReport `
                --unit-metadata $unitReport
            if ($LASTEXITCODE -ne 0) { throw "Constraint building failed" }
        }

    Write-Host "[10/14] Applying plant rules to reconstructed utility geometry"
    Invoke-TimedPipelineStage -Id "10" -Name "Plant allow-zone calculation" `
        -Artifacts @($zones, $zoneReport) -Action {
            & $python .\src\plant_allow_zone.py $constraints $normalized `
                --output $zones `
                --report $zoneReport `
                --utility-geometries $reconstructedUtilities `
                --dxf-units-per-meter $DxfUnitsPerMeter `
                --unit-metadata $unitReport
            if ($LASTEXITCODE -ne 0) { throw "Plant allow-zone calculation failed" }
        }

    Write-Host "[11/14] Verifying calculated zones"
    Invoke-TimedPipelineStage -Id "11" -Name "Calculated-zone verification" `
        -Artifacts @($zoneVerificationReport) -Action {
            & $python .\scripts\verify_outputs.py $constraints $zones `
                --zone-report $zoneReport `
                --output $zoneVerificationReport
            if ($LASTEXITCODE -ne 0) { throw "Spatial verification failed" }
        }

    $writeCacheArguments = @(
        ".\scripts\pipeline_cache.py", "write",
        "--workspace", $workspace,
        "--input-dxf", $inputPath,
        "--output-directory", $outputPath,
        "--manifest", $cacheManifest,
        "--dxf-units-argument", $unitsArgumentForCache,
        "--extra-dependency", $semanticConfigPath,
        "--extra-dependency", $surfaceConfigPath,
        "--status-output", $cacheStatusReport
    )
    if ($null -ne $detectorModelPath) {
        $writeCacheArguments += @("--detector-model", $detectorModelPath)
    }
    Invoke-TimedPipelineStage -Id "11b" -Name "Preprocessing cache write" `
        -Artifacts @($cacheManifest, $cacheStatusReport) -Action {
            & $python @writeCacheArguments
            if ($LASTEXITCODE -ne 0) { throw "Preprocessing cache write failed" }
        }
    } else {
        Write-Host "[2-11/14] Reusing extraction, cleaned networks, constraints and allow zones"
        Add-SkippedPipelineStage -Id "02" -Name "DXF unit detection" -Status "cached"
        Add-SkippedPipelineStage -Id "03" -Name "Semantic DXF extraction (Go)" -Status "cached"
        Add-SkippedPipelineStage -Id "04" -Name "Surface DXF extraction (Go)" -Status "cached"
        Add-SkippedPipelineStage -Id "05" -Name "Geometry normalization" -Status "cached"
        Add-SkippedPipelineStage -Id "06" -Name "Engineering utility cleaning and ONNX inference" -Status "cached"
        Add-SkippedPipelineStage -Id "07" -Name "Utility network reconstruction" -Status "cached"
        Add-SkippedPipelineStage -Id "07b" -Name "Overhead power reconstruction" -Status "cached"
        Add-SkippedPipelineStage -Id "08" -Name "Surface diagnostics rendering" -Status "cached"
        Add-SkippedPipelineStage -Id "09" -Name "Constraint and road construction" -Status "cached"
        Add-SkippedPipelineStage -Id "10" -Name "Plant allow-zone calculation" -Status "cached"
        Add-SkippedPipelineStage -Id "11" -Name "Calculated-zone verification" -Status "cached"
        Add-SkippedPipelineStage -Id "11b" -Name "Preprocessing cache write" -Status "cached"
    }

    Write-Host "[12/14] Generating concrete planting points and explanations"
    $plantingArguments = @(
        ".\src\planting_service.py", $zones, $zoneReport, $normalized, $constraints,
        "--utilities", $reconstructedUtilities,
        "--config", ".\nanocad-plugin\config\greenai.plugin.json",
        "--output", $plantingPlan,
        "--decisions-output", $plantingDecisions,
        "--report", $plantingPlanReport,
        "--explanations-output", $plantingExplanations
    )
    if ($null -ne $plantingRequestPath) {
        $plantingArguments += @("--request", $plantingRequestPath)
    }
    else {
        $plantingArguments += @("--preset", $PlantingPreset)
        if ($TreeSpacingM -gt 0) {
            $plantingArguments += @("--tree-spacing-m", $TreeSpacingM)
        }
        if ($TreeMaxCount -gt 0) {
            $plantingArguments += @("--tree-max-count", $TreeMaxCount)
        }
    }
    if ($DiagnosticRejectedMaxCount -ge 0) {
        $plantingArguments += @(
            "--diagnostic-rejected-max-count", $DiagnosticRejectedMaxCount
        )
    }
    Invoke-TimedPipelineStage -Id "12" -Name "Planting plan generation" `
        -Artifacts @($plantingPlan, $plantingDecisions, $plantingPlanReport, $plantingExplanations) -Action {
            & $python @plantingArguments
            if ($LASTEXITCODE -ne 0) { throw "Planting plan generation failed" }
        }

    if ($actualPipelineMode -in @("fast", "lean")) {
        Write-Host "[12b/14] Large diagnostic DXF/PNG export skipped in $actualPipelineMode mode"
        Add-SkippedPipelineStage -Id "12b" -Name "Full diagnostic DXF and PNG export" -Status "skipped"
        Write-Host "[13/14] Writing lightweight planting overlay DXF"
        Invoke-TimedPipelineStage -Id "13" -Name "Lightweight planting overlay DXF export" `
            -Artifacts @($resultDxf) -Action {
                & $python .\src\dxf_exporter.py $inputPath $zones `
                    --constraint-map $constraints `
                    --planting-plan $plantingPlan `
                    --output $resultDxf `
                    --overlay-only `
                    --insunits ([int]$unitMetadata.insert_units_code) `
                    --strict-output
                if ($LASTEXITCODE -ne 0) { throw "Planting overlay DXF export failed" }
            }

        Write-Host "[14/14] Verifying plan geometry, rules and explanations"
        Invoke-TimedPipelineStage -Id "14" -Name "Final geometry and rule verification" `
            -Artifacts @($verificationReport) -Action {
                & $python .\scripts\verify_outputs.py $constraints $zones `
                    --planting-plan $plantingPlan `
                    --zone-report $zoneReport `
                    --plan-report $plantingPlanReport `
                    --output $verificationReport
                if ($LASTEXITCODE -ne 0) { throw "Final delivery verification failed" }
            }
    } else {
        Write-Host "[12b/14] Updating diagnostic DXF with accepted and rejected candidates"
        Invoke-TimedPipelineStage -Id "12b" -Name "Full diagnostic DXF and PNG export" `
            -Artifacts @($debugDxf, $debugPng) -Action {
                & $python .\src\plant_allow_zone_debug.py $zones $constraints $normalized `
                    --dxf-output $debugDxf `
                    --png-output $debugPng `
                    --utility-geometries $reconstructedUtilities `
                    --raw-objects $objects `
                    --planting-plan $plantingPlan `
                    --planting-decisions $plantingDecisions `
                    --base-dxf $inputPath `
                    --zone-report $zoneReport
                if ($LASTEXITCODE -ne 0) { throw "Planting diagnostics export failed" }
            }

        Write-Host "[13/14] Writing result layers into a copy of the source DXF"
        Invoke-TimedPipelineStage -Id "13" -Name "Full result DXF export" `
            -Artifacts @($resultDxf) -Action {
                & $python .\src\dxf_exporter.py $inputPath $zones `
                    --constraint-map $constraints `
                    --planting-plan $plantingPlan `
                    --output $resultDxf `
                    --strict-output
                if ($LASTEXITCODE -ne 0) { throw "Final DXF export failed" }
            }

        Write-Host "[14/14] Verifying plan, explanations and source-DXF preservation"
        Invoke-TimedPipelineStage -Id "14" -Name "Final DXF preservation and rule verification" `
            -Artifacts @($verificationReport) -Action {
                & $python .\scripts\verify_outputs.py $constraints $zones `
                    --planting-plan $plantingPlan `
                    --zone-report $zoneReport `
                    --plan-report $plantingPlanReport `
                    --input-dxf $inputPath `
                    --output-dxf $resultDxf `
                    --output $verificationReport
                if ($LASTEXITCODE -ne 0) { throw "Final delivery verification failed" }
            }
    }
    $pipelineSucceeded = $true
    $timingReports = Write-PipelineTimingReports -OutputDirectory $outputPath `
        -Mode $actualPipelineMode -InputFile $inputPath -FinalStatus "passed"
    Write-Host ""
    Write-Host "Pipeline completed"
    Write-Host "Mode: $actualPipelineMode (requested: $PipelineMode)"
    Write-Host ("Elapsed: {0:N1} s" -f $pipelineStopwatch.Elapsed.TotalSeconds)
    Write-Host "Final DXF: $resultDxf"
    if ($actualPipelineMode -eq "full") {
        Write-Host "Preview PNG: $debugPng"
    }
    Write-Host "Rule report: $zoneReport"
    Write-Host "Per-plant report: $plantingPlanReport"
    Write-Host "Planting explanations: $plantingExplanations"
    Write-Host "Network reconstruction: $networkReconstructionReport"
    Write-Host "Overhead power reconstruction: $overheadPowerReport"
    Write-Host "Verification: $verificationReport"
    Write-Host "Run parameters: $runParametersReport"
    Write-Host "Stage timings: $($timingReports.markdown)"
} finally {
    Pop-Location
    if (-not $pipelineSucceeded) {
        $failedMode = if ($null -ne $actualPipelineMode) { $actualPipelineMode } else { "unknown" }
        Write-PipelineTimingReports -OutputDirectory $outputPath `
            -Mode $failedMode -InputFile $inputPath -FinalStatus "failed" | Out-Null
    }
}
