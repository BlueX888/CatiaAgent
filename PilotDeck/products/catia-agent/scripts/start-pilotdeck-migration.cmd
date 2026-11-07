@echo off
REM Migration-only launcher. Does not read or modify the user's default ~/.pilotdeck.
REM Reads DEEPSEEK_API_KEY from the environment or Windows user environment; this file never stores it.
REM
REM If started from a shell whose job object kills children on exit, launch via WMI:
REM   powershell -NoProfile -Command "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c F:\CatiaAgent\PilotDeck\products\catia-agent\scripts\start-pilotdeck-migration.cmd'}"

setlocal
if not defined DEEPSEEK_API_KEY for /f "delims=" %%K in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')"') do set "DEEPSEEK_API_KEY=%%K"
if "%DEEPSEEK_API_KEY%"=="" (
  echo [catia-migration] DEEPSEEK_API_KEY is not set. Export it before launching.
  exit /b 1
)

set "MIGRATION_ROOT=F:\CatiaAgent\work\pilotdeck-migration-20261105"
set "PILOT_HOME=%MIGRATION_ROOT%\home"
set "PILOTDECK_CONFIG_PATH=F:\CatiaAgent\PilotDeck\products\catia-agent\config\pilotdeck.yaml"
set "CATIA_MCP_PYTHON=F:\CatiaAgent\catia-v5-mcp-server\.venv\Scripts\python.exe"
set "CATIA_MCP_WORK_DIR=%MIGRATION_ROOT%\runtime\catia-work"
if exist "F:\CatiaAgent\work\text-to-cad-env\Scripts\python.exe" set "CATIA_CAD_SUPPORT_PYTHON=F:\CatiaAgent\work\text-to-cad-env\Scripts\python.exe"
set "TEMP=%MIGRATION_ROOT%\tmp"
set "TMP=%MIGRATION_ROOT%\tmp"
set "npm_config_cache=%MIGRATION_ROOT%\npm-cache"
set "SERVER_PORT=3001"
set "VITE_PORT=5173"
set "PILOTDECK_GATEWAY_PORT=18789"
set "PATH=F:\CodingAgent\claude-code\work\CatiaAgent\20261021-node22\node\node-v22.23.3-win-x64;%PATH%"

if not exist "%PILOT_HOME%" mkdir "%PILOT_HOME%"
if not exist "%CATIA_MCP_WORK_DIR%" mkdir "%CATIA_MCP_WORK_DIR%"
if not exist "%TEMP%" mkdir "%TEMP%"

cd /d F:\CatiaAgent\PilotDeck
node --version
call npm run dev >> "%MIGRATION_ROOT%\logs\dev.out.log" 2>&1
endlocal
