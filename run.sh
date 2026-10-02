#!/bin/bash
# Start scanCam using its venv (create it first with ./install.sh).
cd "$(dirname "$0")"
exec venv/bin/python -u scanCam.py "$@"
