<#
.SYNOPSIS
  Install the catia-edit plugin into a dedicated PilotDeck home.

.DESCRIPTION
  Copies products\catia-agent\plugins\catia-edit into <PILOT_HOME>\plugins\catia-edit.
  This migration intentionally refuses to touch the user's default ~/.pilotdeck:
  set PILOT_HOME (or pass -PilotHome) to an isolated workspace directory.

  -CopySkills also copies each SKILL.md into <PILOT_HOME>\skills\<skill> so the
  Web UI skill page can list it. The plugin runtime already loads plugin skills;
  the copy is a UI convenience and can create a second standalone entry.

  No service is restarted. Existing non-plugin targets are refused.
#>
[CmdletBinding()]
param(
    [string]$PilotHome = $env:PILOT_HOME,
    [switch]$CopySkills
)

$ErrorActionPreference = 'Stop'
$pluginName = 'catia-edit'
$sourceDir = Join-Path $PSScriptRoot '..\plugins\catia-edit'
if (-not (Test-Path -LiteralPath (Join-Path $sourceDir 'plugin.json') -PathType Leaf)) {
    throw "Source plugin is incomplete: $(Join-Path $sourceDir 'plugin.json') is missing."
}
$sourceDir = (Resolve-Path -LiteralPath $sourceDir).Path

if ([string]::IsNullOrWhiteSpace($PilotHome)) {
    throw 'PILOT_HOME is not set. Pass -PilotHome with an isolated workspace directory; the migration must not modify the default ~/.pilotdeck.'
}
$homeRoot = [System.IO.Path]::GetFullPath($PilotHome)
$pluginsRoot = Join-Path $homeRoot 'plugins'
$targetDir = Join-Path $pluginsRoot $pluginName

if (Test-Path -LiteralPath $targetDir) {
    if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
        throw "Refusing to overwrite: $targetDir exists and is not a directory."
    }
    $existingManifest = Join-Path $targetDir 'plugin.json'
    $existing = $null
    if (Test-Path -LiteralPath $existingManifest -PathType Leaf) {
        try {
            $existing = (Get-Content -LiteralPath $existingManifest -Raw -Encoding UTF8 | ConvertFrom-Json).name
        } catch {
            $existing = $null
        }
    }
    if ($existing -ne $pluginName) {
        throw "Refusing to overwrite: plugin name in $targetDir is '$existing', not '$pluginName'."
    }
}

New-Item -ItemType Directory -Path $pluginsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
# Copy file-by-file instead of robocopy /MIR: never delete files that the user
# added to an existing plugin directory.
Get-ChildItem -LiteralPath $sourceDir -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring($sourceDir.Length + 1)
    $destination = Join-Path $targetDir $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
}

$installedManifest = Join-Path $targetDir 'plugin.json'
$installed = Get-Content -LiteralPath $installedManifest -Raw -Encoding UTF8 | ConvertFrom-Json
if ($installed.name -ne $pluginName) {
    throw "Post-copy verification failed: $installedManifest name is '$($installed.name)'."
}

if ($CopySkills) {
    $skillRoot = Join-Path $homeRoot 'skills'
    foreach ($skillName in @('catia-parametric-edit', 'catia-pdf-model')) {
        $skillSource = Join-Path $sourceDir "skills\$skillName\SKILL.md"
        if (Test-Path -LiteralPath $skillSource -PathType Leaf) {
            $skillTarget = Join-Path $skillRoot $skillName
            New-Item -ItemType Directory -Path $skillTarget -Force | Out-Null
            Copy-Item -LiteralPath $skillSource -Destination (Join-Path $skillTarget 'SKILL.md') -Force
        }
    }
}

Write-Host "Installed: $targetDir"
Write-Host "Plugin: $($installed.name) $($installed.version)"
Write-Host 'Required environment for the gateway process:'
Write-Host '  CATIA_MCP_PYTHON       = <catia-v5-mcp-server>\.venv\Scripts\python.exe'
Write-Host '  CATIA_MCP_WORK_DIR     = <workspace>\runtime\catia-work'
Write-Host '  CATIA_CAD_SUPPORT_PYTHON (optional) = isolated Python with build123d'
Write-Host '  CATIA_OCR_PYTHON       (optional) = Python used for PDF OCR'
