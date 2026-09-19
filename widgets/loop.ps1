# loop.ps1 - keep one widget service alive.
#
# Point a scheduled task at this, set to run at logon, with -WindowStyle
# Hidden. It kills any stale instance, then runs the service in a restart
# loop, appending stdout and stderr to a log beside the script.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden `
#     -File C:\widgets\loop.ps1 -Script C:\widgets\docker-desktop\server.py
#
# PowerShell 5.1 compatible: no &&, no ternaries, no null-coalescing.

[CmdletBinding()]
param(
    # Full path to the service's server.py.
    [Parameter(Mandatory = $true)]
    [string]$Script,

    # Python interpreter. Override if yours is not on PATH.
    [string]$Python = "python.exe",

    # Defaults to server.log beside $Script.
    [string]$LogFile,

    # Seconds to wait before restarting after an exit.
    [int]$RestartDelay = 5
)

if (-not (Test-Path $Script)) { throw "Script not found: $Script" }
if (-not $LogFile) {
    $LogFile = Join-Path (Split-Path -Parent $Script) 'server.log'
}

# Match on the script path, not just the interpreter: every widget service
# shares one python.exe, so filtering by process name alone would kill them
# all.
#
# /!\ This only matches the `python.exe <full path>` form. Launching the same
# server by hand from its own directory gives a command line of just
# `python.exe server.py`, which does NOT match -- and Windows then lets BOTH
# processes bind the port. The second bind succeeds silently (SO_REUSEADDR
# permits it), the older process quietly wins new connections, and the
# symptom looks exactly like a missing firewall rule or a stale cache. If a
# widget serves stale data roughly half the time, look for a duplicate
# process before you debug anything else.
$leaf = Split-Path -Leaf $Script
$dir = Split-Path -Leaf (Split-Path -Parent $Script)
$match = "*$dir\$leaf*"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -like $match
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

while ($true) {
    "$(Get-Date -Format o) starting $Script" |
        Out-File -FilePath $LogFile -Append -Encoding utf8
    & $Python $Script *>> $LogFile
    "$(Get-Date -Format o) exited with $LASTEXITCODE" |
        Out-File -FilePath $LogFile -Append -Encoding utf8
    Start-Sleep -Seconds $RestartDelay
}
