# install_shortcuts.ps1
# Create two desktop shortcuts that run start_all.ps1 / stop_all.ps1 through
# powershell.exe with a hidden window (no console popup).
# ASCII only. Chinese shortcut names are built from code points on purpose.

$ErrorActionPreference = "Stop"

$Root        = "C:\Users\mabobot\Mabobot"
$PowerShell  = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$StartScript = Join-Path $PSScriptRoot "start_all.ps1"
$StopScript  = Join-Path $PSScriptRoot "stop_all.ps1"
$IconPath    = Join-Path $Root "mabobot_launcher\assets\mabobot.ico"

# Shortcut names: "Mabobot" + U+542F U+52A8 (start) and U+505C U+6B62 (stop).
# Built from code points so this file stays pure ASCII.
$StartName = "Mabobot " + [char]0x542F + [char]0x52A8
$StopName  = "Mabobot " + [char]0x505C + [char]0x6B62

$Desktop = [Environment]::GetFolderPath("Desktop")

function New-MabobotShortcut {
    param(
        [string]$Name,
        [string]$ScriptPath
    )
    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        throw "script not found: $ScriptPath"
    }
    $linkPath = Join-Path $Desktop ("$Name.lnk")
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($linkPath)
    $shortcut.TargetPath = $PowerShell
    $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""
    $shortcut.WorkingDirectory = $Root
    $shortcut.Description = "Mabobot"
    if (Test-Path -LiteralPath $IconPath) {
        $shortcut.IconLocation = $IconPath
    }
    $shortcut.Save()
    Write-Host ("created: " + $linkPath)
}

New-MabobotShortcut -Name $StartName -ScriptPath $StartScript
New-MabobotShortcut -Name $StopName -ScriptPath $StopScript
Write-Host "done"
