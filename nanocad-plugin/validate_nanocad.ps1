param(
    [string]$NanoCadExe = "F:\NanoCAD\nCad.exe",
    [string]$Drawing,
    [string]$PluginDll,
    [string]$DataDirectory,
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $pluginRoot

if ([string]::IsNullOrWhiteSpace($Drawing)) {
    $verifiedDrawing = Join-Path $projectRoot "output\verification_final_20260920\result_with_planting_plan.dxf"
    $Drawing = if (Test-Path -LiteralPath $verifiedDrawing -PathType Leaf) {
        $verifiedDrawing
    }
    else {
        Join-Path $projectRoot "output\result_with_planting_plan.dxf"
    }
}

if ([string]::IsNullOrWhiteSpace($PluginDll)) {
    $PluginDll = Join-Path $pluginRoot "build\GreenAI.NanoCad.dll"
}
if ([string]::IsNullOrWhiteSpace($DataDirectory)) {
    $DataDirectory = Split-Path -Parent $Drawing
}
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$probePath = Join-Path $DataDirectory "nanocad_probe_$stamp.json"
$resultPath = Join-Path $DataDirectory "nanocad_smoke_$stamp.json"

foreach ($requiredPath in @($NanoCadExe, $PluginDll, $Drawing)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required file was not found: $requiredPath"
    }
}

if ($TimeoutSeconds -le 0) {
    throw "TimeoutSeconds must be greater than zero."
}

$previousEnvironment = @{
    GREENAI_PLUGIN_PROBE_FILE = $env:GREENAI_PLUGIN_PROBE_FILE
    GREENAI_PLUGIN_AUTORUN = $env:GREENAI_PLUGIN_AUTORUN
    GREENAI_PLUGIN_AUTORUN_FILE = $env:GREENAI_PLUGIN_AUTORUN_FILE
    GREENAI_DATA_DIR = $env:GREENAI_DATA_DIR
}

$process = $null
try {
    $env:GREENAI_PLUGIN_PROBE_FILE = $probePath
    $env:GREENAI_PLUGIN_AUTORUN = "1"
    $env:GREENAI_PLUGIN_AUTORUN_FILE = $resultPath
    $env:GREENAI_DATA_DIR = $DataDirectory

    $arguments = "-invisible -g `"$PluginDll`" `"$Drawing`""
    $process = Start-Process `
        -FilePath $NanoCadExe `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $resultPath)) {
        if ($process.HasExited) {
            throw "nanoCAD exited before it wrote the validation result. Exit code: $($process.ExitCode)"
        }
        Start-Sleep -Milliseconds 500
        $process.Refresh()
    }

    if (-not (Test-Path -LiteralPath $probePath)) {
        throw "The plugin did not confirm that its DLL was loaded: $probePath"
    }
    if (-not (Test-Path -LiteralPath $resultPath)) {
        throw "nanoCAD validation timed out after $TimeoutSeconds seconds."
    }

    $result = Get-Content -LiteralPath $resultPath -Encoding utf8 -Raw | ConvertFrom-Json
    $checks = [ordered]@{
        StatusPassed = $result.status -eq "passed"
        BatchMetadataFound = $result.batchMetadataSmoke.Found -eq $true
        BatchMetadataParsed = $result.batchMetadataSmoke.Parsed -eq $true
        BatchPointMetadataParsed = $result.batchMetadataSmoke.PointParsed -eq $true
        BatchAreaMetadataParsed = $result.batchMetadataSmoke.AreaParsed -eq $true
        AllPointMetadataPresent = $result.pointMetadataCount -eq $result.pointCount
        PointIdsUnique = $result.uniquePointIds -eq $result.pointCount
        AllAreaMetadataPresent = $result.areaMetadataCount -eq $result.areaCount
        ContextPresetApplied = $result.contextMenuSmoke.PresetApplied -eq $true
        ContextScopeCaptured = $result.contextMenuSmoke.ScopeCaptured -eq $true
        ContextPreviewGenerated = $result.contextMenuSmoke.PreviewGenerated -eq $true
        ZoneMetadataPersisted = $result.editorZoneSmoke.MetadataPersisted -eq $true
        InsidePlacementPassed = $result.editorZoneSmoke.InsidePassed -eq $true
        OutsidePlacementRejected = $result.editorZoneSmoke.OutsideRejected -eq $true
        OccupiedZonePreviewGenerated = $result.zoneReplacementSmoke.PreviewGenerated -eq $true
        ExistingObjectsReplaced = $result.zoneReplacementSmoke.ExistingObjectsReplaced -eq $true
        ReplacementIdsUnique = $result.zoneReplacementSmoke.ResultIdsUnique -eq $true
        LastResultRemembered = $result.zoneReplacementSmoke.LastResultRemembered -eq $true
        ResultLayersShown = $result.zoneReplacementSmoke.ResultLayersShown -eq $true
        ViewCenteredOnLastResult = $result.zoneReplacementSmoke.ViewCenteredOnLastResult -eq $true
        StalePatternResolved = $result.guidePreviewSmoke.StalePatternResolved -eq $true
        PreviewContextRecorded = $result.guidePreviewSmoke.PreviewContextRecorded -eq $true
        GuideHasNoFalseZone = $result.guidePreviewSmoke.MetadataHasNoZone -eq $true
        PreviewAppliedToModelSpace = $result.guidePreviewSmoke.AppliedToModelSpace -eq $true
        ReportCreated = $result.reportExists -eq $true
    }

    $failedChecks = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
    if ($failedChecks.Count -gt 0) {
        throw "nanoCAD validation failed: $($failedChecks -join ', '). Result: $resultPath"
    }

    [PSCustomObject]@{
        Status = "PASS"
        Dll = $PluginDll
        DataDirectory = $DataDirectory
        Drawing = $Drawing
        Runtime = (Get-Content -LiteralPath $probePath -Encoding utf8 -Raw | ConvertFrom-Json).runtime
        PointCount = $result.pointCount
        AreaCount = $result.areaCount
        Result = $resultPath
    } | Format-List
}
finally {
    if ($null -ne $process) {
        $process.Refresh()
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit(5000) | Out-Null
        }
    }

    foreach ($name in $previousEnvironment.Keys) {
        $value = $previousEnvironment[$name]
        if ($null -eq $value) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$name" $value
        }
    }
}
