# classify-cuts.ps1 - the single source of truth for "was this power cut explained?"
#
# WHY THIS EXISTS: counting Kernel-Power 41 and calling the total "crashes" is
# wrong. On the machine this came from, two cuts two days apart were the SAME
# Windows Update driver install (pci\ven_8086&dev_0162, an HD 4000) dying
# mid-write. Both left an unclosed section in setupapi.dev.log. Neither
# belonged in the unexplained-power-off count -- and on one of them every
# sensor was nominal, so no amount of flight-recorder data would have told you.
#
# Make this the ONLY implementation of that judgement. Anything else that
# counts these cuts -- a dashboard panel, a scheduled "did the fix work?"
# notification -- should call this rather than reimplement it, or the two will
# disagree exactly when it matters.
#
# Emits ONE json object on stdout. Everything else goes to stderr or nowhere.
#
#   .\classify-cuts.ps1 -Days 180 | ConvertFrom-Json
#
# PowerShell 5.1 compatible: no &&, no ternaries, no null-coalescing.

[CmdletBinding()]
param(
    # How far back to look. -Since wins if both are given.
    [int]$Days = 180,
    [datetime]$Since,

    # Two events for one reboot land seconds apart; anything further out is a
    # different cut. 41 and 6008 are CLUSTERED, not paired - see below.
    [int]$ClusterSeconds = 120,

    # How close a setupapi [Boot Session:] marker must be to the event-log boot
    # time to be the same restart. They are written by different subsystems
    # seconds apart, so this is generous on purpose.
    [int]$MatchMinutes = 15
)

$ErrorActionPreference = 'Stop'
if (-not $PSBoundParameters.ContainsKey('Since')) { $Since = (Get-Date).AddDays(-$Days) }

# ---------------------------------------------------------------------------
# 1. every unexpected-shutdown event in the window
# ---------------------------------------------------------------------------
$events = @(Get-WinEvent -FilterHashtable @{
        LogName = 'System'; Id = 41, 6008; StartTime = $Since
    } -ErrorAction SilentlyContinue)

$marks = @()
foreach ($e in $events) {
    $bc = $null
    if ($e.Id -eq 41) {
        try {
            $x = [xml]$e.ToXml()
            $d = $x.Event.EventData.Data | Where-Object { $_.Name -eq 'BugcheckCode' } | Select-Object -First 1
            if ($d) { $bc = [int]$d.'#text' }
        } catch { }
    }
    # The LRM/RLM marks Windows embeds around the date in 6008's message break
    # every naive parse of it. Strip them before anything reads this string.
    $msg = ($e.Message -replace "[\u200e\u200f]", "") -replace '\s+', ' '
    $marks += [pscustomobject]@{ Id = $e.Id; T = $e.TimeCreated; Bugcheck = $bc; Msg = $msg }
}

# One reboot writes 41 and 6008 seconds apart, so cluster rather than pair off
# 41. A 6008 with NO 41 is still an unexpected shutdown -- one such cut existed
# on the machine this came from, and keying the list to 41 hid it entirely.
$incidents = @()
foreach ($m in ($marks | Sort-Object T)) {
    $g = $null
    if ($incidents.Count -gt 0) {
        $last = $incidents[-1]
        if (($m.T - $last.Boot).TotalSeconds -le $ClusterSeconds) { $g = $last }
    }
    if (-not $g) {
        $g = [pscustomobject]@{
            Boot = $m.T; Bugcheck = $null; Claimed = $null; Has41 = $false
        }
        $incidents += $g
    }
    if ($m.Id -eq 41) {
        $g.Has41 = $true
        $g.Bugcheck = $m.Bugcheck
    } elseif (-not $g.Claimed) {
        # "The previous system shutdown at 4:43:03 PM on 9/16/2026 was unexpected."
        # Locale-formatted inside an English sentence, so it is a regex and not
        # a field. Only ever reported as a skew against a better clock, never
        # used as the cut time, so a miss costs one value and nothing else.
        if ($m.Msg -match 'at (\d{1,2}):(\d{2}):(\d{2}) (AM|PM) on (\d{1,2})/(\d{1,2})/(\d{4})') {
            $hh = [int]$Matches[1] % 12
            if ($Matches[4] -eq 'PM') { $hh += 12 }
            try {
                $g.Claimed = Get-Date -Year ([int]$Matches[7]) -Month ([int]$Matches[5]) `
                    -Day ([int]$Matches[6]) -Hour $hh -Minute ([int]$Matches[2]) `
                    -Second ([int]$Matches[3]) -Millisecond 0
            } catch { }
        }
    }
}

# ---------------------------------------------------------------------------
# 2. torn driver installs in setupapi.dev.log
# ---------------------------------------------------------------------------
# A driver install writes ">>>  [Device Install ...]" then ">>>  Section start
# <ts>", and closes with "<<<  Section end". If the machine dies mid-install
# the section never closes and the next thing in the file is the next boot's
# "[Boot Session: <ts>]" marker. That unclosed section IS the fingerprint, and
# its Section start time is an independent second opinion on when the cut
# happened -- on one cut it read 16:48:42 while event 6008 claimed 16:43:03.
function Get-TornSections {
    param([datetime]$From)

    # The log rotates at ~4.3MB into setupapi.dev.<stamp>.log, so the window may
    # span several files. Take every log touched since $From, plus the newest
    # one older than that (its CONTENT runs up to its rotation moment).
    $all = @(Get-ChildItem 'C:\Windows\INF\setupapi.dev*.log' -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime)
    $keep = @($all | Where-Object { $_.LastWriteTime -ge $From })
    $older = @($all | Where-Object { $_.LastWriteTime -lt $From } | Select-Object -Last 1)
    $scan = @($older + $keep | Where-Object { $_ } | Sort-Object FullName -Unique)

    $torn = @()
    foreach ($f in $scan) {
        # FileShare ReadWrite: the log may be held open by a concurrent install,
        # and a plain read would throw. The tear itself leaves invalid bytes,
        # so decode replaces rather than fails -- grep calls this file binary
        # for exactly that reason.
        $fs = $null; $sr = $null
        try {
            $fs = [IO.File]::Open($f.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8, $true)
            $curName = $null; $curStart = $null; $prev = $null
            while ($null -ne ($line = $sr.ReadLine())) {
                $t = $line.Trim()
                if ($t.StartsWith('>>>  Section start')) {
                    $curStart = $t.Substring('>>>  Section start'.Length).Trim()
                    $curName = $prev
                } elseif ($t.StartsWith('<<<  Section end')) {
                    $curName = $null; $curStart = $null
                } elseif ($t.StartsWith('[Boot Session:') -and $curName) {
                    $bs = $t.Trim('[', ']').Replace('Boot Session: ', '').Trim()
                    $bdt = $null
                    try { $bdt = [datetime]::ParseExact($bs, 'yyyy/MM/dd HH:mm:ss.fff', $null) } catch { }
                    $sdt = $null
                    try { $sdt = [datetime]::ParseExact($curStart, 'yyyy/MM/dd HH:mm:ss.fff', $null) } catch { }
                    if ($bdt) {
                        $torn += [pscustomobject]@{
                            Name  = ($curName -replace '^>>>\s*\[', '' -replace '\]$', '')
                            Start = $sdt
                            Boot  = $bdt
                            File  = $f.Name
                        }
                    }
                    $curName = $null; $curStart = $null
                }
                if ($t.StartsWith('>>>  [')) { $prev = $t }
            }
        } catch {
            Write-Verbose "setupapi read failed for $($f.FullName): $($_.Exception.Message)"
        } finally {
            if ($sr) { $sr.Dispose() }
            if ($fs) { $fs.Dispose() }
        }
    }
    return $torn
}

$torn = @(Get-TornSections -From $Since)

# ---------------------------------------------------------------------------
# 3. weaker context: update / PnP activity near the cut
# ---------------------------------------------------------------------------
# These are NOT proof and never set Explained. A download finishing 40s before
# a cut is a coincidence until it repeats; treating it as a cause is the same
# error as treating nominal sensors as an alibi.
function Get-Context {
    param([datetime]$Boot)
    $from = $Boot.AddMinutes(-30)
    $bits = @()

    $wu = @(Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-WindowsUpdateClient/Operational'
            Id = 19, 20, 43; StartTime = $from; EndTime = $Boot
        } -ErrorAction SilentlyContinue)
    if ($wu.Count) { $bits += "$($wu.Count) Windows Update install event(s) in the prior 30 min" }

    # SWD\MMDEVAPI churn is audio endpoints appearing and vanishing all day.
    # It is background noise and must not read as driver activity.
    $pnp = @(Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-Kernel-PnP/Configuration'
            StartTime = $Boot.AddMinutes(-5); EndTime = $Boot
        } -ErrorAction SilentlyContinue | Where-Object { $_.Message -notlike '*MMDEVAPI*' })
    if ($pnp.Count) { $bits += "$($pnp.Count) non-audio PnP configure event(s) in the prior 5 min" }

    return $bits
}

# ---------------------------------------------------------------------------
# 4. classify
# ---------------------------------------------------------------------------
$out = @()
foreach ($inc in $incidents) {
    $hit = $torn | Where-Object {
        [math]::Abs(($_.Boot - $inc.Boot).TotalMinutes) -le $MatchMinutes
    } | Select-Object -First 1

    $explained = $false; $cause = $null; $evidence = $null; $tornStart = $null
    if ($hit) {
        $explained = $true
        $cause = 'driver install'
        $tornStart = if ($hit.Start) { $hit.Start.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
        $evidence = "$($hit.Name) - setupapi section opened $tornStart and never closed"
    }

    $out += [pscustomobject]@{
        boot      = $inc.Boot.ToString('yyyy-MM-dd HH:mm:ss')
        claimed   = if ($inc.Claimed) { $inc.Claimed.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
        bugcheck  = $inc.Bugcheck
        no41      = (-not $inc.Has41)
        explained = $explained
        cause     = $cause
        evidence  = $evidence
        tornStart = $tornStart
        context   = @(Get-Context -Boot $inc.Boot)
    }
}

$out = @($out | Sort-Object boot -Descending)

@{
    generated   = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    since       = $Since.ToString('yyyy-MM-dd HH:mm:ss')
    total       = $out.Count
    explained   = @($out | Where-Object { $_.explained }).Count
    unexplained = @($out | Where-Object { -not $_.explained }).Count
    cuts        = $out
} | ConvertTo-Json -Depth 5 -Compress
