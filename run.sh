#!/bin/bash
# Start scanCam using its venv (create it first with python3 install.py).
cd "$(dirname "$0")"
exec venv/bin/python -u code/scanCam.py "$@"
