<#
.SYNOPSIS
    Installs or uninstalls Termind globally for the current Windows user.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
    powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1 -Uninstall
#>

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    Write-Error "This script can only be run on Windows!"
    exit 1
}

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\Termind"
$BinDir     = Join-Path $InstallDir "bin"
$VenvDir    = Join-Path $InstallDir ".venv"

function Update-UserPath {
    param(
        [string]$TargetDir,
        [ValidateSet("Add", "Remove")]
        [string]$Mode
    )

    $CurrentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $CurrentPath) {
        $CurrentPath = ""
    }

    $NormalizedTarget = $TargetDir.TrimEnd("\")
    $Entries = @($CurrentPath -split ";" | Where-Object { $_ -and $_.Trim() -ne "" })
    $Filtered = @($Entries | Where-Object { $_.Trim().TrimEnd("\") -ine $NormalizedTarget })

    if ($Mode -eq "Add") {
        $NewEntries = $Filtered + @($NormalizedTarget)
        $NewPath = ($NewEntries -join ";")
        if ($Entries.Count -eq $Filtered.Count) {
            [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
            Write-Host "[+] Added to User PATH: $NormalizedTarget" -ForegroundColor Green
        } else {
            Write-Host "[*] Already in User PATH: $NormalizedTarget" -ForegroundColor Cyan
        }
        $env:Path = "$NormalizedTarget;$env:Path"
    } else {
        $NewPath = ($Filtered -join ";")
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        Write-Host "[+] Removed from User PATH: $NormalizedTarget" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------
# UNINSTALL MODE
# ---------------------------------------------------------------------
if ($Uninstall) {
    Write-Host "Uninstalling Termind..." -ForegroundColor Yellow
    Update-UserPath -TargetDir $BinDir -Mode Remove

    # Remove Start Menu shortcut
    $StartMenuShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Termind.lnk"
    if (Test-Path $StartMenuShortcut) {
        Remove-Item -Force $StartMenuShortcut
        Write-Host "[+] Removed Start Menu shortcut" -ForegroundColor Yellow
    }

    # Remove Explorer Context Menu entries
    $ContextRegistryKeys = @(
        "HKCU:\Software\Classes\Directory\shell\Termind",
        "HKCU:\Software\Classes\Directory\Background\shell\Termind"
    )
    foreach ($regKey in $ContextRegistryKeys) {
        if (Test-Path $regKey) {
            Remove-Item -Recurse -Force $regKey
            Write-Host "[+] Removed Explorer context menu: $regKey" -ForegroundColor Yellow
        }
    }

    if (Test-Path $InstallDir) {
        Remove-Item -Recurse -Force $InstallDir
        Write-Host "[+] Removed directory: $InstallDir" -ForegroundColor Green
    }
    Write-Host "`nTermind has been uninstalled." -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------------
# PRE-CHECKS & PYTHON 3.11+ DISCOVERY
# ---------------------------------------------------------------------
function Find-ValidPython {
    $Candidates = @(
        @{ Exe = "py";      Arg = "-3" },
        @{ Exe = "py";      Arg = "-3.15" },
        @{ Exe = "py";      Arg = "-3.14" },
        @{ Exe = "py";      Arg = "-3.13" },
        @{ Exe = "py";      Arg = "-3.12" },
        @{ Exe = "py";      Arg = "-3.11" },
        @{ Exe = "python";  Arg = $null },
        @{ Exe = "python3"; Arg = $null }
    )

    foreach ($c in $Candidates) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # Skip Microsoft Store dummy stub
        if ($cmd.Source -like "*\WindowsApps\*") {
            try {
                $testOut = if ($c.Arg) { & $c.Exe $c.Arg --version 2>$null } else { & $c.Exe --version 2>$null }
                if ($LASTEXITCODE -ne 0) {
                    continue
                }
            } catch {
                continue
            }
        }
        try {
            $ver = if ($c.Arg) {
                & $c.Exe $c.Arg -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            } else {
                & $c.Exe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            }
            if ($LASTEXITCODE -eq 0 -and $ver -match '^(\d+)\.(\d+)$') {
                $maj = [int]$Matches[1]
                $min = [int]$Matches[2]
                if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
                    return $c
                }
            }
        } catch {}
    }
    return $null
}

$Python = Find-ValidPython

if (-not $Python) {
    Write-Host "[!] Python 3.11+ was not found on this system." -ForegroundColor Yellow
    if (Get-Command "winget" -ErrorAction SilentlyContinue) {
        $reply = Read-Host "Install Python 3.15 automatically via winget? [y/N]"
        if ($reply -match '^(y|yes)$') {
            winget install -e --id Python.Python.3.15 --scope user --accept-source-agreements --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
            $Python = Find-ValidPython
        }
    }
    if (-not $Python) {
        Write-Error "Python 3.11 or higher is required. Please install it from https://www.python.org/downloads/ and re-run this script."
        exit 1
    }
}

$PyLabel = if ($Python.Arg) { "$($Python.Exe) $($Python.Arg)" } else { $Python.Exe }
Write-Host "[*] Using Python: $PyLabel" -ForegroundColor Cyan

# ---------------------------------------------------------------------
# 1. COPY APPLICATION FILES
# ---------------------------------------------------------------------
Write-Host "[*] Installing Termind into $InstallDir ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir     | Out-Null

Copy-Item "pyproject.toml", "README.md", "LICENSE" -Destination $InstallDir -Force
$Icon = Join-Path $InstallDir "icon.ico"
Copy-Item "assets\icon.ico" -Destination $Icon -Force

# ---------------------------------------------------------------------
# 2. CREATE ISOLATED VENV & INSTALL DEPENDENCIES
# ---------------------------------------------------------------------
if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Write-Host "[*] Creating isolated virtual environment..." -ForegroundColor Cyan
    if ($Python.Arg) {
        & $Python.Exe $Python.Arg -m venv $VenvDir
    } else {
        & $Python.Exe -m venv $VenvDir
    }
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$BuildDir = Join-Path ([System.IO.Path]::GetTempPath()) "termind-build-$PID"
if (Test-Path $BuildDir) {
    Remove-Item -Recurse -Force $BuildDir
}
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

try {
    Copy-Item "termind" -Destination $BuildDir -Recurse -Force
    Get-ChildItem -Path (Join-Path $BuildDir "termind") -Directory -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
    Copy-Item "pyproject.toml", "README.md", "LICENSE" -Destination $BuildDir -Force

    Write-Host "[*] Installing Termind and dependencies from pyproject.toml ..." -ForegroundColor Cyan
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install $BuildDir
} finally {
    if (Test-Path $BuildDir) {
        Remove-Item -Recurse -Force $BuildDir
    }
}

# ---------------------------------------------------------------------
# 3. CREATE GLOBAL LAUNCHER (termind.cmd WITHOUT UTF-8 BOM)
# ---------------------------------------------------------------------
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$LauncherPath = Join-Path $BinDir "termind.cmd"
$LauncherContent = "@echo off`r`nset PYTHONUTF8=1`r`n`"$VenvPython`" -P -m termind %*`r`n"
[System.IO.File]::WriteAllText($LauncherPath, $LauncherContent, $Utf8NoBom)

# ---------------------------------------------------------------------
# 4. REGISTER IN USER PATH
# ---------------------------------------------------------------------
Update-UserPath -TargetDir $BinDir -Mode Add

# ---------------------------------------------------------------------
# 5. CREATE START MENU SHORTCUT (.lnk)
# ---------------------------------------------------------------------
Write-Host "[*] Creating Start Menu shortcut..." -ForegroundColor Cyan
$StartMenuDir  = [Environment]::GetFolderPath("Programs")
$ShortcutPath  = Join-Path $StartMenuDir "Termind.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath       = $LauncherPath
$Shortcut.WorkingDirectory = $env:USERPROFILE
$Shortcut.WindowStyle      = 1
$Shortcut.IconLocation     = $Icon
$Shortcut.Description      = "Cross-platform, multi-provider autonomous AI engineering assistant for your terminal"
$Shortcut.Save()
Write-Host "[+] Start Menu shortcut created" -ForegroundColor Green

# ---------------------------------------------------------------------
# 6. REGISTER EXPLORER CONTEXT MENU ("Open with Termind")
# ---------------------------------------------------------------------
Write-Host "[*] Adding 'Open with Termind' to Explorer context menu..." -ForegroundColor Cyan

# 6.1. Right-click on empty space inside a folder (Background)
$BgKey = "HKCU:\Software\Classes\Directory\Background\shell\Termind"
New-Item -Path $BgKey -Force | Out-Null
Set-ItemProperty -Path $BgKey -Name "(Default)" -Value "Open with Termind"
Set-ItemProperty -Path $BgKey -Name "Icon" -Value $Icon
$BgCmdKey = Join-Path $BgKey "command"
New-Item -Path $BgCmdKey -Force | Out-Null
Set-ItemProperty -Path $BgCmdKey -Name "(Default)" -Value "`"$LauncherPath`""

# 6.2. Right-click on a folder icon
$DirKey = "HKCU:\Software\Classes\Directory\shell\Termind"
New-Item -Path $DirKey -Force | Out-Null
Set-ItemProperty -Path $DirKey -Name "(Default)" -Value "Open with Termind"
Set-ItemProperty -Path $DirKey -Name "Icon" -Value $Icon
$DirCmdKey = Join-Path $DirKey "command"
New-Item -Path $DirCmdKey -Force | Out-Null
Set-ItemProperty -Path $DirCmdKey -Name "(Default)" -Value "`"$LauncherPath`" `"%V\.`""

Write-Host "[+] Explorer context menu registered" -ForegroundColor Green

Write-Host "`nDone!`n" -ForegroundColor Green
