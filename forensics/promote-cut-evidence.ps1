# promote-cut-evidence.ps1 - save the CSVs that document a real power cut,
# before the recorder deletes them.
#
# flight-recorder.ps1 prunes its own day files after 30 days. If your backups
# cover the scripts but not the CSVs -- an easy thing to get wrong, since the
# CSVs are noisy and mostly worthless -- then the one file documenting an
# actual crash sits on a 30-day timer.
#
# Backing up every CSV nightly is the wrong fix: ~1.3MB a day forever, and
# which day matters is only knowable AFTER the cut. This copies on detection
# instead: ~2.5MB per cut, kept permanently.
#
# Idempotent. Run it on a schedule (daily is plenty) or straight after the
# recorder starts. Point -Dest at somewhere your BACKUPS actually reach --
# that is the entire purpose, and the most likely thing to get wrong.
#
#   .\promote-cut-evidence.ps1 -Dest 'D:\backed-up\flightrec-crashes'
#
# PowerShell 5.1 compatible: no &&, no ternaries, no null-coalescing.

[CmdletBinding()]
param(
    # Where flight-recorder.ps1 writes its day files.
    [string]$FlightDir = 'C:\flightrec',

    # Keep-forever destination. MUST be inside a backup source.
    [Parameter(Mandatory = $true)]
    [string]$Dest,

    [string]$Classifier = 'C:\flightrec\classify-cuts.ps1',

    # Path to a file holding classify-cuts.ps1 output already computed. Skips
    # invoking the classifier, which scans every rotated setupapi log and is
    # not something to repeat when the caller just did it. A long-running
    # poller should pass this; a scheduled task should not bother.
    [string]$CutsJson,

    # How far back to look for cuts. Ignored when -CutsJson is given.
    [int]$Days = 180,

    # A day file is ~1.3MB. Anything near this is a runaway, not evidence.
    [int]$MaxBytes = 100MB
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }

if ($CutsJson) {
    if (-not (Test-Path $CutsJson)) { throw "CutsJson not found: $CutsJson" }
    $cl = Get-Content -LiteralPath $CutsJson -Raw | ConvertFrom-Json
} else {
    if (-not (Test-Path $Classifier)) { throw "Classifier not found: $Classifier" }
    $cl = & $Classifier -Days $Days | ConvertFrom-Json
}
$promoted = 0
$skipped = 0

foreach ($cut in @($cl.cuts)) {
    $boot = $null
    try { $boot = [datetime]::ParseExact($cut.boot, 'yyyy-MM-dd HH:mm:ss', $null) } catch { continue }

    # Both the boot day AND the day before it: a cut at 00:20 has its entire
    # run-up in yesterday's file, and copying only the boot day keeps the half
    # that shows nothing.
    $wanted = @()
    foreach ($d in @($boot.Date.AddDays(-1), $boot.Date)) {
        $src = Join-Path $FlightDir ('flight_{0:yyyyMMdd}.csv' -f $d)
        if (Test-Path $src) { $wanted += $src }
    }
    if ($wanted.Count -eq 0) {
        # Already pruned, or the cut predates the recorder. Not an error --
        # most of a 180-day window is usually older than the recorder, and
        # nothing can be done for those.
        $skipped++
        continue
    }

    $dir = Join-Path $Dest ($boot.ToString('yyyy-MM-dd_HHmmss'))
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    foreach ($src in $wanted) {
        $item = Get-Item $src
        if ($item.Length -gt $MaxBytes) {
            Write-Warning "refusing to promote $src ($($item.Length) bytes)"
            continue
        }
        $dst = Join-Path $dir $item.Name
        # Idempotent by size. The exception is a cut TODAY, whose day file is
        # still being appended to -- that one re-copies until the day closes,
        # which is intended rather than a bug.
        if ((Test-Path $dst) -and ((Get-Item $dst).Length -eq $item.Length)) { continue }

        # Copy to .part then rename. The recorder appends to the source every
        # 5s, and a half-copied CSV that looks complete is worse than none.
        $tmp = "$dst.part"
        Copy-Item -LiteralPath $src -Destination $tmp -Force
        Move-Item -LiteralPath $tmp -Destination $dst -Force
        $promoted++
    }

    # The CSV alone never says why it mattered. The verdict goes beside it so
    # the evidence still reads on its own years later, with no classifier and
    # no dashboard.
    $cut | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $dir 'cut.json') -Encoding utf8
}

Write-Output "promoted $promoted file(s); $skipped cut(s) had no CSV left to save"
