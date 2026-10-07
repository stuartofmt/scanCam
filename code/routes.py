import copy
import threading
import time
from typing import Optional

from flask import Flask, Response, request, send_from_directory
from pydantic import BaseModel, ValidationError, model_validator

from defaults import (
    STATIC_DIR,
    DefaultCameraSettings,
)
from multi_camera import MultiCameraManager
from camera_settings import get_camera_settings, update_camera_setting


# ============================================================
# Pydantic Models
# ============================================================


class CameraConfig(BaseModel):
    name: str
    source: str | int
    cameratype: str
    fps: Optional[float] = None
    # USB only: rate requested from the device, when it differs from the served fps.
    capturefps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    rotate: int = 0
    jpegresolution: int = 95
    format: Optional[str] = None
    # URL path segments: /<camera name>/<streamname> and /<camera name>/<snapshotname>
    streamname: str = DefaultCameraSettings.streamname.value
    snapshotname: str = DefaultCameraSettings.snapshotname.value
    # Picamera only: libcamera controls applied when the camera starts.
    controls: Optional[dict] = None

    @model_validator(mode="after")
    def validate_camera_type_settings(self):
        camera_type = self.cameratype.upper()
        if camera_type not in {"USB", "PICAMERA"}:
            raise ValueError(f"Unsupported camera type: {self.cameratype}")
        missing = [
            field for field in ("fps", "width", "height")
            if getattr(self, field) is None
        ]
        if missing:
            raise ValueError(
                f"{camera_type} cameras require: {', '.join(missing)}"
            )
        return self


# ============================================================
# Streaming Settings
# ============================================================

SNAPSHOT_TIMEOUT_SEC = 3.0

NO_CACHE = "no-store, no-cache, must-revalidate, max-age=0"

# ============================================================
# Flask App
# ============================================================

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.after_request
def add_headers(response):
    if request.path.startswith("/static/vendor/"):
        # Vue, Vuetify and the icons (see update_vendor.py) only change with a new release.
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif request.path in ("/", "/index") or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = NO_CACHE
        response.headers["Pragma"] = "no-cache"

    # Allow pages served from elsewhere (e.g. DWC) to use the API.
    response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = request.headers.get("Access-Control-Request-Headers", "*")
    response.headers.add("Vary", "Origin")

    return response


@app.errorhandler(ValidationError)
def invalid_request(error):
    return {"status": "error", "errors": error.errors(include_url=False, include_context=False)}, 422


def request_model(model):
    """The JSON request body, checked against a pydantic model (ValidationError if invalid)."""
    return model.model_validate(request.get_json(silent=True) or {})

# ============================================================
# Camera Manager
# ============================================================

manager = MultiCameraManager()

# camera name -> {"stream": streamname, "snapshot": snapshotname}
camera_urls = {}

# camera name -> config the camera runs with (see get_config.configure_cameras)
camera_configs = {}

# camera name -> config the camera started with, for the defaults shown on the index page
startup_configs = {}

# camera name -> [{"setting", "min", "max", "default", "current", "step", "choices"}] (see camera_settings.get_camera_settings)
camera_settings = {}

# Cameras skipped at startup because another process is using them: [{"name", "source", "cameratype"}]
in_use_cameras = []

# Settings changes are applied one at a time, as some restart the camera.
camera_settings_lock = threading.Lock()


def set_camera_settings(configs, settings):
    camera_configs.clear()
    camera_configs.update(configs)
    startup_configs.clear()
    startup_configs.update(copy.deepcopy(configs))
    camera_settings.clear()
    camera_settings.update(settings)


def set_in_use_cameras(cameras):
    in_use_cameras.clear()
    in_use_cameras.extend(cameras)


def add_camera(config: dict):
    """Register a camera (a config as returned by get_config.configure_cameras); it starts when first viewed."""
    camera = CameraConfig.model_validate(config)
    manager.add_camera(
        name=camera.name,
        source=camera.source,
        fps=camera.fps,
        capturefps=camera.capturefps,
        width=camera.width,
        height=camera.height,
        rotate=camera.rotate,
        jpegresolution=camera.jpegresolution,
        cameratype=camera.cameratype,
        format=camera.format,
        controls=camera.controls,
    )
    camera_urls[camera.name] = {
        "stream": camera.streamname,
        "snapshot": camera.snapshotname,
    }

# ============================================================
# Routes
# ============================================================

@app.get("/")
@app.get("/index")
def root():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/cameras")
def list_cameras():
    return {
        "cameras": [
            {
                "name": name,
                **camera_urls[name],
                "cameratype": camera_configs.get(name, {}).get("cameratype"),
                "settings": camera_settings.get(name, []),
            }
            for name in manager.cameras
        ],
        "in_use": in_use_cameras,
    }


class CameraSettingRequest(BaseModel):
    name: str
    setting: str
    value: float


# Changing a setting can restart the camera, which blocks this request's thread only.
@app.post("/api/camera-setting")
def api_camera_setting():
    setting_request = request_model(CameraSettingRequest)

    if setting_request.name not in manager.cameras or setting_request.name not in camera_configs:
        return {"status": "error", "message": f"Unknown camera '{setting_request.name}'"}, 404

    with camera_settings_lock:
        old_config = camera_configs[setting_request.name]
        try:
            new_config, controls, restart = update_camera_setting(old_config, setting_request.setting, setting_request.value)
        except ValueError as e:
            return {"status": "error", "message": str(e)}, 400
        except Exception as e:
            print(f"[{setting_request.name}] Could not change {setting_request.setting}: {e}")
            return {"status": "error", "message": f"Could not change {setting_request.setting}: {e}"}, 500

        try:
            manager.update_camera(setting_request.name, new_config, controls, restart)
        except Exception as e:
            print(f"[{setting_request.name}] Could not apply {setting_request.setting}: {e}; restoring previous settings")
            try:
                manager.update_camera(setting_request.name, old_config, restart=restart)
            except Exception as restore_error:
                print(f"[{setting_request.name}] Could not restore previous settings: {restore_error}")
            return {"status": "error", "message": f"Could not apply {setting_request.setting}: {e}"}, 500

        camera_configs[setting_request.name] = new_config
        camera_settings.update(get_camera_settings({setting_request.name: new_config}, startup_configs))

    return {"status": "success", "settings": camera_settings[setting_request.name]}


# ============================================================
# Exit When The Page Is Closed
# ============================================================

# A closed page has this long to open again (a reload does) before the program exits.
PAGE_CLOSED_GRACE_SEC = 5.0

# ids of the index pages open in a browser (see index.js)
open_pages = set()
open_pages_lock = threading.Lock()

# Called (from a timer thread) once the last open page has been closed; see set_exit_on_page_close.
exit_handler = None
exit_timer: Optional[threading.Timer] = None


def set_exit_on_page_close(handler):
    """Call handler once every open page has been closed and nothing else is streaming."""
    global exit_handler
    exit_handler = handler


def _schedule_exit_check():
    global exit_timer
    if exit_timer is not None:
        exit_timer.cancel()
    exit_timer = threading.Timer(PAGE_CLOSED_GRACE_SEC, _exit_if_unused)
    exit_timer.daemon = True
    exit_timer.start()


def _exit_if_unused():
    with open_pages_lock:
        if open_pages:
            return
        if manager.has_clients():
            # Still being viewed elsewhere (e.g. a stream opened in its own tab, or DWC).
            _schedule_exit_check()
            return
    print("The scanCam page was closed - exiting")
    exit_handler()


# The page reports these with navigator.sendBeacon, which sends a POST with no JSON body.
@app.post("/api/page-opened")
def api_page_opened():
    with open_pages_lock:
        open_pages.add(request.args.get("id", ""))
        if exit_timer is not None:
            exit_timer.cancel()
    return "", 204


@app.post("/api/page-closed")
def api_page_closed():
    with open_pages_lock:
        open_pages.discard(request.args.get("id", ""))
        if not open_pages and exit_handler is not None:
            _schedule_exit_check()
    return "", 204


# ============================================================
# MJPEG Streaming
# ============================================================

def mjpeg_generator(camera_name: str):
    if camera_name not in manager.cameras:
        return

    frame_count = 0
    last_timestamp = None

    # Capture threads only encode while at least one client is registered.
    manager.add_client(camera_name)
    print(f"Client connected: {camera_name}")
    try:
        try:
            manager.ensure_running(camera_name)
        except Exception as e:
            print(f"Could not start {camera_name}: {e}")
            return
        while True:
            try:
                # Capture threads already limit output to the camera fps; only send new frames.
                jpg_bytes, timestamp = manager.get_jpeg_with_timestamp(camera_name)

                if jpg_bytes is None or timestamp == last_timestamp:
                    time.sleep(0.01)
                    continue
                last_timestamp = timestamp

                # When the client disconnects, the server closes this generator here (GeneratorExit).
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpg_bytes)).encode() + b"\r\n\r\n" +
                    jpg_bytes +
                    b"\r\n"
                )

                frame_count += 1
                if frame_count % 10000 == 0:  # periodic output
                    print(f"[{camera_name}] Streamed {frame_count} frames")
            except Exception as e:
                print(f"Error in mjpeg_generator for {camera_name}: {e}")
                break
    finally:
        manager.remove_client(camera_name)
        print(f"Client disconnected: {camera_name}")


@app.get("/streaming/<camera_name>")
def stream_camera(camera_name: str):
    if camera_name not in manager.cameras:
        return {"error": f"Unknown camera '{camera_name}'"}

    # Started here as well so a camera that cannot open gets an error response.
    try:
        manager.ensure_running(camera_name)
    except Exception as e:
        print(f"Could not start {camera_name}: {e}")
        return {"error": f"Could not start camera '{camera_name}': {e}"}, 503

    return Response(
        mjpeg_generator(camera_name),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": NO_CACHE},
    )


def camera_snapshot(camera_name: str):
    # The cached JPEG may be stale if nobody is streaming, so request a fresh one.
    requested_at = time.time()
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SEC
    manager.add_client(camera_name)
    try:
        try:
            manager.ensure_running(camera_name)
        except Exception as e:
            print(f"Could not start {camera_name}: {e}")
            return {"error": f"Could not start camera '{camera_name}': {e}"}, 503
        while True:
            jpg_bytes, timestamp = manager.get_jpeg_with_timestamp(camera_name)
            if jpg_bytes is not None and timestamp >= requested_at:
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)
    finally:
        manager.remove_client(camera_name)

    if jpg_bytes is None:
        return {"error": f"Camera '{camera_name}' has no valid captured frame"}

    return Response(jpg_bytes, mimetype="image/jpeg")


# Fixed paths such as /api/cameras and /streaming/<camera> take precedence over this one.
@app.get("/<camera_name>/<endpoint>")
def camera_endpoint(camera_name: str, endpoint: str):
    if camera_name not in manager.cameras:
        return {"error": f"Unknown camera '{camera_name}'"}

    urls = camera_urls[camera_name]
    if endpoint == urls["stream"]:
        return stream_camera(camera_name)
    if endpoint == urls["snapshot"]:
        return camera_snapshot(camera_name)
    return {"error": f"Unknown endpoint '{endpoint}' for camera '{camera_name}'"}
