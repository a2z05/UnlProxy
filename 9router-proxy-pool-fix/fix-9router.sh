#!/bin/sh
# 9router proxy-pool fix - Linux / macOS launcher for apply.mjs
#
#   ./fix-9router.sh              patch the installed 9router
#   ./fix-9router.sh --check      is it patched?
#   ./fix-9router.sh --revert     undo
#
# Any argument apply.mjs takes works through here, e.g.
#   ./fix-9router.sh --dir /usr/local/lib/node_modules/9router
#
# The logic lives in apply.mjs; this only finds node and runs it.
set -eu
cd "$(dirname "$0")"

if [ ! -f apply.mjs ]; then
  echo "apply.mjs is missing - it belongs next to this script." >&2
  echo "Download the whole folder, not just this file:" >&2
  echo "  https://github.com/a2z05/UnlProxy/tree/master/9router-proxy-pool-fix" >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "node not found. 9router runs on Node, so it should be installed;" >&2
  echo "if it is, open a new shell so PATH picks it up." >&2
  exit 1
fi

exec node apply.mjs "$@"
