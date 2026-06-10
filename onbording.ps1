<#
  TokenCost — Windows setup / start / stop script
  PowerShell equivalent of onbording.sh (macOS).

  Run:   powershell -ExecutionPolicy Bypass -File onbording.ps1
  or:    double-click tokencost.bat

  What it does (Start):
    1. Creates a Python venv and installs fastapi / uvicorn / httpx
    2. Imports your local Claude / VS Code history into tracker.db
    3. Sets ANTHROPIC_BASE_URL=http://localhost:8082 as a User env var
       (picked up by Claude Code, VS Code, Claude Desktop, new terminals)
    4. Registers a scheduled task so the proxy autostarts at logon, plus a
       5-minute log-sync task (best effort — skipped if not permitted)
    5. Starts the proxy in the background and opens the dashboard
#>

$ErrorActionPreference = 'Stop'
$PORT       = 8082
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

$BaseUrl    = "http://localhost:$PORT"
$SmartFile  = Join-Path $ScriptDir ".smart_routing"
$VenvPy     = Join-Path $ScriptDir "venv\Scripts\python.exe"
$ProxyLog   = Join-Path $ScriptDir "proxy.log"
$ProxyErr   = Join-Path $ScriptDir "proxy-error.log"
$TaskProxy  = "TokenCostProxy"
$TaskSync   = "TokenCostSync"

# ── Helpers ───────────────────────────────────────────────────────────────────
function Write-Step($n, $msg) { Write-Host "  [$n] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)       { Write-Host "  [ok] $msg"  -ForegroundColor Green }
function Write-Warn2($msg)    { Write-Host "  [!]  $msg"  -ForegroundColor Yellow }

function Get-ProxyPids {
    try {
        return (Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { return @() }
}
function Test-ProxyRunning { return (@(Get-ProxyPids).Count -gt 0) }

function Stop-Proxy {
    foreach ($procId in (Get-ProxyPids)) {
        try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Milliseconds 800
}

function Test-SmartRoutingOn {
    return ((Test-Path $SmartFile) -and ((Get-Content $SmartFile -Raw).Trim() -eq "1"))
}

function Find-Python {
    foreach ($cand in @("py -3", "python", "python3")) {
        $exe = $cand.Split(" ")[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $v = & $exe ($cand.Split(" ")[1..10]) --version 2>&1
                if ($LASTEXITCODE -eq 0) { return $cand }
            } catch {}
        }
    }
    return $null
}

# ── Scheduled tasks (best effort, no admin required for current user) ──────────
function Register-Tasks {
    try {
        $action = New-ScheduledTaskAction -Execute $VenvPy `
                    -Argument "-B `"$ScriptDir\proxy.py`"" -WorkingDirectory $ScriptDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName $TaskProxy -Action $action -Trigger $trigger `
            -Settings $settings -Force -ErrorAction Stop | Out-Null

        $syncAction = New-ScheduledTaskAction -Execute $VenvPy `
                    -Argument "`"$ScriptDir\import_history.py`" --silent" -WorkingDirectory $ScriptDir
        $syncTrigger = New-ScheduledTaskTrigger -AtLogOn
        $syncTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
                    -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
        Register-ScheduledTask -TaskName $TaskSync -Action $syncAction -Trigger $syncTrigger `
            -Settings $settings -Force -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Unregister-Tasks {
    foreach ($t in @($TaskProxy, $TaskSync)) {
        try { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop } catch {}
    }
}

# ── Start the proxy in the background ─────────────────────────────────────────
function Start-ProxyBackground {
    if (Test-SmartRoutingOn) { $env:SMART_ROUTING = "1" } else { $env:SMART_ROUTING = "0" }
    Start-Process -FilePath $VenvPy `
        -ArgumentList "-B", "`"$ScriptDir\proxy.py`"" `
        -WorkingDirectory $ScriptDir `
        -RedirectStandardOutput $ProxyLog `
        -RedirectStandardError  $ProxyErr `
        -WindowStyle Hidden | Out-Null
}

# ── Action: Start ─────────────────────────────────────────────────────────────
function Action-Start {
    Clear-Host
    Write-Host ""
    Write-Host "  TokenCost — Windows Setup" -ForegroundColor White
    Write-Host ""

    # Smart routing prompt
    if (Test-SmartRoutingOn) { $cur = "enabled" } else { $cur = "disabled" }
    Write-Host "  Smart Model Routing (currently: $cur)"
    Write-Host "  Switches Opus/Sonnet -> Haiku for simple requests. Saves ~60% on short tasks."
    $choice = Read-Host "  Enable optimizer? [y/N]"
    if ($choice -match '^(y|yes)$') { "1" | Set-Content $SmartFile -NoNewline; Write-Ok "Optimizer enabled" }
    else { "0" | Set-Content $SmartFile -NoNewline; Write-Host "  Optimizer disabled" }

    # 1. Python
    Write-Host ""
    Write-Step "1/7" "Checking Python..."
    $py = Find-Python
    if (-not $py) {
        Write-Warn2 "Python not found. Install Python 3.9+ from https://www.python.org/downloads/ (check 'Add to PATH')."
        return
    }
    $pyExe  = $py.Split(" ")[0]
    $pyArgs = @($py.Split(" ")[1..10] | Where-Object { $_ })
    Write-Ok ((& $pyExe @pyArgs --version 2>&1) | Out-String).Trim()

    # 2. venv + dependencies
    Write-Host ""
    Write-Step "2/7" "Setting up virtual environment + dependencies..."
    if (-not (Test-Path $VenvPy)) {
        & $pyExe @pyArgs -m venv (Join-Path $ScriptDir "venv")
    }
    $check = & $VenvPy -c "import fastapi, uvicorn, httpx" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Installing packages (~30s)..."
        $req = Join-Path $ScriptDir "requirements.txt"
        if (Test-Path $req) { & $VenvPy -m pip install -r $req -q }
        else { & $VenvPy -m pip install fastapi uvicorn httpx -q }
        if ($LASTEXITCODE -ne 0) { Write-Warn2 "pip install failed"; return }
    }
    Write-Ok "Dependencies ready"

    # 3. Import history
    Write-Host ""
    Write-Step "3/7" "Importing history from local logs..."
    & $VenvPy (Join-Path $ScriptDir "import_history.py") --silent 2>&1 | Out-Null
    Write-Ok "History imported into tracker.db"

    # 4. Env var (Claude Code / VS Code / Claude Desktop / new terminals)
    Write-Host ""
    Write-Step "4/7" "Setting ANTHROPIC_BASE_URL (User environment)..."
    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $BaseUrl, "User")
    $env:ANTHROPIC_BASE_URL = $BaseUrl
    Write-Ok "ANTHROPIC_BASE_URL = $BaseUrl"

    # 5. Scheduled tasks (autostart + 5-min sync)
    Write-Host ""
    Write-Step "5/7" "Registering autostart + sync tasks..."
    if (Register-Tasks) { Write-Ok "Scheduled tasks registered (proxy autostart, sync every 5 min)" }
    else { Write-Warn2 "Could not register scheduled tasks (non-critical — proxy still starts now)" }

    # 6. Start proxy
    Write-Host ""
    Write-Step "6/7" "Starting proxy..."
    Stop-Proxy
    Start-ProxyBackground
    $ready = $false
    foreach ($i in 1..15) {
        Start-Sleep -Seconds 1
        if (Test-ProxyRunning) { $ready = $true; break }
    }
    if ($ready) { Write-Ok "Proxy running on $BaseUrl" }
    else { Write-Warn2 "Proxy did not report ready — check proxy-error.log" }

    # 7. Open dashboard
    Write-Host ""
    Write-Step "7/7" "Opening dashboard..."
    Start-Process "$BaseUrl/dashboard" | Out-Null

    Write-Host ""
    Write-Host "  ===================================================" -ForegroundColor Green
    Write-Host "   Setup complete!  ->  $BaseUrl/dashboard"            -ForegroundColor Green
    Write-Host "  ===================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Restart VS Code / Cursor / Claude Desktop once so they pick up"
    Write-Host "  the new ANTHROPIC_BASE_URL. New terminals get it automatically."
    Write-Host ""
}

# ── Action: Disable ───────────────────────────────────────────────────────────
function Action-Disable {
    Clear-Host
    Write-Host ""
    Write-Host "  TokenCost — Disable" -ForegroundColor White
    Write-Host ""

    if (Test-ProxyRunning) { Stop-Proxy; Write-Ok "Proxy stopped" }
    else { Write-Host "  Proxy was not running" }

    Unregister-Tasks
    Write-Ok "Removed scheduled tasks"

    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $null, "User")
    Remove-Item Env:\ANTHROPIC_BASE_URL -ErrorAction SilentlyContinue
    Write-Ok "Removed ANTHROPIC_BASE_URL env var"

    Write-Host ""
    Write-Host "  Done. TokenCost fully disabled." -ForegroundColor Green
    Write-Host "  Claude Code and VS Code now connect directly to Anthropic."
    Write-Host "  (Restart open apps to drop the proxy setting.)"
    Write-Host ""
}

# ── Menu ──────────────────────────────────────────────────────────────────────
Clear-Host
Write-Host ""
Write-Host "  ==============================" -ForegroundColor White
Write-Host "       TokenCost  (Windows)"       -ForegroundColor White
Write-Host "  ==============================" -ForegroundColor White
Write-Host ""
if (Test-ProxyRunning) { Write-Host "  Proxy:     running on port $PORT" -ForegroundColor Green }
else                   { Write-Host "  Proxy:     stopped"               -ForegroundColor Yellow }
if ([Environment]::GetEnvironmentVariable("ANTHROPIC_BASE_URL", "User")) {
    Write-Host "  Routing:   configured (ANTHROPIC_BASE_URL set)" -ForegroundColor Green
} else {
    Write-Host "  Routing:   not configured" -ForegroundColor Yellow
}
if (Test-SmartRoutingOn) { Write-Host "  Optimizer: enabled"  -ForegroundColor Green }
else                     { Write-Host "  Optimizer: disabled" -ForegroundColor Yellow }
Write-Host ""
Write-Host "  1  Start proxy + open dashboard"
Write-Host "  2  Disable proxy completely"
Write-Host "  3  Exit"
Write-Host ""
$sel = Read-Host "  Choose [1/2/3]"

switch ($sel) {
    "1" { Action-Start }
    "2" { Action-Disable }
    "3" { exit 0 }
    default { Write-Warn2 "Invalid choice" }
}
