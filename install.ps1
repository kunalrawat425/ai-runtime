# install.ps1 — Install ai-runtime on Windows
# Run with: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillName = "ai-runtime"

# Claude Code skill location on Windows
$SkillTarget = "$env:APPDATA\Claude\skills"
Write-Host "[ai-runtime] installing Claude Code skill -> $SkillTarget\$SkillName"
New-Item -ItemType Directory -Force -Path $SkillTarget | Out-Null

$Dest = "$SkillTarget\$SkillName"
if (Test-Path $Dest) {
    Remove-Item -Recurse -Force $Dest
}
Copy-Item -Recurse "$ScriptDir\skills\$SkillName" $Dest
Write-Host "[ai-runtime] skill installed OK"

# CLI install
$CliDir = "$env:USERPROFILE\.local\bin"
New-Item -ItemType Directory -Force -Path $CliDir | Out-Null
Copy-Item "$ScriptDir\skills\$SkillName\ai_runtime.py" "$CliDir\ai_runtime.py"

$WrapperPath = "$CliDir\ai-runtime.cmd"
@"
@echo off
python "%~dp0ai_runtime.py" %*
"@ | Set-Content $WrapperPath

Write-Host "[ai-runtime] CLI installed -> $WrapperPath"
Write-Host ""
Write-Host "Add $CliDir to your PATH if not already present."
Write-Host "Done. Try: ai-runtime run 'your task here'"
