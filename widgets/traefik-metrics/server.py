"""Tiny HTTP server exposing Traefik's Prometheus metrics as flat JSON for a
Homepage customapi widget.

The built-in Homepage "traefik" widget only shows static router/middleware
counts via the dashboard API -- it cannot surface request volume or error rate,
which is what is actually useful for spotting things like tunnel-reconnect 502
spikes. This polls Traefik's own --metrics.prometheus=true endpoint (no
separate Prometheus/Grafana needed) and computes cumulative totals plus a
requests/min rate from the delta between polls.

Polls in a background thread so each HTTP request returns instantly from cache
instead of paying the scrape cost per request.

Configuration, all via environment:
    TRAEFIK_METRICS_URL  default http://127.0.0.1:8080/metrics
    BIND_HOST            default 127.0.0.1
    PORT                 default 9995

Traefik needs --metrics.prometheus=true and the entrypoint serving /metrics
reachable from this host.
"""
import json
import re
import os
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9995"))
# Loopback by default. Homepage in a container reaches the host through
# host.docker.internal, which still resolves to a loopback listener; binding
# 0.0.0.0 would put an unauthenticated endpoint on the LAN for no gain.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
METRICS_URL = os.environ.get("TRAEFIK_METRICS_URL",
                             "http://127.0.0.1:8080/metrics")
REFRESH_SECONDS = 30

# NOT the file loop.ps1 redirects the child's stdout/stderr into: Windows makes
# that redirect an exclusive lock, so appending to it from here raises
# PermissionError. The try/except below caught it -- so the poller survived,
# but every diagnostic vanished silently and no "ok:" line was ever written.
# Use a file nothing else has open.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "widget.log")

LINE_RE = re.compile(
    r'^traefik_entrypoint_requests_total\{([^}]*)\}\s+([\d.]+)$')
CODE_RE = re.compile(r'code="(\d+)"')

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
        pass  # a logging failure must never take down refresh_loop


_lock = threading.Lock()
_cache = {
    "total_requests": "-",
    "errors_5xx": "-",
    "error_rate_pct": "-",
    "requests_per_min": "-",
}
_prev = {"total": None, "time": None}


def fetch_metrics():
    with urllib.request.urlopen(METRICS_URL, timeout=8) as r:
        return r.read().decode()


def refresh():
    text = fetch_metrics()

    total = 0
    errors = 0
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        labels, value = m.group(1), float(m.group(2))
        total += value
        code_m = CODE_RE.search(labels)
        if code_m and code_m.group(1).startswith("5"):
            errors += value

    # monotonic, not wall clock: a clock step would otherwise produce a
    # nonsense rate or a negative elapsed.
    now = time.monotonic()
    rate = "-"
    if _prev["total"] is not None and _prev["time"] is not None:
        elapsed_min = (now - _prev["time"]) / 60
        if elapsed_min > 0:
            # Counters reset when Traefik restarts, so clamp at zero rather
            # than reporting a large negative rate for one interval.
            rate = round(max(0, total - _prev["total"]) / elapsed_min, 1)
    _prev["total"] = total
    _prev["time"] = now

    data = {
        "total_requests": int(total),
        "errors_5xx": int(errors),
        "error_rate_pct": round(100 * errors / total, 2) if total else 0,
        "requests_per_min": rate,
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
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = HTTPServer((BIND_HOST, PORT), Handler)
    print(f"Listening on {BIND_HOST}:{PORT}", flush=True)
    server.serve_forever()
