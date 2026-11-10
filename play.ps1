#Requires -Version 5.1
# Starts the patched game. The Play button in Steam starts the clean one.
param(
    [ValidateSet('s1', 's2')][string]$Game = 's1',
    [string]$GameDir = 'C:\opt\shenmue-game'
)
$ErrorActionPreference = 'Stop'

$dir, $exe = if ($Game -eq 's1') { 'sm1', 'Shenmue.exe' } else { 'sm2', 'Shenmue2.exe' }
$path = Join-Path $GameDir "$dir\$exe"
if (-not (Test-Path $path)) { throw "Patched game not found, run build.ps1: $path" }
if (-not (Get-Process steam -ErrorAction SilentlyContinue)) { throw 'Start Steam first.' }
Start-Process $path -WorkingDirectory (Split-Path $path)
