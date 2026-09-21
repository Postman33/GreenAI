param(
    [string]$DataDirectory = "..\output"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:DOTNET_CLI_HOME = Join-Path (Split-Path -Parent $root) ".tools\dotnet-home"
$env:NUGET_PACKAGES = Join-Path (Split-Path -Parent $root) ".tools\nuget-packages"
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = "1"
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$dotnet = Join-Path (Split-Path -Parent $root) ".tools\dotnet6\dotnet.exe"
if (-not (Test-Path -LiteralPath $dotnet)) { $dotnet = "dotnet" }
$config = Join-Path $root "config\greenai.plugin.json"
$data = [IO.Path]::GetFullPath((Join-Path $root $DataDirectory))

& $dotnet run --project (Join-Path $root "tests\GreenAI.Core.SmokeTests\GreenAI.Core.SmokeTests.csproj") `
    --configuration Release --no-restore -- $config $data
exit $LASTEXITCODE
