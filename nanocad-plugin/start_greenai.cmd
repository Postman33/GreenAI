@echo off
setlocal

set "PLUGIN_DIR=%~dp0"
set "PROJECT_DIR=%PLUGIN_DIR%.."
set "NANOCAD_EXE=F:\NanoCAD\nCad.exe"
set "PLUGIN_DLL=%PLUGIN_DIR%build\GreenAI.NanoCad.dll"
set "GREENAI_DATA_DIR=%PROJECT_DIR%\output\verification_final_20260920"
set "DRAWING=%GREENAI_DATA_DIR%\result_with_planting_plan.dxf"

if not exist "%DRAWING%" (
  set "GREENAI_DATA_DIR=%PROJECT_DIR%\output"
  set "DRAWING=%PROJECT_DIR%\output\result_with_planting_plan.dxf"
)

if not exist "%NANOCAD_EXE%" (
  echo nanoCAD not found: %NANOCAD_EXE%
  pause
  exit /b 1
)

if not exist "%PLUGIN_DLL%" (
  echo GreenAI plugin not found: %PLUGIN_DLL%
  pause
  exit /b 1
)

if not exist "%DRAWING%" (
  echo Drawing not found: %DRAWING%
  pause
  exit /b 1
)

start "" "%NANOCAD_EXE%" -g "%PLUGIN_DLL%" "%DRAWING%"
endlocal
