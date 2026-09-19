"""Tiny HTTP server exposing Recyclarr's last-run summary as flat JSON for a
Homepage customapi widget.

Recyclarr has no API of its own -- it just writes one debug log per run to
config/logs/cli/recyclarr_<date>_<time>.debug.log. This finds the newest such
file, counts "Completed at" lines (one per Sonarr/Radarr server synced) plus
WRN/ERR/FTL log-level lines, and serves the result as JSON.

Polls in a background thread so each HTTP request returns instantly from cache
instead of re-scanning the logs directory per request.

Configuration, all via environment:
    RECYCLARR_LOG_DIR  required -- Recyclarr's config/logs/cli directory
    BIND_HOST          default 127.0.0.1
    PORT               default 9993
"""
import glob
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9993"))
# Loopback by default. Homepage in a container reaches the host through
# host.docker.internal, which still resolves to a loopback listener; binding
# 0.0.0.0 would put an unauthenticated endpoint on the LAN for no gain.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
LOG_DIR = os.environ.get("RECYCLARR_LOG_DIR", "")
REFRESH_SECONDS = 300

# NOT the file loop.ps1 redirects the child's stdout/stderr into: Windows makes
# that redirect an exclusive lock, so appending to it from here raises
# PermissionError. log() was the whole body of refresh_loop's `except`, so that
# raise escaped the loop and killed the poller thread for good on the first
# transient error. Use a file nothing else has open.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "widget.log")

FILENAME_RE = re.compile(
    r"recyclarr_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.debug\.log$")
LEVEL_RE = re.compile(r"\[\d{2}:\d{2}:\d{2} (DBG|INF|WRN|ERR|FTL)\]")

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
    "status": "-",
    "last_run": "-",
    "servers_synced": "-",
    "warnings": "-",
    "errors": "-",
}


def refresh():
    files = glob.glob(os.path.join(LOG_DIR, "*.debug.log"))
    if not files:
        return
    latest = max(files, key=os.path.getmtime)

    m = FILENAME_RE.search(os.path.basename(latest))
    last_run = f"{m.group(1)}T{m.group(2).replace('-', ':')}" if m else "-"

    with open(latest, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    servers_synced = text.count("Completed at ")
    warnings = len(re.findall(r"\[\d{2}:\d{2}:\d{2} WRN\]", text))
    errors = len(re.findall(r"\[\d{2}:\d{2}:\d{2} (ERR|FTL)\]", text))

    status = "Errors" if errors else ("Warnings" if warnings else "OK")

    data = {
        "status": status,
        "last_run": last_run,
        "servers_synced": servers_synced,
        "warnings": warnings,
        "errors": errors,
    }

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
    # An unset or wrong log dir makes refresh() return early forever while the
    # widget shows "-", which is indistinguishable from Recyclarr never having
    # run. Fail at startup instead.
    if not LOG_DIR or not os.path.isdir(LOG_DIR):
        sys.exit("RECYCLARR_LOG_DIR is not set or is not a directory. Point "
                 "it at Recyclarr's config/logs/cli directory.")
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = HTTPServer((BIND_HOST, PORT), Handler)
    print(f"Listening on {BIND_HOST}:{PORT}", flush=True)
    server.serve_forever()
