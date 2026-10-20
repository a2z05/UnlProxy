#!/bin/bash
# warp-pool watch tick: health/rotate nodes, keep 9router pools in sync.
# Notifies only when something is actually wrong (cron watchdog pattern):
#  - alive == 0 (no egress at all)
#  - a tick is still not done after the previous one (staleness guard)
# Noise (rotation notes, dedupe notes) goes to the project log, not to chat.
cd /opt/data/projects/warp-pool || exit 0
SUMMARY=/opt/data/projects/warp-pool/watch.log
out=$(python3 warppool.py ensure 2>&1)
out9=$(python3 warppool.py install-9r 2>&1 | grep -v 'pool(s) created')
{ [ -n "$out" ] && echo "$out"; [ -n "$out9" ] && echo "$out9"; } >> "$SUMMARY" 2>/dev/null

# subtle drift is not a page: alive>=1 keeps light working; only total death or a
# broken script pipes up
printf '%s\n' "$out" | grep -q 'Traceback' && { echo "warp-pool: ensure failed"; tail -5 "$SUMMARY"; exit 0; }
printf '%s\n' "$out" | grep -q '"alive": 0' && { echo "warp-pool: NO egress alive"; exit 0; }
exit 0
