param(
    [switch]$FrontendOnly
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
$ReleaseVersion = "v1.1.2.2"
$AppName = "ianAutoKTV_Server"
$DistRoot = Join-Path $ProjectDir "dist"
$BuildDir = Join-Path $DistRoot "ianAutoKTV_Server"
$TempDistRoot = Join-Path $DistRoot "_update_build"
$TempBuildDir = Join-Path $TempDistRoot "ianAutoKTV_Server"
$UpdateDir = Join-Path $DistRoot "ianAutoKTV_Update_$ReleaseVersion"
$YtDlp = Join-Path $ProjectDir "yt-dlp.exe"
$StaticDir = Join-Path $ProjectDir "static"

if (-not (Test-Path $StaticDir)) {
    throw "Static directory not found: $StaticDir"
}

Remove-Item $UpdateDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item $UpdateDir -ItemType Directory -Force | Out-Null
Copy-Item (Join-Path $ProjectDir "templates") $UpdateDir -Recurse -Force
Copy-Item $StaticDir $UpdateDir -Recurse -Force
Copy-Item (Join-Path $ProjectDir "optimize_existing_video.ps1") $UpdateDir -Force

if (-not $FrontendOnly) {
    $Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        throw "Python virtual environment not found: $Python"
    }

    if (-not (Test-Path $YtDlp)) {
        Invoke-WebRequest -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $YtDlp
    }

    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }

    Remove-Item $TempDistRoot -Recurse -Force -ErrorAction SilentlyContinue
    & $Python -m PyInstaller --noconfirm --clean --distpath $TempDistRoot (Join-Path $ProjectDir "ianAutoKTV_Server.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    Copy-Item (Join-Path $TempBuildDir "ianAutoKTV_Server.exe") $UpdateDir -Force
    Copy-Item (Join-Path $TempBuildDir "_internal") $UpdateDir -Recurse -Force
    Copy-Item $YtDlp $UpdateDir -Force
    $ScipySpecial = Join-Path $ProjectDir ".venv\Lib\site-packages\scipy\special\cython_special.cp38-win_amd64.pyd"
    if (Test-Path $ScipySpecial) {
        $ScipyTarget = Join-Path $UpdateDir "_internal\scipy\special"
        New-Item $ScipyTarget -ItemType Directory -Force | Out-Null
        Copy-Item $ScipySpecial $ScipyTarget -Force
    }
}

Set-Content (Join-Path $UpdateDir "VERSION.txt") $ReleaseVersion -Encoding ASCII
Remove-Item $TempDistRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Update package ready: $UpdateDir" -ForegroundColor Green
if ($FrontendOnly) {
    Write-Host "Frontend-only update: copy templates, the complete static folder, and VERSION.txt over the existing installation." -ForegroundColor Green
} else {
    Write-Host "Full update: copy all files over an existing installation after closing the server." -ForegroundColor Green
}