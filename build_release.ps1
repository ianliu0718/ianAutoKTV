$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$ReleaseVersion = "v1.1.2.1"
$AppName = "ianAutoKTV_Server"
$DistDir = Join-Path $ProjectDir "dist\$AppName"
$DistRoot = Join-Path $ProjectDir "dist"

if (-not (Test-Path $Python)) {
    throw "Python virtual environment not found: $Python"
}

if (-not (Test-Path (Join-Path $ProjectDir "ffmpeg\bin\ffmpeg.exe"))) {
    $SystemFfmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($SystemFfmpeg) {
        New-Item (Join-Path $ProjectDir "ffmpeg\bin") -ItemType Directory -Force | Out-Null
        $SystemFfmpegDir = Split-Path $SystemFfmpeg.Source -Parent
        Copy-Item (Join-Path $SystemFfmpegDir "*") (Join-Path $ProjectDir "ffmpeg\bin") -Force
    } else {
        throw "FFmpeg not found. Install FFmpeg or place ffmpeg.exe in ffmpeg\bin."
    }
}

$YtDlp = Join-Path $ProjectDir "yt-dlp.exe"
if (-not (Test-Path $YtDlp)) {
    Invoke-WebRequest -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $YtDlp
}

& $Python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }

Remove-Item (Join-Path $DistRoot "ianAutoKTV_Server"), (Join-Path $DistRoot "ianAutoKTV_Server.exe") -Recurse -Force -ErrorAction SilentlyContinue
& $Python -m PyInstaller --noconfirm --clean (Join-Path $ProjectDir "ianAutoKTV_Server.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

Copy-Item (Join-Path $ProjectDir "templates") $DistDir -Recurse -Force
Copy-Item (Join-Path $ProjectDir "static") $DistDir -Recurse -Force
Copy-Item (Join-Path $ProjectDir "pretrained_models") $DistDir -Recurse -Force
Copy-Item (Join-Path $ProjectDir "ffmpeg") $DistDir -Recurse -Force
$ScipySpecial = Join-Path $ProjectDir ".venv\Lib\site-packages\scipy\special\cython_special.cp38-win_amd64.pyd"
if (Test-Path $ScipySpecial) {
    $ScipyTarget = Join-Path $DistDir "_internal\scipy\special"
    New-Item $ScipyTarget -ItemType Directory -Force | Out-Null
    Copy-Item $ScipySpecial $ScipyTarget -Force
}
Copy-Item $YtDlp $DistDir -Force
Copy-Item (Join-Path $ProjectDir "optimize_existing_video.ps1") $DistDir -Force
New-Item (Join-Path $DistDir "ktv_songs") -ItemType Directory -Force | Out-Null

Write-Host "Build complete: $DistDir\ianAutoKTV_Server.exe" -ForegroundColor Green
Write-Host "Publish this folder as the $ReleaseVersion base installation." -ForegroundColor Green
