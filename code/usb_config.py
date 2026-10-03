"""
USB camera configuration: controls, formats and discovery.

On Linux this uses v4l2-ctl and /dev/videoN devices; on Windows, DirectShow (dshow.py).

validate_usb_camera_configs validates a camera-config dict against what each /dev/videoN device
actually reports via `v4l2-ctl -d <source> --list-formats-ext`, and
adjusts format / resolution / fps to the closest supported values.

Fallback rules (as specified):
  1. If the requested format (MJPEG/MPEG -> MJPG) isn't supported,
	 fall back to the first format the device reports.
  2. If the requested width/height IS supported under the chosen
	 format, but the requested fps is not, drop to the next lower
	 fps available at that resolution, or if there is none, rise to
	 the next higher one.
  3. If the requested width/height is NOT supported, drop to the
	 next lower resolution (by pixel area) available under the
	 chosen format, or if there is none, rise to the next size up. If the original fps isn't available at that new
	 resolution, drop to the next lower fps available there (or the
	 next higher one if there is none lower).
"""

import copy
import errno
import glob
import os
import re
import struct
import subprocess
from typing import Dict, List, Optional, Tuple

from config_common import (AllowedOptions, CameraInUseError, add_applied_options, clamp,
						highlight_print, next_lower_resolution)
from defaults import IS_WINDOWS

if IS_WINDOWS:
	import dshow
else:
	import fcntl


# Maps each allowable canonical key to the real v4l2 control name (v4l2-ctl --list-ctrls).
CONTROL_NAME_MAP_USB = {
	"brightness": "brightness",
	"contrast": "contrast",
	"balance": "white_balance_temperature_auto",
	"saturation": "saturation",
	"autofocus": "focus_auto",
	"sharpness": "sharpness",
	"autoexposure": "exposure_auto",
}

# Some controls go by different names depending on driver/kernel version.
# First alias found in --list-ctrls output wins.
CONTROL_NAME_ALIASES_USB = {
	"white_balance_temperature_auto": ["white_balance_temperature_auto", "white_balance_automatic"],
	"exposure_auto": ["exposure_auto", "auto_exposure"],
	"focus_auto": ["focus_auto", "auto_focus"],
}


# Control names and ranges don't change while running, so query each device once.
_USB_CTRLS_CACHE = {}
_USB_OPTIONS_CACHE = {}


def cached_usb_options(source):
	"""{canonical_name: {"min", "max", "default"}} found by get_camera_options_usb, or {}."""
	return _USB_OPTIONS_CACHE.get(source, {}).get(source, {})


def _parse_list_ctrls(device_path):
	"""
	Run v4l2-ctl --list-ctrls and parse out {real_name: (min, max, default)}
	for every control the driver reports.
	"""
	if device_path not in _USB_CTRLS_CACHE:
		if IS_WINDOWS:
			_USB_CTRLS_CACHE[device_path] = dshow.list_controls(device_path)
		else:
			_USB_CTRLS_CACHE[device_path] = _query_list_ctrls(device_path)
	return _USB_CTRLS_CACHE[device_path]


def _set_usb_ctrls(device_path, values):
	"""
	Set several controls with one v4l2-ctl call. v4l2-ctl still applies the
	others when one fails and names each failure on stderr, so return those names.
	"""
	if not values:
		return set()
	if IS_WINDOWS:
		return dshow.set_controls(device_path, values)

	ctrl_arg = ",".join(f"{name}={value}" for name, value in values.items())
	try:
		result = subprocess.run(
			["v4l2-ctl", "-d", device_path, "--set-ctrl", ctrl_arg],
			capture_output=True,
			text=True,
		)
	except FileNotFoundError:
		print(
			"Error: 'v4l2-ctl' utility not found. Install it using 'sudo apt install v4l-utils'."
		)
		return set(values)

	if result.returncode == 0:
		return set()

	failed = {
		name for name in re.findall(r"^(\w+):", result.stderr, re.MULTILINE)
		if name in values
	}
	# A failure that names no control (e.g. device error) means nothing can be trusted.
	return failed or set(values)


def _query_list_ctrls(device_path):
	try:
		result = subprocess.run(
			["v4l2-ctl", "-d", device_path, "--list-ctrls"],
			check=True,
			capture_output=True,
			text=True,
		)
	except subprocess.CalledProcessError as e:
		print(f"Error listing controls for {device_path}: {e.stderr}")
		return {}
	except FileNotFoundError:
		print(
			"Error: 'v4l2-ctl' utility not found. Install it using 'sudo apt install v4l-utils'."
		)
		return {}

	all_controls = {}
	for line in result.stdout.splitlines():
		line_clean = line.strip()
		# Example: "brightness 0x00980900 (int) : min=-64 max=64 step=1 default=0 value=0"
		name_match = re.match(r"^(\w+)\s+0x[0-9a-fA-F]+", line_clean)
		if not name_match:
			continue
		real_name = name_match.group(1)

		min_match = re.search(r"min=(-?\d+)", line_clean)
		max_match = re.search(r"max=(-?\d+)", line_clean)
		default_match = re.search(r"default=(-?\d+)", line_clean)

		min_val = int(min_match.group(1)) if min_match else None
		max_val = int(max_match.group(1)) if max_match else None
		default_val = int(default_match.group(1)) if default_match else None

		all_controls[real_name] = (min_val, max_val, default_val)

	return all_controls


def _resolve_real_name(canonical_name, real_control_names):
	"""
	Given a canonical key, find which real control name (accounting for
	driver-specific aliases) is actually present on this camera.
	"""
	base_real_name = CONTROL_NAME_MAP_USB.get(canonical_name)
	if base_real_name is None:
		return None

	candidates = CONTROL_NAME_ALIASES_USB.get(base_real_name, [base_real_name])
	for candidate in candidates:
		if candidate in real_control_names:
			return candidate
	return None


# VIDIOC_REQBUFS = _IOWR('V', 8, struct v4l2_requestbuffers), a 20-byte struct:
# count, type, memory, capabilities (u32 each), flags (u8), reserved[3] (u8).
_VIDIOC_REQBUFS = 0xC0145608
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_V4L2_MEMORY_MMAP = 1


def _usb_camera_in_use(device_path):
	"""
	Return True if another process owns the capture stream of a V4L2 device.

	V4L2 lets any number of processes open a device and change its controls;
	only the stream is exclusive, and it belongs to whichever process first
	requested buffers. Requesting a buffer here fails with EBUSY while another
	process holds the stream. The buffer is released again straight away.

	DirectShow has no such check; on Windows a busy camera fails when it is opened.
	"""
	if IS_WINDOWS:
		return False
	fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
	try:
		request = struct.pack("5I", 1, _V4L2_BUF_TYPE_VIDEO_CAPTURE, _V4L2_MEMORY_MMAP, 0, 0)
		try:
			fcntl.ioctl(fd, _VIDIOC_REQBUFS, request)
		except OSError as e:
			if e.errno == errno.EBUSY:
				return True
			raise
		release = struct.pack("5I", 0, _V4L2_BUF_TYPE_VIDEO_CAPTURE, _V4L2_MEMORY_MMAP, 0, 0)
		fcntl.ioctl(fd, _VIDIOC_REQBUFS, release)
		return False
	finally:
		os.close(fd)


def get_camera_options_usb(camera_name, source):
	"""
	Reset ALL controls a USB/UVC camera reports back to their default
	values, but return min/max/default only for the subset in
	ALLOWABLE_OPTIONS.

	Args:
		camera_name: Human-readable label for this camera, used in logging.
		source: Device path (e.g. "/dev/video0"). Also used as the key in
				the returned dict.

	Returns:
		A dict keyed on `source`:
			{
				source: {
					canonical_name: {"min": ..., "max": ..., "default": ...},
					...
				}
			}
		Only controls in AllowedOptions that are confirmed present and
		successfully reset on this camera are included, even though every
		control on the camera was reset.
	"""
	# UVC control values persist in the device, so resetting once per run is enough.
	if source in _USB_OPTIONS_CACHE:
		return _USB_OPTIONS_CACHE[source]

	# Controls belong to the device, so resetting them would change the picture
	# of whatever process is already streaming from it; don't touch a busy camera.
	if _usb_camera_in_use(source):
		raise CameraInUseError(f'{source} is in use by another process')

	try:
		all_controls = _parse_list_ctrls(source)

		writable_controls = {}
		reverse_lookup = {}  # real_name actually used -> canonical_name

		for canonical_name in AllowedOptions.__members__:
			real_name = _resolve_real_name(canonical_name, all_controls.keys())
			if real_name is None:
				print(f"[{camera_name}] {canonical_name}: not present on this camera")
				continue
			reverse_lookup[real_name] = canonical_name

		defaults = {
			real_name: default_val
			for real_name, (_, _, default_val) in all_controls.items()
			if default_val is not None
		}
		failed = _set_usb_ctrls(source, defaults)
		print(f"[{camera_name}] reset to defaults: {defaults}; not settable: {failed or 'none'}")

		for real_name, (min_val, max_val, default_val) in all_controls.items():
			if real_name not in defaults or real_name in failed:
				continue

			canonical_name = reverse_lookup.get(real_name)
			if canonical_name is None:
				continue  # not one of the allowable options, don't include in output

			resolved_min = min_val if min_val is not None else default_val
			resolved_max = max_val if max_val is not None else default_val

			writable_controls[canonical_name] = {
				"min": resolved_min,
				"max": resolved_max,
				"default": default_val,
			}

		_USB_OPTIONS_CACHE[source] = {source: writable_controls}
		return _USB_OPTIONS_CACHE[source]
	except Exception as e:
		raise Exception(f'Issue setting camera defaults - {e}') from e


def set_controls_usb(controls_by_source, camera_config_options=None):
	"""
	Apply controls to one or more USB/UVC cameras, given a dict in the
	same shape as get_camera_options_usb's return value:

		{
			source: {
				canonical_name: {"min": ..., "max": ..., "default": ...},
				...
			},
			...
		}

	For each entry, the "default" value is written to the camera via
	v4l2-ctl --set-ctrl, clamped to [min, max] first as a safety check.

	Args:
		controls_by_source: dict keyed on source (device path), with
			values in the same shape produced by get_camera_options_usb.
		camera_config_options: optional dict of requested options. Only controls
			present in both this dict and AllowedOptions will be set.

	Returns:
		A dict keyed on source, each value a dict of
		{canonical_name: value} for the controls that were set, with
		value as actually applied (after clamping).
	"""
	results_by_source = {}

	for source, controls in controls_by_source.items():
		all_real_controls = _parse_list_ctrls(source).keys()
		results = {}
		to_set = {}  # real_name -> (canonical_name, value)

		for canonical_name, bounds in controls.items():
			if camera_config_options and canonical_name not in camera_config_options:
				continue
			if canonical_name not in AllowedOptions.__members__:
				continue
			real_name = _resolve_real_name(canonical_name, all_real_controls)
			if real_name is None:
				print(f"[{source}] {canonical_name}: not present on this camera")
				continue

			if camera_config_options:
				value = camera_config_options.get(canonical_name)
			else:
				value = bounds.get("default")

			if value is None:
				print(f"[{source}] {canonical_name}: no value to set, skipping")
				continue

			# v4l2 controls are integers; config values arrive as floats.
			value = int(round(clamp(source, canonical_name, value, bounds)))
			to_set[real_name] = (canonical_name, value)

		failed = _set_usb_ctrls(source, {name: value for name, (_, value) in to_set.items()})
		for real_name, (canonical_name, value) in to_set.items():
			if real_name in failed:
				print(f"[{source}] {canonical_name} ({real_name}) not settable")
			else:
				results[canonical_name] = value
				print(f"[{source}] {canonical_name} ({real_name}): set to {value}")

		results_by_source[source] = results

	return results_by_source


# Aliases: config may say "MPEG"/"MJPEG", v4l2-ctl reports the fourcc "MJPG".
FORMAT_ALIASES = {
	"MPEG": "MJPG",
	"MJPEG": "MJPG",
}

# Fallback format sequence if requested format is unavailable
FORMAT_FALLBACK_SEQUENCE = ["MJPG", "YUYV", "YUY2"]

FORMAT_LINE_RE = re.compile(r"^\s*\[\d+\]:\s*'(\w+)'")
SIZE_LINE_RE = re.compile(r"Size:\s*Discrete\s*(\d+)x(\d+)")
FPS_LINE_RE = re.compile(r"\(([\d.]+)\s*fps\)")


def _run_v4l2_ctl(source: str) -> Optional[str]:
	"""Run v4l2-ctl --list-formats-ext for a source. Returns stdout, or None on failure."""

	try:
		result = subprocess.run(
			["v4l2-ctl", "-d", source, "--list-formats-ext"],
			capture_output=True,
			text=True,
			timeout=5,
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		print(f"Could not query {source} with v4l2-ctl: {exc}")
		return None

	if result.returncode != 0 or not result.stdout.strip():
		print(
			f"v4l2-ctl reported no formats for {source}: {result.stderr.strip()}"
		)
		return None

	return result.stdout


def _parse_formats(output: str) -> "dict[str, dict[Tuple[int, int], List[float]]]":
	"""
	Parse `v4l2-ctl --list-formats-ext` output into:
		{ format_code: { (width, height): [fps, fps, ...] } }
	Order of formats/resolutions/fps as reported is preserved (dict
	insertion order), since v4l2-ctl typically lists preferred/higher
	options first.
	"""

	formats: "dict[str, dict[Tuple[int, int], List[float]]]" = {}

	current_format: Optional[str] = None
	current_size: Optional[Tuple[int, int]] = None

	for line in output.splitlines():

		fmt_match = FORMAT_LINE_RE.match(line)
		if fmt_match:
			current_format = fmt_match.group(1).upper()
			formats.setdefault(current_format, {})
			current_size = None
			continue

		size_match = SIZE_LINE_RE.search(line)
		if size_match and current_format is not None:
			current_size = (int(size_match.group(1)), int(size_match.group(2)))
			formats[current_format].setdefault(current_size, [])
			continue

		fps_match = FPS_LINE_RE.search(line)
		if fps_match and current_format is not None and current_size is not None:
			formats[current_format][current_size].append(float(fps_match.group(1)))
			continue

	return formats


def usb_formats(source) -> "dict[str, dict[Tuple[int, int], List[float]]]":
	"""{format: {(width, height): [fps, ...]}} a USB camera supports, or {} if it can't be queried."""
	if IS_WINDOWS:
		try:
			return dshow.list_formats(source)
		except Exception as exc:
			print(f"Could not query formats of camera {source}: {exc}")
			return {}
	output = _run_v4l2_ctl(source)
	return _parse_formats(output) if output else {}


def _normalize_format(requested_format: str) -> str:
	upper = requested_format.upper()
	return FORMAT_ALIASES.get(upper, upper)


def _next_lower_fps(
	requested_fps: float,
	available_fps: List[float],
) -> Optional[float]:
	"""Highest available fps that is still lower than requested. Falls
	back to the next higher fps (the lowest available above requested)
	if nothing is lower."""

	if not available_fps:
		return None

	lower = [f for f in available_fps if f < requested_fps]
	if lower:
		return max(lower)

	return min(f for f in available_fps if f > requested_fps)


def validate_usb_camera_configs(cameras: Dict[str, dict], applied_options: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
	"""
	Takes a dict of camera configs keyed by camera name, e.g.:

		{'Cam 2': {'name': 'Cam 2', 'source': '/dev/video2',
				   'cameratype': 'USB', 'fps': 15, 'width': 1024,
				   'height': 768, 'jpegresolution': 95, 'rotate': 0,
				   'format': 'MPEG'}}

	For each entry, queries `v4l2-ctl -d <source> --list-formats-ext`
	and adjusts 'format', 'width'/'height', and 'fps' to the closest
	values the device actually supports, per the fallback rules
	described in the module docstring.

	Any AllowedOptions in applied_options[<camera name>] (the values
	actually set, after clamping) are added to that camera's entry.

	Returns a NEW dict (the input is not mutated). Entries whose
	source can't be queried via v4l2-ctl (e.g. it isn't a local V4L2
	device, or the device didn't respond) are otherwise returned unchanged.
	"""

	adjusted = copy.deepcopy(cameras)
	for cam_name, cam in adjusted.items():
		if cam.get('cameratype') != 'USB':
			continue
		add_applied_options(cam, (applied_options or {}).get(cam_name))
		try:
			source = cam.get("source")
			formats = usb_formats(source)

			if not formats:
				print(f"[{cam_name}] No formats found for {source}; skipping format validation")
				continue
		except Exception as e:
			print(f'Format parsing{e}')
			raise
		try:
			# --- Step 1: format ---
			requested_format = _normalize_format(str(cam.get("format", "")))

			if requested_format in formats:
				chosen_format = requested_format
			else:
				# Try fallback sequence if requested format not available
				chosen_format = None
				for fallback_fmt in FORMAT_FALLBACK_SEQUENCE:
					if fallback_fmt in formats:
						chosen_format = fallback_fmt
						break

				# If no fallback worked, use first available format
				if chosen_format is None:
					chosen_format = next(iter(formats))

				print(
					f"[{cam_name}] Format '{cam.get('format')}' not supported on "
					f"{source}; Adjusting to '{chosen_format}'"
				)

			cam["format"] = chosen_format
			resolutions = formats[chosen_format]
		except Exception as e:
			print(f'Format setting{e}')
			raise
		try:
			# --- Step 2/3: resolution + fps ---
			requested_wh = (int(cam["width"]), int(cam["height"]))
			requested_fps = float(cam["fps"])

			if requested_wh in resolutions:
				chosen_wh = requested_wh
			else:
				chosen_wh = next_lower_resolution(requested_wh, list(resolutions.keys()))
				if chosen_wh is None:
					print(
						f"[{cam_name}] No usable resolution found for format "
						f"'{chosen_format}' on {source}; leaving as requested"
					)
					continue
				print(
					f"[{cam_name}] Resolution {requested_wh[0]}x{requested_wh[1]} not "
					f"supported for '{chosen_format}' on {source}; Adjusting to "
					f"{chosen_wh[0]}x{chosen_wh[1]}"
				)

			cam["width"], cam["height"] = chosen_wh
		except Exception as e:
			print(f'Resolution setting{e}')
			raise

		try:
			available_fps = resolutions.get(chosen_wh, [])

			if requested_fps in available_fps:
				chosen_fps = requested_fps
			else:
				chosen_fps = _next_lower_fps(requested_fps, available_fps)
				if chosen_fps is None:
					print(
						f"[{cam_name}] No usable fps found at "
						f"{chosen_wh[0]}x{chosen_wh[1]} for '{chosen_format}' on "
						f"{source}; leaving fps as requested"
					)
					continue
				if chosen_fps > requested_fps:
					fallback = (
						f"capturing at {int(chosen_fps)} and streaming at "
						f"{int(requested_fps)}"
					)
				else:
					fallback = f"falling back to {int(chosen_fps)}"
				print(
					f"[{cam_name}] fps {int(requested_fps)} not supported at "
					f"{chosen_wh[0]}x{chosen_wh[1]} for '{chosen_format}' on "
					f"{source}; {fallback}"
				)

			# The camera runs at capturefps; the capture thread drops frames to serve fps.
			# If the camera only offers a higher rate, the requested rate is still served.
			cam["capturefps"] = chosen_fps
			cam["fps"] = min(requested_fps, chosen_fps)
		except Exception as e:
			print(f'FPS setting{e}')
			raise
	return adjusted


def _find_usb_devices_windows():
	"""{source: label} for every DirectShow camera, e.g. {"0": "0 (HD Webcam)"}."""
	try:
		return {source: f"{source} ({name})" for source, name in dshow.list_cameras()}
	except Exception as e:
		raise Exception(f"Error listing DirectShow cameras - {e}")


def _find_usb_devices_linux():
	"""
	{source: label} for the /dev/videoN paths of USB cameras that actually
	support video capture (filters out metadata-only nodes by
	checking reported capabilities, not by even/odd guessing).
	"""
	try:
		video_nodes = sorted(glob.glob('/dev/video*'),
							key=lambda x: int(re.search(r'\d+', x).group()))
	except Exception as e:
		raise Exception(f"Error listing /dev/video* nodes - {e}")


	usb_devices = []
	try:
		for node in video_nodes:
			result = subprocess.run(
					['v4l2-ctl', '-d', node, '--info'],
					capture_output=True, text=True, timeout=2
				)

			if result.returncode != 0:
				continue

			out = result.stdout

			is_usb = 'usb' in out.lower()
			# "Device Caps" lists the node's own capabilities; some nodes
			# only show "Video Capture" under "All Caps" (i.e. the driver
			# supports it) without exposing it on this particular node —
			# we want it listed under Device Caps to be usable.
			device_caps_section = out.split('Device Caps')[-1] if 'Device Caps' in out else ''
			supports_capture = 'Video Capture' in device_caps_section

			if is_usb and supports_capture:
				usb_devices.append(node)
	except Exception as e:
		raise Exception(f"Error checking USB camera capabilities - {e}")

	return {node: node for node in usb_devices}


def find_usb_cameras():
	"""
	Return the sources of the USB cameras: /dev/videoN paths on Linux,
	DirectShow device indexes ("0", "1", ...) on Windows.
	"""
	usb_devices = _find_usb_devices_windows() if IS_WINDOWS else _find_usb_devices_linux()

	camera_results = []
	if usb_devices:
		camera_results.append("USB camera(s) found with these options:")
		for dev, label in usb_devices.items():
			camera_results.append(f"\n-- {label}")
			try:
				camera_options = get_camera_options_usb(f"USB-{dev}", dev)
				if camera_options.get(dev):
					for control_name, bounds in camera_options[dev].items():
						min_val = bounds.get("min", "N/A")
						max_val = bounds.get("max", "N/A")
						default_val = bounds.get("default", "N/A")
						camera_results.append(f"   {control_name}: min={min_val}, max={max_val}, default={default_val}")
			except CameraInUseError:
				camera_results.append("   In use by another process")
			except Exception as e:
				print(f"Could not query options for {dev}: {e}")
	else:
		camera_results.append("No USB cameras were found.")

	highlight_print(camera_results)

	return list(usb_devices)
