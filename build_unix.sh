#!/usr/bin/env bash
# Build GifConvert on Linux or macOS — for the system you run it on.
#   Linux -> dist/GifConvert        macOS -> dist/GifConvert.app
# Use another Python like this:  PYTHON=/path/to/python3 ./build_unix.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! "$PY" -c "import tkinter" 2>/dev/null; then
    echo "This Python ($PY) has no tkinter."
    echo "  Ubuntu/Debian/Mint:  sudo apt install python3-tk python3-venv python3-dev"
    echo "  Fedora:              sudo dnf install python3-tkinter python3-devel"
    echo "  macOS:               install Python from python.org (it includes tkinter)"
    exit 1
fi

VENV=".venv-build"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r requirements-build.txt
"$VENV/bin/python" -m PyInstaller --noconfirm --clean GifConvert.spec

echo
if [ "$(uname -s)" = "Darwin" ]; then
    echo "Done: dist/GifConvert.app"
else
    echo "Done: dist/GifConvert"
fi
