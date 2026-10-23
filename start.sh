#!/bin/sh
# Linux: run ./start.sh in a terminal to start mini-server.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 was not found. Install it with your package manager (e.g. python3)."
    exit 1
fi
exec python3 mini_server.py "$@"
