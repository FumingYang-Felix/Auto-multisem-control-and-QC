#!/usr/bin/env bash
# One-click environment setup for the MultiSEM GUI (macOS / Linux).
# Installs deps into python3 on PATH. Pass --venv to use an isolated .venv.
set -e
cd "$(dirname "$0")"
python3 setup_env.py "$@"
