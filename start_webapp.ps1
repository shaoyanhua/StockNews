$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$healthUrl = "http://127.0.0.1:8688/api/health"
$pidFile = Join-Path $projectDir "webapp.pid"
$stdoutLog = Join-Path $projectDir "webapp.current.stdout.log"
$stderrLog = Join-Path $projectDir "webapp.current.stderr.log"
$webappFile = Join-Path $projectDir "webapp.py"

try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    if ($health.ok) {
        Write-Host "webapp is already running (PID $($health.pid)): http://127.0.0.1:8688"
        exit 0
    }
}
catch {
    # Not running; continue with startup.
}

$python = Get-Command python -ErrorAction Stop
$process = Start-Process `
    -FilePath $python.Source `
    -ArgumentList @("-u", $webappFile) `
    -WorkingDirectory $projectDir `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden `
    -PassThru

$process.Id | Set-Content -LiteralPath $pidFile -Encoding ascii

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        throw "webapp failed with exit code $($process.ExitCode). See $stderrLog"
    }
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.ok) {
            Write-Host "webapp started (PID $($health.pid)): http://127.0.0.1:8688"
            Write-Host "Logs: $stdoutLog / $stderrLog"
            exit 0
        }
    }
    catch {
        # Still starting.
    }
}

throw "webapp startup timed out. See $stderrLog"
