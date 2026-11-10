#Requires -Version 5.1
# Configures and builds the Windows ASI targets (shenmue1_asi, shenmue2_asi)
# and the standalone executables (shenmue1, shenmue2).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$BuildDir  = 'build/windows-asi'
# Patched game folder made by setup-game.ps1. The built ASIs are copied here after a
# successful build. The Steam install stays clean.
$GameDir   = 'C:\opt\shenmue-game'
$Generator = 'Visual Studio 18 2026'

# Use VCPKG_ROOT if it is set, otherwise the local install.
if (-not $env:VCPKG_ROOT) { $env:VCPKG_ROOT = 'C:\opt\vcpkg' }
$Toolchain = "$env:VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake"
if (-not (Test-Path $Toolchain)) { throw "vcpkg toolchain not found: $Toolchain" }

function Invoke-Step {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

Invoke-Step git @('submodule', 'update', '--init', '--recursive')

# CMake only reads the toolchain and generator on the first configure.
# If the cache was made with different ones, start over.
$Cache = Join-Path $BuildDir 'CMakeCache.txt'
if (Test-Path $Cache) {
    $cacheText = Get-Content $Cache -Raw
    $cachedToolchain = if ($cacheText -match '(?m)^CMAKE_TOOLCHAIN_FILE:[A-Z]+=(.*)$') { $Matches[1].Trim() } else { '' }
    $cachedGenerator = if ($cacheText -match '(?m)^CMAKE_GENERATOR:INTERNAL=(.*)$') { $Matches[1].Trim() } else { '' }
    $normalize = { param($p) ($p -replace '\\', '/').ToLowerInvariant() }
    if ((& $normalize $cachedToolchain) -ne (& $normalize $Toolchain) -or $cachedGenerator -ne $Generator) {
        Write-Host "Stale CMake cache in $BuildDir, removing it."
        # Idle MSBuild worker nodes keep handles on the build tree and block the delete.
        Get-Process MSBuild, vctip -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Milliseconds 500
        Remove-Item -Recurse -Force $BuildDir
    }
}

Invoke-Step cmake @(
    '-S', '.', '-B', $BuildDir,
    '-G', $Generator, '-A', 'x64',
    "-DCMAKE_TOOLCHAIN_FILE=$Toolchain",
    '-DVCPKG_TARGET_TRIPLET=x64-windows-static',
    '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded$<$<CONFIG:Debug>:Debug>',
    '-DSHENMUE_STEAM=ON',
    '-DSHENMUE_RT_ARCADE=ON',
    '-DSHENMUE_LOAD_HANGON_DLL=ON',
    '-DSHENMUE_LOCAL_SAVES=ON',
    '-DSHENMUE_ENHANCEMENT_S1_QTE_SELECT=ON'
)

Invoke-Step cmake @(
    '--build', $BuildDir,
    '--config', 'Release',
    '--target', 'shenmue1_asi', 'shenmue2_asi', 'shenmue1', 'shenmue2',
    '--parallel', '4'
)

# Deploy: copy the ASIs next to the game exes (the ASI loader there is dxgi.dll).
# Symlinks would need admin rights or Developer Mode, so plain copies are used.
& (Join-Path $PSScriptRoot 'setup-game.ps1') -GameDir $GameDir
foreach ($pair in @(@('sm1', 'shenmue1e.asi', 'Shenmue'), @('sm2', 'shenmue2e.asi', 'Shenmue2'))) {
    $dir, $asi, $process = $pair
    $target = Join-Path $GameDir $dir
    # The clean Steam game may keep running: only this folder's exe locks the ASI.
    if (Get-Process $process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$target\*" }) {
        Write-Warning "$process.exe is running; $asi was not deployed. Close the game and rerun."
        continue
    }
    Copy-Item (Join-Path $BuildDir "Release/$asi") $target -Force
    Write-Host "Deployed $asi -> $target"
}
