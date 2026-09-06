param(
    [string]$Playlist = $env:SPOTDL_PLAYLIST_URL,
    [string]$Output = $env:SPOTDL_OUTPUT_DIR
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = $env:SPOTDL_PYTHON
if (-not $Py) {
    $Py = "python"
}

$Script = Join-Path $Root "download_missing_autonomous_v2.py"
if (-not $Output) {
    $Output = Join-Path $Root "downloads"
}

if (-not $Playlist) {
    throw "Définis SPOTDL_PLAYLIST_URL ou passe -Playlist."
}

& $Py $Script `
    --playlist $Playlist `
    --output $Output
