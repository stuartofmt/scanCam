#!/bin/bash
# Create (or recreate) the venv for scanCam next to this script.
# --system-site-packages lets the venv use picamera2 / libcamera installed with apt.
cd "$(dirname "$0")"
rm -rf venv
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt
echo "venv ready"
