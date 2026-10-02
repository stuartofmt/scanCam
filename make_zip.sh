#!/bin/bash
# Build dist/scanCam-<version>.zip, which unzips to a scanCam/ folder ready for ./install.sh.
set -e
cd "$(dirname "$0")"

version=$(grep -oP "progVersion = '\K[^']+" scanCam.py)
out="$PWD/dist/scanCam-$version.zip"
files="*.py requirements.txt static README.md install.sh createVenv.sh run.sh"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/scanCam"
cp -r $files "$staging/scanCam/"

mkdir -p dist
rm -f "$out"
(cd "$staging" && zip -qr "$out" scanCam -x '*/__pycache__/*')
echo "Created $out"
