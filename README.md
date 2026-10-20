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

---

## Why

Rate limits and IP bans on anonymous free tiers are a function of your **egress
address**, so the fix is an address that is never the same for long:

> every node = its own WARP registration = its own egress IP.
> Rotation is a fresh registration, and dedupe guarantees nodes never
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

## How it works

- **rotate** — re-register a node (new device identity, new USQUE config). If
  Cloudflare hands back the same address, up to 3 retries until it changes.
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

## Commands

| command             | job |
|---------------------|-----|
| `setup`             | one command: 2 answers, config + usque + nodes + (optional) UI |
| `init`              | headless setup (no questions) — config, usque, bring nodes up |
| `ensure`            | health + dedupe + rotations due + 9router sync (cron entry) |
| `up` / `down`       | start / stop every node |
| `rotate [port|all]` | force a fresh registration now (up to 3 tries for a distinct address) |
| `status`            | JSON table of nodes |
| `cron`-watch        | `scripts/warp_pool_watch.sh` every 5 min: ensure + alert on dead pool |
| `serve`             | local UI (dark, auto-refresh, rotate + manage dialog) |
| `install-9r`        | create + test pool entries in 9router |

## Notes

- A node that cannot distinguish its address stays `drained` until you revive it
  from the UI or rotate it by hand.
- Everything that used to be hand-edited in `config.json` is now in the UI manage
  dialog; the file is a fallback only.
- Configured via `usque_dns` (inside-tunnel resolver, default `1.1.1.1`).

## Resource footprint

Measured on a live 3-node Windows pool (idle traffic + one chat request through it):

| piece                | RAM    | CPU          |
|----------------------|--------|--------------|
| each usque node      | ~28–29 MB | 0.2–0.3 % of one core |
| UI (`serve`)         | ~24 MB | ~0 %         |
| **3-node pool total**| **~110 MB** | **<1 %** |

Extra nodes are ~28 MB + near-zero CPU each. Registration, trace probes and
9router tests fire only on the 5-minute tick; between ticks a node holds its
HTTP/2 keepalive on the MASQUE tunnel (~a few KB/s).

## License

MIT.
