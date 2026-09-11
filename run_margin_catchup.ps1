# Nightly margin-data catch-up: official TWSE margin balance (MI_MARGN) is
# usually published around 21:00, later than the 19:00 main run, so the 19:00
# run may see stale (previous day's) numbers and skip recording them (see
# margin_store.is_batch_stale / margin_catchup.py for the freshness check).
# This task re-fetches margin balance after it should really be out, and if
# it's confirmed fresh, patches today's picks CSV + dashboard with the
# corrected margin-related Smart Money factors and republishes.
#
# Called by Windows Task Scheduler at 23:30 daily. Logs go to logs/.
#
# Note: this file intentionally avoids embedding Chinese literals directly,
# because Windows PowerShell 5.1 parses a .ps1 file using the system's legacy
# codepage (Big5 here) unless the file carries a UTF-8 BOM, which silently
# corrupts any non-ASCII string literals at parse time (same reasoning as
# run_daily.ps1).

$ErrorActionPreference = "Stop"
Set-Location "C:\futures\stock_picker"

$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# winget-installed tools (git) may not be on PATH for a freshly spawned process
# (Task Scheduler runs are always fresh) until this is refreshed from the registry.
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = $machinePath + ";" + $userPath

$venvPython = "C:\futures\venv\Scripts\python.exe"
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("margin_catchup_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$text) {
    [System.IO.File]::AppendAllText($logFile, $text + "`r`n", $utf8NoBom)
}

Write-Log "===== START $(Get-Date) ====="

try {
    $out1 = & $venvPython margin_catchup.py 2>&1 | Out-String
    Write-Log $out1
    $catchupExit = $LASTEXITCODE

    if ($catchupExit -eq 2) {
        Write-Log "Nothing to catch up (no picks today, or margin data still not fresh) - skipping rebuild/publish."
        Write-Log "===== DONE $(Get-Date) ====="
        exit 0
    }
    if ($catchupExit -ne 0) { throw "margin_catchup.py failed, exit code $catchupExit" }

    $out2 = & $venvPython build_dashboard.py 2>&1 | Out-String
    Write-Log $out2
    if ($LASTEXITCODE -ne 0) { throw "build_dashboard.py failed, exit code $LASTEXITCODE" }

    Write-Log "----- Publishing to GitHub Pages -----"
    Copy-Item "dashboard.html" "docs\index.html" -Force

    # Note: deliberately NOT redirecting git's stderr (no 2>&1) - see run_daily.ps1
    # for why (git writes normal progress to stderr even on success).
    $gitStatus = git status --porcelain -- docs/index.html
    if ([string]::IsNullOrWhiteSpace($gitStatus)) {
        Write-Log "docs/index.html unchanged, nothing to push."
    } else {
        git add docs/index.html
        if ($LASTEXITCODE -ne 0) { throw "git add failed, exit code $LASTEXITCODE" }

        $commitMsg = "Margin data catch-up: $(Get-Date -Format 'yyyy-MM-dd')"
        git commit -m $commitMsg
        if ($LASTEXITCODE -ne 0) { throw "git commit failed, exit code $LASTEXITCODE" }

        git push
        if ($LASTEXITCODE -ne 0) { throw "git push failed, exit code $LASTEXITCODE" }
        Write-Log "Pushed successfully. https://jacks1234541.github.io/tw-stock-screener/ will update within a minute or two."
    }

    Write-Log "===== DONE $(Get-Date) ====="
} catch {
    Write-Log "===== ERROR $(Get-Date) ====="
    Write-Log ($_ | Out-String)
    exit 1
}
