"""
Raspberry Pi (CSI) camera configuration via Picamera2: controls, sensor modes and discovery.
"""

import copy
import errno
from typing import Dict, Optional, Tuple

from config_common import (AllowedOptions, CameraInUseError, add_applied_options, clamp,
						highlight_print, next_lower_resolution)

try:
	from picamera2 import Picamera2
except ImportError:
	# Not a Raspberry Pi (picamera2 is installed with apt); only USB cameras are used.
	Picamera2 = None


# Maps each allowable canonical key to the real Picamera2/libcamera control name.
CONTROL_NAME_MAP_PICAM = {
	"brightness": "Brightness",
	"contrast": "Contrast",
	"balance": "AwbEnable",
	"saturation": "Saturation",
	"autofocus": "AfMode",
	"sharpness": "Sharpness",
	"autoexposure": "AeEnable",
}

# Reverse lookup: real Picamera2 control name -> canonical name
PI_REAL_TO_CANONICAL = {v: k for k, v in CONTROL_NAME_MAP_PICAM.items()}


# source -> {"controls": {canonical: bounds}, "modes": {(w, h): max_fps}}
_PICAM_INFO_CACHE = {}


def cached_picam_info(source):
	"""{"controls": ..., "modes": ...} found by get_camera_options_picam, or {}."""
	return _PICAM_INFO_CACHE.get(source, {})


def _picam_error_is_busy(error):
	"""Return True if a Picamera2 error, or any error it was raised from, is EBUSY."""
	while error is not None:
		if getattr(error, 'errno', None) == errno.EBUSY or 'Device or resource busy' in str(error):
			return True
		error = error.__cause__ or error.__context__
	return False


def get_camera_options_picam(camera_name, source):
	"""
	Return min/max/default for the AllowedOptions controls a Pi camera supports.

	No reset is needed: libcamera controls only last for one camera session,
	so every new session starts at the defaults. The camera is opened once per
	run; its controls and sensor modes are cached.

	Returns:
		{source: {canonical_name: {"min": ..., "max": ..., "default": ...}}}
	"""
	if source not in _PICAM_INFO_CACHE:
		try:
			picam2 = Picamera2(camera_num=source)
		except RuntimeError as e:
			# Picamera2 reports "Camera __init__ sequence did not complete."; the
			# underlying acquire() error says whether the camera is busy.
			if _picam_error_is_busy(e):
				raise CameraInUseError(f'Camera {source} is in use by another process') from e
			raise
		try:
			all_controls = picam2.camera_controls
			sensor_modes = picam2.sensor_modes
		finally:
			picam2.close()

		controls = {}
		for real_name, (min_val, max_val, default_val) in all_controls.items():
			canonical_name = PI_REAL_TO_CANONICAL.get(real_name)
			if canonical_name is None or default_val is None:
				continue
			controls[canonical_name] = {
				"min": min_val if min_val is not None else default_val,
				"max": max_val if max_val is not None else default_val,
				"default": default_val,
			}

		modes = {}
		for mode in sensor_modes:
			wh = tuple(mode["size"])
			modes[wh] = max(modes.get(wh, 0.0), float(mode["fps"]))

		_PICAM_INFO_CACHE[source] = {"controls": controls, "modes": modes}
		print(f'[{camera_name}] PI CONTROLS = {controls}')

	return {source: _PICAM_INFO_CACHE[source]["controls"]}


def set_controls_picam(controls_by_source, camera_config_options=None):
	"""
	Resolve requested options into libcamera control values, clamped to
	each control's [min, max].

	Values are not written to the camera here: libcamera controls reset when
	the camera is closed, so they are passed to the stream and applied when
	it starts.

	Args:
		controls_by_source: {source: {canonical_name: {"min", "max", "default"}}}
			as returned by get_camera_options_picam.
		camera_config_options: requested options; only these are set, since
			every other control already starts at its default.

	Returns:
		{source: {real_name: value}}
	"""

	resolved_by_source = {}

	for source, controls in controls_by_source.items():
		resolved = {}

		for canonical_name, value in (camera_config_options or {}).items():
			if canonical_name not in AllowedOptions.__members__:
				continue
			bounds = controls.get(canonical_name)
			if bounds is None:
				print(f"[{source}] {canonical_name}: not present on this camera")
				continue

			value = clamp(source, canonical_name, value, bounds)
			# Config values are floats; libcamera needs the control's own type (bool/int/float).
			value = type(bounds["default"])(value)
			resolved[CONTROL_NAME_MAP_PICAM[canonical_name]] = value
			print(f"[{source}] {canonical_name}: will be set to {value}")

		resolved_by_source[source] = resolved

	return resolved_by_source


def _get_picam_sensor_modes(source) -> "dict[Tuple[int, int], float]":
	"""Return {(width, height): max_fps} for each sensor mode of a Pi camera."""

	get_camera_options_picam(f"PiCamera-{source}", source)
	return _PICAM_INFO_CACHE[source]["modes"]


def validate_pi_camera_configs(cameras: Dict[str, dict], applied_options: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
	"""
	Pi camera equivalent of validate_usb_camera_configs.

	Adjusts 'width'/'height' to a supported sensor mode and caps 'fps'
	at that mode's maximum. Format is not validated because the Pi
	stream always captures RGB888.

	Any AllowedOptions in applied_options[<camera name>] (the values
	actually set, after clamping) are added to that camera's entry.

	Returns a NEW dict (the input is not mutated). Entries whose sensor
	modes can't be queried are otherwise returned unchanged.
	"""

	adjusted = copy.deepcopy(cameras)
	for cam_name, cam in adjusted.items():
		if cam.get('cameratype') != 'PICAMERA':
			continue
		add_applied_options(cam, (applied_options or {}).get(cam_name))

		source = cam.get("source")
		try:
			modes = _get_picam_sensor_modes(source)
		except Exception as e:
			print(f"[{cam_name}] Could not query sensor modes for camera {source}: {e}")
			continue

		if not modes:
			print(f"[{cam_name}] No sensor modes reported for camera {source}; skipping")
			continue

		requested_wh = (int(cam["width"]), int(cam["height"]))
		requested_fps = float(cam["fps"])

		# Sensor modes are raw readout sizes; the ISP scales the output stream
		# to any size, so any resolution within a sensor mode is supported.
		covering = [wh for wh in modes if wh[0] >= requested_wh[0] and wh[1] >= requested_wh[1]]
		if covering:
			chosen_wh = requested_wh
		else:
			chosen_wh = next_lower_resolution(requested_wh, list(modes.keys()))
			covering = [chosen_wh]
			print(
				f"[{cam_name}] Resolution {requested_wh[0]}x{requested_wh[1]} exceeds "
				f"the sensor on camera {source}; Adjusting to "
				f"{chosen_wh[0]}x{chosen_wh[1]}"
			)

		cam["width"], cam["height"] = chosen_wh

		# Pi sensors accept any frame rate up to the maximum of the sensor mode
		# libcamera picks: the fastest mode large enough for the output.
		max_fps = max(modes[wh] for wh in covering)
		if requested_fps > max_fps:
			print(
				f"[{cam_name}] fps {int(requested_fps)} not supported at "
				f"{chosen_wh[0]}x{chosen_wh[1]} on camera {source}; "
				f"falling back to {int(max_fps)}"
			)
			cam["fps"] = max_fps
		else:
			cam["fps"] = requested_fps

	return adjusted


def find_pi_cameras():
	"""
	Log the Pi (non-USB) cameras found by Picamera2 and the options each
	supports.

	Returns a list of the Picamera2 slots of those cameras. These match the
	camera sources used by get_detected_cameras.
	"""
	try:
		pi_camera_indices = get_pi_camera_indices()

		camera_results = []
		if pi_camera_indices:
			camera_results.append(f"PiCamera(s) found with these options:")
			for index, pi_camera_index in enumerate(pi_camera_indices):
				camera_results.append(f"\n--index {index}")
				try:
					camera_options = get_camera_options_picam(f"PiCamera-{index}", pi_camera_index)
					if camera_options.get(pi_camera_index):
						for control_name, bounds in camera_options[pi_camera_index].items():
							min_val = bounds.get("min", "N/A")
							max_val = bounds.get("max", "N/A")
							default_val = bounds.get("default", "N/A")
							camera_results.append(f"   {control_name}: min={min_val}, max={max_val}, default={default_val}")
				except CameraInUseError:
					camera_results.append("   In use by another process")
				except Exception as e:
					print(f"Could not query options for camera index {index}: {e}")
		else:
			camera_results.append("No Pi cameras found.")

		highlight_print(camera_results)

		return pi_camera_indices
	except Exception as e:
		raise Exception(f"Error listing Pi cameras - {e}") from e


def get_pi_camera_indices():
	"""
	Return the Picamera2 camera numbers of the Pi (CSI) cameras, in
	global_camera_info order.

	libcamera also lists USB cameras (uvcvideo pipeline), so only cameras
	on a Raspberry Pi pipeline (rpi/vc4, rpi/pisp) are kept.
	"""
	if Picamera2 is None:
		return []

	# global_camera_info is sorted by Id, so its order is not the camera number; 'Num' is.
	# It omits PipelineHandler, so read that from the libcamera camera itself.
	libcamera_cameras = Picamera2._cm.cms.cameras
	pi_cameras = []
	for cam_info in Picamera2.global_camera_info():
		num = cam_info['Num']
		properties = {k.name: v for k, v in libcamera_cameras[num].properties.items()}
		pipeline = properties.get('PipelineHandler')
		if pipeline is not None:
			is_pi_camera = pipeline.startswith('rpi/')
		else:
			# Older libcamera doesn't report PipelineHandler.
			is_pi_camera = '/usb' not in cam_info['Id'].lower()
		if is_pi_camera:
			pi_cameras.append(num)
	return pi_cameras
