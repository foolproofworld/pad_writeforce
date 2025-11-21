# Windows build helper for storage_stress and file_generator
# Usage: run in PowerShell from repo root: .\build_windows.ps1

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

Write-Host "Repo root: $repoRoot"

$venvPath = Join-Path $repoRoot '.venv'
if (-not (Test-Path $venvPath)) {
    Write-Host "Creating venv at $venvPath" -ForegroundColor Cyan
    python -m venv $venvPath
}

$venvPython = Join-Path $venvPath 'Scripts/python.exe'
$venvPip = Join-Path $venvPath 'Scripts/pip.exe'

& $venvPip install --upgrade pip > $null
& $venvPip install --upgrade pyinstaller > $null

$commonArgs = @('--noconfirm', '--onedir', '--clean')

Write-Host "Building storage_stress.exe..." -ForegroundColor Cyan
& $venvPython -m PyInstaller @commonArgs --name storage_stress storage_stress.py

Write-Host "Building file_generator.exe..." -ForegroundColor Cyan
& $venvPython -m PyInstaller @commonArgs --name file_generator file_generator.py

Write-Host "Build finished. Dist outputs are under: $repoRoot\\dist" -ForegroundColor Green
