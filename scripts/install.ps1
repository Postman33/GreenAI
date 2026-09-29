param(
    [switch]$WithBlender,
    [switch]$SkipDocker,
    [switch]$NoSystemInstall
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    # Native tools may write progress/warnings to stderr even on success.
    $ErrorActionPreference = 'Continue'
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

function Refresh-Path {
    $env:PATH = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        $env:PATH
    ) -join ';'
}

function Install-WingetPackage {
    param([string]$Id)
    if ($NoSystemInstall) { throw "$Id is missing. Install it and run this script again." }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { throw "winget is required to install $Id. Install App Installer, or install $Id manually." }
    Write-Host "Installing $Id..."
    Invoke-Checked $winget.Source @('install', '--exact', '--id', $Id, '--accept-source-agreements', '--accept-package-agreements')
    Refresh-Path
}

function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in @('3.11', '3.12', '3.10')) {
            $candidate = try { & py "-$version" -c 'import sys; print(sys.executable)' 2>$null } catch { $null }
            if ($LASTEXITCODE -eq 0 -and $candidate) { $candidates += $candidate.Trim() }
        }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try { & $candidate -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,13) else 1)' 2>$null } catch { continue }
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Install-WingetPackage 'Python.Python.3.11'
    $python = Find-Python
    if (-not $python) { throw 'Python 3.10–3.12 was installed but is not visible. Open a new PowerShell window and rerun the installer.' }
}
Write-Host "Python: $python"

$go = Get-Command go -ErrorAction SilentlyContinue
if (-not $go) {
    Install-WingetPackage 'GoLang.Go'
    $go = Get-Command go -ErrorAction SilentlyContinue
    if (-not $go) {
        $defaultGo = Join-Path $env:ProgramFiles 'Go\bin\go.exe'
        if (Test-Path -LiteralPath $defaultGo) { $go = [pscustomobject]@{ Source = $defaultGo } }
    }
    if (-not $go) { throw 'Go was installed but is not visible. Open a new PowerShell window and rerun the installer.' }
}
$goVersion = & $go.Source version
if ($goVersion -notmatch 'go(\d+)\.(\d+)' -or [int]$Matches[1] -lt 1 -or ([int]$Matches[1] -eq 1 -and [int]$Matches[2] -lt 21)) {
    throw "Go 1.21 or newer is required (found: $goVersion). Upgrade Go and rerun the installer."
}

$poetryEnv = Join-Path $root '.tools\poetry'
$poetry = Join-Path $poetryEnv 'Scripts\poetry.exe'
if (-not (Test-Path -LiteralPath $poetry)) {
    Write-Host 'Installing Poetry in .tools/poetry...'
    Invoke-Checked $python @('-m', 'venv', $poetryEnv)
    $poetryPython = Join-Path $poetryEnv 'Scripts\python.exe'
    Invoke-Checked $poetryPython @('-m', 'pip', 'install', 'poetry>=2.0,<3')
}
$env:POETRY_VIRTUALENVS_IN_PROJECT = 'true'
$env:POETRY_VIRTUALENVS_CREATE = 'true'
# Poetry otherwise reuses an activated environment from another checkout.
$previousVirtualEnv = $env:VIRTUAL_ENV
$previousCondaPrefix = $env:CONDA_PREFIX
try {
    $env:VIRTUAL_ENV = $null
    $env:CONDA_PREFIX = $null
    Invoke-Checked $poetry @('check', '--lock')
    Invoke-Checked $poetry @('env', 'use', $python)
    Invoke-Checked $poetry @('install', '--no-root', '--no-interaction')
} finally {
    $env:VIRTUAL_ENV = $previousVirtualEnv
    $env:CONDA_PREFIX = $previousCondaPrefix
}

$extractorDir = Join-Path $root '.gotmp'
New-Item -ItemType Directory -Path $extractorDir -Force | Out-Null
$env:GOCACHE = Join-Path $root '.gocache'
New-Item -ItemType Directory -Path $env:GOCACHE -Force | Out-Null
Invoke-Checked $go.Source @('build', '-buildvcs=false', '-o', (Join-Path $extractorDir 'dxf_extract_go.exe'), './parser/dxf_extract_go')

if (-not $SkipDocker) {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        Install-WingetPackage 'Docker.DockerDesktop'
        $docker = Get-Command docker -ErrorAction SilentlyContinue
        if (-not $docker) {
            $defaultDocker = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
            if (Test-Path -LiteralPath $defaultDocker) { $docker = [pscustomobject]@{ Source = $defaultDocker } }
        }
    }
    if (-not $docker) { throw 'Docker was installed but is not visible. Open a new PowerShell window and rerun the installer.' }
    $compose = Get-Command docker-compose -ErrorAction SilentlyContinue
    if ($compose) {
        Invoke-Checked $compose.Source @('version')
        Invoke-Checked $compose.Source @('config', '--quiet')
    } else {
        Invoke-Checked $docker.Source @('compose', 'version')
        Invoke-Checked $docker.Source @('compose', 'config', '--quiet')
    }
    $engineReady = $false
    try {
        & $docker.Source info --format '{{.ServerVersion}}' *> $null
        $engineReady = $LASTEXITCODE -eq 0
    } catch { }
    if ($engineReady) {
        if ($compose) {
            Invoke-Checked $compose.Source @('pull', 'postgis')
        } else {
            Invoke-Checked $docker.Source @('compose', 'pull', 'postgis')
        }
    } else {
        Write-Warning 'Docker Desktop is installed, but its engine is not running. Start Docker Desktop and rerun this script to pull PostGIS.'
    }
}

if ($WithBlender) {
    $blender = Get-Command blender -ErrorAction SilentlyContinue
    if (-not $blender -and -not (Test-Path -LiteralPath (Join-Path $env:ProgramFiles 'Blender Foundation'))) {
        Install-WingetPackage 'BlenderFoundation.Blender'
    }
}

Write-Host 'Installation complete. Start the pipeline with scripts/run_pipeline.ps1.'
