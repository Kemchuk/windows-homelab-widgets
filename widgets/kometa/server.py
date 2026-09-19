"""Tiny HTTP server exposing Kometa's last-run summary as flat JSON for a
Homepage customapi widget.

Kometa has no API or metrics endpoint of its own -- it just writes a run
summary to logs/meta.log (one file per run, rotated to meta-1.log..meta-9.log
daily by Kometa itself, current run always at meta.log). This tails meta.log,
parses the "Finished ... Run" summary block (start/finish time, run time) plus
the Warning/Error Summary tables, and serves the result as JSON.

Polls in a background thread so each HTTP request returns instantly from cache
instead of re-reading and re-parsing the log file per request.

Configuration, all via environment:
    KOMETA_LOG      required -- path to Kometa's config/logs/meta.log
    KOMETA_IGNORE   optional -- '||'-separated warning substrings to ignore
    BIND_HOST       default 127.0.0.1
    PORT            default 9994
"""
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9994"))
# Loopback by default. Homepage in a container reaches the host through
# host.docker.internal, which still resolves to a loopback listener; binding
# 0.0.0.0 would put an unauthenticated endpoint on the LAN for no gain.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
LOG_PATH = os.environ.get("KOMETA_LOG", "")
REFRESH_SECONDS = 300
# NOT the file a supervisor redirects the child's stdout/stderr into: Windows
# makes that redirect an exclusive lock, so appending to it from here raises
# PermissionError. log() was the whole body of refresh_loop's `except`, so
# that raise escaped the loop and killed the poller thread for good on the
# first transient error. Use a file nothing else has open.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "widget.log")

FINISHED_RE = re.compile(
    r"Start Time:\s*(\S+ \S+)\s+Finished:\s*(\S+ \S+)\s+Run Time:\s*(\S+)"
)
SUMMARY_ROW_RE = re.compile(r"\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*$")

# Warning messages that are expected and should not light up the widget.
# Substring match. Set KOMETA_IGNORE to override, '||'-separated.
#
# The two defaults, and why they are noise rather than signal:
#
# - "Asset Directory Not Found and Created": Kometa emits this every time new
#   media shows up, if create_asset_folders is on. It makes the empty asset
#   folder itself and then warns that it had to.
# - "Collection Warning: No items found": label rules that cover franchises
#   you may not own yet (a "Keep - Dune" rule while no Dune films are in the
#   library). Kometa warns every run until a title lands; nothing is
#   actionable.
#
# A widget that is permanently amber for reasons you have already decided are
# fine is a widget you stop looking at, which is worse than not having it.
IGNORED_WARNINGS = tuple(
    s for s in os.environ.get(
        "KOMETA_IGNORE",
        "Asset Directory Not Found and Created"
        "||Collection Warning: No items found").split("||") if s
)

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
    "run_time": "-",
    "warnings": "-",
    "errors": "-",
}


def summary_rows(lines, start_idx):
    """Read (count, message) pairs from a summary table after a header line."""
    rows = []
    for line in lines[start_idx:]:
        if line.count("=") > 20:
            if rows or "|" not in line:
                break
            continue
        m = SUMMARY_ROW_RE.search(line)
        if m:
            rows.append((int(m.group(1)), m.group(2)))
        elif "|" not in line:
            break
    return rows


def count_summary_rows(lines, start_idx, ignored=()):
    """Sum a summary table's Count column, skipping ignored messages."""
    return sum(
        count
        for count, message in summary_rows(lines, start_idx)
        if not any(skip in message for skip in ignored)
    )


def refresh():
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.splitlines()

    finished_match = None
    for m in FINISHED_RE.finditer(text):
        finished_match = m  # last match wins

    if not finished_match:
        # No "Finished" block yet means the run is still going, not that
        # something broke.
        with _lock:
            _cache.update({
                "status": "Running",
                "last_run": "-",
                "run_time": "-",
                "warnings": "-",
                "errors": "-",
            })
        return

    warnings = 0
    errors = 0
    for i, line in enumerate(lines):
        if "Warning Summary" in line:
            warnings = count_summary_rows(lines, i + 3, IGNORED_WARNINGS)
        if "Error Summary" in line:
            errors = count_summary_rows(lines, i + 3)

    status = "Errors" if errors else ("Warnings" if warnings else "OK")

    finish_time, finish_date = finished_match.group(2).split(" ")

    data = {
        "status": status,
        "last_run": f"{finish_date}T{finish_time}",
        "run_time": finished_match.group(3),
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
    if not LOG_PATH:
        sys.exit("KOMETA_LOG is not set. Point it at Kometa's "
                 "config/logs/meta.log.")
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = HTTPServer((BIND_HOST, PORT), Handler)
    print(f"Listening on {BIND_HOST}:{PORT}", flush=True)
    server.serve_forever()
