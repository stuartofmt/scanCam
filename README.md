# scanCam

View the USB and Raspberry Pi cameras attached to a Linux or Windows machine in a web page, and adjust their settings live.

## Install

### Linux

```bash
unzip scanCam-<version>.zip
cd scanCam
python3 install.py
```

### Windows

Install Python 3.10 or later from [python.org](https://www.python.org/downloads/), then unzip `scanCam-<version>.zip` and, in a Command Prompt:

```bat
cd scanCam
py install.py
```

`install.py` asks which directory to install scanCam into (default `~/scanCam`, or `%USERPROFILE%\scanCam` on Windows; press Enter to accept it) and copies `README.md` and the `code` folder there. On Linux it then installs any missing system packages (`v4l-utils`, and `python3-picamera2` / `python3-libcamera` on a Raspberry Pi) using `sudo apt-get`. Finally it creates a Python venv and a launcher (`run.sh`, or `run.bat` on Windows) in that directory. Running it again updates the program files and recreates the venv.

## Run

From the install directory:

```bash
./run.sh
```

or on Windows:

```bat
run.bat
```

The address to open is printed at startup, e.g. `View cameras at http://192.168.1.20:17800`. On the machine itself, `http://localhost:17800` also works. When started from a desktop session, it also opens in the default browser.

Cameras are only opened while they are being viewed. On Linux, the user running scanCam needs access to `/dev/video*` (membership of the `video` group, the default on Raspberry Pi OS).

Cameras that another program is using when scanCam starts are left alone and listed, both at the end of the startup output and on the web page, as in use and unavailable. They are only checked at startup: close the other program and restart scanCam to use them. On Windows a busy camera can't be detected at startup; it shows an error when it is viewed instead.

On Windows, scanCam uses every camera DirectShow lists, including virtual cameras such as OBS Virtual Camera. The first time it runs, Windows may ask whether to allow it through the firewall; allow it on private networks to view the cameras from other machines.

Closing the scanCam page in the browser stops scanCam a few seconds later; switching to another tab or window, or minimising the browser, doesn't. With several pages open, it stops when the last one is closed, and not while a camera stream is still being viewed elsewhere (e.g. a stream link opened in its own tab, or in DWC). If no page is ever opened, it runs until stopped.

Stop it with Ctrl+C.
