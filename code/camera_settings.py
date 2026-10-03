"""
Camera settings for the web page: what each camera offers, and changing a setting while it runs.
"""

import copy

from config_common import AllowedOptions, cast_option
from pi_config import cached_picam_info, set_controls_picam, validate_pi_camera_configs
from usb_config import cached_usb_options, set_controls_usb, usb_formats, validate_usb_camera_configs


# Pi cameras scale to any size and run at any rate up to the sensor's limits,
# so these common values (that fit the sensor) are offered alongside its own modes.
PICAM_COMMON_RESOLUTIONS = [
	(640, 480), (800, 600), (1024, 768), (1280, 720),
	(1280, 960), (1600, 1200), (1920, 1080),
]
PICAM_COMMON_FPS = [1, 2, 5, 10, 15, 20, 24, 25, 30, 50, 60, 90, 120]


def _ui_value(value):
	"""Picamera2 reports some controls (e.g. AwbEnable) as bool; show those as 0/1."""
	return int(value) if isinstance(value, bool) else value


def _with_current(choices, current):
	"""Sorted choices, always including the value the camera runs with."""
	return sorted(set(choices) | {current})


def _usb_setting_choices(cam):
	"""
	Values of fps, width and height the camera supports in the format it
	streams in: every width, the heights offered at the current width and the
	rates offered at the current resolution.
	"""
	resolutions = usb_formats(cam["source"]).get(cam.get("format"))
	if not resolutions:
		return {}
	width, height = int(cam["width"]), int(cam["height"])
	return {
		"width": _with_current((w for w, _ in resolutions), width),
		"height": _with_current((h for w, h in resolutions if w == width), height),
		"fps": _with_current(resolutions.get((width, height), []), cam["fps"]),
	}


def _picam_setting_choices(cam, modes):
	"""
	Values of fps, width and height to offer for a Pi camera: its sensor modes
	and the common resolutions that fit them, and the common rates up to the
	fastest mode large enough for the current resolution.
	"""
	if not modes:
		return {}
	width, height = int(cam["width"]), int(cam["height"])

	def fits(wh):
		return any(wh[0] <= mw and wh[1] <= mh for mw, mh in modes)

	sizes = [wh for wh in set(modes) | set(PICAM_COMMON_RESOLUTIONS) if fits(wh)]
	covering = [modes[wh] for wh in modes if wh[0] >= width and wh[1] >= height]
	max_fps = max(covering) if covering else max(modes.values())
	return {
		"width": _with_current((w for w, _ in sizes), width),
		"height": _with_current((h for _, h in sizes if fits((width, h))), height),
		"fps": _with_current([fps for fps in PICAM_COMMON_FPS if fps <= max_fps] + [round(max_fps, 2)], cam["fps"]),
	}


def get_camera_settings(configured_cameras, startup_cameras=None):
	"""
	Report each camera's settings and options for the UI.

	Uses the options already cached while the cameras were configured, so no
	camera is reset or reopened.

	Args:
		configured_cameras: as returned by configure_cameras, or as changed since.
		startup_cameras: the cameras as configured at startup; defaults to
			configured_cameras.

	Returns:
		{camera name: [{"setting", "min", "max", "default", "current", "step", "choices"}, ...]}.
		default is the camera's own default for options; cameras don't report
		one for fps, width and height, so it is the value used at startup
		(the plugin's default, adjusted to what the camera supports).
		fps, width and height come first, with choices listing the values the
		camera supports; then the AllowedOptions the camera supports, with step
		the increment of their value. Options that were not configured are at
		the camera's default, so that is reported for them.
	"""
	settings = {}
	for name, cam in configured_cameras.items():
		startup_cam = (startup_cameras or configured_cameras).get(name, cam)
		if cam["cameratype"] == "USB":
			try:
				choices = _usb_setting_choices(cam)
			except Exception as e:
				print(f"[{name}] Could not read formats for {cam['source']}: {e}")
				choices = {}
			options = cached_usb_options(cam["source"])
		elif cam["cameratype"] == "PICAMERA":
			info = cached_picam_info(cam["source"])
			choices = _picam_setting_choices(cam, info.get("modes"))
			options = info.get("controls", {})
		else:
			choices, options = {}, {}

		rows = []
		for setting in ("fps", "width", "height"):
			if cam.get(setting) is None:
				continue
			values = choices.get(setting) or [cam[setting]]
			rows.append({
				"setting": setting,
				"min": min(values),
				"max": max(values),
				"default": startup_cam.get(setting),
				"current": cam[setting],
				"step": None,
				"choices": values,
			})
		for option, limits in options.items():
			rows.append({
				"setting": option,
				"min": _ui_value(limits["min"]),
				"max": _ui_value(limits["max"]),
				"default": _ui_value(limits["default"]),
				"current": _ui_value(cam.get(option, limits["default"])),
				# v4l2 controls are integers; libcamera float controls take fractions.
				"step": 0.1 if isinstance(limits["default"], float) else 1,
				"choices": None,
			})
		settings[name] = rows
	return settings


# Settings fixed when a camera is opened, so changing one restarts the camera.
RESTART_SETTINGS = ("fps", "width", "height")


def _usb_resolution_with(cam, setting, value):
	"""
	USB cameras only offer discrete resolutions. When width or height is
	changed on its own, pick a resolution with that dimension, keeping the
	other dimension as close as possible to its current value.
	"""
	resolutions = usb_formats(cam["source"]).get(cam.get("format"), {})
	index, other = (0, 1) if setting == "width" else (1, 0)
	other_setting = "height" if setting == "width" else "width"
	matching = [wh for wh in resolutions if wh[index] == value]
	if not matching:
		return None
	return min(matching, key=lambda wh: abs(wh[other] - int(cam[other_setting])))


def update_camera_setting(cam, setting, value):
	"""
	Work out the effect of changing one setting or option of a running camera.

	USB options are written to the camera here; the caller applies the rest.

	Args:
		cam: the camera's current config, as returned by configure_cameras.
		setting: the setting or option name, as in get_camera_settings.
		value: the requested value (a number).

	Returns:
		(new config, live controls, restart). live controls are the Picamera2
		controls ({real_name: value}) to apply now; restart is True when the
		camera has to be restarted with the new config.

	Raises:
		ValueError if the setting can't be changed or the value is invalid.
	"""
	name = cam["name"]
	source = cam["source"]
	cameratype = cam["cameratype"]
	new_cam = copy.deepcopy(cam)

	try:
		value = float(value)
	except (TypeError, ValueError):
		raise ValueError(f"{setting} must be a number")

	if setting in AllowedOptions.__members__:
		if cameratype == "USB":
			options = cached_usb_options(source)
			if setting not in options:
				raise ValueError(f"{setting} is not supported by {name}")
			applied = set_controls_usb({source: options}, {setting: value})[source]
			if setting not in applied:
				raise ValueError(f"{name} did not accept {setting} (it may depend on another setting)")
			new_cam[setting] = cast_option(setting, applied[setting])
			print(f"[{name}] {setting} set to {new_cam[setting]}")
			return new_cam, {}, False

		if cameratype == "PICAMERA":
			options = cached_picam_info(source).get("controls", {})
			if setting not in options:
				raise ValueError(f"{setting} is not supported by {name}")
			resolved = set_controls_picam({source: options}, {setting: value})[source]
			new_cam["controls"] = {**new_cam.get("controls", {}), **resolved}
			new_cam[setting] = next(iter(resolved.values()))
			print(f"[{name}] {setting} set to {new_cam[setting]}")
			return new_cam, resolved, False

		raise ValueError(f"{setting} cannot be changed on {name}")

	if setting not in RESTART_SETTINGS:
		raise ValueError(f"{setting} cannot be changed")

	if setting == "fps":
		if value <= 0:
			raise ValueError("fps must be greater than 0")
		new_cam["fps"] = value
	else:
		if value <= 0:
			raise ValueError(f"{setting} must be greater than 0")
		new_cam[setting] = int(round(value))
		if cameratype == "USB":
			resolution = _usb_resolution_with(cam, setting, new_cam[setting])
			if resolution is not None:
				new_cam["width"], new_cam["height"] = resolution

	# Bring fps / width / height back within what the camera supports.
	if cameratype == "USB":
		new_cam = validate_usb_camera_configs({name: new_cam})[name]
	elif cameratype == "PICAMERA":
		new_cam = validate_pi_camera_configs({name: new_cam})[name]

	print(f"[{name}] {setting} changed; restarting with "
		f"{new_cam.get('width')}x{new_cam.get('height')} at {new_cam['fps']} fps")
	return new_cam, {}, True
