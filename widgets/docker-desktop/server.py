"""Tiny HTTP server exposing Docker Desktop's installed version, whether a
newer release exists, and engine service state as flat JSON for a Homepage
customapi widget.

Nothing pushes this information, so a background thread polls and the HTTP
handler always serves from cache.

**"Latest" comes from Docker's release notes, NOT from winget.** winget is the
authority on what is *installed*, but it lags Docker's own releases by days --
so the tile said "Up to date" while Docker Desktop itself was offering an
upgrade. Concretely: Docker published 4.87.0 one morning while winget still
carried only 4.86.0, and even Docker's own auto-update appcast
(desktop.docker.com/win/main/amd64/appcast.xml) still offered 4.86.0 because
rollout is staged. The release-notes page is the source that matches what the
app tells you, which is the whole point of the tile.

Consequence worth knowing: the tile can show an update that `winget upgrade`
cannot install yet. That is intended -- Docker Desktop updates itself through
its own updater, not through winget.

The version lookups hit the network, so they run on a slow interval
(UPDATE_CHECK_SECONDS). Engine service state is a local Get-Service call and
costs nothing, so it refreshes on the tight loop interval instead.

Configuration, all via environment:
    BIND_HOST  default 127.0.0.1
    PORT       default 9991
"""
import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9991"))
# Loopback by default. Homepage in a container reaches the host through
# host.docker.internal, which still resolves to a loopback listener; binding
# 0.0.0.0 would put an unauthenticated endpoint on the LAN for no gain.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
LOOP_SECONDS = 30
UPDATE_CHECK_SECONDS = 6 * 60 * 60
# A failed winget query used to wait the full 6h before retrying, so a single
# transient failure left version/update/checked showing "-" for most of a day.
# That is what actually happened: the task runs at logon, the startup query
# failed (winget/network not ready that early), and nothing retried until the
# next 6h boundary. Back off to minutes on failure instead.
UPDATE_RETRY_SECONDS = 15 * 60
# NOT the file a supervisor redirects the child's stdout/stderr into: on
# Windows that redirect is an exclusive lock. Appending to it from here raises
# PermissionError, which log()'s own `except OSError` then swallows -- so every
# failure vanished silently while the service looked up. That is exactly why
# version/update/checked sat at "-" with no visible error.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "widget.log")
WINGET_ID = "Docker.DockerDesktop"
SERVICE_NAME = "com.docker.service"

RELEASE_NOTES_URL = "https://docs.docker.com/desktop/release-notes/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Anchored on "<version> <date>", which is how each release section is headed.
# A bare \d+\.\d+\.\d+ would also match the Docker Engine and Go versions
# listed in the notes -- and those are numerically LARGER (e.g. 28.5.2 >
# 4.87.0), so an unanchored "take the max" would confidently report the wrong
# number.
RELEASE_RE = re.compile(r"\b(\d+\.\d+\.\d+)\s+(20\d\d-\d\d-\d\d)\b")

LOG_MAX_BYTES = 512 * 1024


def log(msg):
    try:
        # Poller runs indefinitely; a persistent winget failure would otherwise
        # append a traceback every UPDATE_RETRY_SECONDS forever.
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except OSError:
        pass  # a logging failure must never take down refresh_loop


_lock = threading.Lock()
_cache = {
    "version": "-",
    "update": "-",
    "engine": "-",
    "checked": "-",
    "released": "-",
    "winget": "-",
}


def run(args, timeout):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout


def columns_after_id(line):
    # Column widths are sized to content, so padding between Name and Id can
    # collapse to a single space -- splitting on N+ spaces is unreliable.
    # Name is the only column that can itself contain spaces, so split on the
    # (space-free) Id token instead and whitespace-split what's left.
    return line.split(WINGET_ID, 1)[1].split()


def query_winget():
    # `winget list --id` is read-only -- it never installs or modifies
    # anything. Its table gains an Available column precisely when an update
    # is pending, so this alone answers both "what's installed" and "is
    # there an update", with the same info `winget upgrade` would show.
    #
    # /!\ Deliberately NOT `winget upgrade --id <pkg>`: unlike bare `winget
    # upgrade` (which only lists), scoping it to a specific --id actually
    # PERFORMS that package's upgrade. An earlier version of this script did
    # that on a poll cycle and it silently kicked off a real 7-Zip install
    # attempt (caught only because the UAC prompt had nothing to elevate
    # against in a service context and cancelled itself out).
    out = run(["winget", "list", "--id", WINGET_ID, "--exact",
               "--accept-source-agreements", "--disable-interactivity"],
              timeout=30)
    for line in out.splitlines():
        if WINGET_ID in line:
            cols = columns_after_id(line)
            if len(cols) >= 2:
                version = cols[0]
                update = (f"{cols[1]} available" if len(cols) >= 3
                          else "Up to date")
                return version, update
    return "-", "unknown"


def version_tuple(v):
    parts = []
    for chunk in str(v).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def latest_published():
    """Newest version Docker has actually released, as (version, date)."""
    req = urllib.request.Request(RELEASE_NOTES_URL,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")

    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    hits = RELEASE_RE.findall(text)
    if not hits:
        raise ValueError(
            "no '<version> <date>' headings found on the release notes")
    return max(hits, key=lambda h: version_tuple(h[0]))


def engine_status():
    out = run(["powershell.exe", "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-Command",
               f"(Get-Service -Name '{SERVICE_NAME}' "
               "-ErrorAction SilentlyContinue).Status"],
              timeout=15)
    return out.strip() or "unknown"


def refresh_update():
    version, winget_says = query_winget()
    latest, released = latest_published()

    if version == "-":
        update = "unknown"
    elif version_tuple(latest) > version_tuple(version):
        update = f"{latest} available"
    else:
        update = "Up to date"

    with _lock:
        _cache["version"] = version
        _cache["update"] = update
        _cache["checked"] = datetime.now().strftime("%H:%M")
        _cache["released"] = released
        # Leave this unmapped in services.yaml. Kept because the two sources
        # legitimately disagree for a few days after each Docker release, and
        # when they do this is the field that explains why the tile and
        # `winget upgrade` tell you different things.
        _cache["winget"] = winget_says


def refresh_loop():
    last_update_check = 0.0
    update_interval = UPDATE_CHECK_SECONDS
    while True:
        try:
            engine = engine_status()
            with _lock:
                _cache["engine"] = engine
        except Exception:
            log(f"engine_status failed:\n{traceback.format_exc()}")

        now = time.monotonic()
        if (now - last_update_check >= update_interval
                or last_update_check == 0.0):
            try:
                refresh_update()
                update_interval = UPDATE_CHECK_SECONDS
            except Exception:
                # Come back in minutes, not hours -- otherwise one bad query
                # at logon blanks the tile for the rest of the day.
                update_interval = UPDATE_RETRY_SECONDS
                log(f"refresh_update failed, retrying in "
                    f"{UPDATE_RETRY_SECONDS // 60}min:\n"
                    f"{traceback.format_exc()}")
            last_update_check = now

        time.sleep(LOOP_SECONDS)


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
