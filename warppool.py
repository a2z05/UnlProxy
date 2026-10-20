#!/usr/bin/env python3
"""warppool — per-node Cloudflare WARP egress pool with healthy-IP rotation.

Built on the mechanics of ebouther/opencode-unlimited-proxy (MIT), re-implemented
portable and dependency-free:

  * every node = its own WARP registration = its own egress address
  * rotation = re-register (fresh device) on an interval or on demand
  * dedupe = measure each node's REAL egress; colliding nodes are re-registered,
    nodes that cannot be given a distinct address are drained (stopped)
  * health = a dead node is restarted; a drained node stays down until revived

Runs on Linux and Windows with stdlib only (wireproxy does the WireGuard side in
userspace, SOCKS5 mode — no admin, no TUN, no Docker).

Commands:
  warppool.py init            write config.json + download wireproxy if missing
  warppool.py up              start every non-drained node
  warppool.py down            stop every node
  warppool.py ensure          health + dedupe + rotate-due  (cron entrypoint)
  warppool.py status          print table + JSON summary
  warppool.py rotate [p|all]  force fresh registration
  warppool.py serve           UI on ui_port (default 127.0.0.1:8180)
  warppool.py install-9r      push nodes into 9router's proxy pools (CLI token)
"""
import json, os, re, signal, socket, ssl, subprocess, sys, time, urllib.request, urllib.error, hashlib, base64, random, string

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("WARPPOOL_CONFIG", os.path.join(HERE, "config.json"))
STATE_PATH = os.path.join(HERE, "state.json")
WIN = os.name == "nt"

DEFAULTS = {
    "nodes": 3,
    "base_port": 18090,
    "rotate_interval_s": 300,     # same cadence as the reference project
    "measure_url": "https://www.cloudflare.com/cdn-cgi/trace",
    "upstream_proxy": "",         # e.g. socks5://127.0.0.1:10808 (Iran: your v2ray)
    "wg_endpoint_override": "",   # e.g. 127.0.0.1:10809 (v2ray dokodemo UDP relay)
    "wireproxy_bin": "",          # auto: ./bin/wireproxy(.exe)
    "ui_bind": "127.0.0.1",
    "ui_port": 8180,
    "nine_router": {"enabled": False, "base_url": "http://127.0.0.1:20128",
                    "cli_token": "", "pool_prefix": "warp-"},
    "cf_client_version": "a-6.81-2410032352.1",
    "reg_backoff_s": [60, 120],   # CF 429/403: long backoff (their own lesson)
}

STALE_MEASURE_S = 150


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

def save_state(st):
    json.dump(st, open(STATE_PATH, "w"), indent=1)

# ---------------------------------------------------------------- x25519 (RFC 7748)
_P = 2**255 - 19
_A24 = 121665
def _clamp(k: bytes) -> int:
    v = int.from_bytes(k, "little")
    v &= ~7; v &= ~(127 << 255); v |= 64 << 254
    return v
def _x25519(k: bytes, u: bytes) -> bytes:
    k = _clamp(k); x1 = int.from_bytes(u, "little") % _P
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        kt = (k >> t) & 1; swap ^= kt
        if swap: x2, x3 = x3, x2; z2, z3 = z3, z2
        swap = kt
        A = (x2 + z2) % _P; AA = A * A % _P
        B = (x2 - z2) % _P; BB = B * B % _P
        E = (AA - BB) % _P
        C = (x3 + z3) % _P; D = (x3 - z3) % _P
        DA = D * A % _P; CB = C * B % _P
        x3 = (DA + CB) ** 2 % _P
        z3 = x1 * ((DA - CB) ** 2 % _P) % _P
        x2 = AA * BB % _P
        z2 = E * (AA + _A24 * E) % _P
    if swap: x2, x3 = x3, x2; z2, z3 = z3, z2
    inv = pow(z2, _P - 2, _P)
    return (x2 * inv % _P).to_bytes(32, "little")
def keypair():
    priv = os.urandom(32)
    return base64.b64encode(priv).decode(), base64.b64encode(_x25519(priv, b"\x09" + b"\x00"*31)).decode()
def selftest_x25519():
    # RFC 7748 §5.2 vector 1
    k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    e = bytes.fromhex("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
    return _x25519(k, u) == e

# ---------------------------------------------------------------- io helpers
def opener(cfg, timeout=20):
    handlers = []
    if cfg.get("upstream_proxy"):
        handlers.append(urllib.request.ProxyHandler({
            "http": cfg["upstream_proxy"], "https": cfg["upstream_proxy"]}))
    return urllib.request.build_opener(*handlers), timeout

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

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------------------------------------------------------------- wireproxy
def wbin(cfg):
    p = cfg.get("wireproxy_bin") or os.path.join(HERE, "bin", "wireproxy.exe" if WIN else "wireproxy")
    return p

def download_wireproxy(cfg):
    if os.path.exists(wbin(cfg)):
        return wbin(cfg)
    plat = "wireproxy_windows_amd64.tar.gz" if WIN else "wireproxy_linux_amd64.tar.gz"
    # octeep/wireproxy moved to repo id 468743437; follow the redirect
    url = f"https://api.github.com/repositories/468743437/releases/latest"
    _, rel = http_json(cfg, url)
    asset = next(a for a in rel["assets"] if a["name"] == plat)
    os.makedirs(os.path.dirname(wbin(cfg)), exist_ok=True)
    import tarfile, io
    data = fetch_text_cfg(cfg, asset["browser_download_url"])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if os.path.basename(m.name) == ("wireproxy.exe" if WIN else "wireproxy"):
                m.name = os.path.basename(m.name)
                tf.extract(m, os.path.dirname(wbin(cfg)))
    if not WIN:
        os.chmod(wbin(cfg), 0o755)
    return wbin(cfg)

def fetch_text_cfg(cfg, url, timeout=60):
    op, to = opener(cfg, timeout)
    with op.open(url, timeout=to) as r:
        return r.read()

def conf_path(cfg, port):
    return os.path.join(HERE, "nodes", f"node-{port}.conf")

def write_conf(cfg, port, priv, endpoint, peer_pub, v4, v6):
    os.makedirs(os.path.dirname(conf_path(cfg, port)), exist_ok=True)
    open(conf_path(cfg, port), "w").write(f"""[Interface]
PrivateKey = {priv}
Address = {v4}/32
Address = {v6}/128
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = {peer_pub}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {endpoint}
PersistentKeepalive = 30

[Socks5]
BindAddress = 127.0.0.1:{port}
""")

def register(cfg, port):
    """Fresh WARP device -> its own egress address. Backs off hard on 429/403."""
    priv, pub = keypair()
    sn = str(__import__("uuid").uuid4())
    tos = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    payload = {"install_id": "", "tos": tos, "key": pub, "fcm_token": "",
               "serial_number": sn, "type": "Android", "model": "PC", "locale": "en_US"}
    tries = 3
    for i in range(tries):
        try:
            status, d = http_json(cfg, "https://api.cloudflareclient.com/v0a2483/reg",
                                  payload, headers={"CF-Client-Version": cfg["cf_client_version"]}, timeout=25)
        except urllib.error.HTTPError as e:
            status, d = e.code, {}
            try: d = json.loads(e.read())
            except Exception: pass
        except Exception as e:
            log(f"register {port}: transport {e}"); time.sleep(5); continue
        if status == 200 and "config" in d:
            cf = d["config"]; peer = cf["peers"][0]
            ep = cfg.get("wg_endpoint_override") or peer["endpoint"]["host"]
            write_conf(cfg, port, priv, ep, peer["public_key"],
                       cf["interface"]["addresses"]["v4"], cf["interface"]["addresses"]["v6"])
            return {"id": d.get("id"), "registered_at": time.time()}
        if status in (429, 400, 403):
            wait = random.choice(cfg["reg_backoff_s"]) + random.randint(0, 60)
            log(f"register {port}: HTTP {status} — long backoff {wait}s (CF rate limit)")
            time.sleep(min(wait, 90)); continue
        time.sleep(4 + i * 6)
    return None

def pid_alive(cfg, port):
    st = load_state().get("nodes", {}).get(str(port)) or {}
    p = st.get("pid")
    if not p: return False
    try:
        if WIN:
            # os.kill on windows with 0 is unreliable; probe the socks port instead
            return sock_open(port)
        os.kill(int(p), 0)
        return True
    except (OSError, ValueError):
        return False

def sock_open(port, host="127.0.0.1", timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout): return True
    except OSError:
        return False

def spawn(cfg, port, st=None):
    # truncate node log each spawn: it only captures last crash, not history
    try:
        lp = os.path.join(HERE, "nodes", f"node-{port}.log")
        if os.path.exists(lp) and os.path.getsize(lp) > 200_000:
            with open(lp, "rb") as f: tail = f.read()[-50_000:]
            with open(lp, "wb") as f: f.write(tail)
    except OSError:
        pass
    logf = open(os.path.join(HERE, "nodes", f"node-{port}.log"), "ab")
    kw = {"stdout": logf, "stderr": logf}
    if not WIN: kw["start_new_session"] = True
    # Go GC knobs: soft heap ceiling keeps idle wireproxy RSS flat instead of
    # drifting up with allocations; GOGC=50 trades negligible GC CPU (~2% of a
    # mostly-idle 0.3% baseline) for a smaller resident set. Performance of
    # proxied traffic is unaffected (kernel path is the overhead).
    env = {**os.environ, "GOGC": "50", "GOMEMLIMIT": "32MiB"}
    p = subprocess.Popen([wbin(cfg), "-c", conf_path(cfg, port)], env=env, **kw)
    if st is None:
        st = load_state()
        st.setdefault("nodes", {}).setdefault(str(port), {"registered_at": 0, "drained": False})
    st["nodes"][str(port)]["pid"] = p.pid
    if st is None:
        save_state(st)
    return p.pid

def _find_proc(port):
    """A slot whose stored pid is stale still owns a live wireproxy — find it
    by its config path; register/rotate отчёт lie otherwise (bind: in use)."""
    needle = f"node-{port}.conf".encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit(): continue
        try:
            if needle in open(f"/proc/{pid}/cmdline", "rb").read(): return int(pid)
        except OSError: pass
    return None

def stop_node(port, st=None):
    if st is None:
        st = load_state()
    n = st.get("nodes", {}).get(str(port)) or {}
    p = n.get("pid") or _find_proc(port)
    if p:
        try:
            if WIN:
                subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True)
            else:
                os.kill(int(p), signal.SIGTERM)          # Go daemons often ignore SIGTERM
                time.sleep(0.6)
                try: os.kill(int(p), 0)                   # still alive?
                except OSError: pass
                else: os.kill(int(p), signal.SIGKILL)
                # wait for the socks port to actually free: spawn would otherwise bind-fail
                for _ in range(20):
                    try:
                        socket.create_connection(("127.0.0.1", int(port)), timeout=0.5).close()
                        time.sleep(0.3)                   # still listening at PID level
                    except OSError:
                        break
                    time.sleep(0.3)
        except Exception:
            pass
    n.pop("pid", None)
    # free the listener even if the pid was gone
    time.sleep(0.3)


def adopt_pid(cfg, port, st):
    """Attach a pid to a node whose socks port answers but whose pid was lost."""
    n = st["nodes"][str(port)]
    if n.get("pid") or not WIN:
        if n.get("pid"): return
        needle = f"node-{port}.conf".encode()
        for pid in os.listdir("/proc"):
            if not pid.isdigit(): continue
            try:
                if needle in open(f"/proc/{pid}/cmdline", "rb").read():
                    n["pid"] = int(pid); return
            except OSError:
                pass
    else:
        pass

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
    r = subprocess.run(["curl", "-sS", "-m", "15", "-x", f"socks5h://127.0.0.1:{port}", url],
                       capture_output=True, text=True)
    out = dict(re.findall(r"^([a-z]+)=(.*)$", r.stdout, re.M))
    if "ip" not in out: return {"error": (r.stderr or r.stdout or "no trace")[:80]}
    return {"ip": out.get("ip"), "warp": out.get("warp") == "on", "colo": out.get("colo")}

# ---------------------------------------------------------------- ensure cycle
def ensure(cfg):
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
                    log(f"{port}: registration failed"); continue
                n.update(r); changed.append(f"{port} registered")
            spawn(cfg, port, st)
            time.sleep(1.5)
            if sock_open(int(port)): changed.append(f"{port} started")
            else: log(f"{port}: wireproxy did not open its socks port")
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
                spawn(cfg, int(p), st); time.sleep(1.5)
                got = measure(cfg, int(p)).get("ip") if sock_open(int(p)) else None
                nodes[p]["measure"] = {"ip": got, "warp": True} if got else {}
                nodes[p]["measured_at"] = time.time()
                changed.append(f"{p} re-registered (dedupe)")
            else:
                nodes[p]["drained"] = True; changed.append(f"{p} DRAINED (no distinct address)")

    # 3) rotate due
    now = time.time()
    for port, n in list(nodes.items()):
        if n.get("drained"): continue
        if now - (n.get("registered_at") or 0) >= cfg["rotate_interval_s"]:
            log(f"{port}: rotation due")
            old_ip = (n.get("measure") or {}).get("ip")
            stop_node(port, st)
            r = register(cfg, int(port))
            if r:
                n.update(r); spawn(cfg, int(port), st)
                time.sleep(1.5)
                got = measure(cfg, int(port)).get("ip") if sock_open(int(port)) else None
                tries = 1
                while old_ip and got == old_ip and tries < 3:
                    # same egress came back — re-register once more (CF can hand the
                    # same colo address back); we want a DIFFERENT quota bucket
                    stop_node(port, st); r = register(cfg, int(port))
                    if not r: break
                    n.update(r); spawn(cfg, int(port), st); time.sleep(1.5); tries += 1
                    got = measure(cfg, int(port)).get("ip") if sock_open(int(port)) else None
                n["measure"] = {"ip": got, "warp": True} if got else {}
                n["measured_at"] = time.time()
                changed.append(f"{port} rotated" + (f" -> {got}" if got else ""))
            else:
                n["registered_at"] = now - cfg["rotate_interval_s"] + 60  # retry next cycle

    save_state(st)
    alive = sum(1 for p in nodes if not nodes[p].get("drained") and sock_open(int(p)))
    drained = sum(1 for p in nodes if nodes[p].get("drained"))
    distinct = {n.get("measure", {}).get("ip") for n in nodes.values()
                if not n.get("drained") and n.get("measure", {}).get("ip")}
    return {"alive": alive, "drained": drained, "distinct_ips": len(distinct), "changed": changed}

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
        for cand in ("/opt/data/.9router", os.path.expanduser("~/.9router")):
            if os.path.exists(os.path.join(cand, "machine-id")): base = cand; break
        if base:
            n9["cli_token"] = hashlib.sha256((open(os.path.join(base, "machine-id")).read().strip() +
                "9r-cli-auth" + open(os.path.join(base, "auth", "cli-secret")).read().strip()
                ).encode()).hexdigest()[:16]
            save_cfg(cfg)
    hdr = {"x-9r-cli-token": n9["cli_token"], "Content-Type": "application/json"}
    base = n9["base_url"]
    st, d = http_json(cfg, base + "/api/proxy-pools", headers=hdr, timeout=10)
    have = {p.get("proxyUrl") for p in (d.get("proxyPools") or []) if p.get("proxyUrl")}
    created = []
    for row in status(cfg):
        if row["state"] != "up": continue
        url = f"socks5://127.0.0.1:{row['port']}"
        if url in have: continue
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
    log(f"9router: {len(created)} pool(s) created" + (f": {created}" if created else ""))
    return created

# ---------------------------------------------------------------- UI
INDEX = """<!doctype html><meta charset=utf-8><title>warp-pool</title>
<style>body{background:#0b0d12;color:#dfe3ea;font:14px/1.5 ui-monospace,Consolas,monospace;padding:24px}
h1{color:#ff6b57;font-size:20px}table{border-collapse:collapse;margin-top:14px;min-width:640px}
th,td{border:1px solid #232838;padding:6px 12px;text-align:left}th{color:#8a93a8}
.up{color:#38d97a}.down{color:#ff5c5c}.drained{color:#ffb020}
button{background:#1a2030;color:#dfe3ea;border:1px solid #2a3350;border-radius:6px;padding:5px 14px;cursor:pointer}
button:hover{background:#242d47}#j{white-space:pre;margin-top:18px;color:#9fb0d0;font-size:12px}</style>
<h1>warp-pool — healthy egress rotation</h1><div id=t>loading…</div>
<div style="margin-top:14px"><button onclick="cmd('ensure')">run ensure</button>
<button onclick="cmd('rotate?port=all')">rotate all</button></div><pre id=j></pre>
<script>
async function load(){const r=await (await fetch('api/status')).json();
 const h=['<table><tr><th>port</th><th>state</th><th>egress ip</th><th>warp</th><th>age</th><th>pid</th><th></th></tr>'];
 for(const n of r){h.push(`<tr><td>${n.port}</td><td class=${n.state}>${n.state}</td><td>${n.ip||'—'}</td>
 <td>${n.warp?'on':'—'}</td><td>${n.age_min}m</td><td>${n.pid||''}</td>
 <td><button onclick="cmd('rotate?port=${n.port}')">rotate</button></td></tr>`)}
 document.getElementById('t').innerHTML=h.join('')+'</table>';load2()}
 async function load2(){const r=await (await fetch('api/log')).json();document.getElementById('j').textContent=r.changed.join('\\n')||'no changes yet'}
 async function cmd(q){const r=await (await fetch('api/'+q,{method:'POST'})).json();document.getElementById('j').textContent=JSON.stringify(r,null,1);load()}
 load();setInterval(load,15000)</script>"""


def _public_cfg(cfg):
    """Config the UI needs, secrets omitted."""
    n9 = cfg.get("nine_router") or {}
    return {"nodes": cfg.get("nodes"), "rotate_interval_s": cfg.get("rotate_interval_s"),
            "upstream_proxy": bool(cfg.get("upstream_proxy")),
            "wg_endpoint_override": bool(cfg.get("wg_endpoint_override")),
            "nine_router": {"enabled": bool(n9.get("enabled")), "base_url": n9.get("base_url"),
                            "connected": bool(n9.get("cli_token"))},
            "path": HERE}

def serve(cfg):
    import http.server, threading
    LOGBOX = {"changed": []}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                try:
                    self._send(200, open(os.path.join(HERE, "ui.html"), "rb").read(), "text/html; charset=utf-8")
                except OSError:
                    self._send(200, INDEX, "text/html; charset=utf-8")
            elif self.path.startswith("/api/status"): self._send(200, json.dumps(status(load_cfg())))
            elif self.path.startswith("/api/log"): self._send(200, json.dumps(LOGBOX))
            elif self.path.startswith("/api/config"): self._send(200, json.dumps(_public_cfg(load_cfg())))
            elif self.path.endswith(".svg") or self.path.endswith(".ico"):
                self._send(404, "{}")
            else: self._send(404, "{}")
        def do_POST(self):
            c = load_cfg()
            if self.path.startswith("/api/ensure"):
                r = ensure(c); LOGBOX["changed"] = r["changed"] or LOGBOX["changed"]; self._send(200, json.dumps(r))
            elif self.path.startswith("/api/rotate"):
                q = self.path.split("port=")[-1] if "port=" in self.path else "all"
                st = load_state().get("nodes", {})
                ports = [k for k in st] if q == "all" else [q]
                done = []
                for p in ports:
                    if st.get(p, {}).get("drained"): continue
                    wrapper = {"nodes": st}
                    stop_node(p, wrapper); r = register(c, int(p))
                    if r:
                        st[p].update(r); spawn(c, int(p), wrapper); time.sleep(1.5)
                        got = measure(c, int(p)).get("ip") if sock_open(int(p)) else None
                        st[p]["measure"] = {"ip": got, "warp": True} if got else {}
                        st[p]["measured_at"] = time.time(); done.append(p)
                save_state(st)
                self._send(200, json.dumps({"rotated": done}))
            elif self.path.startswith("/api/settings"):  # settable at runtime: IR mode, node count, rotation
                body = self._body(); out = {}
                if body:
                    ir = body.get("restricted")
                    if ir is not None:
                        # Iran mode: ONE toggle fills both v2ray placeholders from user input once
                        c["upstream_proxy"] = (body.get("upstream_proxy") or c.get("upstream_proxy") or "").strip() if ir else ""
                        c["wg_endpoint_override"] = (body.get("wg_endpoint_override") or c.get("wg_endpoint_override") or "").strip() if ir else ""
                        out["restricted"] = ir
                    if body.get("nodes"):
                        c["nodes"] = max(0, int(body["nodes"]))
                        stn = load_state()
                        want = c["nodes"]
                        nodes = stn.setdefault("nodes", {})
                        while len([n for n in nodes if not nodes[n].get("drained")]) > want:
                            # pick newest slot, stop+drain rather than delete
                            free = [p for p in nodes if not nodes[p].get("drained")]
                            p = max(free, key=lambda x: nodes[x].get("registered_at", 0))
                            stop_node(p, {"nodes": nodes}); nodes[p]["drained"] = True
                        save_state(stn)
                        ensure(c)
                        out["nodes"] = c["nodes"]
                    if body.get("nine_router_enabled") is not None:
                        c["nine_router"]["enabled"] = bool(body["nine_router_enabled"])
                        out["nine_router_enabled"] = c["nine_router"]["enabled"]
                    if body.get("rotate_interval_s"):
                        c["rotate_interval_s"] = max(30, int(body["rotate_interval_s"]))
                        out["rotate_interval_s"] = c["rotate_interval_s"]
                    if body.get("upstream_proxy") is not None and ir is None:
                        c["upstream_proxy"] = body["upstream_proxy"].strip(); out["upstream_proxy"] = c["upstream_proxy"]
                    if body.get("wg_endpoint_override") is not None and ir is None:
                        c["wg_endpoint_override"] = body["wg_endpoint_override"].strip(); out["wg_endpoint_override"] = c["wg_endpoint_override"]
                    save_cfg(c); LOGBOX["changed"] = LOGBOX.get("changed", []) + [f"settings updated {out}"]
                    if c["nine_router"].get("enabled"):
                        try: install_9r(c); LOGBOX["changed"].append("9router pools synced")
                        except Exception as e: LOGBOX["changed"].append(f"9router sync failed: {e}")
                self._send(200, json.dumps(out or {"ok": True}))
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
    srv.serve_forever()

# ---------------------------------------------------------------- main
def main():
    if not selftest_x25519():
        print("x25519 self-test FAILED"); return 2
    if len(sys.argv) < 2:
        print(__doc__); return 2
    cmd = sys.argv[1]
    cfg = load_cfg()
    if cmd == "setup":
        print("UnlProxy setup — 3 answers, everything else is automatic\n")
        where = (input("Where will this run? [server / windows-desktop / iran] (server): ") or "server").strip().lower()
        try: n = int(input("How many egress nodes? [3]: ") or "3")
        except ValueError: n = 3
        serve_q = (input("Start the control UI? [Y/n]: ") or "y").strip().lower() != "n"
        if not os.path.exists(CONFIG_PATH): save_cfg(cfg)
        if where == "iran" and cfg.get("upstream_proxy") == "":
            print("\nIran mode: v2ray must be running. Two values are needed:")
            print("  1) socks inbound of v2ray   (HTTP/registration + API calls)")
            print("  2) dokodemo-door relay port (the UDP WireGuard tunnel itself)")
            p1 = input("socks inbound    e.g. 127.0.0.1:10808 : ").strip()
            p2 = input("dokodemo-door    e.g. 127.0.0.1:10809 : ").strip()
            cfg["upstream_proxy"] = "socks5://" + p1 if p1 and "://" not in p1 else p1
            cfg["wg_endpoint_override"] = p2
            save_cfg(cfg)
        cfg["nodes"] = max(1, n); save_cfg(cfg)
        download_wireproxy(cfg)
        os.makedirs(os.path.join(HERE, "nodes"), exist_ok=True)
        r = ensure(cfg); log(json.dumps(r))
        if r["alive"] == 0: print("no node came up; check upstream/endpoint values above"); return 1
        print(f"\npool ready: {r['alive']} node(s) up, {r['distinct_ips']} distinct egress address(es)")
        print(f"UI:   http://{'127.0.0.1' if cfg['ui_bind']=='0.0.0.0' else cfg['ui_bind']}:{cfg['ui_port']}/")
        if cfg["nine_router"].get("enabled"):
            try: install_9r(cfg); print("9router pools: created + tested")
            except Exception as e: print("9router install skipped:", e)
        if serve_q:
            print("(Ctrl+C to stop the UI; pool keeps running)\n")
            serve(cfg)
        return 0
    if cmd == "init":
        if not os.path.exists(CONFIG_PATH): save_cfg(cfg)
        download_wireproxy(cfg)
        os.makedirs(os.path.join(HERE, "nodes"), exist_ok=True)
        log(f"config at {CONFIG_PATH}; wireproxy at {wbin(cfg)}")
        r = ensure(cfg); log(json.dumps(r)); return 0
    if cmd == "up":
        cfg.setdefault("nodes", 3); st = load_state(); st.setdefault("nodes", {})
        for i in range(cfg["nodes"]):
            st["nodes"].setdefault(str(cfg["base_port"] + i), {"registered_at": 0, "drained": False})
        save_state(st); r = ensure(cfg); log(json.dumps(r)); return 0

        if cfg["nine_router"].get("enabled"):
            try: install_9r(cfg)
            except Exception as e: log(f"9r install failed: {e}")
        return 0
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
        st = load_state().get("nodes", {})
        ports = [k for k in st] if q == "all" else [q]
        for p in ports:
            stop_node(p, {"nodes": st}); r = register(cfg, int(p))
            if r:
                st[p].update(r); spawn(cfg, int(p), {"nodes": st}); time.sleep(1.5)
                got = measure(cfg, int(p)).get("ip") if sock_open(int(p)) else None
                st[p]["measure"] = {"ip": got, "warp": True} if got else {}
                st[p]["measured_at"] = time.time(); log(f"{p} rotated -> {got or '?'}")
            else: log(f"{p} rotate FAILED")
        save_state({"nodes": st}); return 0
    if cmd == "serve": serve(cfg); return 0
    if cmd == "install-9r": install_9r(cfg); return 0
    print(__doc__); return 2

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
