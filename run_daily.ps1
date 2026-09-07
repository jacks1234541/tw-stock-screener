# Daily automation: fetch data -> run screener -> build dashboard -> publish to GitHub Pages.
# Called by Windows Task Scheduler at a fixed time each day. Logs go to logs/.
# Publishing: copies dashboard.html to docs/index.html and pushes to the "master"
# branch on GitHub; GitHub Pages is configured to serve that folder, so a
# successful push is what makes https://jacks1234541.github.io/tw-stock-screener/
# update. Requires git + gh to already be authenticated on this machine.
#
# Note: this file intentionally avoids embedding Chinese literals directly,
# because Windows PowerShell 5.1 parses a .ps1 file using the system's legacy
# codepage (Big5 here) unless the file carries a UTF-8 BOM, which silently
# corrupts any non-ASCII string literals at parse time. The actual Python
# scripts' Chinese output is unaffected (Python 3 always reads .py source as
# UTF-8), so only these wrapper log lines are kept in English.

$ErrorActionPreference = "Stop"
Set-Location "C:\futures\stock_picker"

# Force the Python subprocess and PowerShell's own capture to agree on UTF-8,
# otherwise Chinese text coming back from screener.py/build_dashboard.py gets
# decoded with the system's legacy codepage and turns into mojibake.
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
$logFile = Join-Path $logDir ("run_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string]$text) {
    [System.IO.File]::AppendAllText($logFile, $text + "`r`n", $utf8NoBom)
}

Write-Log "===== START $(Get-Date) ====="

try {
    $out1 = & $venvPython screener.py 2>&1 | Out-String
    Write-Log $out1
    if ($LASTEXITCODE -ne 0) { throw "screener.py failed, exit code $LASTEXITCODE" }

    $out2 = & $venvPython build_dashboard.py 2>&1 | Out-String
    Write-Log $out2
    if ($LASTEXITCODE -ne 0) { throw "build_dashboard.py failed, exit code $LASTEXITCODE" }

    Write-Log "----- Publishing to GitHub Pages -----"
    Copy-Item "dashboard.html" "docs\index.html" -Force

    $gitStatus = git status --porcelain -- docs/index.html
    if ([string]::IsNullOrWhiteSpace($gitStatus)) {
        Write-Log "docs/index.html unchanged, nothing to push."
    } else {
        git add docs/index.html 2>&1 | Out-String | Write-Log
        $commitMsg = "Daily update: $(Get-Date -Format 'yyyy-MM-dd')"
        git commit -m $commitMsg 2>&1 | Out-String | Write-Log
        git push 2>&1 | Out-String | Write-Log
        if ($LASTEXITCODE -ne 0) { throw "git push failed, exit code $LASTEXITCODE" }
        Write-Log "Pushed successfully. https://jacks1234541.github.io/tw-stock-screener/ will update within a minute or two."
    }

    Write-Log "===== DONE $(Get-Date) ====="
} catch {
    Write-Log "===== ERROR $(Get-Date) ====="
    Write-Log ($_ | Out-String)
    exit 1
}
