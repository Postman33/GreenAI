param(
    [switch]$SkipPython,
    [switch]$SkipGo
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

$testTempRoot = Join-Path $root "tmp\test-runs"
$testTemp = Join-Path $testTempRoot "green-dxf-tests-$PID"
try {
    if (-not $SkipPython) {
        $python = Join-Path $root ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $python)) {
            $python = "python"
        }
        Write-Host "[1/2] Python unit tests"
        & $python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "Python tests failed" }
    }

    if (-not $SkipGo) {
        $sdkGo = Join-Path $env:USERPROFILE "sdk\go1.25.5\bin\go.exe"
        $go = if (Test-Path -LiteralPath $sdkGo) { $sdkGo } else { "go" }
        New-Item -ItemType Directory -Force -Path $testTemp | Out-Null
        $env:GOTOOLCHAIN = "local"
        $env:GOTMPDIR = New-Item -ItemType Directory -Force -Path (Join-Path $testTemp "tmp") | Select-Object -ExpandProperty FullName
        $env:GOCACHE = New-Item -ItemType Directory -Force -Path (Join-Path $testTemp "cache") | Select-Object -ExpandProperty FullName
        Write-Host "[2/2] Go unit tests"
        & $go test ./...
        if ($LASTEXITCODE -ne 0) { throw "Go tests failed" }
    }

    Write-Host "All tests passed"
} finally {
    Pop-Location
    if (Test-Path -LiteralPath $testTemp) {
        $resolvedTestTemp = (Resolve-Path -LiteralPath $testTemp).Path
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($testTempRoot)
        if ($resolvedTestTemp.StartsWith(
            $resolvedTestRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            Remove-Item -LiteralPath $resolvedTestTemp -Recurse -Force
        } else {
            throw "Refusing to remove test cache outside the system temp directory: $resolvedTestTemp"
        }
    }
}
