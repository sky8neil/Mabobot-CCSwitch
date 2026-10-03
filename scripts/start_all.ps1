# start_all.ps1
# Mabobot one-shot startup orchestrator.
# Order: logs dir -> WSL probe -> WeChat bridge -> app -> plugin readiness
#        -> idle cleanup daemon -> summary.
# Every wait is bounded and polled; every step is appended to logs\startup.log.
# ASCII only. No config.json / database changes.

param(
    [switch]$SkipCleanupDaemon
)

$ErrorActionPreference = "Continue"

# ---- Fixed paths (do not change the existing start protocol) -------------
$Root          = "C:\Users\mabobot\Mabobot"
$LogDir        = Join-Path $Root "logs"
$StartupLog    = Join-Path $LogDir "startup.log"
$CleanupLog    = Join-Path $LogDir "cleanup.log"
$VenvPython    = Join-Path $Root ".venv\Scripts\python.exe"
$VenvPythonw   = Join-Path $Root ".venv\Scripts\pythonw.exe"
$WslDistro     = "Ubuntu-24.04"
$BridgeHealth  = "http://127.0.0.1:5555/health"
$AppStats      = "http://127.0.0.1:8888/api/plugins/stats"

$script:StepResults = New-Object System.Collections.ArrayList

function Write-StartupLog {
    param([string]$Message)
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff")
    $line = "$stamp $Message"
    try {
        Add-Content -LiteralPath $StartupLog -Value $line -Encoding UTF8
    } catch {
        Write-Host $line
    }
}

function Add-StepResult {
    param([string]$Name, [bool]$Ok, [double]$ElapsedMs, [string]$Detail)
    [void]$script:StepResults.Add([pscustomobject]@{
        Name = $Name; Ok = $Ok; ElapsedMs = $ElapsedMs; Detail = $Detail
    })
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [int]$IntervalSec,
        [int]$TimeoutSec,
        [string]$Name
    )
    $started = Get-Date
    $deadline = $started.AddSeconds($TimeoutSec)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 6 -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                $elapsed = ((Get-Date) - $started).TotalMilliseconds
                Write-StartupLog ("step={0} result=ok http=200 elapsed_ms={1:N0}" -f $Name, $elapsed)
                Add-StepResult -Name $Name -Ok $true -ElapsedMs $elapsed -Detail "http=200"
                return $true
            }
            $lastError = "status=$($response.StatusCode)"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds $IntervalSec
    }
    $elapsed = ((Get-Date) - $started).TotalMilliseconds
    Write-StartupLog ("step={0} result=timeout elapsed_ms={1:N0} last_error={2}" -f $Name, $elapsed, $lastError)
    Add-StepResult -Name $Name -Ok $false -ElapsedMs $elapsed -Detail $lastError
    return $false
}

function Test-WslAlive {
    try {
        $proc = Start-Process -FilePath "wsl.exe" `
            -ArgumentList @("-d", $WslDistro, "-e", "true") `
            -PassThru -WindowStyle Hidden
    } catch {
        return $false
    }
    $deadline = (Get-Date).AddSeconds(10)
    while (-not $proc.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (-not $proc.HasExited) {
        try { $proc.Kill() } catch { }
        return $false
    }
    return ($proc.ExitCode -eq 0)
}

function Get-CleanupScriptPath {
    $candidates = @(
        (Join-Path $Root "scripts\cleanup_idle_service.py"),
        (Join-Path $PSScriptRoot "cleanup_idle_service.py"),
        (Join-Path (Split-Path -Parent $PSScriptRoot) "req2\cleanup_idle_service.py"),
        "C:\mabobot-deploy\req2\cleanup_idle_service.py"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Get-CleanupProcesses {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match "python" -and
            ([string]$_.CommandLine).IndexOf("cleanup_idle_service.py",
                [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        })
}

# ---- Step 0: ensure log directory ---------------------------------------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Write-StartupLog "=== start_all begin ==="

# ---- Step 1: WSL probe (warn only, never abort) -------------------------
$wslStarted = Get-Date
$wslOk = $false
$wslDeadline = $wslStarted.AddSeconds(120)
while ((Get-Date) -lt $wslDeadline) {
    if (Test-WslAlive) { $wslOk = $true; break }
    Start-Sleep -Seconds 3
}
$wslElapsed = ((Get-Date) - $wslStarted).TotalMilliseconds
if ($wslOk) {
    Write-StartupLog ("step=wsl_probe result=ok elapsed_ms={0:N0}" -f $wslElapsed)
} else {
    Write-StartupLog ("step=wsl_probe result=warn elapsed_ms={0:N0} note=continuing" -f $wslElapsed)
}
Add-StepResult -Name "wsl_probe" -Ok $wslOk -ElapsedMs $wslElapsed -Detail $(if ($wslOk) { "ok" } else { "warn only" })

# ---- Step 2: WeChat bridge (started exactly like START.bat does) --------
$bridgeRunning = $false
try {
    $probe = Invoke-WebRequest -Uri $BridgeHealth -TimeoutSec 5 -UseBasicParsing
    if ($probe.StatusCode -eq 200) { $bridgeRunning = $true }
} catch { }
if (-not $bridgeRunning) {
    Write-StartupLog "step=bridge_start action=START.bat"
    try {
        Start-Process -FilePath "cmd.exe" `
            -ArgumentList @("/c", "cd /d `"$Root`" && START.bat") `
            -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
    } catch {
        Write-StartupLog ("step=bridge_start result=error error={0}" -f $_.Exception.Message)
    }
} else {
    Write-StartupLog "step=bridge_start action=already_running"
}
$bridgeOk = Wait-HttpOk -Url $BridgeHealth -IntervalSec 3 -TimeoutSec 120 -Name "bridge_health"

# ---- Step 3: app (polled, cold start needs ~210s) -----------------------
$appOk = Wait-HttpOk -Url $AppStats -IntervalSec 5 -TimeoutSec 300 -Name "app_stats"

# ---- Step 4: plugin readiness (loaded >= 7) -----------------------------
$loadedCount = -1
try {
    $stats = Invoke-RestMethod -Uri $AppStats -TimeoutSec 20
    if ($stats.stats -and ($null -ne $stats.stats.loaded_plugins)) {
        $loadedCount = [int]$stats.stats.loaded_plugins
    } elseif ($null -ne $stats.loaded) {
        $loadedCount = [int]$stats.loaded
    }
} catch {
    Write-StartupLog ("step=plugin_ready result=error error={0}" -f $_.Exception.Message)
}
if ($loadedCount -ge 7) {
    Write-StartupLog ("step=plugin_ready result=ok loaded={0}" -f $loadedCount)
    Add-StepResult -Name "plugin_ready" -Ok $true -ElapsedMs 0 -Detail "loaded=$loadedCount"
} else {
    Write-StartupLog ("step=plugin_ready result=warn loaded={0} threshold=7" -f $loadedCount)
    Add-StepResult -Name "plugin_ready" -Ok $false -ElapsedMs 0 -Detail "loaded=$loadedCount (threshold 7)"
}

# ---- Step 5: idle cleanup daemon ----------------------------------------
$cleanupOk = $false
if ($SkipCleanupDaemon) {
    Write-StartupLog "step=cleanup_daemon result=skipped reason=flag"
    Add-StepResult -Name "cleanup_daemon" -Ok $true -ElapsedMs 0 -Detail "skipped by flag"
} else {
    $existing = @(Get-CleanupProcesses)
    if ($existing.Count -gt 0) {
        Write-StartupLog ("step=cleanup_daemon result=ok action=already_running pids={0}" -f (($existing | ForEach-Object { $_.ProcessId }) -join ","))
        Add-StepResult -Name "cleanup_daemon" -Ok $true -ElapsedMs 0 -Detail "already running"
        $cleanupOk = $true
    } else {
        $cleanupScript = Get-CleanupScriptPath
        if ($null -eq $cleanupScript) {
            Write-StartupLog "step=cleanup_daemon result=warn reason=script_not_found"
            Add-StepResult -Name "cleanup_daemon" -Ok $false -ElapsedMs 0 -Detail "script not found"
        } else {
            $pythonExe = if (Test-Path -LiteralPath $VenvPythonw) { $VenvPythonw } else { $VenvPython }
            try {
                Start-Process -FilePath $pythonExe `
                    -ArgumentList @($cleanupScript, "--log-file", $CleanupLog) `
                    -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
                Start-Sleep -Seconds 3
                $after = @(Get-CleanupProcesses)
                if ($after.Count -gt 0) {
                    Write-StartupLog ("step=cleanup_daemon result=ok action=started pids={0}" -f (($after | ForEach-Object { $_.ProcessId }) -join ","))
                    Add-StepResult -Name "cleanup_daemon" -Ok $true -ElapsedMs 3000 -Detail "started"
                    $cleanupOk = $true
                } else {
                    Write-StartupLog "step=cleanup_daemon result=warn reason=process_not_seen"
                    Add-StepResult -Name "cleanup_daemon" -Ok $false -ElapsedMs 3000 -Detail "process not seen"
                }
            } catch {
                Write-StartupLog ("step=cleanup_daemon result=error error={0}" -f $_.Exception.Message)
                Add-StepResult -Name "cleanup_daemon" -Ok $false -ElapsedMs 0 -Detail $_.Exception.Message
            }
        }
    }
}

# ---- Step 6: summary and exit code --------------------------------------
Write-StartupLog "=== summary ==="
foreach ($step in $script:StepResults) {
    Write-StartupLog ("summary step={0} ok={1} elapsed_ms={2:N0} detail={3}" -f $step.Name, $step.Ok, $step.ElapsedMs, $step.Detail)
}
$fatal = (-not $bridgeOk) -or (-not $appOk)
if ($fatal) {
    Write-StartupLog "=== start_all end rc=1 ==="
    exit 1
}
Write-StartupLog "=== start_all end rc=0 ==="
exit 0
