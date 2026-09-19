"""Tiny HTTP server exposing Claude Code token/cost usage (via ccusage) as JSON
for a Homepage customapi widget.

Polls ccusage in a background thread so each HTTP request returns instantly
from cache instead of paying the ~1-2s ccusage subprocess cost per request.

Setup: install ccusage LOCALLY into this directory, not globally --

    cd widgets/claude-usage
    npm install ccusage

See the CCUSAGE_CLI comment below for why a global install can be invisible to
a scheduled task.

Configuration, all via environment:
    NODE_EXE     default C:\\Program Files\\nodejs\\node.exe
    CCUSAGE_CLI  default ./node_modules/ccusage/src/cli.js
    BIND_HOST    default 127.0.0.1
    PORT         default 9997
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

PORT = int(os.environ.get("PORT", "9997"))
# Loopback by default. Homepage in a container reaches the host through
# host.docker.internal, which still resolves to a loopback listener; binding
# 0.0.0.0 would put an unauthenticated endpoint on the LAN for no gain.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
NODE = os.environ.get("NODE_EXE", r"C:\Program Files\nodejs\node.exe")
# Install locally into this directory, NOT globally via `npm install -g`.
# A global install lands under %APPDATA%\npm, which on a machine with
# Claude Desktop installed from the Store can be transparently redirected by
# Windows package virtualization to a sandbox-specific LocalCache folder.
# That redirect only resolves for processes running inside that same
# sandboxed package context, so anything launched outside it (a Scheduled
# Task, in particular) sees the real, empty %APPDATA%\npm and fails with
# "Cannot find module". A local install is a plain disk path with no such
# virtualization.
CCUSAGE_CLI = os.environ.get(
    "CCUSAGE_CLI",
    os.path.join(HERE, "node_modules", "ccusage", "src", "cli.js"))
REFRESH_SECONDS = 30
# NOT the file a supervisor redirects the child's stdout/stderr into: Windows
# makes that redirect an exclusive lock, so appending to it from here raises
# PermissionError -- which aborted refresh() before it could populate the
# cache, and replaced the real ccusage error with a misleading permissions
# traceback. Use a file nothing else has open.
LOG_FILE = os.path.join(HERE, "widget.log")

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
_cache = {
    "status": "starting",
    "block_tokens": "-",
    "block_cost": "-",
    "block_remaining": "-",
    "block_rate": "-",
    "today_tokens": "-",
    "today_cost": "-",
}


def run_ccusage(args):
    # Call node.exe directly on ccusage's CLI entry point rather than going
    # through the ccusage.cmd wrapper. The wrapper's batch-file resolution
    # (via %COMSPEC%, PATH lookup of bare "node") behaves inconsistently
    # across process-launch contexts -- it worked interactively and failed
    # under a Scheduled-Task-spawned process. Calling node.exe by absolute
    # path on an absolute script path sidesteps all of that.
    result = subprocess.run(
        [NODE, CCUSAGE_CLI, *args],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ccusage {' '.join(args)} exited "
                           f"{result.returncode}: {result.stderr[:300]}")
    return json.loads(result.stdout)


def fmt_tokens(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_cost(c):
    return f"${(c or 0):.2f}"


def fmt_minutes(mins):
    mins = max(0, round(mins or 0))
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def refresh():
    data = {}

    try:
        active = run_ccusage(["blocks", "--active", "--json"])
        blocks = active.get("blocks", [])
    except Exception:
        blocks = []
        log(f"blocks --active failed:\n{traceback.format_exc()}")

    if blocks:
        b = blocks[0]
        tc = b.get("tokenCounts", {})
        total = b.get("totalTokens", 0) or sum(tc.values())
        end = datetime.fromisoformat(b["endTime"].replace("Z", "+00:00"))
        remaining_min = (end - datetime.now(timezone.utc)).total_seconds() / 60
        burn = b.get("burnRate") or {}
        proj = b.get("projection") or {}

        data["status"] = "active"
        data["block_tokens"] = f"{fmt_tokens(total)} tok"
        data["block_cost"] = fmt_cost(b.get("costUSD"))
        data["block_remaining"] = f"{fmt_minutes(remaining_min)} left"
        if burn.get("tokensPerMinute"):
            data["block_rate"] = f"{fmt_tokens(burn['tokensPerMinute'])}/min"
        elif proj.get("totalTokens"):
            data["block_rate"] = f"~{fmt_tokens(proj['totalTokens'])} proj"
        else:
            data["block_rate"] = "-"
    else:
        data["status"] = "idle"
        data["block_tokens"] = "no active block"
        data["block_cost"] = "-"
        data["block_remaining"] = "-"
        data["block_rate"] = "-"

    try:
        today = datetime.now().strftime("%Y%m%d")
        daily = run_ccusage(["daily", "--json", "--since", today])
        totals = daily.get("totals", {})
        data["today_tokens"] = f"{fmt_tokens(totals.get('totalTokens'))} tok"
        data["today_cost"] = fmt_cost(totals.get("totalCost"))
    except Exception:
        data["today_tokens"] = "-"
        data["today_cost"] = "-"

    with _lock:
        _cache.update(data)


def refresh_loop():
    while True:
        try:
            refresh()
        except Exception:
            log(f"refresh failed:\n{traceback.format_exc()}")
        time.sleep(REFRESH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            body = json.dumps(_cache).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress per-request console noise


if __name__ == "__main__":
    # Both of these produce a widget stuck on "-" with the real reason buried
    # in a log file nobody is tailing. Say so at startup instead.
    if not os.path.exists(NODE):
        sys.exit(f"node.exe not found at {NODE}. Set NODE_EXE.")
    if not os.path.exists(CCUSAGE_CLI):
        sys.exit(f"ccusage CLI not found at {CCUSAGE_CLI}. Run "
                 "`npm install ccusage` in this directory, or set CCUSAGE_CLI.")
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = HTTPServer((BIND_HOST, PORT), Handler)
    print(f"Listening on {BIND_HOST}:{PORT}", flush=True)
    server.serve_forever()
