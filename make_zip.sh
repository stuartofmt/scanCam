#!/bin/bash
# Build dist/scanCam-<version>.zip, which unzips to a scanCam/ folder holding
# README.md, install.py and the program in code/.
set -e
cd "$(dirname "$0")"

version=$(grep -oP "progVersion = '\K[^']+" code/scanCam.py)
out="$PWD/dist/scanCam-$version.zip"
files="README.md install.py code"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/scanCam"
cp -r $files "$staging/scanCam/"

mkdir -p dist
rm -f "$out"
(cd "$staging" && zip -qr "$out" scanCam -x '*/__pycache__/*')
echo "Created $out"
