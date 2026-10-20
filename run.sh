#!/bin/sh
cd "$(dirname "$0")" || exit 1
# headless: bring the pool up (idempotent), sync 9router, then open the console
python3 warppool.py init
exec python3 warppool.py cli
