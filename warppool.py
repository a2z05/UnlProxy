#!/usr/bin/env python3
"""warppool — per-node Cloudflare WARP egress pool with healthy-IP rotation.

Built on the mechanics of ebouther/opencode-unlimited-proxy (MIT), re-implemented
portable and dependency-free:

* every node = its own WARP registration = its own egress address
* rotation = reconnect the tunnel first (cheap, ~5s, usually a new address);
  re-register (fresh device, 20-40s) only when the address did not change
* dedupe = measure each node's REAL egress; colliding nodes are re-registered,
  nodes that cannot be given a distinct address are drained (stopped)
* health = a dead node is restarted; a drained node stays down until revived

The tunnel is Cloudflare WARP over MASQUE (usque, HTTP/2 over TCP 443), not
WireGuard-over-UDP 2408: UDP to Cloudflare is blackholed on Iranian ISPs, so a
WireGuard handshake never comes back — TCP/443 does. Per-node distinct egress
comes from one usque registration each.

Rotation is self-driving: `daemon`, `install` and `serve` all run a background
tick that calls ensure() every 15s, so no cron entry is needed.

Runs on Linux and Windows with stdlib only (usque does the tunnel in userspace,
SOCKS5 mode — no admin, no TUN, no Docker).

Commands:
warppool.py setup           2 answers, config + usque + nodes + (optional) UI
warppool.py cli             interactive console (status/rotate/nodes/9r), no browser
warppool.py init            write config.json + download usque if missing
warppool.py install         one command: config + usque + nodes up + rotating daemon
warppool.py daemon          pool only, rotating daemon (no UI)
warppool.py config          tune headless: --nodes --dns --rotate --9r/--no-9r
warppool.py up / down       start / stop every node
warppool.py ensure          health + dedupe + rotate-due (one cycle, cron entrypoint)
warppool.py status          print table + JSON summary
warppool.py rotate [p|all]  force a rotation now (reconnect, else re-register)
warppool.py serve           UI on ui_port (default 127.0.0.1:8180) + rotating tick
warppool.py install-9r      push nodes into 9router's proxy pools (CLI token)
"""
import json, os, re, signal, socket, ssl, subprocess, sys, time, urllib.request, urllib.error, hashlib, base64, random, string, threading, webbrowser, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("WARPPOOL_CONFIG", os.path.join(HERE, "config.json"))
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "events.log")
WIN = os.name == "nt"

DEFAULTS = {
    "nodes": 3,
    "base_port": 18090,
    "rotate_interval_s": 300,     # same cadence as the reference project
    "measure_url": "https://www.cloudflare.com/cdn-cgi/trace",
    "usque_bin": "",              # auto: ./bin/usque(.exe)
    "usque_dns": "1.1.1.1",       # inside-tunnel resolver; the defaults stall under HTTP/2
    "ui_bind": "127.0.0.1",
    "ui_port": 8180,
    "nine_router": {"enabled": False, "base_url": "http://127.0.0.1:20128",
                    "cli_token": "", "pool_prefix": "warp-"},
    "reg_backoff_s": [60, 120],   # CF 429/403: long backoff (their own lesson)
    "webhook": {"enabled": False, "url": "", "min_level": "warn"},  # warn | error
    "notify_timeout_s": 10,
}

STALE_MEASURE_S = 150
VERSION = "2.2.0"
REPO = "a2z05/UnlProxy"
RAW = f"https://raw.githubusercontent.com/{REPO}/master"
SELF_FILES = ("warppool.py", "ui.html", "unlp", "unlp.cmd", "install.sh", "install.ps1",
              "config.example.json")

# one writer at a time: the tick thread, the UI buttons and the CLI all touch
# state.json, and interleaving them corrupts node state (pid/registered_at)
STATE_LOCK = threading.RLock()


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cfg.update(json.load(open(CONFIG_PATH)))
    return cfg

def save_cfg(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w"), indent=1)

def load_state():
    try: return json.load(open(STATE_PATH))
    except Exception: return {"nodes": {}}

def _events():
    try:
        with open(LOG_PATH, encoding="utf-8") as f: return [l for l in f.read().splitlines() if l.strip()][-400:]
    except OSError: return []

def save_state(st):
    json.dump(st, open(STATE_PATH, "w"), indent=1)

# ---------------------------------------------------------------- io helpers
def opener(cfg, timeout=20):
    return urllib.request.build_opener(), timeout

def http_json(cfg, url, data=None, headers=None, method=None, timeout=20):
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"),
                                 headers={"User-Agent": "okhttp/3.12.1",
                                          "Content-Type": "application/json", **(headers or {})})
    op, to = opener(cfg, timeout)
    with op.open(req, timeout=to) as r:
        return r.status, json.loads(r.read() or b"{}")

def fetch_text(cfg, url, timeout=20):
    op, to = opener(cfg, timeout)
    with op.open(url, timeout=to) as r:
        return r.read().decode(errors="replace")

_LEVELS = {"info": 0, "warn": 1, "error": 2}
_notify_last = {}

def notify(cfg, text, level="warn", dedupe_s=300):
    """Fire the configured webhook. Never raises: a broken hook must not stop a
    rotation. Repeats of the same text are throttled — an alert that fires every
    15s tick gets muted by whoever is reading it."""
    wh = (cfg or {}).get("webhook") or {}
    url = (wh.get("url") or "").strip()
    if not url or not wh.get("enabled"): return False
    if _LEVELS.get(level, 1) < _LEVELS.get(wh.get("min_level") or "warn", 1): return False
    key = text[:200]
    now = time.time()
    if now - _notify_last.get(key, 0) < dedupe_s: return False
    _notify_last[key] = now
    payload = {"text": text, "content": text, "level": level,   # text/content: Slack & Discord both
               "source": "unlproxy", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "host": socket.gethostname()}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.build_opener().open(req, timeout=int(cfg.get("notify_timeout_s") or 10)) as r:
            log(f"webhook {level}: HTTP {r.status}")
        return True
    except Exception as e:
        log(f"webhook {level} failed: {e}")
        return False

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f: f.write(line + chr(10))
    except OSError:
        pass

# ---------------------------------------------------------------- usque (WARP over MASQUE)
# WireGuard on UDP 2408 is blackholed on Iranian ISPs (handshake never returns);
# usque carries WARP in MASQUE over TCP 443 via --http2. One registration per node.
def ubin(cfg):
    return cfg.get("usque_bin") or os.path.join(HERE, "bin", "usque.exe" if WIN else "usque")

def download_usque(cfg):
    if os.path.exists(ubin(cfg)):
        return ubin(cfg)
    plat = "windows_amd64" if WIN else "linux_amd64"
    _, rel = http_json(cfg, "https://api.github.com/repos/Diniboy1123/usque/releases/latest")
    asset = next(a for a in rel["assets"] if f"_{plat}.zip" in a["name"])
    os.makedirs(os.path.dirname(ubin(cfg)), exist_ok=True)
    import zipfile, io
    with zipfile.ZipFile(io.BytesIO(fetch_text_cfg(cfg, asset["browser_download_url"]))) as z:
        m = next(n for n in z.namelist() if os.path.basename(n) in ("usque.exe", "usque"))
        open(ubin(cfg), "wb").write(z.read(m))
    if not WIN:
        os.chmod(ubin(cfg), 0o755)
    return ubin(cfg)

def latest_version(cfg, timeout=15):
    """Newest release tag on GitHub, without the leading v. Raises on network failure."""
    st, rel = http_json(cfg, f"https://api.github.com/repos/{REPO}/releases/latest",
                        headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    if st != 200 or not rel.get("tag_name"):
        raise OSError(f"GitHub returned HTTP {st}")
    return rel["tag_name"].lstrip("v"), rel.get("html_url") or ""

def update_check(cfg):
    """Compare this copy against the newest release. Never raises."""
    out = {"current": VERSION, "latest": None, "url": "", "update": False, "error": None}
    try:
        out["latest"], out["url"] = latest_version(cfg)
        out["update"] = out["latest"] != VERSION
    except Exception as e:
        out["error"] = str(e)
    return out

def update(cfg):
    """Download the release files over this copy. config.json, state.json and
    nodes/ are untouched: those are the user's, not the program's."""
    cur, url = latest_version(cfg)
    if cur == VERSION:
        return {"updated": False, "version": VERSION, "reason": "already current"}
    here = HERE
    saved = {}
    try:
        for name in SELF_FILES:
            try:
                data = fetch_text_cfg(cfg, f"{RAW}/{name}")
            except Exception:
                continue          # a file this release does not ship
            dest = os.path.join(here, name)
            if os.path.exists(dest):
                with open(dest, "rb") as f: saved[name] = f.read()
            tmp = dest + ".new"
            with open(tmp, "wb") as f: f.write(data)
            os.replace(tmp, dest)   # never leave a half-written warppool.py
        log(f"updated {VERSION} -> {cur}")
        return {"updated": True, "from": VERSION, "to": cur, "url": url}
    except Exception as e:
        for name, data in saved.items():          # put back what we replaced
            try:
                with open(os.path.join(here, name), "wb") as f: f.write(data)
            except OSError: pass
        log(f"update failed: {e}")
        return {"updated": False, "version": VERSION, "error": str(e)}

def fetch_text_cfg(cfg, url, timeout=60):
    op, to = opener(cfg, timeout)
    with op.open(url, timeout=to) as r:
        return r.read()

def conf_path(cfg, port):
    """Each node is a directory: usque keeps its config.json in the cwd unless -c says otherwise."""
    return os.path.join(HERE, "nodes", f"node-{port}", "config.json")

def register(cfg, port):
    """Fresh WARP device -> its own egress address. usque owns the whole dance:\nregistration, HTTPS enrol, MASQUE switch. Backs off hard on 429/403."""
    os.makedirs(os.path.dirname(conf_path(cfg, port)), exist_ok=True)
    # register into a temp path: usque asks "overwrite? (y/n)" when the config
    # exists, and a prompt-less subprocess dies on the EOF
    tmp = conf_path(cfg, port) + ".new"
    for i in range(3):
        r = subprocess.run([ubin(cfg), "register", "-a", "-c", tmp],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, conf_path(cfg, port))
            return {"id": os.path.basename(os.path.dirname(conf_path(cfg, port))), "registered_at": time.time()}
        err = (r.stderr or r.stdout).strip().splitlines()
        log(f"register {port}: {(err[-1] if err else 'no output')[:160]}")
        if any(x in (r.stdout + r.stderr) for x in ("429", "403", "rate")):
            wait = random.choice(cfg["reg_backoff_s"]) + random.randint(0, 60)
            log(f"register {port}: CF rate limit - backoff {wait}s")
            time.sleep(min(wait, 90))
        else:
            time.sleep(4 + i * 6)
    return None

def sock_open(port, host="127.0.0.1", timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout): return True
    except OSError:
        return False

def spawn(cfg, port, st=None):
    # truncate node log each spawn: it only captures last crash, not history
    lp = os.path.join(HERE, "nodes", f"node-{port}.log")
    try:
        if os.path.exists(lp) and os.path.getsize(lp) > 200_000:
            with open(lp, "rb") as f: tail = f.read()[-50_000:]
            with open(lp, "wb") as f: f.write(tail)
    except OSError:
        pass
    logf = open(lp, "ab")
    kw = {"stdout": logf, "stderr": logf}
    if not WIN: kw["start_new_session"] = True
    # --http2: MASQUE over TCP+TLS/443. UDP/QUIC to Cloudflare is blocked on these
    # ISPs, so the default QUIC path and l4-socks both hang.
    p = subprocess.Popen([ubin(cfg), "socks", "--http2",
                          "-b", "127.0.0.1", "-p", str(port), "-d", cfg.get("usque_dns") or "1.1.1.1",
                          "-c", conf_path(cfg, port)], **kw)
    if st is None: st = load_state()
    st.setdefault("nodes", {}).setdefault(str(port), {"registered_at": 0, "drained": False})
    st["nodes"][str(port)]["pid"] = p.pid
    save_state(st)
    return p.pid

def _find_proc(port):
    """A slot whose stored pid is stale still owns a live usque - find it by its\nconfig path; register/rotate lie otherwise (bind: in use)."""
    if WIN: return _find_proc_win(port)
    needle = os.path.join("node-%s" % port, "config.json").encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit(): continue
        try:
            if needle in open(f"/proc/{pid}/cmdline", "rb").read(): return int(pid)
        except OSError: pass
    return None

def _find_proc_win(port):
    """wmic is gone from Windows 11; CIM is the supported way to read command lines."""
    needle = os.path.join("node-%s" % port, "config.json")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_Process -Filter \"Name='usque.exe'\").ProcessId,"
                              "(Get-CimInstance Win32_Process -Filter \"Name='usque.exe'\").CommandLine"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    # pairs come out as all PIDs then all command lines
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    pids  = [l for l in lines if l.isdigit()]
    cmds  = [l for l in lines if not l.isdigit()]
    for pid, cmd in zip(pids, cmds):
        if needle in cmd: return int(pid)
    return None

def stop_node(port, st=None):
    if st is None: st = load_state()
    n = st.get("nodes", {}).get(str(port)) or {}
    p = n.get("pid") or _find_proc(port)
    if p:
        try:
            if WIN:
                subprocess.run(["taskkill", "/PID", str(p), "/F", "/T"], capture_output=True)
            else:
                os.kill(int(p), signal.SIGTERM)
                time.sleep(0.6)
                try: os.kill(int(p), 0)
                except OSError: pass
                else: os.kill(int(p), signal.SIGKILL)
                for _ in range(20):   # wait for the socks port to actually free
                    try:
                        socket.create_connection(("127.0.0.1", int(port)), timeout=0.5).close()
                    except OSError:
                        break
                    time.sleep(0.3)
        except Exception:
            pass
    n.pop("pid", None)
    time.sleep(0.3)

def adopt_pid(cfg, port, st):
    """Attach a pid to a node whose socks port answers but whose pid was lost."""
    n = st["nodes"][str(port)]
    if not n.get("pid"):
        p = _find_proc(port)
        if p: n["pid"] = p

def measure(cfg, port):
    try:
        return _measure_via_socks(cfg, port)
    except Exception as e:
        return {"error": str(e)}

def _measure_via_socks(cfg, port):
    # stdlib has no socks client; use curl (present on the box and on most systems)
    import shutil
    if shutil.which("curl") is None:
        return {"error": "curl missing"}
    url = cfg["measure_url"] + "?r=" + str(random.randint(1, 1 << 30))
    # the MASQUE tunnel takes ~1s to establish on a cold node; 15s is too tight
    r = subprocess.run(["curl", "-sS", "-m", "30", "-x", f"socks5h://127.0.0.1:{port}", url],
                       capture_output=True, text=True)
    out = dict(re.findall(r"^([a-z]+)=(.*)$", r.stdout, re.M))
    if "ip" not in out: return {"error": (r.stderr or r.stdout or "no trace")[:80]}
    return {"ip": out.get("ip"), "warp": out.get("warp") == "on", "colo": out.get("colo")}

# ---------------------------------------------------------------- rotation

def rotate_ports(cfg, q="all"):
    """Rotate one port or every port, under the state lock. Returns the list of
    ports that came back with an address."""
    with STATE_LOCK:
        st = load_state().get("nodes", {})
        ports = [k for k in st] if q == "all" else [q]
        done = []
        for p in ports:
            if st.get(p, {}).get("drained"): continue
            wrapper = {"nodes": st}
            old_ip = (st[p].get("measure") or {}).get("ip")
            got, mode = rotate_node(cfg, int(p), wrapper, old_ip)
            if got:
                st[p]["measure"] = {"ip": got, "warp": True}
                st[p]["measured_at"] = time.time(); st[p]["rotated_at"] = time.time()
                log(f"{p} rotated ({mode}) -> {got}")
                done.append(p)
            else:
                log(f"{p} rotate FAILED")
                notify(cfg, f"node {p} failed to rotate", "error")
        save_state(st)
        return done

def rotate_node(cfg, port, st, old_ip=None):
    """Reconnect first, re-register only if that did not change the address.

    A usque reconnect alone usually lands on a new egress address (new tunnel,
    same registration), and costs ~5s; a full register() costs ~20-40s and burns
    a new device. Short intervals are only affordable because of this order.
    Returns (ip, "reconnect" | "re-register") or (None, mode)."""
    nodes = st.setdefault("nodes", {})
    n = nodes.setdefault(str(port), {"registered_at": 0, "drained": False})
    stop_node(port, st)
    if os.path.exists(conf_path(cfg, port)):
        spawn(cfg, port, st); time.sleep(3)
        ip = measure(cfg, port).get("ip") if sock_open(port) else None
        if ip and ip != old_ip:
            # reset the clock here: the due check reads registered_at, so leaving
            # it alone would make this node "due" again on every single cycle
            n["registered_at"] = time.time()
            return ip, "reconnect"
        # reconnect came back on the same address (or not at all)
        stop_node(port, st)
    r = register(cfg, port)
    if not r:
        return None, "re-register"
    n.update(r); spawn(cfg, port, st); time.sleep(3)
    got = measure(cfg, port).get("ip") if sock_open(port) else None
    tries = 1
    while old_ip and got == old_ip and tries < 3:
        # CF can hand the same colo address back; we want a different one
        stop_node(port, st); r = register(cfg, port)
        if not r: break
        n.update(r); spawn(cfg, port, st); time.sleep(3); tries += 1
        got = measure(cfg, port).get("ip") if sock_open(port) else None
    return got, "re-register"

# ---------------------------------------------------------------- ensure cycle
def ensure(cfg):
    with STATE_LOCK:
        return _ensure(cfg)

def _ensure(cfg):
    st = load_state()
    nodes = st.setdefault("nodes", {})
    changed = []
    # 0) adopt/repair node slots
    for i in range(cfg["nodes"]):
        port = str(cfg["base_port"] + i)
        nodes.setdefault(port, {"registered_at": 0, "drained": False})
    save_state(st)  # persist slots before any spawn() re-reads state.json

    # 1) health: start anything alive-worthy that is not running
    for port, n in list(nodes.items()):
        if n.get("drained"): continue
        if not sock_open(int(port)):
            if not os.path.exists(conf_path(cfg, int(port))):
                r = register(cfg, int(port))
                if not r:
                    log(f"{port}: registration failed")
                    notify(cfg, f"node {port} could not register (Cloudflare refused)", "error")
                    continue
                n.update(r); changed.append(f"{port} registered")
            spawn(cfg, port, st)
            time.sleep(3)   # usque needs a moment to bind its socks listener
            if sock_open(int(port)): changed.append(f"{port} started")
            else: log(f"{port}: usque did not open its socks port")
        elif not nodes[port].get("pid"):
            adopt_pid(cfg, port, st)

    # 2) measure + dedupe
    # measure EVERY live node fresh right here: dedupe must act on current
    # egress, not the last cycle's measurement (a rotated node's old IP made
    # the next run re-register it again in a loop)
    ips = {}
    for port, n in list(nodes.items()):
        if n.get("drained"): continue
        if not sock_open(int(port)): continue
        m = measure(cfg, int(port))
        n["measure"] = m; n["measured_at"] = time.time()
        ip = m.get("ip")
        if m.get("warp") is False and ip:
            # usable but not WARP — keep, note it
            n["note"] = "egress not warp=on"
        if ip: ips.setdefault(ip, []).append(port)
    for ip, ports in ips.items():
        if len(ports) < 2: continue
        # keep the oldest, re-register the rest (their address collides)
        ports.sort(key=lambda p: nodes[p].get("registered_at", 0))
        for p in ports[1:]:
            log(f"dedupe: {p} shares {ip} — re-registering")
            stop_node(p, st)
            r = register(cfg, int(p))
            if r:
                nodes[p].update(r)
                spawn(cfg, int(p), st); time.sleep(3)
                got = measure(cfg, int(p)).get("ip") if sock_open(int(p)) else None
                nodes[p]["measure"] = {"ip": got, "warp": True} if got else {}
                nodes[p]["measured_at"] = time.time()
                changed.append(f"{p} re-registered (dedupe)")
            else:
                nodes[p]["drained"] = True; changed.append(f"{p} DRAINED (no distinct address)")
                notify(cfg, f"node {p} drained: no distinct egress address", "error")

    # 3) rotate due (skipped while paused: the nodes stay up and healthy, they
    # just keep their current address)
    now = time.time()
    if not st.get("paused"):
        for port, n in list(nodes.items()):
            if n.get("drained"): continue
            if now - (n.get("registered_at") or 0) < cfg["rotate_interval_s"]: continue
            log(f"{port}: rotation due")
            old_ip = (n.get("measure") or {}).get("ip")
            got, mode = rotate_node(cfg, int(port), st, old_ip)
            if got:
                n["measure"] = {"ip": got, "warp": True}
                n["measured_at"] = time.time()
                n["rotated_at"] = time.time()
                changed.append(f"{port} rotated ({mode})" + (f" -> {got}" if got else ""))
            else:
                n["registered_at"] = now - cfg["rotate_interval_s"] + 60  # retry next cycle

    save_state(st)
    alive = sum(1 for p in nodes if not nodes[p].get("drained") and sock_open(int(p)))
    drained = sum(1 for p in nodes if nodes[p].get("drained"))
    distinct = {n.get("measure", {}).get("ip") for n in nodes.values()
                if not n.get("drained") and n.get("measure", {}).get("ip")}
    if cfg.get("nine_router", {}).get("enabled"):
        # publish here, not in the caller: every cycle (up / ensure / rotate) must
        # leave 9router's pools created AND tested, or it reaps them
        try: install_9r(cfg)
        except Exception as e: log(f"9r install failed: {e}")
    if alive == 0 and nodes:
        notify(cfg, "pool has no egress: every node is down", "error")
    return {"alive": alive, "drained": drained, "distinct_ips": len(distinct),
            "paused": bool(st.get("paused")), "changed": changed}

def status(cfg):
    st = load_state().get("nodes", {})
    rows = []
    now = time.time()
    for port in sorted(st, key=int):
        n = st[port]
        m = n.get("measure") or {}
        # a measure older than the rotation interval is stale — say so, not show it as current
        if m.get("ip") and (now - (n.get("measured_at") or 0)) > STALE_MEASURE_S:
            m = {"ip": m["ip"] + "(stale)", "warp": m.get("warp")}
        rows.append({"port": int(port),
                     "state": "drained" if n.get("drained") else ("up" if sock_open(int(port)) else "down"),
                     "ip": m.get("ip"), "warp": m.get("warp"),
                     "age_min": round((time.time() - (n.get("registered_at") or 0)) / 60),
                     "pid": n.get("pid")})
    return rows

# ---------------------------------------------------------------- 9router
def install_9r(cfg):
    n9 = cfg["nine_router"]
    if not n9.get("cli_token"):
        # derive exactly like the box's documented 999 trigger does
        base = None
        for cand in ("/opt/data/.9router", os.path.expanduser("~/.9router"),
                     os.path.join(os.environ.get("APPDATA") or "", "9router")):
            if os.path.exists(os.path.join(cand, "machine-id")): base = cand; break
        if base:
            n9["cli_token"] = hashlib.sha256((open(os.path.join(base, "machine-id")).read().strip() +
                "9r-cli-auth" + open(os.path.join(base, "auth", "cli-secret")).read().strip()
                ).encode()).hexdigest()[:16]
            save_cfg(cfg)
    hdr = {"x-9r-cli-token": n9["cli_token"], "Content-Type": "application/json"}
    base = n9["base_url"]
    st, d = http_json(cfg, base + "/api/proxy-pools", headers=hdr, timeout=10)
    pools = d.get("proxyPools") or []
    by_url = {p.get("proxyUrl"): p for p in pools if p.get("proxyUrl")}
    created, retested = [], []
    for row in status(cfg):
        if row["state"] != "up": continue
        url = f"socks5://127.0.0.1:{row['port']}"
        old = by_url.get(url)
        if old:
            # a pool for this node already exists — retest it: 9router reaps pools
            # left untested, and a stale one still points at a dead node
            pid = old.get("id")
            if pid and old.get("testStatus") != "active":
                try:
                    http_json(cfg, f"{base}/api/proxy-pools/{pid}/test", {}, headers=hdr, timeout=30)
                    retested.append(url)
                except Exception as e:
                    log(f"9r test {url}: {e}")
            continue
        st2, res = http_json(cfg, base + "/api/proxy-pools",
                             {"name": f"{n9['pool_prefix']}{row['port']}", "proxyUrl": url,
                              "noProxy": "", "isActive": True, "strictProxy": False, "type": "http"},
                             headers=hdr, timeout=15)
        if st2 == 201:
            pid = (res.get("proxyPool") or {}).get("id")
            if pid:  # 9router prunes pools ~2min unless a client test marks them active
                http_json(cfg, f"{base}/api/proxy-pools/{pid}/test", {}, headers=hdr, timeout=30)
            created.append(url)
        else: log(f"9r create {url}: HTTP {st2}")
    if created or retested:   # a quiet "0 created" every tick drowns the event log
        log(f"9router: {len(created)} created" + (f": {created}" if created else "") +
            (f", {len(retested)} retested" if retested else ""))
    return created

# ---------------------------------------------------------------- UI
INDEX = """<!doctype html><meta charset=utf-8><title>warp-pool</title>\n<style>body{background:#0b0d12;color:#dfe3ea;font:14px/1.5 ui-monospace,Consolas,monospace;padding:24px}\nh1{color:#ff6b57;font-size:20px}table{border-collapse:collapse;margin-top:14px;min-width:640px}\nth,td{border:1px solid #232838;padding:6px 12px;text-align:left}th{color:#8a93a8}\n.up{color:#38d97a}.down{color:#ff5c5c}.drained{color:#ffb020}\nbutton{background:#1a2030;color:#dfe3ea;border:1px solid #2a3350;border-radius:6px;padding:5px 14px;cursor:pointer}\nbutton:hover{background:#242d47}#j{white-space:pre;margin-top:18px;color:#9fb0d0;font-size:12px}</style>\n<h1>warp-pool — healthy egress rotation</h1><div id=t>loading…</div>\n<div style="margin-top:14px"><button onclick="cmd('ensure')">run ensure</button>\n<button onclick="cmd('rotate?port=all')">rotate all</button></div><pre id=j></pre>\n<script>\nasync function load(){const r=await (await fetch('api/status')).json();\nconst h=['<table><tr><th>port</th><th>state</th><th>egress ip</th><th>warp</th><th>age</th><th>pid</th><th></th></tr>'];\nfor(const n of r){h.push(`<tr><td>${n.port}</td><td class=${n.state}>${n.state}</td><td>${n.ip||'—'}</td>\n<td>${n.warp?'on':'—'}</td><td>${n.age_min}m</td><td>${n.pid||''}</td>\n<td><button onclick="cmd('rotate?port=${n.port}')">rotate</button></td></tr>`)}\ndocument.getElementById('t').innerHTML=h.join('')+'</table>';load2()}\nasync function load2(){const r=await (await fetch('api/log')).json();document.getElementById('j').textContent=r.changed.join('\\n')||'no changes yet'}\nasync function cmd(q){const r=await (await fetch('api/'+q,{method:'POST'})).json();document.getElementById('j').textContent=JSON.stringify(r,null,1);load()}\nload();setInterval(load,15000)</script>"""


def _public_cfg(cfg):
    """Config the UI needs, secrets omitted."""
    n9 = cfg.get("nine_router") or {}
    wh = cfg.get("webhook") or {}
    return {"nodes": cfg.get("nodes"), "rotate_interval_s": cfg.get("rotate_interval_s"),
            "base_port": cfg.get("base_port"),
            "usque_dns": cfg.get("usque_dns"), "measure_url": cfg.get("measure_url"),
            "webhook": {"enabled": bool(wh.get("enabled")), "url": wh.get("url") or "",
                        "min_level": wh.get("min_level") or "warn"},
            "nine_router": {"enabled": bool(n9.get("enabled")), "base_url": n9.get("base_url"),
                            "pool_prefix": n9.get("pool_prefix") or "warp-",
                            "connected": bool(n9.get("cli_token"))},
            "ui_bind": cfg.get("ui_bind"), "ui_port": cfg.get("ui_port"),
            "path": HERE}

def tick_loop(interval=15):
    """Self-driving rotation: nobody else calls ensure() when there is no cron,
    so without this the pool never rotates. 15s between cycles because one due
    node's rotation (reconnect, or register+spawn+measure+retries) takes 5-40s."""
    time.sleep(2)   # let the first window load before the first cycle
    while True:
        # The lock is taken and dropped inside one cycle, so two processes take
        # turns. That is fine and silent; only a rotation is worth a log line.
        lock = None
        try:
            lock = _tick_holder()
            if lock is not None:
                r = ensure(load_cfg())
                if r.get("changed"): log(f"tick: {', '.join(r['changed'])}")
        except Exception as e:
            log(f"tick: {e}")
        finally:
            if lock is not None:
                try: lock.close()
                except OSError: pass
        time.sleep(interval)

def _tick_holder():
    """Cross-process single-writer guard for the rotation tick.

    The daemon, the UI server and a hand-run `ensure` all tick the same pool.
    Without this they rotate the same node twice and Cloudflare starts
    rate-limiting the registrations. Returns the open lock file, or None when
    somebody else holds the lock (the caller stands down for that cycle)."""
    f = open(os.path.join(HERE, ".tick.lock"), "a+b")
    try:
        if WIN:
            import msvcrt
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"0"); f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close(); return None
    return f

def serve(cfg):
    import http.server, threading
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            # route on the path only: a cache-busting ?v=2 on the index URL used
            # to miss every branch and return "{}" instead of the page
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                try:
                    self._send(200, open(os.path.join(HERE, "ui.html"), "rb").read(), "text/html; charset=utf-8")
                except OSError:
                    self._send(200, INDEX, "text/html; charset=utf-8")
            elif route.startswith("/api/status"): self._send(200, json.dumps(status(load_cfg())))
            elif route.startswith("/api/log"): self._send(200, json.dumps({"changed": _events()}))
            elif route.startswith("/api/config"): self._send(200, json.dumps(_public_cfg(load_cfg())))
            elif route.startswith("/api/version"):
                v = update_check(load_cfg())
                v["paused"] = bool(load_state().get("paused"))
                self._send(200, json.dumps(v))
            elif route.endswith(".svg") or route.endswith(".ico"):
                self._send(404, "{}")
            else: self._send(404, "{}")
        def do_POST(self):
            c = load_cfg()
            if self.path.startswith("/api/ensure"):
                r = ensure(c); self._send(200, json.dumps(r))
            elif self.path.startswith("/api/rotate"):
                q = self.path.split("port=")[-1] if "port=" in self.path else "all"
                done = rotate_ports(c, q)
                self._send(200, json.dumps({"rotated": done}))
            elif self.path.startswith("/api/settings"):  # settable at runtime: IR mode, node count, rotation
                body = self._body(); out = {}
                if body:
                    if body.get("usque_dns") is not None:
                        c["usque_dns"] = body["usque_dns"].strip() or "1.1.1.1"
                        out["usque_dns"] = c["usque_dns"]
                    if body.get("base_port"):
                        # the pool is keyed by port: moving it drains the old slots
                        # rather than silently leaving orphan usque processes live
                        new_base = max(1024, min(65000, int(body["base_port"])))
                        if new_base != c["base_port"]:
                            with STATE_LOCK:
                                stn = load_state(); nodes = stn.setdefault("nodes", {})
                                for p in list(nodes):
                                    stop_node(p, {"nodes": nodes})
                                    nodes[p]["drained"] = True
                                save_state(stn)
                            c["base_port"] = new_base
                            out["base_port"] = new_base
                    if body.get("nodes"):
                        c["nodes"] = max(0, int(body["nodes"]))
                        with STATE_LOCK:
                            stn = load_state()
                            want = c["nodes"]
                            nodes = stn.setdefault("nodes", {})
                            while len([n for n in nodes if not nodes[n].get("drained")]) > want:
                                # pick newest slot, stop+drain rather than delete
                                free = [p for p in nodes if not nodes[p].get("drained")]
                                p = max(free, key=lambda x: nodes[x].get("registered_at", 0))
                                stop_node(p, {"nodes": nodes}); nodes[p]["drained"] = True
                            save_state(stn)
                        ensure(c)   # runs under the same lock
                        out["nodes"] = c["nodes"]
                    if body.get("nine_router_enabled") is not None:
                        c["nine_router"]["enabled"] = bool(body["nine_router_enabled"])
                        out["nine_router_enabled"] = c["nine_router"]["enabled"]
                    if body.get("nine_router_url"):
                        c["nine_router"]["base_url"] = body["nine_router_url"].rstrip("/")
                        out["nine_router_url"] = c["nine_router"]["base_url"]
                    if body.get("nine_router_prefix"):
                        c["nine_router"]["pool_prefix"] = body["nine_router_prefix"]
                        out["nine_router_prefix"] = c["nine_router"]["pool_prefix"]
                    if body.get("rotate_interval_s"):
                        c["rotate_interval_s"] = max(10, int(body["rotate_interval_s"]))
                        out["rotate_interval_s"] = c["rotate_interval_s"]
                    if body.get("measure_url"):
                        c["measure_url"] = body["measure_url"]
                        out["measure_url"] = c["measure_url"]
                    if body.get("webhook_url") is not None:
                        c["webhook"]["url"] = body["webhook_url"]
                        c["webhook"]["enabled"] = bool(body.get("webhook_enabled"))
                        c["webhook"]["min_level"] = body.get("webhook_level") or "warn"
                        out["webhook"] = {"enabled": c["webhook"]["enabled"],
                                          "min_level": c["webhook"]["min_level"]}
                    if body.get("ui_bind"):
                        c["ui_bind"] = body["ui_bind"]
                        out["ui_bind"] = c["ui_bind"]
                    if body.get("ui_port"):
                        c["ui_port"] = max(1024, min(65000, int(body["ui_port"])))
                        out["ui_port"] = c["ui_port"]
                    save_cfg(c); log(f"settings updated {out}")
                    if c["nine_router"].get("enabled") and not out.get("usque_dns"):
                        try: install_9r(c)
                        except Exception as e: log(f"9router sync failed: {e}")
                self._send(200, json.dumps(out or {"ok": True}))
            elif self.path.startswith("/api/pause"):
                _set_paused(True); self._send(200, json.dumps({"paused": True}))
            elif self.path.startswith("/api/resume"):
                _set_paused(False); self._send(200, json.dumps({"paused": False}))
            elif self.path.startswith("/api/update"):
                # the new build only takes effect once this process restarts
                self._send(200, json.dumps(update(load_cfg())))
            elif self.path.startswith("/api/webhook-test"):
                # test an unsaved URL too, so "send test" works before save
                body = self._body(); url = (body.get("url") or "").strip()
                if not url:
                    self._send(200, json.dumps({"ok": False, "error": "no url"})); return
                probe = load_cfg(); probe["webhook"] = {"enabled": True, "url": url,
                                                        "min_level": "info"}
                _notify_last.clear()   # a test must actually fire
                ok = notify(probe, "UnlProxy webhook test - if you can read this, alerts work",
                            "info", dedupe_s=0)
                self._send(200, json.dumps({"ok": bool(ok)}))
            elif self.path.startswith("/api/revive"):
                # drained nodes: isinstance check no longer needed now that slots keep the label
                st = load_state().get("nodes", {})
                for p in [k for k, n in st.items() if n.get("drained")]:
                    st[p]["drained"] = False
                save_state(st); r = ensure(load_cfg()); self._send(200, json.dumps(r))
            else: self._send(404, "{}")
        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}
            except Exception:
                return {}
    srv = http.server.ThreadingHTTPServer((cfg["ui_bind"], cfg["ui_port"]), H)
    log(f"UI on http://{cfg['ui_bind']}:{cfg['ui_port']}/")
    threading.Thread(target=tick_loop, daemon=True, name="tick").start()
    srv.serve_forever()

# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- cli mode
def _fmt_table(rows):
    if not rows: return "no nodes"
    head = f"{'PORT':<7}{'STATE':<9}{'EGRESS IP':<40}{'WARP':<6}{'AGE':<6}PID"
    out = [head, "-" * len(head)]
    for r in rows:
        out.append(f"{r['port']:<7}{r['state']:<9}{(r['ip'] or '-'):<40}"
                   f"{('on' if r['warp'] else '-'):<6}{str(r['age_min']) + 'm':<6}{r['pid'] or ''}")
    return chr(10).join(out)

HELP = """
UnlProxy - WARP egress pool

  unlp                       open the control UI
  unlp <cmd>                 run any command below

Pool
  status                     node table (ports, egress, age, pid)
  up / down                  start / stop every node
  ensure                     one health + dedupe + rotate-due cycle
  rotate [port]              rotate now (reconnect, else re-register)
  pause / resume             stop / restart automatic rotation; nodes stay up
  nodes <n>                  grow or shrink the pool
  revive                     bring drained nodes back

Run it
  ui                         start the UI and open it         (ui off: see below)
  ui off                     stop a UI started by this process
  serve                      UI in the foreground (no browser)
  daemon                     background service: pool + rotation + UI
  cli                        interactive console
  install                    first run: config + usque + pool + rotation

Settings
  config                     print the live config
  config --nodes 4 --dns 1.1.1.1 --rotate 300 --9r --webhook URL
  webhook                    show / set / clear the alert webhook
  install-9r                 create + test pools in 9router

Maintenance
  version                    this build's version
  update                     check for a newer release and install it
  update --check             only check, do not install
  uninstall                  stop everything and remove the install directory
  help                       this list
"""

def _help():
    print(HELP.strip())

def _set_paused(on):
    with STATE_LOCK:
        st = load_state(); st["paused"] = bool(on); save_state(st)
    log("rotation paused" if on else "rotation resumed")
    return {"paused": bool(on)}

def _ui_state(cfg):
    return {"running": _port_busy(cfg["ui_port"]), "url": _ui_url(cfg)}

def uninstall(keep_config=False):
    """Stop every node and remove the app. Refuses to delete anything outside HERE."""
    stop_all()
    targets = SELF_FILES + ("assets", "bin", "scripts")
    if not keep_config:
        targets += ("config.json", "state.json", "nodes", "events.log", "daemon.log", ".tick.lock")
    removed = []
    for name in targets:
        p = os.path.join(HERE, name)
        if not os.path.exists(p): continue
        try:
            if os.path.isdir(p): shutil.rmtree(p)
            else: os.remove(p)
            removed.append(name)
        except OSError as e:
            log(f"uninstall: could not remove {name}: {e}")
    return removed

def _pids_matching(needle):
    """PIDs whose command line contains `needle`, however the OS exposes them."""
    if WIN:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | ForEach-Object "
                 "{ \"$($_.ProcessId)|$($_.CommandLine)\" }"],
                capture_output=True, text=True, timeout=30).stdout
        except Exception:
            return []
        pids = []
        for line in out.splitlines():
            pid, _, cmdline = line.partition("|")
            if pid.strip().isdigit() and needle in cmdline:
                pids.append(int(pid))
        return pids
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit(): continue
        try:
            cmdline = open(f"/proc/{d}/cmdline", "rb").read().decode("utf-8", "replace")
        except OSError:
            continue
        if needle in cmdline and int(d) != os.getpid():
            pids.append(int(d))
    return pids

def stop_daemon():
    """Stop every other `warppool.py` daemon/serve/ui for this install."""
    pids = sorted(set(p for p in _pids_matching("warppool.py") if p != os.getpid()))
    for pid in pids:
        try:
            if WIN: subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
            else: os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    log(f"stopped {len(pids)} background process(es)")
    return pids

def _spawn_daemon():
    """Re-launch ourselves detached, so `restart` returns."""
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "stdin": subprocess.DEVNULL, "cwd": HERE}
    if not WIN: kw["start_new_session"] = True
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "daemon"], **kw)
    print(f"daemon restarted (pid {p.pid})")
    return 0

def stop_all():
    with STATE_LOCK:
        st = load_state()
        for p in list(st.get("nodes", {})): stop_node(p, st)
        save_state(st)

def _ui_url(cfg):
    host = "127.0.0.1" if cfg["ui_bind"] in ("0.0.0.0", "") else cfg["ui_bind"]
    return f"http://{host}:{cfg['ui_port']}/"

def _port_busy(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, int(port)), timeout=1): return True
    except OSError:
        return False

def cli(cfg):
    """Interactive console: same verbs as the UI, no browser."""
    print("UnlProxy cli - type 'help' for commands, 'q' to leave\n")
    print(_fmt_table(status(cfg)) + "\n")
    cmds = {"help": "this list", "status": "node table", "ensure": "health + dedupe + rotations due",
            "up": "start every node", "down": "stop every node", "rotate": "rotate all nodes",
            "rotate <port>": "rotate one node", "nodes <n>": "grow/shrink the pool",
            "9r": "create + test pools in 9router", "url": "print the UI address",
            "webhook [url|off]": "set the alert webhook",
            "pause / resume": "stop / restart automatic rotation",
            "version": "this build's version", "update": "check for + install a new release",
            "config": "print the live config", "log": "last events", "q": "quit"}
    while True:
        try:
            line = input("unlproxy> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return 0
        if not line: continue
        parts = line.split(); cmd = parts[0].lower(); arg = parts[1] if len(parts) > 1 else ""
        try:
            if cmd in ("q", "quit", "exit"): return 0
            if cmd in ("help", "h", "?"):
                for k, v in cmds.items(): print(f"  {k:<16}{v}")
            elif cmd == "status":
                print(_fmt_table(status(cfg)))
            elif cmd in ("ensure", "up", "down"):
                if cmd == "down":
                    for p in load_state().get("nodes", {}): stop_node(p)
                    log("all stopped")
                else:
                    if cmd == "up":
                        st = load_state(); st.setdefault("nodes", {})
                        for i in range(cfg["nodes"]):
                            st["nodes"].setdefault(str(cfg["base_port"] + i), {"registered_at": 0, "drained": False})
                        save_state(st)
                    r = ensure(cfg); print(json.dumps(r, indent=1))
                print(_fmt_table(status(cfg)))
            elif cmd == "rotate":
                print("rotated:", rotate_ports(cfg, arg or "all")); print(_fmt_table(status(cfg)))
            elif cmd == "nodes":
                n = int(arg); c = load_cfg(); c["nodes"] = max(0, n); save_cfg(c)
                st = load_state(); nodes = st.setdefault("nodes", {})
                while len([k for k in nodes if not nodes[k].get("drained")]) > c["nodes"]:
                    free = [k for k in nodes if not nodes[k].get("drained")]
                    p = max(free, key=lambda x: nodes[x].get("registered_at", 0))
                    stop_node(p, {"nodes": nodes}); nodes[p]["drained"] = True
                save_state(st)
                print(json.dumps(ensure(c), indent=1)); print(_fmt_table(status(c)))
            elif cmd == "9r":
                print("pools:", install_9r(load_cfg()))
            elif cmd == "url":
                print(_ui_url(cfg))
            elif cmd in ("pause", "resume"):
                print(json.dumps(_set_paused(cmd == "pause")))
            elif cmd == "version":
                print(VERSION)
            elif cmd == "update":
                print(json.dumps(update(cfg), indent=1))
            elif cmd == "webhook":
                c = load_cfg()
                if arg == "off":
                    c["webhook"]["enabled"] = False; save_cfg(c); print("webhook off")
                elif arg:
                    c["webhook"].update({"url": arg, "enabled": True}); save_cfg(c)
                    _notify_last.clear()
                    print("webhook test:", "delivered" if notify(c, "UnlProxy webhook test", "info", 0)
                          else "failed - see the event log")
                else:
                    print(json.dumps(c.get("webhook"), indent=1))
            elif cmd == "config":
                print(json.dumps(load_cfg(), indent=1))
            elif cmd == "log":
                print("\n".join(_events()[-25:]) or "no events yet")
            else:
                print(f"unknown: {cmd}  (try help)")
        except Exception as e:
            print(f"error: {e}")

def main():
    if len(sys.argv) < 2:
        _help(); return 2
    cmd = sys.argv[1]
    cfg = load_cfg()
    if cmd in ("help", "-h", "--help"): _help(); return 0
    if cmd in ("version", "-V", "--version"): print(VERSION); return 0
    if cmd in ("pause", "resume"):
        print(json.dumps(_set_paused(cmd == "pause"))); return 0
    if cmd == "update":
        if "--check" in sys.argv:
            r = update_check(cfg)
            print(json.dumps(r, indent=1))
            return 0 if not r["error"] else 1
        r = update(cfg)
        print(json.dumps(r, indent=1))
        if r.get("updated"): print("\nrestart the daemon/service to run the new build")
        return 0 if not r.get("error") else 1
    if cmd == "uninstall":
        n = uninstall(keep_config="--keep-config" in sys.argv)
        print(f"removed: {', '.join(n) or 'nothing'}")
        print(f"left in place: {HERE}" if n else HERE)
        print("to finish: delete the 'unlp' launcher from ~/.local/bin (or %LOCALAPPDATA%)"
              if not WIN else "to finish: delete unlp.cmd from %USERPROFILE%\\.local\\bin")
        return 0
    if cmd == "cli": return cli(cfg)
    if cmd == "setup":
        print("UnlProxy setup — 2 answers, everything else is automatic\n")
        # MASQUE over 443 needs no relay, so there is no "where does this run" question
        try: n = int(input("How many egress nodes? [3]: ") or "3")
        except ValueError: n = 3
        serve_q = (input("Start the control UI? [Y/n]: ") or "y").strip().lower() != "n"
        if not os.path.exists(CONFIG_PATH): save_cfg(cfg)
        cfg["nodes"] = max(1, n); save_cfg(cfg)
        download_usque(cfg)
        os.makedirs(os.path.join(HERE, "nodes"), exist_ok=True)
        r = ensure(cfg); log(json.dumps(r))
        if r["alive"] == 0: print("no node came up; check the logs in nodes/"); return 1
        print(f"\npool ready: {r['alive']} node(s) up, {r['distinct_ips']} distinct egress address(es)")
        print(f"UI:   http://{'127.0.0.1' if cfg['ui_bind']=='0.0.0.0' else cfg['ui_bind']}:{cfg['ui_port']}/")
        if cfg["nine_router"].get("enabled"):
            try: install_9r(cfg); print("9router pools: created + tested")
            except Exception as e: print("9router install skipped:", e)
        if serve_q:
            print("(Ctrl+C to stop the UI; pool keeps running)\n")
            serve(cfg)
        return 0
    if cmd == "config":
        # headless tune: config --nodes 4 --dns 1.1.1.1 --9r --no-9r --rotate 300
        a = sys.argv[1:]
        if "--nodes" in a: cfg["nodes"] = max(1, int(a[a.index("--nodes") + 1]))
        if "--dns" in a: cfg["usque_dns"] = a[a.index("--dns") + 1]
        if "--rotate" in a: cfg["rotate_interval_s"] = max(10, int(a[a.index("--rotate") + 1]))
        if "--9r" in a: cfg["nine_router"]["enabled"] = True
        if "--no-9r" in a: cfg["nine_router"]["enabled"] = False
        if "--webhook" in a:
            cfg["webhook"]["url"] = a[a.index("--webhook") + 1]; cfg["webhook"]["enabled"] = True
        if "--no-webhook" in a: cfg["webhook"]["enabled"] = False
        if "--port" in a: cfg["base_port"] = max(1024, int(a[a.index("--port") + 1]))
        if "--ui-port" in a: cfg["ui_port"] = max(1024, int(a[a.index("--ui-port") + 1]))
        save_cfg(cfg)
        if "--quiet" not in a:
            print(json.dumps({k: cfg[k] for k in ("nodes", "base_port", "rotate_interval_s",
                                                  "usque_dns", "webhook", "ui_port")}, indent=1))
        return 0
    if cmd == "init":
        if not os.path.exists(CONFIG_PATH): save_cfg(cfg)
        download_usque(cfg)
        os.makedirs(os.path.join(HERE, "nodes"), exist_ok=True)
        log(f"config at {CONFIG_PATH}; usque at {ubin(cfg)}")
        r = ensure(cfg); log(json.dumps(r)); return 0
    if cmd == "up":
        cfg.setdefault("nodes", 3); st = load_state(); st.setdefault("nodes", {})
        for i in range(cfg["nodes"]):
            st["nodes"].setdefault(str(cfg["base_port"] + i), {"registered_at": 0, "drained": False})
        save_state(st); r = ensure(cfg); log(json.dumps(r)); return 0
    if cmd == "down":
        for p in load_state().get("nodes", {}): stop_node(p)
        log("all stopped"); return 0
    if cmd == "ensure":
        r = ensure(cfg)
        if r["changed"]: log(json.dumps(r))
        return 0 if r["alive"] > 0 else 1
    if cmd == "status":
        print(json.dumps(status(cfg), indent=1)); return 0
    if cmd == "rotate":
        q = sys.argv[2] if len(sys.argv) > 2 else "all"
        print(json.dumps({"rotated": rotate_ports(cfg, q)})); return 0
    if cmd == "serve": serve(cfg); return 0
    if cmd == "restart":
        # stop our own background processes and hand back to the service manager;
        # under nohup/startup nothing restarts us, so start the daemon here
        stop_daemon()
        time.sleep(1)
        return _spawn_daemon()
    if cmd == "ui":
        arg = sys.argv[2].lower() if len(sys.argv) > 2 else ""
        url = _ui_url(cfg)
        if arg in ("off", "stop", "disable"):
            # the UI lives in the daemon here, so "off" means remember not to
            # serve it and let the daemon's next start honour it
            cfg["ui_enabled"] = False; save_cfg(cfg)
            if _port_busy(cfg["ui_port"]):
                print(f"UI disabled for the next start. It is still answering on {url}")
                print("to stop it now: unlp restart")
            else:
                print("UI disabled")
            return 0
        if arg in ("on", "enable"):
            cfg["ui_enabled"] = True; save_cfg(cfg)
            print("UI enabled")
            return 0
        cfg["ui_enabled"] = True; save_cfg(cfg)
        if _port_busy(cfg["ui_port"]):
            print(f"UnlProxy UI is already running: {url}")
            try: webbrowser.open(url)
            except Exception: pass
            return 0
        ensure(cfg)   # pool up before the browser opens, so the table is not empty
        threading.Thread(target=serve, args=(cfg,), daemon=True, name="ui").start()
        for _ in range(40):
            time.sleep(0.25)
            if _port_busy(cfg["ui_port"]): break
        try: webbrowser.open(url)
        except Exception as e: print("could not open a browser:", e)
        print(f"UnlProxy UI: {url}   (Ctrl+C to stop)")
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt:
            print()
            return 0
    if cmd == "daemon":
        # the background service: pool + rotation, and the control UI too, so
        # the panel is always there after an install
        r = ensure(cfg)
        log(json.dumps(r))
        if r["alive"] == 0:
            print("no node came up - see nodes/*.log"); return 1
        print(f"pool up: {r['alive']} node(s), {r['distinct_ips']} distinct egress address(es)")
        if cfg.get("ui_enabled", True) and not _port_busy(cfg["ui_port"]):
            threading.Thread(target=serve, args=(cfg,), daemon=True, name="ui").start()
            print(f"UI: {_ui_url(cfg)}")
        print("rotation runs on its own here - Ctrl+C to stop the loop (the nodes stay up)")
        tick_loop()
        return 0
    if cmd == "install":
        # one command: config + usque + nodes up + a self-driving rotation loop
        if not os.path.exists(CONFIG_PATH): save_cfg(cfg)
        cfg["nodes"] = max(1, cfg.get("nodes", 3)); save_cfg(cfg)
        download_usque(cfg)
        os.makedirs(os.path.join(HERE, "nodes"), exist_ok=True)
        r = ensure(cfg); log(json.dumps(r))
        if r["alive"] == 0:
            print("no node came up - see nodes/*.log"); return 1
        print(f"pool ready: {r['alive']} node(s), {r['distinct_ips']} distinct egress address(es)")
        if cfg.get("ui_enabled", True) and not _port_busy(cfg["ui_port"]):
            threading.Thread(target=serve, args=(cfg,), daemon=True, name="ui").start()
            print(f"UI: {_ui_url(cfg)}")
        print("rotation runs on its own - Ctrl+C to stop (the nodes stay up)")
        tick_loop()
        return 0
    if cmd == "install-9r": install_9r(cfg); return 0
    print(__doc__); return 2

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
