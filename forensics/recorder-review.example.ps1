# recorder-review.example.ps1 - "did the fix actually work?", as a notification.
#
# Run once, N days after a suspected repair, from a one-shot scheduled task.
# It counts UNEXPLAINED power cuts since the fix, checks the recorder is still
# writing, reports worst thermals, and pushes a verdict.
#
# This is an EXAMPLE. The notification block at the bottom uses ntfy; replace
# it with whatever you use. Credentials come from the environment -- never
# hardcode a token in a file you might publish.
#
#   $env:NTFY_URL   = 'https://ntfy.example.com/mytopic'
#   $env:NTFY_TOKEN = '...'
#
# PowerShell 5.1 compatible: no &&, no ternaries, no null-coalescing.

[CmdletBinding()]
param(
    # When the suspected fix happened. Cuts before this are not counted.
    [Parameter(Mandatory = $true)]
    [datetime]$Since,

    [string]$FlightDir = 'C:\flightrec',
    [string]$Classifier = 'C:\flightrec\classify-cuts.ps1'
)

$ErrorActionPreference = 'SilentlyContinue'
$Days = [math]::Round(((Get-Date) - $Since).TotalDays, 0)

# 1. any UNEXPLAINED power-off since the fix?
#
# /!\ NOT a raw Event 41 count, and this is the whole point of the script.
# On the machine this came from, two of the Event 41s since the "fix" were
# the same graphics driver install dying mid-write, each leaving a torn
# section in setupapi.dev.log. Counting those would have reported "KEEP IT
# RUNNING" for a fault that had already been repaired -- on the one
# notification set up to be trusted. classify-cuts.ps1 is the judge.
$degraded      = $null
$crashes       = @()
$explainedCuts = @()
if (Test-Path $Classifier) {
    try {
        $cl            = & $Classifier -Since $Since | ConvertFrom-Json
        $crashes       = @($cl.cuts | Where-Object { -not $_.explained })
        $explainedCuts = @($cl.cuts | Where-Object { $_.explained })
    } catch {
        $degraded = "classifier FAILED ($($_.Exception.Message))"
    }
} else {
    $degraded = 'classify-cuts.ps1 MISSING'
}
# Falling back silently would hand back the exact over-count this replaced,
# so the fallback is loud and the message says the verdict is unreliable.
if ($degraded) {
    $crashes = @(Get-WinEvent -FilterHashtable @{LogName='System';Id=41;StartTime=$Since} -ErrorAction SilentlyContinue |
                 ForEach-Object { [pscustomobject]@{ boot = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss') } })
}
$n = $crashes.Count

# 2. is the recorder still alive and writing?
$csv = Get-ChildItem (Join-Path $FlightDir 'flight_*.csv') |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
$fresh = if ($csv) { ((Get-Date) - $csv.LastWriteTime).TotalMinutes -lt 10 } else { $false }

# 3. worst thermals across everything the recorder captured.
# /!\ Get-Content, not [System.IO.File]::ReadLines: the recorder's
# StreamWriter does not share read access, so ReadLines cannot open the
# active day file.
$worstTemp = $null; $minHead = $null
foreach ($f in Get-ChildItem (Join-Path $FlightDir 'flight_*.csv')) {
    foreach ($ln in (Get-Content $f.FullName -ErrorAction SilentlyContinue)) {
        if ($ln -like '#*' -or $ln -like 'time,*') { continue }
        $c = $ln -split ','
        if ($c.Count -ge 17 -and $c[3]) {
            $t = [double]$c[3]
            if ($worstTemp -eq $null -or $t -gt $worstTemp) { $worstTemp = $t }
            if ($c[5]) {
                $h = [double]$c[5]
                if ($minHead -eq $null -or $h -lt $minHead) { $minHead = $h }
            }
        }
    }
}

if ($n -eq 0) {
    $title = "Flight recorder: $Days days CLEAN - safe to retire"
    $prio  = 'default'
    $tag   = 'white_check_mark'
    $verdict = "No UNEXPLAINED power-offs since the fix."
    $action  = "To retire it, run ELEVATED:`nUnregister-ScheduledTask -TaskName FlightRecorder -Confirm:`$false`nGet-CimInstance Win32_Process -Filter `"Name='powershell.exe'`" | Where-Object { `$_.CommandLine -like '*flight-recorder*' } | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }`n`nLogs stay in $FlightDir if you want them."
} else {
    $when = ($crashes | Sort-Object boot -Descending | Select-Object -First 3 |
             ForEach-Object { $_.boot.Substring(5, 11) }) -join ', '
    $title = "Flight recorder: $n UNEXPLAINED CUT(S) since the fix - KEEP IT RUNNING"
    $prio  = 'high'
    $tag   = 'rotating_light'
    $verdict = "The fix was NOT the whole story. Unexplained cut(s) at: $when"
    $action  = "Read the last lines before each crash in $FlightDir\flight_*.csv`n  +12V or Vcore sagging -> PSU or EPS connector`n  temp spike          -> cooling`n  all nominal         -> board VRM or CPU, faster than the 5s sample rate"
}

$health = if ($fresh) { "recorder alive, last write $($csv.LastWriteTime.ToString('MM-dd HH:mm'))" }
          else { "WARNING: recorder not writing (last $(if($csv){$csv.LastWriteTime.ToString('MM-dd HH:mm')}else{'never'})) - verdict may be based on partial data" }

$excluded = ''
if ($explainedCuts.Count -gt 0) {
    $ex = ($explainedCuts | ForEach-Object { "$($_.boot.Substring(5,11)) ($($_.cause))" }) -join ', '
    $excluded = "`nExcluded as explained: $ex"
}
if ($degraded) {
    $excluded = "`nWARNING: $degraded - this is a RAW Event 41 count and OVER-REPORTS. Driver-install resets are counted as crashes. Verdict unreliable."
}

$thermal = if ($worstTemp) { "Peak CPU $worstTemp C, closest to TjMax $minHead C" } else { "no sensor data captured" }

$msg = "$verdict$excluded`n`n$thermal`n$health`n`n$action"

# ---- notification: replace this block with whatever you use ---------------
$ntfyUrl = $env:NTFY_URL
$ntfyTok = $env:NTFY_TOKEN
if ($ntfyUrl) {
    $headers = @{ Title = $title; Priority = $prio; Tags = $tag }
    if ($ntfyTok) { $headers['Authorization'] = "Bearer $ntfyTok" }
    try {
        Invoke-RestMethod -Uri $ntfyUrl -Method Post `
            -Body ([Text.Encoding]::UTF8.GetBytes($msg)) `
            -Headers $headers -TimeoutSec 20 | Out-Null
    } catch {
        Add-Content (Join-Path $FlightDir 'review-errors.log') `
            "$(Get-Date -Format s) push failed: $($_.Exception.Message)"
    }
} else {
    Write-Output $title
    Write-Output $msg
}

Add-Content (Join-Path $FlightDir 'review-result.log') `
    "$(Get-Date -Format s) unexplained=$n explained=$($explainedCuts.Count) degraded=$([bool]$degraded) fresh=$fresh peak=$worstTemp minHead=$minHead"
