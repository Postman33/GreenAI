param(
    [switch]$WithBlender,
    [switch]$SkipDocker,
    [switch]$NoSystemInstall,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (-not $SkipInstall) {
    & (Join-Path $root 'scripts\install.ps1') `
        -WithBlender:$WithBlender -SkipDocker:$SkipDocker -NoSystemInstall:$NoSystemInstall
}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment was not found: $python. Run .\init.ps1 without -SkipInstall first."
}
& $python -X utf8 (Join-Path $root 'scripts\download_demo_dxf.py')
if ($LASTEXITCODE -ne 0) { throw 'Demo DXF download or validation failed' }
