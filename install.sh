#!/bin/bash
# One-time setup for scanCam: system packages, then the Python venv.
set -e
cd "$(dirname "$0")"

packages="python3-venv v4l-utils"
# Pi cameras need picamera2 / libcamera, which come from apt (not pip).
if grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
	packages="$packages python3-picamera2 python3-libcamera"
fi

missing=""
for package in $packages; do
	dpkg -s "$package" >/dev/null 2>&1 || missing="$missing $package"
done
if [ -n "$missing" ]; then
	echo "Installing system packages:$missing"
	sudo apt-get update
	sudo apt-get install -y $missing
fi

./createVenv.sh

echo
echo "scanCam is installed. Start it with: ./run.sh"
