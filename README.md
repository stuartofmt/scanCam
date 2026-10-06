# scanCam

View the USB and Raspberry Pi cameras attached to a Linux or Windows machine in a web page, and adjust their settings live.

## Install

### What the computer needs first

`install.py` installs scanCam's Python packages into its own venv, but nothing system-wide. It doesn't use `sudo`, so install these yourself first:

- **Linux / Raspberry Pi OS**:

  ```bash
  sudo apt install python3-venv v4l-utils
  ```

  `v4l-utils` provides `v4l2-ctl`, which scanCam uses for USB cameras. On a Raspberry Pi, Raspberry Pi OS already includes the camera packages (`python3-picamera2`, `python3-libcamera`), and the venv uses them as they are. On a stripped-down system, add them with `sudo apt install python3-picamera2`. To choose the install folder in a window rather than typing it, you also need `python3-tk`, which Raspberry Pi OS Desktop already has.

- **Windows**: Python 3.10 or later from [python.org](https://www.python.org/downloads/). Its installer includes everything `install.py` needs.

The first install needs internet access, to download the Python packages listed in `requirements.txt`.

### Installing

Unzip the release and run `install.py` in the unzipped `scanCam` folder:

```bash
unzip scanCam-<version>.zip
cd scanCam
python3 install.py
```

On Windows, unzip `scanCam-<version>.zip` and, in a Command Prompt:

```bat
cd scanCam
py install.py
```

`install.py` then:

1. **Asks where to install scanCam.**
   - **On a desktop**, a folder window opens. Go to where you want to install and press OK: a `scanCam` folder is made there. To install over an earlier install, pick its `scanCam` folder. To name the folder yourself, type its full path in the **Selection** box: a folder that doesn't exist yet is created and used as typed. You then confirm the final path.
   - **Over SSH, without a screen, or without tkinter**, it asks in the terminal instead. Press Enter to accept the default, `~/scanCam` (`%USERPROFILE%\scanCam` on Windows).
2. **Copies** `README.md` and the `code` folder there.
3. **Creates scanCam's venv** in that folder and installs `requirements.txt` into it with the venv's own pip. On Linux, the venv can also use Python packages installed on the system, such as `picamera2`.
4. **Adds a launcher**: `run.sh`, or `run.bat` on Windows.
5. **Starts scanCam** straight away, once everything has installed.

Options:

```bash
python3 install.py /home/pi/scanCam   # install into this folder, without asking
python3 install.py --no-gui           # ask for the folder in the terminal, not in a window
python3 install.py --no-run           # install without starting scanCam
```

Running `install.py` again updates the program files and recreates the venv. If the install fails, the terminal shows why (and so does a message box, if you chose the folder in a window). The two usual causes are a missing `python3-venv` and no internet access.

## Run

`install.py` starts scanCam when it finishes installing. After that, start it from the install directory:

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
