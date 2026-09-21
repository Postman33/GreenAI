param(
    [string]$NanoCadDir = "F:\NanoCAD\bin",
    [string]$DestinationDirectory = "build",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:DOTNET_CLI_HOME = Join-Path (Split-Path -Parent $root) ".tools\dotnet-home"
$env:NUGET_PACKAGES = Join-Path (Split-Path -Parent $root) ".tools\nuget-packages"
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = "1"
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$dotnet = Join-Path (Split-Path -Parent $root) ".tools\dotnet6\dotnet.exe"
if (-not (Test-Path -LiteralPath $dotnet)) {
    $dotnet = "dotnet"
}

& $dotnet build (Join-Path $root "GreenAI.sln") `
    --configuration $Configuration `
    --property:Platform=x64 `
    --property:NanoCadDir=$NanoCadDir `
    --configfile (Join-Path $root "NuGet.Config")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$source = Join-Path $root "src\GreenAI.NanoCad\bin\x64\$Configuration\net6.0-windows"
$destination = Join-Path $root $DestinationDirectory
New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $source "GreenAI.NanoCad.dll") -Destination $destination -Force
Copy-Item -LiteralPath (Join-Path $source "GreenAI.Core.dll") -Destination $destination -Force
Get-ChildItem -LiteralPath $source -Filter "NetTopologySuite*.dll" | Copy-Item -Destination $destination -Force
Copy-Item -LiteralPath (Join-Path $root "config\greenai.plugin.json") -Destination $destination -Force

Write-Host "Plugin: $(Join-Path $destination 'GreenAI.NanoCad.dll')"
Write-Host "Load it in nanoCAD with NETLOAD or APPLOAD."
