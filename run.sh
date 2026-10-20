#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 warppool.py init
python3 warppool.py up
python3 warppool.py serve
