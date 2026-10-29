#!/usr/bin/env python3
"""
Make UnlProxy's WARP pool usable from xray / v2ray.

    python unlp-xray.py --config <xray config.json> [--tag warp-unlp] [--dry-run]
    python unlp-xray.py --config <config.json> --revert

What it does: adds one SOCKS outbound per WARP node, all sharing a balancer tag
(default `warp-unlp`). You then route anything through that tag:

    {"type": "field", "domain": ["openrouter.ai"], "balancerTag": "warp-unlp"}

or set it as the default outbound. Nothing existing is touched: outbounds and
rules are appended, and the file is backed up before it is rewritten.

Why this instead of pointing 9router at the pool directly: 9router's proxy path
opens a SOCKS connection to usque and abandons it before the handshake finishes,
then silently falls back to the direct IP. xray's SOCKS client does not have that
problem (verified against the same nodes), so the pool works through xray.
"""
import argparse, json, os, shutil, socket, sys

DEFAULT_NODES = [18090, 18091, 18092]
BALANCER = "warp-unlp"


def alive(port, host="127.0.0.1"):
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


def build(cfg, ports, balancer):
    cfg.setdefault("outbounds", [])
    cfg.setdefault("routing", {}).setdefault("rules", [])
    cfg["routing"].setdefault("balancers", [])

    existing = {o.get("tag") for o in cfg["outbounds"]}
    added = []
    for p in ports:
        tag = f"warp-{p}"
        if tag in existing:
            continue
        cfg["outbounds"].append({
            "tag": tag,
            "protocol": "socks",
            "settings": {"servers": [{"address": "127.0.0.1", "port": p}]},
        })
        added.append(tag)

    tags = [f"warp-{p}" for p in ports]
    if not any(b.get("tag") == balancer for b in cfg["routing"]["balancers"]):
        cfg["routing"]["balancers"].append({
            "tag": balancer,
            "selector": tags,
            "strategy": {"type": "roundRobin"},
        })
    return added, tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--ports", default=",".join(map(str, DEFAULT_NODES)))
    ap.add_argument("--tag", default=BALANCER)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()

    path = a.config
    if not os.path.exists(path):
        sys.exit(f"no such file: {path}")

    if a.revert:
        bak = path + ".bak-unlp"
        if not os.path.exists(bak):
            sys.exit(f"no backup at {bak}")
        shutil.copy2(bak, path)
        print(f"restored {path} from {bak}")
        return

    ports = [int(x) for x in a.ports.split(",") if x.strip()]
    dead = [p for p in ports if not alive(p)]
    if dead:
        print(f"warning: nothing listening on {dead} - those outbounds will fail until the pool is up")

    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    added, tags = build(cfg, ports, a.tag)

    if a.dry_run:
        print(f"would add outbounds: {added or '(none, already present)'}")
        print(f"balancer tag: {a.tag} -> {tags}")
        return

    if added:
        shutil.copy2(path, path + ".bak-unlp")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    print(f"added outbounds: {added or '(none, already present)'}")
    print(f"balancer: {a.tag} -> {tags}")
    print()
    print("Route through it by adding a rule that uses balancerTag:")
    print(f'  {{"type":"field","domain":["openrouter.ai"],"balancerTag":"{a.tag}"}}')
    print("Then restart xray / v2rayN to load the config.")


if __name__ == "__main__":
    main()
