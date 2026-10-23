#!/bin/sh
# macOS: double-click this file to start mini-server.
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    python3 mini_server.py "$@"
else
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
fi
echo
echo "mini-server has stopped. Press Return to close this window."
read -r _
