# scanCam

View the USB and Raspberry Pi cameras attached to a Linux machine in a web page, and adjust their settings live.

## Install

```bash
unzip scanCam-<version>.zip
cd scanCam
./install.sh
```

`install.sh` installs any missing system packages (`v4l-utils`, and `python3-picamera2` / `python3-libcamera` on a Raspberry Pi) using `sudo apt-get`, then creates a Python venv in the `scanCam` folder.

## Run

```bash
./run.sh
```

The address to open is printed at startup, e.g. `View cameras at http://192.168.1.20:17800`. When started from a desktop session, it also opens in the default browser.

Cameras are only opened while they are being viewed. The user running scanCam needs access to `/dev/video*` (membership of the `video` group, the default on Raspberry Pi OS).

Stop it with Ctrl+C.
