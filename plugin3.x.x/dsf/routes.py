import asyncio
import copy
import threading
import time
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from defaults import (
    STATIC_DIR,
    DefaultCameraSettings,
)
from multi_camera import MultiCameraManager
import logger_module


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
    api_preference: Optional[int] = None
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
        if camera_type in {"USB", "PICAMERA"}:
            missing = [
                field for field in ("fps", "width", "height")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError(
                    f"{camera_type} cameras require: {', '.join(missing)}"
                )
        elif camera_type == "STREAM":
            self.fps = self.fps or DefaultCameraSettings.fps.value
            self.width = None
            self.height = None
        else:
            raise ValueError(f"Unsupported camera type: {self.cameratype}")
        return self


# ============================================================
# Streaming Settings
# ============================================================

SNAPSHOT_TIMEOUT_SEC = 3.0

# ============================================================
# FastAPI App
# ============================================================

app = FastAPI()


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if request.url.path in ("/", "/index") or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"

        return response


app.add_middleware(NoCacheStaticMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# camera name -> [{"setting", "min", "max", "default", "current", "step", "choices"}] (see get_config.get_camera_settings)
camera_settings = {}

# Settings changes are applied one at a time, as some restart the camera.
camera_settings_lock = threading.Lock()


def start_cameras():
    manager.start()


def set_camera_settings(configs, settings):
    camera_configs.clear()
    camera_configs.update(configs)
    startup_configs.clear()
    startup_configs.update(copy.deepcopy(configs))
    camera_settings.clear()
    camera_settings.update(settings)


app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)

# ============================================================
# Routes
# ============================================================

@app.get("/")
@app.get("/index")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/cameras")
async def list_cameras():
    return {
        "cameras": [
            {
                "name": name,
                **camera_urls[name],
                "cameratype": camera_configs.get(name, {}).get("cameratype"),
                "settings": camera_settings.get(name, []),
            }
            for name in manager.cameras
        ]
    }


class CameraSettingRequest(BaseModel):
    name: str
    setting: str
    value: float


# Not async: changing a setting can restart the camera, which blocks, so FastAPI runs this in a worker thread.
@app.post("/api/camera-setting")
def api_camera_setting(request: CameraSettingRequest):
    # get_config needs logging set up, so it is imported once the server is running.
    from get_config import get_camera_settings, update_camera_setting

    if request.name not in manager.cameras or request.name not in camera_configs:
        return JSONResponse({"status": "error", "message": f"Unknown camera '{request.name}'"}, status_code=404)

    with camera_settings_lock:
        old_config = camera_configs[request.name]
        try:
            new_config, controls, restart = update_camera_setting(old_config, request.setting, request.value)
        except ValueError as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
        except Exception as e:
            logger_module.logger.error(f"[{request.name}] Could not change {request.setting}: {e}")
            return JSONResponse({"status": "error", "message": f"Could not change {request.setting}: {e}"}, status_code=500)

        try:
            manager.update_camera(request.name, new_config, controls, restart)
        except Exception as e:
            logger_module.logger.error(f"[{request.name}] Could not apply {request.setting}: {e}; restoring previous settings")
            try:
                manager.update_camera(request.name, old_config, restart=restart)
            except Exception as restore_error:
                logger_module.logger.error(f"[{request.name}] Could not restore previous settings: {restore_error}")
            return JSONResponse({"status": "error", "message": f"Could not apply {request.setting}: {e}"}, status_code=500)

        camera_configs[request.name] = new_config
        camera_settings.update(get_camera_settings({request.name: new_config}, startup_configs))

    return {"status": "success", "settings": camera_settings[request.name]}


@app.post("/api/add-camera")
async def api_add_camera(config: CameraConfig):
    try:
        manager.add_camera(
            name=config.name,
            source=config.source,
            fps=config.fps,
            capturefps=config.capturefps,
            width=config.width,
            height=config.height,
            api_preference=config.api_preference,
            rotate=config.rotate,
            jpegresolution=config.jpegresolution,
            cameratype=config.cameratype,
            format=config.format,
            controls=config.controls,
        )
        camera_urls[config.name] = {
            "stream": config.streamname,
            "snapshot": config.snapshotname,
        }
        return {"status": "success", "name": config.name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class StartCameraRequest(BaseModel):
    name: str


@app.post("/api/start-camera")
async def api_start_camera(request: StartCameraRequest):
    try:
        manager.start_camera(request.name)
        return {"status": "success", "name": request.name}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# MJPEG Streaming
# ============================================================

async def mjpeg_generator(request: Request, camera_name: str):
    if camera_name not in manager.cameras:
        return

    frame_count = 0
    last_timestamp = None

    # Capture threads only encode while at least one client is registered.
    manager.add_client(camera_name)
    logger_module.logger.debug(f"Client connected: {camera_name}")
    try:
        while True:
            if await request.is_disconnected():
                logger_module.logger.debug(f"Client disconnected: {camera_name}")
                break

            try:
                # Capture threads already limit output to the camera fps; only send new frames.
                jpg_bytes, timestamp = manager.get_jpeg_with_timestamp(camera_name)

                if jpg_bytes is None or timestamp == last_timestamp:
                    await asyncio.sleep(0.01)
                    continue
                last_timestamp = timestamp

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpg_bytes)).encode() + b"\r\n\r\n" +
                    jpg_bytes +
                    b"\r\n"
                )

                frame_count += 1
                if frame_count % 10000 == 0:  # periodic output
                    logger_module.logger.debug(f"[{camera_name}] Streamed {frame_count} frames")
            except Exception as e:
                logger_module.logger.error(f"Error in mjpeg_generator for {camera_name}: {e}")
                break
    finally:
        manager.remove_client(camera_name)


@app.get("/streaming/{camera_name}")
async def stream_camera(request: Request, camera_name: str):
    if camera_name not in manager.cameras:
        return {"error": f"Unknown camera '{camera_name}'"}

    return StreamingResponse(
        mjpeg_generator(request, camera_name),
        media_type=(
            "multipart/x-mixed-replace;"
            " boundary=frame"
        ),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


async def camera_snapshot(camera_name: str):
    # The cached JPEG may be stale if nobody is streaming, so request a fresh one.
    requested_at = time.time()
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SEC
    manager.add_client(camera_name)
    try:
        while True:
            jpg_bytes, timestamp = manager.get_jpeg_with_timestamp(camera_name)
            if jpg_bytes is not None and timestamp >= requested_at:
                break
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.02)
    finally:
        manager.remove_client(camera_name)

    if jpg_bytes is None:
        return {"error": f"Camera '{camera_name}' has no valid captured frame"}

    return Response(content=jpg_bytes, media_type="image/jpeg")



# Declared last so fixed paths such as /api/cameras and /streaming/<camera> match first.
@app.get("/{camera_name}/{endpoint}")
async def camera_endpoint(request: Request, camera_name: str, endpoint: str):
    if camera_name not in manager.cameras:
        return {"error": f"Unknown camera '{camera_name}'"}

    urls = camera_urls[camera_name]
    if endpoint == urls["stream"]:
        return await stream_camera(request, camera_name)
    if endpoint == urls["snapshot"]:
        return await camera_snapshot(camera_name)
    return {"error": f"Unknown endpoint '{endpoint}' for camera '{camera_name}'"}
