param(
    [string]$TectonicPath = ""
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$source = Join-Path $repoRoot 'docs/sylvitect-core.tex'
$outDir = Join-Path $repoRoot 'output/pdf'
$cacheDir = Join-Path $repoRoot 'tmp/latex_toolchain/cache'
New-Item -ItemType Directory -Force -Path $outDir, $cacheDir | Out-Null

if (-not $TectonicPath) {
    $installed = Get-Command tectonic -ErrorAction SilentlyContinue
    if ($installed) {
        $TectonicPath = $installed.Source
    } else {
        $portable = Join-Path $repoRoot 'tmp/latex_toolchain/tectonic/tectonic.exe'
        if (Test-Path -LiteralPath $portable) { $TectonicPath = $portable }
    }
}
if (-not $TectonicPath -or -not (Test-Path -LiteralPath $TectonicPath)) {
    throw 'Tectonic not found. Install it or pass -TectonicPath.'
}

$env:TECTONIC_CACHE_DIR = $cacheDir
Push-Location $repoRoot
try {
    $ErrorActionPreference = 'Continue'
    & $TectonicPath -b 'https://data1.fullyjustified.net/tlextras-2022.0r0.tar' -o $outDir $source
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 0) { throw "LaTeX build failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
Write-Output (Join-Path $outDir 'sylvitect-core.pdf')
