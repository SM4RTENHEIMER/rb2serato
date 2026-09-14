#!/bin/sh
# One-time setup: a virtualenv with the two dependencies, using macOS' own Python 3.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
[ -d .venv ] || /usr/bin/python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "Ready. Try: ./rb2serato plan --xml <your-export.xml>"
