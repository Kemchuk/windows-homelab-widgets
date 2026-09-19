# The widgets

Each directory is one self-contained service. No shared library, deliberately —
they are small enough that a shared module would cost more in coupling than it
saves in lines, and they need to fail independently.

## Wiring one up

Run it:

```powershell
python .\docker-desktop\server.py
```

Check it:

```powershell
Invoke-RestMethod http://127.0.0.1:9991/
```

Point Homepage at it in `services.yaml`:

```yaml
- Docker Desktop:
    icon: docker.png
    widget:
      type: customapi
      url: http://host.docker.internal:9991/
      refreshInterval: 60000
      mappings:
        - field: version
          label: Version
        - field: update
          label: Update
        - field: engine
          label: Engine
```

`host.docker.internal` matters if Homepage is itself a container — it cannot
reach `127.0.0.1` on the host. On Docker Desktop for Windows this resolves; on
other setups use the host's LAN address.

Every service returns **flat** JSON with pre-formatted string values, so a
mapping is always one `field:` with no traversal.

## Keeping them running

`loop.ps1` restarts a service if it exits. Point a scheduled task at it, set to
run **at logon**, with `-WindowStyle Hidden`.

Hidden is not cosmetic: without it every logon opens a PowerShell window per
service. Expect roughly 20 seconds of launch cost before a task's process is
actually serving.

## Ports

They sit in `9991`–`9999`. Pick your own range, but pick it deliberately — see
the double-bind warning in the root README. That range fills up faster than you
expect.

## Per-service notes

**`claude-usage` (9997)** — shells out to `ccusage`. The npm install must be
**local to the machine the task runs as**; a global install performed inside a
different session may be invisible to the scheduled task.

**`docker-desktop` (9991)** — reports installed version, whether a newer release
exists, and engine state. The published "latest" version lags what package
managers offer, so treat a disagreement as expected rather than a bug.

**`kometa` (9994)** — tails Kometa's log rather than calling an API, because
Kometa does not expose one.

**`recyclarr` (9993)** — same approach, same reason.

**`technitium-dhcp` (9996)** — needs an API token. Set `TECHNITIUM_TOKEN` in the
environment and `TECHNITIUM_URL` if the server is not on the default local
address. Reports real scope utilisation, which the built-in widget does not.

**`top-processes` (9999)** — top 5 by CPU via the
`Win32_PerfFormattedData_PerfProc_Process` counter, not `psutil`, which was too
slow on the hardware this was written for to finish inside a refresh interval.
The cost is billed to `WmiPrvSE`, not to this process, so do not trust a
process list when judging how expensive this widget is. Keep the sample
interval at 60s.

**`traefik-metrics` (9995)** — scrapes Traefik's Prometheus endpoint and
flattens it. Traefik must have `metrics.prometheus` enabled.
