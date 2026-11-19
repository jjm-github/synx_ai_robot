#Requires -Version 5.1
<#
.SYNOPSIS
  IIRobot installer for Windows 10/11 (per-user, no admin required).

.DESCRIPTION
  Installs Python if needed, creates .venv, installs Python dependencies,
  downloads voice models and optionally Ollama + local LLM models.
  Must be run from the repository folder (or use -FromSetup with -AppDir).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Llm small
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Llm both -VoiceModels yes
#>
[CmdletBinding()]
param(
    [ValidateSet("none", "small", "default", "both", "custom")]
    [string]$Llm = "none",
    [string]$LlmModels = "",
    [ValidateSet("auto", "yes", "no")]
    [string]$VoiceModels = "auto",
    [ValidateSet("auto", "yes", "no")]
    [string]$Ollama = "auto",
    [string]$AppDir = "",
    [string]$WheelsDir = "",
    [string]$ToolsDir = "",
    [string]$PythonInstaller = "",
    [switch]$SkipSystem,
    [switch]$NoShortcuts,
    [switch]$Unattended,
    [switch]$FromSetup
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Log($Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Ok($Message)  { Write-Host "  OK  $Message" -ForegroundColor Green }
function Warn($Message) { Write-Host "  !   $Message" -ForegroundColor Yellow }
function Fail($Message) { Write-Host "ERROR: $Message" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}

function Ask-YesNo([string]$Question, [bool]$DefaultYes) {
    if ($Unattended -or -not [Environment]::UserInteractive) { return $DefaultYes }
    $suffix = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$Question $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultYes }
    return $answer -match '^(y|yes|д|да|1)$'
}

function Get-PythonVersion([string]$Exe) {
    try {
        $v = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($v | Select-Object -First 1).Trim()
    } catch { return $null }
}

function Find-Python {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3.10") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $cmdArgs = @($c.Args) + @("-c", "import sys; print(sys.executable)")
            $out = & $c.Exe $cmdArgs 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $exe = ($out | Select-Object -First 1).Trim()
            $ver = Get-PythonVersion $exe
            if ($ver) {
                $parts = $ver.Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10 -and [int]$parts[1] -le 13) {
                    return $exe
                }
            }
        } catch { }
    }
    $known = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($p in $known) {
        if (Test-Path $p) {
            $ver = Get-PythonVersion $p
            if ($ver) { return $p }
        }
    }
    return $null
}

function Install-Python {
    Log "Python 3.10+ not found; installing Python 3.12"
    if ($PythonInstaller -and (Test-Path $PythonInstaller)) {
        Log "Using bundled Python installer: $PythonInstaller"
        $proc = Start-Process -FilePath $PythonInstaller -Wait -PassThru `
            -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1"
        if ($proc.ExitCode -ne 0) { Fail "Python installer exited with code $($proc.ExitCode)" }
        Refresh-Path
        $py = Find-Python
        if ($py) { return $py }
    }
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install -e --id Python.Python.3.12 --silent `
            --accept-package-agreements --accept-source-agreements
    }
    Refresh-Path
    $py = Find-Python
    if ($py) { return $py }

    $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    $installer = Join-Path $env:TEMP "iirobot-python-3.12.10-amd64.exe"
    Log "Downloading Python installer"
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    $proc = Start-Process -FilePath $installer -Wait -PassThru `
        -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1"
    if ($proc.ExitCode -ne 0) { Fail "Python installer exited with code $($proc.ExitCode)" }
    Refresh-Path
    $py = Find-Python
    if (-not $py) { Fail "Python installed but python.exe was not found. Install Python 3.12 manually." }
    return $py
}

function Copy-App([string]$Source, [string]$Dest) {
    if ((Resolve-Path $Source).Path -eq (Resolve-Path $Dest -ErrorAction SilentlyContinue).Path) {
        return
    }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    Log "Copying application to $Dest"
    $rcArgs = @(
        $Source, $Dest, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
        "/XD", ".git", ".venv", "__pycache__", "data", "build", "dist", "tmp",
        "/XF", "config.json", "bots.json", "mcp_servers.json", "*.pyc"
    )
    & robocopy @rcArgs | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed with code $LASTEXITCODE" }
}

function Download-File([string]$Url, [string]$Dest) {
    if (Test-Path $Dest) { return }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Dest) -Force | Out-Null
    $tmp = "$Dest.part"
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
    Move-Item -Force $tmp $Dest
}

function Ensure-Tools([string]$Root) {
    if ($SkipSystem) {
        Warn "Skipping ffmpeg/poppler installation (-SkipSystem)"
        return
    }
    # ffmpeg: prefer winget, fall back to the gyan.dev ZIP
    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Log "Installing ffmpeg via winget"
            & winget install -e --id Gyan.FFmpeg --silent `
                --accept-package-agreements --accept-source-agreements | Out-Null
            Refresh-Path
        }
    }
    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
        $ff = Join-Path $Root "ffmpeg\bin\ffmpeg.exe"
        if (-not (Test-Path $ff)) {
            Log "Downloading ffmpeg"
            $zip = Join-Path $env:TEMP "iirobot-ffmpeg.zip"
            $tmp = Join-Path $env:TEMP "iirobot-ffmpeg"
            Download-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $exe = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
            if (-not $exe) { Fail "ffmpeg.exe not found in archive" }
            New-Item -ItemType Directory -Path (Join-Path $Root "ffmpeg\bin") -Force | Out-Null
            Copy-Item (Join-Path $exe.Directory.FullName "*") (Join-Path $Root "ffmpeg\bin") -Recurse -Force
        }
    }

    # poppler: pdfinfo/pdftoppm/pdftotext/pdffonts
    if (-not (Get-Command pdfinfo.exe -ErrorAction SilentlyContinue)) {
        $pdfinfo = Join-Path $Root "poppler\Library\bin\pdfinfo.exe"
        if (-not (Test-Path $pdfinfo)) {
            Log "Downloading poppler (PDF tools)"
            $zip = Join-Path $env:TEMP "iirobot-poppler.zip"
            $tmp = Join-Path $env:TEMP "iirobot-poppler"
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
            New-Item -ItemType Directory -Path (Join-Path $Root "poppler\Library") -Force | Out-Null
            Copy-Item $exe.Directory.FullName (Join-Path $Root "poppler\Library\bin") -Recurse -Force
        }
    }
    Ok "ffmpeg/poppler ready"
}

function Install-VoiceModels([string]$DataDir) {
    $dir = Join-Path $DataDir "voice\models"
    $tone = Join-Path $dir "t_one"
    $giga = Join-Path $dir "gigaam_v3"
    New-Item -ItemType Directory -Path $tone, $giga -Force | Out-Null
    $toneBase = "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-t-one-russian-2025-09-08/resolve/main"
    $gigaBase = "https://huggingface.co/Smirnov75/GigaAM-v3-sherpa-onnx/resolve/main"
    Log "Downloading voice models (~450 MB)"
    Download-File "$toneBase/model.onnx" (Join-Path $tone "model.onnx")
    Download-File "$toneBase/tokens.txt" (Join-Path $tone "tokens.txt")
    Download-File "$gigaBase/gigaam_v3_e2e_ctc_int8.onnx" (Join-Path $giga "gigaam_v3_e2e_ctc_int8.onnx")
    Download-File "$gigaBase/gigaam_v3_e2e_ctc_tokens.txt" (Join-Path $giga "gigaam_v3_e2e_ctc_tokens.txt")
    Ok "Voice models installed to $dir"
}

function Test-Ollama {
    try {
        & ollama ps *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Ensure-Ollama {
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        if ($Ollama -eq "no") { return $false }
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Log "Installing Ollama"
            & winget install -e --id Ollama.Ollama --silent `
                --accept-package-agreements --accept-source-agreements | Out-Null
            Refresh-Path
        }
    }
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        if ($Ollama -eq "no") { return $false }
        Log "Downloading Ollama installer"
        $setup = Join-Path $env:TEMP "OllamaSetup.exe"
        Download-File "https://ollama.com/download/OllamaSetup.exe" $setup
        Start-Process -FilePath $setup -ArgumentList "/S" -Wait
        Refresh-Path
    }
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) { return $false }
    if (-not (Test-Ollama)) {
        Log "Starting Ollama server"
        $exe = (Get-Command ollama).Source
        Start-Process -FilePath $exe -ArgumentList "serve" -WindowStyle Hidden | Out-Null
        Start-Sleep -Seconds 4
    }
    return (Test-Ollama)
}

function Configure-Voice([string]$Python, [string]$Model, [bool]$Enabled) {
    $env:IIROBOT_DATA_DIR = $script:DataDir
    $env:IIROBOT_CONFIG = $script:ConfigFile
    $env:IIROBOT_BOTS = $script:BotsFile
    $env:IIROBOT_PROMPT = $script:PromptFile
    $env:IIROBOT_MCP_SERVERS = $script:McpFile
    $code = @'
import sys
from config import ensure_files, load_config, save_config
ensure_files()
cfg = load_config()
voice = cfg.setdefault("voice", {})
voice["llm_correct"] = bool(int(sys.argv[1]))
correct = voice.setdefault("correct", {})
model = sys.argv[2] if len(sys.argv) > 2 else ""
if int(sys.argv[1]):
    correct["provider"] = "ollama"
    correct["base_url"] = correct.get("base_url") or "http://127.0.0.1:11434"
    correct["model"] = model
    correct.setdefault("keep_alive", "5m")
    correct.setdefault("timeout", 30)
    correct.setdefault("temperature", 0)
    correct.setdefault("num_predict", 1024)
else:
    correct["provider"] = "none"
save_config(cfg)
print("voice.llm_correct =", voice["llm_correct"], "model =", correct.get("model", "—"))
'@
    $enabledFlag = if ($Enabled) { "1" } else { "0" }
    $code | & $Python - $enabledFlag $Model
    if ($LASTEXITCODE -ne 0) { Warn "Could not update voice configuration" }
}

function Create-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkDir, [string]$Icon) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkDir
    $shortcut.IconLocation = $Icon
    $shortcut.Description = "IIRobot production assistant"
    $shortcut.Save()
}

# ------------------------------------------------------------------ setup ---
if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Fail "This installer is for Windows."
}
if ([Environment]::OSVersion.Version.Major -lt 10) {
    Fail "Windows 10 or 11 is required."
}

$script:DataDir = if ($env:IIROBOT_DATA_DIR) { $env:IIROBOT_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "IIRobot\data" }
$script:ConfigDir = if ($env:IIROBOT_CONFIG) { Split-Path -Parent $env:IIROBOT_CONFIG } else { Join-Path $env:APPDATA "IIRobot" }
$script:ConfigFile = Join-Path $script:ConfigDir "config.json"
$script:BotsFile = Join-Path $script:ConfigDir "bots.json"
$script:PromptFile = Join-Path $script:ConfigDir "system.md"
$script:McpFile = Join-Path $script:ConfigDir "mcp_servers.json"
$script:ToolsDir = if ($ToolsDir) { $ToolsDir } else { Join-Path $env:LOCALAPPDATA "IIRobot\tools" }
New-Item -ItemType Directory -Path $script:DataDir, $script:ConfigDir, $script:ToolsDir -Force | Out-Null

foreach ($pair in @(
    @("IIROBOT_DATA_DIR", $script:DataDir),
    @("IIROBOT_CONFIG", $script:ConfigFile),
    @("IIROBOT_BOTS", $script:BotsFile),
    @("IIROBOT_PROMPT", $script:PromptFile),
    @("IIROBOT_MCP_SERVERS", $script:McpFile),
    @("IIROBOT_TOOLS_DIR", $script:ToolsDir)
)) {
    [Environment]::SetEnvironmentVariable($pair[0], $pair[1], "User")
    Set-Item -Path "Env:$($pair[0])" -Value $pair[1]
}

if (-not $AppDir) {
    $AppDir = if ($FromSetup) { Split-Path -Parent $PSScriptRoot } else { Join-Path $env:LOCALAPPDATA "IIRobot\app" }
}
$AppDir = [IO.Path]::GetFullPath($AppDir)

if (-not $FromSetup) {
    $source = $PSScriptRoot
    if (-not (Test-Path (Join-Path $source "main.py"))) {
        Fail "main.py not found next to install-windows.ps1. Run the script from the repository folder."
    }
    Copy-App $source $AppDir
}
if (-not (Test-Path (Join-Path $AppDir "main.py"))) { Fail "Application files not found in $AppDir" }

Log "Application directory: $AppDir"
if ($PythonInstaller -and (Test-Path $PythonInstaller)) {
    Log "Installing bundled Python runtime"
    $proc = Start-Process -FilePath $PythonInstaller -Wait -PassThru `
        -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1"
    if ($proc.ExitCode -ne 0) { Fail "Python installer exited with code $($proc.ExitCode)" }
    Refresh-Path
}
$python = Find-Python
if (-not $python) {
    if ($SkipSystem) { Fail "Python 3.10+ is required. Install it and rerun." }
    $python = Install-Python
}
Ok "Python: $python"

$venv = Join-Path $AppDir ".venv"
$vpy = Join-Path $venv "Scripts\python.exe"
$vpw = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $vpy)) {
    Log "Creating virtual environment"
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create venv" }
}
& $vpy -m pip install --upgrade pip
if ($WheelsDir -and (Test-Path $WheelsDir)) {
    Log "Installing Python dependencies from bundled wheels"
    & $vpy -m pip install --no-index --find-links $WheelsDir -r (Join-Path $AppDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Warn "Offline wheels failed, trying PyPI"
        & $vpy -m pip install -r (Join-Path $AppDir "requirements.txt")
    }
} else {
    Log "Installing Python dependencies from PyPI"
    & $vpy -m pip install -r (Join-Path $AppDir "requirements.txt")
}
if ($LASTEXITCODE -ne 0) { Fail "Python dependency installation failed" }
Ok "Python dependencies installed"

Ensure-Tools $script:ToolsDir

if ($VoiceModels -eq "auto") {
    $VoiceModels = if (Ask-YesNo "Download voice models T-one + GigaAM (~450 MB)?" $true) { "yes" } else { "no" }
}
if ($VoiceModels -eq "yes") {
    try { Install-VoiceModels $script:DataDir }
    catch { Warn "Voice models were not downloaded: $_" }
} else {
    Warn "Voice models skipped; voice input will not work"
}

$models = @()
switch ($Llm) {
    "none"    { $models = @() }
    "small"   { $models = @("qwen3.5:4b") }
    "default" { $models = @("qwen2.5:7b-instruct-q4_K_M") }
    "both"    { $models = @("qwen3.5:4b", "qwen2.5:7b-instruct-q4_K_M") }
    "custom"  { $models = @($LlmModels -split "[,\s]+" | Where-Object { $_ }) }
}
if ($models.Count -eq 0) {
    Log "No local LLM selected; voice correction disabled"
    Configure-Voice $vpy "" $false
} else {
    if ($Ollama -eq "auto") {
        $Ollama = if (Ask-YesNo "Install Ollama and download local LLM models?" $true) { "yes" } else { "no" }
    }
    $ready = @()
    if (($Ollama -eq "yes") -and (Ensure-Ollama)) {
        $installed = @()
        try {
            $installed = (& ollama list) -split "`r?`n" | Select-Object -Skip 1 |
                ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ }
        } catch { }
        foreach ($model in $models) {
            if ($installed -contains $model) {
                Ok "Ollama: $model already installed"
                $ready += $model
            } else {
                Log "Ollama: pulling $model"
                & ollama pull $model
                if ($LASTEXITCODE -eq 0) { $ready += $model }
                else { Warn "Could not pull $model" }
            }
        }
    }
    if ($ready.Count -gt 0) {
        Configure-Voice $vpy $ready[0] $true
        Ok "Voice corrector: $($ready[0])"
    } else {
        Warn "No LLM model available; voice correction disabled"
        Configure-Voice $vpy "" $false
    }
}

if (-not $NoShortcuts) {
    $icon = Join-Path $AppDir "static\app_icon.ico"
    if (-not (Test-Path $icon)) { $icon = Join-Path $AppDir "static\app_icon.png" }
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
    $target = if (Test-Path $vpw) { $vpw } else { $vpy }
    $args = '"' + (Join-Path $AppDir "main.py") + '"'
    Create-Shortcut (Join-Path $desktop "IIRobot.lnk") $target $args $AppDir $icon
    Create-Shortcut (Join-Path $startMenu "IIRobot.lnk") $target $args $AppDir $icon
    Ok "Shortcuts created"
}

Write-Host ""
Log "IIRobot installed"
Write-Host "  Application: $AppDir"
Write-Host "  Data:        $script:DataDir"
Write-Host "  Config:      $script:ConfigFile"
Write-Host "  Launch:      Start menu / Desktop -> IIRobot"
Write-Host ""
Write-Host "ComfyUI is installed separately; set its URL in Settings."
