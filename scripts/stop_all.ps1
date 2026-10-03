# stop_all.ps1
# Stop Mabobot app / WeChat bridge / idle cleanup daemon.
# Uses the same python process identification as C:\mabobot-deploy\vm_restart.ps1.
# Does NOT restart, does NOT touch Chrome, does NOT stop WSL.
# ASCII only. No config.json / database changes.

$ErrorActionPreference = "Continue"

$Root     = "C:\Users\mabobot\Mabobot"
$LogDir   = Join-Path $Root "logs"
$StopLog  = Join-Path $LogDir "stop.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-StopLog {
    param([string]$Message)
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff")
    $line = "$stamp $Message"
    try {
        Add-Content -LiteralPath $StopLog -Value $line -Encoding UTF8
    } catch {
        Write-Host $line
    }
}

# mabobot_launcher (supervisor) + wx_bot.py + start.py + cleanup daemon.
$pattern = "mabobot_launcher|wx_bot\.py|start\.py|cleanup_idle_service\.py"

function Get-MabobotProcesses {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match "python" -and
            ([string]$_.CommandLine) -match $pattern
        })
}

$scriptName = "stop_all.ps1"
Write-StopLog "=== $scriptName begin ==="

$targets = @(Get-MabobotProcesses)
Write-StopLog ("targets pids={0}" -f (($targets | ForEach-Object { $_.ProcessId }) -join ","))

foreach ($proc in $targets) {
    $procId = [int]$proc.ProcessId
    try {
        $output = & taskkill.exe /PID $procId /T /F 2>&1
        Write-StopLog ("taskkill pid=$procId output={0}" -f (($output | Out-String).Trim()))
    } catch {
        Write-StopLog ("taskkill pid=$procId error={0}" -f $_.Exception.Message)
    }
}

Start-Sleep -Seconds 3
$remaining = @(Get-MabobotProcesses)
Write-StopLog ("remaining pids={0}" -f (($remaining | ForEach-Object { $_.ProcessId }) -join ","))
Write-StopLog "=== $scriptName end ==="

if ($remaining.Count -gt 0) {
    exit 1
}
exit 0
