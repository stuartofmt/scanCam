"""
Helpers shared by the USB (usb_config.py) and Pi (pi_config.py) camera configuration.
"""

from typing import List, Optional, Tuple

from defaults import AllowedOptions


# AllowedOptions values name the Python type each option's value should have.
OPTION_TYPES = {"float": float, "int": int}


def cast_option(name, value):
	"""Convert an option value to the type given for it in AllowedOptions."""
	option_type = OPTION_TYPES[AllowedOptions[name].value]
	if option_type is int:
		# Config values may be written as "1.0"; int("1.0") would fail.
		return int(round(float(value)))
	return option_type(value)


def clamp(source, canonical_name, value, bounds):
	"""Limit a requested control value to the camera's reported [min, max]."""
	min_val = bounds.get("min")
	max_val = bounds.get("max")

	if min_val is not None and float(value) < float(min_val):
		print(f"[{source}] {canonical_name}: value {value} below min {min_val}, clamping")
		return min_val
	if max_val is not None and float(value) > float(max_val):
		print(f"[{source}] {canonical_name}: value {value} above max {max_val}, clamping")
		return max_val
	return value


class CameraInUseError(RuntimeError):
	"""Raised when a camera can't be set up because another process is streaming from it."""


def next_lower_resolution(
	requested_wh: Tuple[int, int],
	available_whs: "List[Tuple[int, int]]",
) -> Optional[Tuple[int, int]]:
	"""Largest available resolution (by pixel area) that is still smaller
	than requested. Falls back to the next size up (the smallest available
	that is not smaller) if nothing is smaller."""

	if not available_whs:
		return None

	requested_area = requested_wh[0] * requested_wh[1]

	# Width breaks ties between resolutions with the same area, so the result is deterministic.
	def area_key(wh):
		return (wh[0] * wh[1], wh[0])

	lower = [wh for wh in available_whs if wh[0] * wh[1] < requested_area]
	if lower:
		return max(lower, key=area_key)

	return min(available_whs, key=area_key)


def add_applied_options(cam: dict, options: Optional[dict]) -> None:
	"""Copy any AllowedOptions in `options` (as applied) into the camera config."""
	for key, value in (options or {}).items():
		if key in AllowedOptions.__members__:
			cam[key] = cast_option(key, value)


def highlight_print(msg):
	"""
	Log a message, or a list/tuple of lines, at INFO level as a block
	framed by separator lines so it stands out in the log.
	"""
	highlight = ["","=" * 95]
	if isinstance(msg, (list, tuple)):
		highlight.extend(msg)
	else:
		highlight.append(msg)
	highlight = highlight +  ["-" * 95, f'\n']
	print("\n".join(highlight))
