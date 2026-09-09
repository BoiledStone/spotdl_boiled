param(
    [string]$Playlist = $env:SPOTDL_PLAYLIST_URL,
    [string]$Output = $env:SPOTDL_OUTPUT_DIR,
    [int]$Workers = 0
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
if ([Console]::IsOutputRedirected -eq $false) {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = $env:SPOTDL_PYTHON
if (-not $Py) {
    $Py = "python"
}

$Script = Join-Path $Root "download_missing_autonomous_v2.py"
if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) {
    Write-Host "Script Python introuvable: $Script" -ForegroundColor Red
    exit 2
}

$PythonCommand = Get-Command $Py -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    Write-Host "Python introuvable: '$Py'. Installe Python 3.10+ ou definis SPOTDL_PYTHON." -ForegroundColor Red
    exit 2
}

$FfmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $FfmpegCommand) {
    Write-Host "FFmpeg introuvable dans le PATH. Installe FFmpeg avant de lancer le resolver." -ForegroundColor Red
    exit 2
}

if ($PSBoundParameters.ContainsKey("Workers") -and ($Workers -lt 1 -or $Workers -gt 5)) {
    Write-Host "Workers invalide: utilise une valeur entre 1 et 5." -ForegroundColor Red
    exit 2
}

if ($Workers -eq 0 -and $env:SPOTDL_WORKERS -match '^[1-5]$') {
    $Workers = [int]$env:SPOTDL_WORKERS
}

$Arguments = @($Script)
if ($Playlist) {
    $Arguments += @("--playlist", $Playlist)
}
if ($Output) {
    $Arguments += @("--output", $Output)
}
if ($Workers -gt 0) {
    $Arguments += @("--workers", $Workers)
}

Write-Host "SpotDL Resolver" -ForegroundColor Cyan
Write-Host "Playlist : $(if ($Playlist) { $Playlist } else { 'configuration du resolver' })"
Write-Host "Sortie   : $(if ($Output) { $Output } else { 'configuration du resolver' })"
Write-Host "Workers  : $(if ($Workers) { $Workers } else { 'configuration du resolver' })"
Write-Host ""

& $PythonCommand.Source @Arguments

$ExitCode = $LASTEXITCODE
if ($ExitCode -ne 0) {
    Write-Host "Le resolver s'est termine avec le code $ExitCode." -ForegroundColor Yellow
}
exit $ExitCode
