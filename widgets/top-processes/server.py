"""Tiny HTTP server returning top 5 Windows processes by CPU as JSON for a
Homepage customapi widget.

Samples in a background thread so each HTTP request returns instantly from
cache.

Two things this gets right, both learned the hard way after this server sat
wedged for eleven days -- answering nothing while pegging ~50% of a core:

1. It used to do the scrape *inside* do_GET on a single-threaded HTTPServer
   while the widget polled every 10s. Work arrived faster than it completed,
   so the backlog never drained. A supervisor like loop.ps1 only restarts a
   service when it *exits*, so a hang is silent and permanent. Handlers now do
   no work at all.

2. psutil is the wrong tool here. On this class of hardware a single
   process_iter(['cpu_percent']) sweep costs ~30 CPU-seconds for ~400
   processes (~75ms each -- Defender's filter driver taxes the OpenProcess
   that psutil needs per process for cpu_times). Names are cheap once warm;
   CPU is not. The Win32_PerfFormattedData_PerfProc_Process performance
   counter returns the same data for every process in ~1.2s with no
   per-process handles -- roughly 25x cheaper on a 4-core machine.

The sampler is one long-lived PowerShell child printing a JSON line per cycle,
rather than a spawn per cycle: process startup plus a cold CIM connection cost
more than the query itself (~3.5s cold vs ~1.2s warm).

Configuration, all via environment:
    BIND_HOST  default 127.0.0.1
    PORT       default 9999
"""
import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "9999"))
# Loopback by default. Homepage in a container reaches the host through
# host.docker.internal, which still resolves to a loopback listener; binding
# 0.0.0.0 would put an unauthenticated endpoint on the LAN for no gain.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")

# /!\ 60, not 15. Each sample is a full
# Win32_PerfFormattedData_PerfProc_Process enumeration: measured at ~0.9-1.1s
# for 424 rows, and the whole cost is billed to WmiPrvSE rather than to this
# process -- which is why this widget read as free in every process list while
# being the biggest WMI consumer on the box. At 15s that was 4 enumerations a
# minute.
#
# 60 matches the widget's refreshInterval, so the dashboard no longer polls
# faster than the source updates. Do not raise it past 60: Homepage does not
# honour a refreshInterval above 60s (it collapses to ~85s), so the two would
# drift apart again.
SAMPLE_SECONDS = 60
# If the sampler dies or wedges, serve placeholders rather than freezing the
# widget on numbers that look live but are hours old. Three missed samples --
# kept proportional to SAMPLE_SECONDS, which is what makes it a wedge detector
# rather than a fixed timeout that happens to be lenient.
STALE_AFTER_SECONDS = 180
# NOT the file a supervisor redirects the child's stdout/stderr into: Windows
# makes that redirect an exclusive lock, so appending to it from here raises
# PermissionError -- and the "sampler exited; respawning" call sits inside the
# try, so its raise was caught by the `except`, whose own log() call then
# raised uncaught and killed the sampler thread. The widget would freeze on
# placeholders the first time the sampler needed a respawn, with the process
# still up and the port still listening. Use a file nothing else has open.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "widget.log")

CPU_COUNT = os.cpu_count() or 1

# PercentProcessorTime is per-core (vmmem routinely reads >100 on 4 cores), so
# divide by the core count to show share of the whole machine. The #N suffix
# disambiguates same-named processes.
SAMPLER_PS = r"""
$ErrorActionPreference = 'Stop'
while ($true) {
  # /!\ Exit if the parent is gone. Killing the python parent does not kill
  # this child, so without this every restart of the service leaves a sampler
  # polling WMI forever -- one such orphan was found still looping 67 hours
  # and 1717 CPU-seconds after its parent died. The name check guards against
  # PID reuse.
  $par = Get-Process -Id __PPID__ -ErrorAction SilentlyContinue
  if (-not $par -or $par.ProcessName -notlike 'python*') { exit }
    try {
        $r = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process |
            Where-Object { $_.Name -ne '_Total' -and $_.Name -ne 'Idle' } |
            Sort-Object PercentProcessorTime -Descending |
            Select-Object -First 5 @{n='n';e={$_.Name -replace '#\d+$',''}},
                                   @{n='c';e={[double]$_.PercentProcessorTime}}
        Write-Output (ConvertTo-Json -InputObject @($r) -Compress -Depth 3)
    } catch {
        Write-Output '[]'
    }
    Start-Sleep -Seconds __SLEEP__
}
""".replace("__SLEEP__", str(SAMPLE_SECONDS)).replace("__PPID__",
                                                      str(os.getpid()))

PLACEHOLDER = {str(i): "-" for i in range(1, 6)}

LOG_MAX_BYTES = 512 * 1024


def log(msg):
    """Never let a logging failure kill the caller's thread."""
    try:
        # This process polls indefinitely, so cap the file. One generation back
        # is plenty -- the interesting entries are always the recent ones.
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


_lock = threading.Lock()
_cache = dict(PLACEHOLDER)
_last_ok = None


def format_rows(rows):
    out = {}
    for i, row in enumerate(rows[:5], 1):
        name = (row.get("n") or "unknown").replace(".exe", "")[:20]
        out[str(i)] = f"{name}  {(row.get('c') or 0.0) / CPU_COUNT:.1f}%"
    for i in range(len(rows) + 1, 6):
        out[str(i)] = "-"
    return out


def sample_loop():
    global _last_ok
    while True:
        try:
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", SAMPLER_PS],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                rows = json.loads(line)
                if isinstance(rows, dict):  # PS emits a bare object for one row
                    rows = [rows]
                data = format_rows(rows)
                with _lock:
                    _cache.update(data)
                    _last_ok = time.monotonic()
            log(f"sampler exited with {proc.wait()}; respawning")
        except Exception:
            log(f"sampler failed:\n{traceback.format_exc()}")
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            stale = (_last_ok is None
                     or (time.monotonic() - _last_ok) > STALE_AFTER_SECONDS)
            body = json.dumps(dict(PLACEHOLDER) if stale else _cache).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress per-request console noise


if __name__ == "__main__":
    threading.Thread(target=sample_loop, daemon=True).start()
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"Listening on {BIND_HOST}:{PORT}", flush=True)
    server.serve_forever()
