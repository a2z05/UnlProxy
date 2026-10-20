#!/bin/sh
# UnlProxy - one command install (Linux / macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/a2z05/UnlProxy/master/install.sh | sh
#
# Gets Python 3 + curl if missing, drops the app in ~/.unlproxy, brings the pool
# up, installs a self-driving rotation daemon, and puts `unlp` on PATH.
# Nothing needs root.
set -eu

DIR="${UNLPROXY_DIR:-$HOME/.unlproxy}"
RAW="${UNLPROXY_RAW:-https://raw.githubusercontent.com/a2z05/UnlProxy/master}"
NODES="${UNLPROXY_NODES:-3}"

say() { printf '\033[1;31m>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!\033[0m %s\n' "$*" >&2; exit 1; }

# --- deps -------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  say "installing python3"
  if command -v apt-get >/dev/null 2>&1; then sudo apt-get update -qq && sudo apt-get install -y -qq python3 curl
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y -q python3 curl
  elif command -v apk >/dev/null 2>&1; then sudo apk add --no-cache python3 curl
  elif command -v pacman >/dev/null 2>&1; then sudo pacman -Sy --noconfirm python curl
  else die "no python3 and no known package manager - install Python 3.9+ and curl, then rerun"; fi
fi
command -v curl >/dev/null 2>&1 || die "curl is required (the pool measures egress through it)"

# --- files ------------------------------------------------------------------
say "installing into $DIR"
mkdir -p "$DIR/assets" "$DIR/scripts"
for f in warppool.py ui.html config.example.json unlp assets/hero.svg assets/architecture.svg; do
  curl -fsSL "$RAW/$f" -o "$DIR/$f"
done
chmod +x "$DIR/warppool.py" "$DIR/unlp"

# --- pool -------------------------------------------------------------------
say "bringing $NODES node(s) up (first run downloads usque and registers with Cloudflare)"
cd "$DIR"
python3 warppool.py config --nodes "$NODES" --quiet 2>/dev/null || true
python3 warppool.py init || die "init failed - see $DIR/nodes/*.log"
python3 warppool.py status

# --- background daemon ------------------------------------------------------
# rotation is self-driving inside `daemon`; no cron entry is needed any more
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/unlproxy.service" <<EOF
[Unit]
Description=UnlProxy WARP egress pool
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$(command -v python3) $DIR/warppool.py daemon
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now unlproxy.service
  say "systemd user service: unlproxy (starts on login, restarts on crash)"
else
  say "no systemd user session - starting in the background with nohup"
  cd "$DIR" && nohup python3 warppool.py daemon >> "$DIR/daemon.log" 2>&1 &
  say "daemon pid $! (log: $DIR/daemon.log) - rerun this script after a reboot"
fi

# `unlp` on PATH: opens the UI in the browser. Also unlproxy for the raw app.
if [ -d "$HOME/.local/bin" ] && [ -w "$HOME/.local/bin" ]; then
  printf '#!/bin/sh
exec python3 "%s/warppool.py" "$@"
' "$DIR" > "$HOME/.local/bin/unlproxy"
  chmod +x "$HOME/.local/bin/unlproxy"
  printf '#!/bin/sh
exec "%s/unlp" "$@"
' "$DIR" > "$HOME/.local/bin/unlp"
  chmod +x "$HOME/.local/bin/unlp"
  say "launchers: unlp (opens the UI), unlproxy (raw app) - in ~/.local/bin"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) say "note: ~/.local/bin is not on your PATH - add it with: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac
fi

say "done."
echo
echo "  unlp   open the control UI in your browser"
echo "  cli    cd $DIR && python3 warppool.py cli      # status, rotate, nodes, 9r"
echo "  ui     cd $DIR && python3 warppool.py serve    # http://127.0.0.1:8180/"
echo "  socks  127.0.0.1:18090 ... :$((18090 + NODES - 1))"
