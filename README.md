<div align="center">

<img src="assets/hero.svg" width="100%" alt="UnlProxy"/>

**UnlProxy** — an always-fresh egress pool with a healthy IP guarantee.

No docker · no TUN · no admin · one Python file · Linux & Windows

</div>

---

## Setup — 2 answers, done

```bash
python3 warppool.py setup
```

Asks:

1. **How many egress nodes?** (3 is a fine start)
2. **Start the control UI?** (Y)

Everything else is automatic: `usque` is downloaded, WARP nodes register,
egress is measured and deduped. For 9router users, tick "push pools to 9router"
inside the **manage** dialog — or set `"nine_router": {"enabled": true}` once in
`config.json`.

Or install it as a background service in one command:

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/a2z05/UnlProxy/master/install.sh | sh
```

```powershell
# Windows
irm https://raw.githubusercontent.com/a2z05/UnlProxy/master/install.ps1 | iex
```

The installer downloads the app, brings the pool up, and registers a logon task
that runs the rotating daemon.

## `unlp` — one word, everywhere

Both installers put **`unlp`** on your `PATH`, so it works from any shell, any
directory, any terminal:

```bash
unlp               # open the control UI in your browser
unlp cli           # interactive console
unlp status        # one-shot node table
unlp rotate        # rotate now
unlp rotate 18091  # rotate one node
```

- **Linux / macOS** — `~/.local/bin/unlp`. `install.sh` tells you if that is not
  on your `PATH`; add it with `export PATH="$HOME/.local/bin:$PATH"`.
- **Windows** — `unlp.cmd`, written next to your `python.exe` (so it is already
  on `PATH`). Open a **new** terminal after installing.

Every `warppool.py` subcommand works through it: `unlp serve`, `unlp daemon`,
`unlp ensure`, `unlp install-9r`, `unlp config`. `unlp` on its own runs `ui`,
which brings the pool up, starts the control UI and opens the browser.

The app itself lives in `~/.unlproxy` (Linux) or `%LOCALAPPDATA%\UnlProxy`
(Windows). Override with `UNLPROXY_DIR`.

---

## Why

Rate limits and IP bans on anonymous free tiers are a function of your **egress
address**, so the fix is an address that is never the same for long:

> every node = its own WARP registration = its own egress IP.
> Rotation keeps the address moving, and dedupe guarantees nodes never
> collapse onto the same address.

Mechanics follow [ebouther/opencode-unlimited-proxy](https://github.com/ebouther/opencode-unlimited-proxy)
(MIT), re-implemented portable and dependency-free: [usque](https://github.com/Diniboy1123/usque)
runs Cloudflare WARP as a userspace SOCKS5 proxy, so there is nothing to install
beyond Python and curl.

## Transport — MASQUE over TCP 443, not WireGuard over UDP

This is the part that decides whether any of it works.

Plain WARP is WireGuard on **UDP 2408**. On Iranian ISPs (and most censored
networks) that UDP path is blackholed: the handshake initiation leaves, nothing
comes back, and the client retries until it gives up. A relay through a local
v2ray/xray tunnel does not help either — the tunnel carries UDP fine, Cloudflare
still does not answer on 2408.

`usque` speaks the WARP client's **MASQUE** mode (Connect-IP over HTTP/2 on
**TCP 443**) instead. TCP 443 is what every censorship regime has to leave open,
so it connects where WireGuard cannot:

```
curl -x socks5h://127.0.0.1:18090 https://www.cloudflare.com/cdn-cgi/trace
ip=2a09:bac5:41d3:2464::3a0:5b
warp=on
```

Two flags make it behave, both set by UnlProxy for you:

- `--http2` — HTTP/2 over TCP+TLS. The default QUIC/UDP path and the faster
  `l4-socks` variant both hang on a censored network.
- `-d 1.1.1.1` — DNS *inside* the tunnel. usque's default resolvers stall under
  HTTP/2 (each lookup times out at 2 s and the request dies with it).

## Rotation — automatic, and cheap

**Nothing has to click and no cron entry is needed.** `daemon`, `install` and
`serve` all start a background thread that runs one `ensure()` cycle every
**15 seconds**. `ensure()` looks at each node's own clock (`registered_at` +
`rotate_interval_s`) and rotates whatever is due. The pool keeps its addresses
fresh on its own; a cron job or a human pressing *rotate* is only for forcing a
rotation early.

15 s between cycles, not the rotation interval, because one due node's rotation
takes 5–40 s — the cycle has to be able to finish before the next one starts.

**Rotation is two levels, and the cheap one comes first:**

| | cost | what changes | when |
|---|---|---|---|
| **reconnect** | ~5 s | new MASQUE tunnel → usually a new egress address | the default; almost always enough |
| **re-register** | 20–40 s | new WARP device identity → a fresh address | only when the reconnect came back on the same address |

A reconnect on its own usually lands on a *different* egress address, because
the tunnel is new even though the registration is unchanged. That is what makes
30–60 s intervals affordable. If the reconnect returns the same address,
UnlProxy falls back to a full re-register (up to 3 tries for a distinct one).

### Quota is per registration, not per rotation

To be precise, because it is easy to get wrong: **rotating the egress address
does not create quota.** Quota is counted per *registration* — per WARP device
identity — which is exactly why every node runs its own registration: each node
has its own quota bucket. What rotation changes is **which address a
destination sees**, so a rate limit or ban keyed to that address no longer
applies to the next request through that node.

### How long does an IP ban last?

Until the egress address changes — that is, until the next reconnect/rotation of
that node. The ban is attached to the address the destination saw, so a node
that has rotated since is clean. The one case a reconnect does not fix is a ban
on the whole registration or ASN rather than the address; that is exactly when
the re-register fallback (new device identity) steps in.

## How it works

- **rotate** — reconnect the node's tunnel and measure it; if the address did
  not change, re-register (new device identity) instead, with up to 3 tries for
  a distinct address.
- **dedupe** — measure every node's real egress (`cloudflare.com/cdn-cgi/trace`
  through its own socks endpoint). Two nodes sharing an address cost two slots
  for one quota bucket, so the newer one is re-registered; a node that cannot
  get a distinct address is *drained* (stopped) instead of lying in the pool.
- **health** — a node that stopped answering is restarted; registration backs
  off hard after Cloudflare 429/403.
- **publish** — every healthy node becomes a `proxyPool` in
  [9router](https://github.com/a2z05/9router): created, then **tested**
  immediately (existing-but-not-active pools are re-tested), because the router
  reaps pools created-but-never-tested (~2 minutes). The CLI token is derived
  from `~/.9router` on Linux or `%APPDATA%\9router` on Windows.

<p align="center">
<img src="assets/architecture.svg" width="96%" alt="UnlProxy architecture"/>
</p>

## Windows

Same `setup`. Double-click `run.bat` or run `python warppool.py setup` from a
terminal; `init`/`up` download `bin\usque.exe` by themselves.

## HTTP/2 is IPv6-only in practice

Under `--http2` usque answers over **IPv6** (`2a09:bac...`), so egress IPs read as
IPv6 addresses. That is fine for quotas and for 9router, but a destination that
only speaks IPv4 will fail through the tunnel. If you need IPv4 egress, run
Oblivion/`warp-plus` on this box and leave it as the upstream — this pool stays
IPv6.

## Webhook alerts

The pool can post to a webhook when something is actually wrong — a node that
cannot register, a node that fails to rotate, a node drained for having no
distinct address, or the whole pool going dark.

Set it in **manage → webhook** (there is a *send test* button that fires a real
request before you save), or:

```bash
unlp config --webhook https://discord.com/api/webhooks/123/abc
unlp config --no-webhook
```

The body is `{"content": …, "text": …, "level": …, "source": "unlproxy", …}` —
Discord reads `content`, Slack reads `text`, so one URL works for both. Any
endpoint that accepts a JSON `POST` works.

- **level filter** — `warnings and errors` or `errors only`.
- **repeat suppression** — the same message is not sent again for 5 minutes, so
  an alert that repeats every 15-second tick does not spam the channel.
- A failed webhook is logged and ignored: a broken hook never stops a rotation.
- Works out of the box with ntfy, Gotify, Slack, Discord, n8n, or a tiny
  self-hosted receiver.

## Commands

| command             | job |
|---------------------|-----|
| `setup`             | one command: 2 answers, config + usque + nodes + (optional) UI |
| `install`           | one command: config + usque + nodes up + self-driving rotation |
| `init`              | headless setup (no questions) — config, usque, bring nodes up |
| `daemon`            | pool only, no UI, rotating forever (what the installer starts) |
| `ui`                | bring the pool up, start the UI, open it in the browser (`unlp`) |
| `cli`               | interactive console: status / ensure / rotate / nodes / 9r / log |
| `config`            | tune headless: `--nodes N --dns 1.1.1.1 --rotate 300 --9r --webhook URL` |
| `ensure`            | one health + dedupe + rotate-due cycle (cron entrypoint) |
| `up` / `down`       | start / stop every node |
| `rotate [port\|all]` | force a rotation now (reconnect, else re-register) |
| `status`            | JSON table of nodes |
| `serve`             | local UI (dark, auto-refresh, rotate + manage dialog) + rotation |
| `install-9r`        | create + test pool entries in 9router |

The old `scripts/warp_pool_watch.sh` (a 5-minute cron watchdog) still works if
you want one, but it is no longer required — the daemon rotates on its own.

## Configuration

Everything is editable in **manage** in the UI; `config.json` is the fallback.

| key | default | what it does |
|---|---|---|
| `nodes` | `3` | how many egress nodes |
| `base_port` | `18090` | first SOCKS5 port (`:18090`, `:18091`, …) |
| `rotate_interval_s` | `300` | how long a node keeps its address before it rotates |
| `measure_url` | `cloudflare.com/cdn-cgi/trace` | any endpoint echoing `ip=` and `warp=` |
| `usque_dns` | `1.1.1.1` | resolver **inside** the tunnel |
| `usque_bin` | *(auto)* | path to `usque`, defaults to `./bin/usque(.exe)` |
| `ui_bind` / `ui_port` | `127.0.0.1` / `8180` | where the control UI listens (`0.0.0.0` for LAN) |
| `webhook.url` / `.enabled` / `.min_level` | *(off)* / `false` / `warn` | alert webhook |
| `notify_timeout_s` | `10` | webhook request timeout |
| `nine_router.enabled` / `.base_url` / `.pool_prefix` | `false` | 9router publishing |
| `reg_backoff_s` | `[60, 120]` | backoff after a Cloudflare 429/403 |

Two things worth knowing:

- **The UI is local by default.** `ui_bind: 0.0.0.0` exposes it on the LAN, and
  the UI has no authentication — only do that on a network you trust.
- **Changing `base_port` moves the pool.** The old ports are drained (their
  processes stopped), not silently left running.

## Single-writer guard

The daemon, the UI server and a hand-run `unlp ensure` all tick the same pool.
A file lock (`<app>/.tick.lock`) makes exactly one of them rotate at a time —
the others stand down until it is their turn. Without it, two processes rotate
the same node twice and Cloudflare starts rate-limiting the registrations.

## Notes

- A node that cannot distinguish its address stays `drained` until you revive it
  from the UI or rotate it by hand.
- Everything that used to be hand-edited in `config.json` is now in the UI manage
  dialog; the file is a fallback only.
- Configured via `usque_dns` (inside-tunnel resolver, default `1.1.1.1`).
- The rotation interval is editable in the manage dialog (10 s and up). Below
  ~60 s a node mostly reconnects; a full re-register only runs when the
  address did not change.

## Resource footprint

Measured on a live 3-node Windows pool (idle traffic + one chat request through it):

| piece                | RAM    | CPU          |
|----------------------|--------|--------------|
| each usque node      | ~28–29 MB | 0.2–0.3 % of one core |
| UI (`serve`)         | ~24 MB | ~0 %         |
| **3-node pool total**| **~110 MB** | **<1 %** |

Extra nodes are ~28 MB + near-zero CPU each. Registration, trace probes and
9router tests fire only when a rotation is actually due; in between a node
holds its HTTP/2 keepalive on the MASQUE tunnel (~a few KB/s).

## License

MIT.
