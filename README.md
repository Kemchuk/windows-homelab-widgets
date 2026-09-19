# Windows homelab widgets

Small HTTP services that expose Windows host facts as flat JSON, for
[Homepage](https://gethomepage.dev) `customapi` widgets — plus a set of tools for
diagnosing hard power-offs that leave no bugcheck.

Most self-hosting tooling assumes Linux. These run on a Windows host and get at
the things Linux-first tools simply cannot see: Docker Desktop's own version,
per-process CPU without paying for a handle per process, Technitium's DHCP
scope, LibreHardwareMonitor sensors,
and Windows' own crash logs.

Everything here runs in production on a single Windows 10 box hosting ~65
containers. Nothing is a demo.

## The pattern

Homepage's `customapi` widget can read any JSON endpoint. So instead of writing
a Homepage plugin for each thing, each fact gets a ~150-line Python service on
localhost that returns **flat** JSON, and Homepage points at it.

```
Homepage  ──customapi──▶  127.0.0.1:99xx  ──▶  the awkward Windows thing
```

Why flat JSON: Homepage's `mappings` cannot traverse arbitrary nesting cleanly,
so every service returns a single-level object with pre-formatted strings. The
formatting decision belongs in the service, where it can be tested, not in YAML.

Why one service per fact: they fail independently. A wedged Docker API cannot
take down the DHCP widget. Each is small enough to read in one sitting.

No framework, no dependencies beyond the standard library unless a service
genuinely needs one. `http.server.ThreadingHTTPServer` is sufficient for a
localhost endpoint polled every 30 seconds.

## Widgets

| Service | Port | What it answers |
|---|---|---|
| `claude-usage` | 9997 | Claude Code token spend and cost, via `ccusage` |
| `docker-desktop` | 9991 | Installed Docker Desktop version, whether a newer release exists, engine state |
| `kometa` | 9994 | Last Kometa run, parsed out of its log |
| `recyclarr` | 9993 | Last Recyclarr sync |
| `technitium-dhcp` | 9996 | Real DHCP scope use and lease counts from Technitium |
| `top-processes` | 9999 | Top 5 processes by CPU, via a WMI performance counter (25x faster than `psutil` here) |
| `traefik-metrics` | 9995 | Traefik's Prometheus metrics, flattened |

`loop.ps1` is the supervisor: a 20-line keep-alive that restarts a service if it
exits. Paired with a scheduled task at logon, it is the whole deployment story.

## Forensics

`forensics/` is for a specific problem: **a machine that hard-powers-off with no
bugcheck, no minidump and no WHEA event.** Nothing in Windows records what the
hardware was doing at the moment it stopped, because the thing that would record
it also stopped.

- `flight-recorder.ps1` — samples CPU/temps/voltages/fan/UPS every 5s and
  flushes each line immediately, so the last line survives an instant cut.
- `classify-cuts.ps1` — decides whether a power cut was **explained**. Counting
  Kernel-Power 41 and calling the total "crashes" over-reports badly; a driver
  install that dies mid-write looks identical to a hardware fault unless you
  check for it.
- `recorder-review.example.ps1` — a worked example of turning the above into a
  scheduled verdict notification.

The techniques in `forensics/README.md` are the part worth reading even if you
never run the scripts.

## Requirements

- Windows 10/11
- Python 3.10+ for the widgets (standard library only, except where a service
  says otherwise)
- PowerShell 5.1 for the forensics scripts — they avoid `&&`, ternaries and
  null-coalescing so they run on the version that ships with Windows

## Setup

Each service is self-contained. Copy the directory, set any environment
variables it documents, and run it:

```powershell
python .\widgets\docker-desktop\server.py
```

Then point Homepage at it. Each widget directory documents its own
`services.yaml` block.

To keep one alive across crashes and logons, use `loop.ps1` with a scheduled
task set to run at logon. Run the task **hidden** — a visible PowerShell window
on every logon gets old fast, and there is a ~20s launch cost either way.

## Gotchas worth knowing before you start

These cost real time to find.

**Windows lets two processes bind the same port.** The second bind succeeds
silently and requests race between them. If a widget returns stale data half the
time, check for a duplicate process before debugging the service.

**`psutil`'s per-process CPU is slow on older hardware** — slow enough to make a
30-second widget refresh miss its window, because it needs an `OpenProcess` per
process and Defender's filter driver taxes each one. `top-processes` reads the
`Win32_PerfFormattedData_PerfProc_Process` performance counter instead, which
returns every process in one query with no per-process handles.

**Homepage caches `public/` at startup.** A newly added icon 404s until the
container restarts.

**A `customapi` widget is not an `info` widget**, and the two take different
YAML. Mixing them up produces a widget that renders nothing with no error.

**Scheduled tasks that run as SYSTEM are invisible** to an unelevated
`Get-ScheduledTask`. A task you cannot see is still firing.

## Licence

MIT. See [LICENSE](LICENSE).
