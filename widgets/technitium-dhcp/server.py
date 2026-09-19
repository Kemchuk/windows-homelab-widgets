"""Tiny HTTP server exposing Technitium's real DHCP scope/lease stats as flat
JSON for a Homepage customapi widget.

Written for a DHCP-only Technitium deployment (something else does actual DNS
resolution). In that setup the built-in Homepage "technitium" widget type is
useless: it only surfaces DNS query stats -- queries/cached/blocked/clients --
and none of those ever move, because nothing queries Technitium for DNS. This
polls Technitium's own DHCP API instead (leases + scope range) and computes the
numbers that are actually meaningful: active client count, pool size,
utilization %, and whether the scope is enabled.

Polls in a background thread so each HTTP request returns instantly from cache
instead of paying the API round-trip per request.

Configuration, all via environment:
    TECHNITIUM_TOKEN   required -- API token from the Technitium admin UI
    TECHNITIUM_URL     default http://127.0.0.1:5380
    BIND_HOST          default 127.0.0.1
    PORT               default 9996
"""
import ipaddress
import json
import threading
import os
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9996"))
# Loopback by default. Homepage in a container reaches the host through
# host.docker.internal, which still resolves to a loopback listener; binding
# 0.0.0.0 would put an unauthenticated endpoint on the LAN for no gain.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
TECHNITIUM_URL = os.environ.get("TECHNITIUM_URL", "http://127.0.0.1:5380")
TOKEN = os.environ.get("TECHNITIUM_TOKEN", "")
REFRESH_SECONDS = 30

# NOT the file loop.ps1 redirects the child's stdout/stderr into: Windows makes
# that redirect an exclusive lock, so appending to it from here raises
# PermissionError. log() was the whole body of refresh_loop's `except`, so that
# raise escaped the loop and killed the poller thread for good on the first
# transient error. Use a file nothing else has open.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "widget.log")

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
    "clients": "-",
    "pool_size": "-",
    "utilization_pct": "-",
    "scope_status": "-",
}


def api_get(path):
    url = f"{TECHNITIUM_URL}{path}?token={TOKEN}"
    with urllib.request.urlopen(url, timeout=8) as r:
        return json.load(r)


def refresh():
    leases = api_get("/api/dhcp/leases/list")["response"]["leases"]
    scopes = api_get("/api/dhcp/scopes/list")["response"]["scopes"]
    scope = scopes[0] if scopes else {}

    pool_size = 0
    if scope.get("startingAddress") and scope.get("endingAddress"):
        start = int(ipaddress.IPv4Address(scope["startingAddress"]))
        end = int(ipaddress.IPv4Address(scope["endingAddress"]))
        pool_size = end - start + 1

    client_count = len(leases)
    util_pct = round(100 * client_count / pool_size, 1) if pool_size else 0

    data = {
        "clients": client_count,
        "pool_size": pool_size,
        "utilization_pct": util_pct,
        "scope_status": "Enabled" if scope.get("enabled") else "Disabled",
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
    # Fail loudly rather than serving dashes forever. An unset token makes
    # every poll 403 into the log file while the widget shows "-", which
    # looks identical to a DHCP server that is simply idle.
    if not TOKEN:
        sys.exit("TECHNITIUM_TOKEN is not set. Create an API token in the "
                 "Technitium admin UI and set it in the environment.")
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = HTTPServer((BIND_HOST, PORT), Handler)
    print(f"Listening on {BIND_HOST}:{PORT}", flush=True)
    server.serve_forever()
