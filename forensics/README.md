# Diagnosing a machine that hard-powers-off with no bugcheck

A box that instantly loses power leaves almost nothing behind. No BSOD, no
minidump, no WHEA machine-check. Event Viewer gives you Kernel-Power 41 with
`BugcheckCode = 0`, which means only "the previous shutdown was not clean" — it
does not tell you why, when, or even accurately *when*.

These scripts came out of chasing exactly that on one machine over 13 months.
The techniques generalise; the specific findings are kept as worked examples
because an abstract method is hard to trust.

## 1. Windows lies about when the machine died

Event **6008** says "the previous system shutdown at *T* was unexpected". That
timestamp is **the kernel's last dirty-shutdown checkpoint**, written
periodically — not the moment power was lost. It is always early, and it can be
early by minutes.

Measured on one cut:

| Source | Says the machine died at |
|---|---|
| Event 6008 | 16:43:03 |
| Flight recorder's last flushed sample | **16:48:55** |
| Torn `setupapi.dev.log` section | 16:48:42 |

**5 minutes 52 seconds out.** That matters more than it sounds: a Windows Update
driver install began at 16:48:04, forty seconds before the real cut. Measured
against 6008's timestamp, that install appears to happen *after* the shutdown
and gets dismissed as irrelevant. It was the cause.

Rule: **the recorder's last line is the cut. Event 6008 is a lower bound.**

## 2. A torn `setupapi.dev.log` section names a driver install that died

This is the most useful trick here and I have not seen it written up elsewhere.

A driver install writes a section header to `C:\Windows\INF\setupapi.dev.log`:

```
>>>  [Device Install (Install Windows Update driver) - pci\ven_8086&dev_0162]
>>>  Section start 2026/09/16 16:48:42.757
     ...
<<<  Section end 2026/09/16 16:48:44.102
```

If the machine dies mid-install the section **never closes**. The file is
truncated mid-write, and the next thing in it is the next boot's marker:

```
>>>  [Device Install (Install Windows Update driver) - pci\ven_8086&dev_0162]
>>>  Section start 2026/09/16 16:48:42.757
<invalid bytes>
[Boot Session: 2026/09/16 17:12:38.500]
```

An unclosed `Section start` followed by a `[Boot Session:]` marker is a driver
install that died with the machine. It gives you both **the cause** and **a
second independent clock** on the cut.

Two practical notes:

- **The log rotates** at roughly 4.3MB into `setupapi.dev.<timestamp>.log`.
  Scanning only the live file misses anything more than a couple of weeks old.
  There were nine archives on the machine this came from.
- **Open it with `FileShare::ReadWrite`.** An install in progress may hold it,
  and the tear leaves invalid bytes, so `grep` calls the file binary and a
  strict UTF-8 decode throws. Decode with replacement.

In the case above this found **two** torn sections in 180 days — the same
graphics driver, two days apart, killing the machine both times before it
eventually installed successfully. Both had previously been counted as
unexplained hardware faults.

## 3. Sensors nominal is not a verdict

`flight-recorder.ps1` samples every 5 seconds with `AutoFlush = $true`, so the
last line on disk is the last moment the machine was alive. It records CPU load,
memory, package and core temperatures, distance to TjMax, board temperature,
Vcore / +12V / +5V / +3.3V, fan RPM, and UPS status via `apcaccess`.

What it buys you is **ruling causes out**, with evidence:

- UPS `ONLINE` at nominal line voltage right to the last sample → not mains.
- 30°C of TjMax headroom → not a thermal trip.
- A rail holding a 0.4% spread across the whole day → not a visible sag.

What it cannot do is rule a cause *in*. **5-second sampling cannot see a PSU
transient.** A trace reading "everything nominal to the last line" is itself a
finding — whatever is ending the session is faster than the sample rate — but it
is not an alibi for the power supply.

The trap worth naming: on the cut above, every sensor was nominal, and the
actual cause was sitting in `setupapi.dev.log` the whole time. Check the logs
before you trust the sensors.

## 4. Count unexplained cuts, not Event 41

`classify-cuts.ps1` enumerates every unexpected shutdown in a window and marks
each **explained** or not. Explained means a torn setupapi section matches the
cut's boot time. Nothing else sets it.

Windows Update activity and PnP events near the cut are recorded as `context`
and deliberately **never** set explained — a download finishing 40 seconds
before a cut is a coincidence until it repeats. (It did not: one cut in four.)

Two things this fixes that a naive count gets wrong:

**A 6008 can exist with no Event 41.** One cut logged 6008 alone. Anything
enumerating by Event 41 undercounts. Cluster both event types by proximity
(they land seconds apart for one reboot) and treat either as evidence.

**Driver-install resets inflate the count.** Before classification the machine
looked like it had crashed twice since a hardware fix. Both were the same driver
install. The unexplained count was over by two, and a scheduled "did the fix
work?" notification was about to report failure for a fault that had been
repaired.

Output is one JSON object:

```powershell
.\classify-cuts.ps1 -Days 180 | ConvertFrom-Json
```

```json
{
  "total": 6, "explained": 2, "unexplained": 4,
  "cuts": [
    { "boot": "...", "claimed": "...", "bugcheck": 0, "no41": false,
      "explained": true, "cause": "driver install",
      "evidence": "... setupapi section opened ... and never closed",
      "tornStart": "...", "context": [] }
  ]
}
```

## 5. Keep the evidence, because the recorder deletes it

`flight-recorder.ps1` prunes its own day files after 30 days. If your backups
cover the scripts but not the CSVs — an easy thing to get wrong, since the CSVs
are noisy and mostly worthless — then the one file documenting an actual crash
is on a timer.

Backing up every CSV nightly is the wrong fix: ~1.3MB/day forever, and which day
matters is only knowable *after* the cut. Copy on detection instead — the boot
day's file and the day before it, since a cut at 00:20 has its whole run-up in
yesterday's file. Roughly 2.5MB per cut, kept permanently.

Write the verdict alongside the CSV. Raw samples never say why they mattered,
and the evidence has to still read on its own years later with no tooling.

`promote-cut-evidence.ps1` does this. It asks `classify-cuts.ps1` for every
cut, copies the boot day's CSV and the day before it into a per-cut folder,
and drops the classifier's verdict beside them as `cut.json`:

```powershell
.\promote-cut-evidence.ps1 -Dest 'D:\backed-up\flightrec-crashes'
```

```
flightrec-crashes/
  2026-09-16_171249/
    cut.json
    flight_20260915.csv
    flight_20260916.csv
```

Idempotent by size, so run it on a schedule — daily is plenty. It copies to
`.part` and renames, because the recorder is appending to the source every
five seconds and a half-copied CSV that looks complete is worse than none.
Cuts with no CSV left are reported and skipped rather than treated as errors;
most of a 180-day window is usually older than the recorder.

If something already has the classifier's output — a dashboard poller, say —
hand it over with `-CutsJson` instead of letting the script run the
classifier again:

```powershell
.\promote-cut-evidence.ps1 -Dest 'D:\backed-up\flightrec-crashes' -CutsJson .\cuts.json
```

That scan walks every rotated setupapi log, so it is not something to repeat
on a fifteen-minute loop. A scheduled task can ignore the flag entirely.

**Point `-Dest` at something your backups actually reach.** That is the whole
purpose and the easiest part to get wrong — a keep-forever folder that no
backup job visits just moves the problem somewhere you will not think to look.

## Running these

PowerShell 5.1, the version that ships with Windows. No `&&`, no ternaries, no
null-coalescing — all three are parse errors there.

`flight-recorder.ps1` **must run elevated**: sensor values are blank without
kernel driver access. It reads temperatures and voltages through
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).
Note that LHM ships the WinRing0 driver, which Defender may flag as a vulnerable
driver and quarantine.

`recorder-review.example.ps1` is a worked example of the whole loop: run N days
after a suspected fix, count only unexplained cuts, and push a verdict. It reads
its notification target from the environment — set `NTFY_URL`, `NTFY_HOST` and
`NTFY_TOKEN`, or replace that block with whatever you use. If the classifier is
missing it falls back to a raw Event 41 count **and says so loudly in the
message**, because a silent fallback here hands back the exact over-count the
classifier exists to prevent.
