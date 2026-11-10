#Requires -Version 5.1
# Builds the patched game folder next to the Steam install, so that Steam keeps the clean game.
# Game files are hard links to the Steam copies (no extra disk space, same drive required).
# Only dxgi.dll (ASI loader), steam_appid.txt, the ASIs and devsaves are real files here.
# Safe to rerun, for example after a Steam update.
param(
    [string]$SteamDir = 'C:\Program Files (x86)\Steam',
    [string]$GameDir  = 'C:\opt\shenmue-game'
)
$ErrorActionPreference = 'Stop'

$AppId     = '758330'
$SteamGame = Join-Path $SteamDir 'steamapps\common\SMLaunch'
$OwnFiles  = @('dxgi.dll', 'steam_appid.txt')

foreach ($title in 'sm1', 'sm2') {
    $source = Join-Path $SteamGame $title
    $target = Join-Path $GameDir $title
    if (-not (Test-Path (Join-Path $source 'archives'))) { throw "Steam game files not found: $source" }
    New-Item -ItemType Directory -Force $target | Out-Null

    $linked = 0
    foreach ($file in Get-ChildItem $source -Recurse -File) {
        if ($file.Extension -eq '.asi' -or $OwnFiles -contains $file.Name) { continue }
        $link = Join-Path $target $file.FullName.Substring($source.Length + 1)
        if (Test-Path $link) {
            # Hard links share size and timestamp, so a difference means Steam replaced the file.
            $current = Get-Item $link
            if ($current.Length -eq $file.Length -and $current.LastWriteTimeUtc -eq $file.LastWriteTimeUtc) { continue }
            Remove-Item $link -Force
        }
        New-Item -ItemType Directory -Force (Split-Path $link) | Out-Null
        New-Item -ItemType HardLink -Path $link -Target $file.FullName | Out-Null
        $linked++
    }
    Write-Host "$title`: $linked file(s) linked"

    $loader = Join-Path $target 'dxgi.dll'
    if (-not (Test-Path $loader)) {
        $steamLoader = Join-Path $source 'dxgi.dll'
        if (-not (Test-Path $steamLoader)) { throw "Put Ultimate ASI Loader (dxgi.dll) in $target" }
        Copy-Item $steamLoader $loader
    }
    Set-Content (Join-Path $target 'steam_appid.txt') $AppId -NoNewline -Encoding ascii

    # First setup only: start the patched game from a copy of the Steam saves.
    $saves = Join-Path $target 'devsaves'
    if (-not (Test-Path $saves)) {
        $accounts = @(Get-ChildItem (Join-Path $SteamDir "userdata\*\$AppId\remote") -Directory -ErrorAction SilentlyContinue)
        if ($accounts.Count -ne 1) {
            Write-Warning "Expected one Steam account with saves, found $($accounts.Count). $title starts with empty saves."
            continue
        }
        New-Item -ItemType Directory $saves | Out-Null
        $steamSaves = Join-Path $accounts[0].FullName "${title}saves"
        if (Test-Path $steamSaves) { Copy-Item (Join-Path $steamSaves '*') $saves }
        Write-Host "$title`: saves copied to $saves"
    }
}
