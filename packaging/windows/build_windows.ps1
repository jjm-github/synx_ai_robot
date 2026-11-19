#Requires -Version 5.1
<#
.SYNOPSIS
  Build the IIRobot Windows installer (IIRobot-<version>-Setup.exe).

.DESCRIPTION
  Must be run on Windows x64 with Python 3.12 and Inno Setup 6 installed.
  Prepares an app staging folder with Windows wheels, Python runtime and
  ffmpeg/poppler tools, then calls ISCC.exe.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
  powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1 -Version 1.1.0
#>
[CmdletBinding()]
param(
    [string]$Version = "1.0.0",
    [string]$Iscc = "",
    [switch]$SkipTools
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Log($Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Ok($Message)  { Write-Host "  OK  $Message" -ForegroundColor Green }
function Warn($Message) { Write-Host "  !   $Message" -ForegroundColor Yellow }
function Fail($Message) { Write-Host "ERROR: $Message" -ForegroundColor Red; exit 1 }

if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Fail "Build the Windows installer on Windows."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Stage = Join-Path $Root "build\windows\app"
$BuildRoot = Join-Path $Root "build\windows"
$Dist = Join-Path $Root "dist"

function Find-Python312 {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $cmdArgs = @($c.Args) + @("-c", "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])")
            $out = & $c.Exe $cmdArgs 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $exe = ($out | Select-Object -First 1).Trim()
            $ver = ($out | Select-Object -Last 1).Trim()
            if ($ver -eq "3.12") { return $exe }
        } catch { }
    }
    $known = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $known) { return $known }
    return $null
}

function Download-File([string]$Url, [string]$Dest) {
    if (Test-Path $Dest) { return }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Dest) -Force | Out-Null
    $tmp = "$Dest.part"
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
    Move-Item -Force $tmp $Dest
}

function Copy-App {
    if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
    New-Item -ItemType Directory -Path $Stage -Force | Out-Null
    Log "Copying application to $Stage"
    $rcArgs = @(
        $Root, $Stage, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
        "/XD", ".git", ".venv", "__pycache__", "data", "build", "dist", "tmp",
        "/XF", "config.json", "bots.json", "mcp_servers.json", "*.pyc"
    )
    & robocopy @rcArgs | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed with code $LASTEXITCODE" }
    if (-not (Test-Path (Join-Path $Stage "install-windows.ps1"))) {
        Fail "install-windows.ps1 was not copied to staging"
    }
}

function Get-WindowsWheels([string]$Python) {
    $wheels = Join-Path $Stage "wheels"
    New-Item -ItemType Directory -Path $wheels -Force | Out-Null
    Log "Downloading Windows wheels"
    & $Python -m pip download -r (Join-Path $Stage "requirements.txt") -d $wheels
    if ($LASTEXITCODE -ne 0) { Fail "pip download failed" }
    # Build wheels for any sdist-only dependencies (e.g. proxy-tools) so the
    # target machine can install everything with --no-index.
    $sdists = Get-ChildItem $wheels -Filter "*.tar.gz"
    foreach ($sdist in $sdists) {
        & $Python -m pip wheel --no-deps -w $wheels $sdist.FullName
        if ($LASTEXITCODE -ne 0) { Fail "could not build wheel for $($sdist.Name)" }
    }
    Get-ChildItem $wheels -Filter "*.tar.gz" | Remove-Item -Force
    Ok "Wheels: $((Get-ChildItem $wheels | Measure-Object).Count) files"
}

function Get-PythonInstaller {
    $dest = Join-Path $Stage "python-installer.exe"
    if (Test-Path $dest) { return }
    foreach ($v in @("3.12.10", "3.12.9", "3.12.8")) {
        $url = "https://www.python.org/ftp/python/$v/python-$v-amd64.exe"
        try {
            Log "Downloading Python $v installer"
            Download-File $url $dest
            return
        } catch {
            Warn "Python $v is not available: $url"
        }
    }
    Fail "Could not download a Python 3.12 installer"
}

function Get-Poppler {
    $dest = Join-Path $Stage "tools\poppler\Library\bin"
    if (Test-Path (Join-Path $dest "pdfinfo.exe")) { return }
    Log "Downloading poppler"
    $zip = Join-Path $env:TEMP "iirobot-poppler-build.zip"
    $tmp = Join-Path $env:TEMP "iirobot-poppler-build"
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/oschwartz10612/poppler-windows/releases/latest"
        $asset = $rel.assets | Where-Object { $_.name -like "*.zip" } | Select-Object -First 1
        if (-not $asset) { throw "no zip asset" }
        Download-File $asset.browser_download_url $zip
    } catch {
        Download-File "https://github.com/oschwartz10612/poppler-windows/releases/download/v24.08.0-0/Release-24.08.0-0.zip" $zip
    }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem $tmp -Recurse -Filter pdfinfo.exe | Select-Object -First 1
    if (-not $exe) { Fail "pdfinfo.exe not found in poppler archive" }
    New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
    Copy-Item $exe.Directory.FullName $dest -Recurse -Force
    Ok "Poppler bundled"
}

function Get-FFmpeg {
    $dest = Join-Path $Stage "tools\ffmpeg\bin"
    if (Test-Path (Join-Path $dest "ffmpeg.exe")) { return }
    Log "Downloading ffmpeg"
    $zip = Join-Path $env:TEMP "iirobot-ffmpeg-build.zip"
    $tmp = Join-Path $env:TEMP "iirobot-ffmpeg-build"
    Download-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $exe) { Fail "ffmpeg.exe not found in archive" }
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Copy-Item (Join-Path $exe.Directory.FullName "*") $dest -Recurse -Force
    Ok "ffmpeg bundled"
}

function Find-Iscc {
    if ($Iscc -and (Test-Path $Iscc)) { return $Iscc }
    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    }
    $pf = [Environment]::GetEnvironmentVariable("ProgramFiles")
    if ($pf) { $candidates += (Join-Path $pf "Inno Setup 6\ISCC.exe") }
    $pf86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    if ($pf86) { $candidates += (Join-Path $pf86 "Inno Setup 6\ISCC.exe") }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

Copy-App
$python = Find-Python312
if (-not $python) { Fail "Python 3.12 x64 is required to build Windows wheels. Install it from python.org." }
Ok "Build Python: $python"

Get-WindowsWheels $python
Get-PythonInstaller
if (-not $SkipTools) {
    try { Get-Poppler } catch { Warn "Poppler was not bundled: $_" }
    try { Get-FFmpeg } catch { Warn "ffmpeg was not bundled: $_" }
}

New-Item -ItemType Directory -Path $Dist -Force | Out-Null
$iscc = Find-Iscc
if (-not $iscc) {
    Fail "Inno Setup 6 not found. Install it: winget install -e --id JRSoftware.InnoSetup, then rerun."
}
Log "Building installer with $iscc"
& $iscc "/DMyAppVersion=$Version" (Join-Path $PSScriptRoot "iirobot.iss")
if ($LASTEXITCODE -ne 0) { Fail "ISCC failed with code $LASTEXITCODE" }
$out = Join-Path $Dist "IIRobot-$Version-Setup.exe"
if (Test-Path $out) {
    Ok "Installer: $out ($([math]::Round((Get-Item $out).Length / 1MB, 1)) MB)"
} else {
    Warn "ISCC finished, but $out was not found"
}
