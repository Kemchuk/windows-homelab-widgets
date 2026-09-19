# flight-recorder.ps1 - a black box for unexplained hard power-offs.
#
# Samples every 5s and flushes each line immediately, so the final sample
# before an instant power cut survives on disk. That flush is the whole point:
# a buffered writer loses exactly the lines you need.
#
# MUST RUN ELEVATED: sensor values are blank without kernel driver access.
#
# Needs LibreHardwareMonitorLib.dll for temperatures and voltages. Download
# from github.com/LibreHardwareMonitor/LibreHardwareMonitor and point -Lib at
# it. Note it ships the WinRing0 driver, which Defender may flag as a
# vulnerable driver and quarantine.
#
# UPS fields need apcupsd's apcaccess.exe; they are simply left blank if it is
# not installed.
#
# Run it from a scheduled task at startup, elevated, hidden:
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden `
#     -File C:\flightrec\flight-recorder.ps1
#
# PowerShell 5.1 compatible.

[CmdletBinding()]
param(
    [string]$OutDir = 'C:\flightrec',
    [string]$Lib = 'C:\flightrec\LHMfw\LibreHardwareMonitorLib.dll',
    [int]$IntervalSeconds = 5,
    # Day files older than this are deleted at each midnight rollover.
    # /!\ If a cut happens, copy that day's file somewhere permanent before
    # this fires. See section 5 of README.md.
    [int]$RetentionDays = 30
)

$ErrorActionPreference = 'SilentlyContinue'

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

$apc = @('C:\apcupsd\bin\apcaccess.exe',
         'C:\Program Files\apcupsd\bin\apcaccess.exe') |
         Where-Object { Test-Path $_ } | Select-Object -First 1

$wid = [Security.Principal.WindowsIdentity]::GetCurrent()
$elev = (New-Object Security.Principal.WindowsPrincipal($wid)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$comp = $null
if (Test-Path $Lib) {
    try {
        Add-Type -Path $Lib
        $comp = New-Object LibreHardwareMonitor.Hardware.Computer
        $comp.IsCpuEnabled         = $true
        $comp.IsMotherboardEnabled = $true
        $comp.IsControllerEnabled  = $true
        $comp.Open()
    } catch { $comp = $null }
}

function Read-Sensors {
    param($c)
    $o = @{}
    if (-not $c) { return $o }
    foreach ($h in $c.Hardware) {
        $h.Update()
        $list = @($h.Sensors)
        foreach ($sh in $h.SubHardware) { $sh.Update(); $list += @($sh.Sensors) }
        foreach ($s in $list) {
            if ($s.Value -eq $null) { continue }
            $v = [math]::Round([double]$s.Value, 3)
            switch ($s.SensorType.ToString()) {
                'Temperature' {
                    if ($s.Name -eq 'CPU Package')  { $o.cput   = $v }
                    if ($s.Name -eq 'Core Max')     { $o.cpumax = $v }
                    if ($s.Name -match 'Temperature|System|Motherboard' -and -not $o.mbt) { $o.mbt = $v }
                    # Distance to TjMax is a COUNTDOWN in the same unit as a
                    # temperature: 39 here means 39 degrees of headroom, not
                    # 39 degrees. Do not match it with a loose 'CPU' pattern.
                    if ($s.Name -match 'Core #1 Distance') { $o.tjdist = $v }
                }
                'Voltage' {
                    if ($s.Name -match 'Vcore|CPU Core' -and -not $o.vc)  { $o.vc  = $v }
                    if ($s.Name -match '\+12|12V'       -and -not $o.v12) { $o.v12 = $v }
                    if ($s.Name -match '\+5V|^5V'       -and -not $o.v5)  { $o.v5  = $v }
                    if ($s.Name -match '\+3\.3|3\.3V'   -and -not $o.v33) { $o.v33 = $v }
                }
                'Fan' { if ($s.Name -match 'CPU' -and -not $o.fan) { $o.fan = [math]::Round($v,0) } }
            }
        }
    }
    return $o
}

$hdr = 'time,cpu_pct,mem_pct,cpu_pkg_c,core_max_c,tjmax_dist_c,mb_c,vcore_v,v12_v,v5_v,v33_v,cpu_fan_rpm,ups_status,ups_linev,ups_loadpct,ups_battv,ups_bcharge'
$csv = Join-Path $OutDir ('flight_{0:yyyyMMdd}.csv' -f (Get-Date))
$new = -not (Test-Path $csv)
try {
    $sw = New-Object System.IO.StreamWriter($csv, $true, [System.Text.Encoding]::ASCII)
    # The entire reason this script works. Without AutoFlush the last few
    # samples live in a buffer that a power cut discards.
    $sw.AutoFlush = $true
} catch {
    # Another instance holds the file, or the path is unwritable. Do NOT spin
    # silently: a recorder that is not recording must be loud, or you find out
    # only after the crash you were trying to catch.
    Add-Content -Path (Join-Path $OutDir 'recorder-errors.log') `
        -Value "$(Get-Date -Format s) pid=$PID CANNOT OPEN $csv : $($_.Exception.Message)"
    exit 1
}
if ($new) { $sw.WriteLine($hdr) }
$sensorState = if ($comp) {
    if ($elev) { 'sensors=LIVE' } else { 'sensors=BLANK(not elevated)' }
} else { 'sensors=LIB-FAILED' }
# A restart marker. Parsers must skip '#' lines -- a naive CSV reader turns
# this into a row of nulls.
$sw.WriteLine("# ---- start $(Get-Date -Format 's') pid=$PID elevated=$elev $sensorState ----")

$i = 0
$u = @{ status=''; linev=''; loadpct=''; battv=''; bcharge='' }

while ($true) {
  try {
    $t   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $cpu = (Get-Counter '\Processor Information(_Total)\% Processor Time').CounterSamples[0].CookedValue
    $cpu = if ($cpu -ne $null) { [math]::Round($cpu,1) } else { '' }
    $os  = Get-CimInstance Win32_OperatingSystem
    $mem = if ($os) { [math]::Round(100 - ($os.FreePhysicalMemory / $os.TotalVisibleMemorySize * 100), 1) } else { '' }

    $s = Read-Sensors $comp

    # apcaccess is a process spawn; every third cycle is plenty for something
    # that only matters when it changes state.
    if ($apc -and ($i % 3 -eq 0)) {
        foreach ($ln in (& $apc status 2>$null)) {
            if ($ln -match '^STATUS\s*:\s*(.+?)\s*$') { $u.status  = $Matches[1] }
            if ($ln -match '^LINEV\s*:\s*([\d.]+)')   { $u.linev   = $Matches[1] }
            if ($ln -match '^LOADPCT\s*:\s*([\d.]+)') { $u.loadpct = $Matches[1] }
            if ($ln -match '^BATTV\s*:\s*([\d.]+)')   { $u.battv   = $Matches[1] }
            if ($ln -match '^BCHARGE\s*:\s*([\d.]+)') { $u.bcharge = $Matches[1] }
        }
    }

    $sw.WriteLine("$t,$cpu,$mem,$($s.cput),$($s.cpumax),$($s.tjdist),$($s.mbt),$($s.vc),$($s.v12),$($s.v5),$($s.v33),$($s.fan),$($u.status),$($u.linev),$($u.loadpct),$($u.battv),$($u.bcharge)")

    $today = Join-Path $OutDir ('flight_{0:yyyyMMdd}.csv' -f (Get-Date))
    if ($today -ne $csv) {
        $sw.Close(); $csv = $today
        $sw = New-Object System.IO.StreamWriter($csv, $true, [System.Text.Encoding]::ASCII)
        $sw.AutoFlush = $true
        $sw.WriteLine($hdr)
        Get-ChildItem $OutDir -Filter 'flight_*.csv' |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$RetentionDays) } |
            Remove-Item -Force
    }
    $i++
  } catch {
    Add-Content -Path (Join-Path $OutDir 'recorder-errors.log') `
        -Value "$(Get-Date -Format s) pid=$PID loop error: $($_.Exception.Message)"
  }
  Start-Sleep -Seconds $IntervalSeconds
}
