"""
dshow.py

Windows (DirectShow) equivalent of the v4l2-ctl queries get_config.py makes
on Linux: listing cameras, their formats and controls, and setting controls.

A camera's source is its DirectShow device index as a string ("0", "1", ...);
OpenCV's DirectShow backend (cv2.CAP_DSHOW) numbers cameras the same way.
"""

import functools
from concurrent.futures import ThreadPoolExecutor
from ctypes import HRESULT, POINTER, c_long, cast
from typing import Dict, List, Tuple

import comtypes
from comtypes import COMMETHOD, GUID, COMError, IUnknown

from pygrabber.dshow_core import VIDEOINFOHEADER, IAMStreamConfig
from pygrabber.dshow_graph import SystemDeviceEnum
from pygrabber.dshow_ids import DeviceCategories, FormatTypes, subtypes


# --- DirectShow camera control interfaces (strmif.h) ---

class IAMVideoProcAmp(IUnknown):
	_iid_ = GUID('{C6E13360-30AC-11D0-A18C-00A0C9118956}')


class IAMCameraControl(IUnknown):
	_iid_ = GUID('{C6E13370-30AC-11D0-A18C-00A0C9118956}')


# Both interfaces have the same methods: GetRange, Set, Get.
for _interface in (IAMVideoProcAmp, IAMCameraControl):
	_interface._methods_ = [
		COMMETHOD([], HRESULT, 'GetRange',
			(['in'], c_long, 'Property'),
			(['out'], POINTER(c_long), 'pMin'),
			(['out'], POINTER(c_long), 'pMax'),
			(['out'], POINTER(c_long), 'pSteppingDelta'),
			(['out'], POINTER(c_long), 'pDefault'),
			(['out'], POINTER(c_long), 'pCapsFlags')),
		COMMETHOD([], HRESULT, 'Set',
			(['in'], c_long, 'Property'),
			(['in'], c_long, 'lValue'),
			(['in'], c_long, 'Flags')),
		COMMETHOD([], HRESULT, 'Get',
			(['in'], c_long, 'Property'),
			(['out'], POINTER(c_long), 'lValue'),
			(['out'], POINTER(c_long), 'Flags')),
	]

# VideoProcAmp_Flags_* / CameraControl_Flags_*
_AUTO = 0x1
_MANUAL = 0x2

# Controls, named as v4l2-ctl names them so get_config treats both platforms alike:
# name -> (interface, property, is_auto_toggle). An auto toggle is the Auto flag
# of a property; it is 1 (auto) or 0 (manual). Toggles come first so a reset
# turns auto back on before the values that only apply in manual mode.
_CONTROLS = {
	'white_balance_automatic': (IAMVideoProcAmp, 7, True),
	'auto_exposure': (IAMCameraControl, 4, True),
	'focus_auto': (IAMCameraControl, 6, True),
	'brightness': (IAMVideoProcAmp, 0, False),
	'contrast': (IAMVideoProcAmp, 1, False),
	'hue': (IAMVideoProcAmp, 2, False),
	'saturation': (IAMVideoProcAmp, 3, False),
	'sharpness': (IAMVideoProcAmp, 4, False),
	'gamma': (IAMVideoProcAmp, 5, False),
	'white_balance_temperature': (IAMVideoProcAmp, 7, False),
	'backlight_compensation': (IAMVideoProcAmp, 8, False),
	'gain': (IAMVideoProcAmp, 9, False),
	'pan': (IAMCameraControl, 0, False),
	'tilt': (IAMCameraControl, 1, False),
	'zoom': (IAMCameraControl, 3, False),
	'exposure': (IAMCameraControl, 4, False),
	'focus': (IAMCameraControl, 6, False),
}

# DirectShow reports a range of frame rates per format; these are offered within it.
_COMMON_FPS = [5, 10, 15, 20, 24, 25, 30, 50, 60]

# Subtype GUIDs built from a FOURCC end with this.
_FOURCC_GUID_SUFFIX = '-0000-0010-8000-00AA00389B71}'


# COM objects must be used on the thread that created them, and request threads
# come and go, so all DirectShow calls run on one thread with COM initialised.
_com_thread = ThreadPoolExecutor(max_workers=1, initializer=comtypes.CoInitialize)


def _on_com_thread(function):
	@functools.wraps(function)
	def wrapper(*args, **kwargs):
		return _com_thread.submit(function, *args, **kwargs).result()
	return wrapper


def _device(source):
	"""The DirectShow filter for a camera."""
	try:
		device, _ = SystemDeviceEnum().get_filter_by_index(DeviceCategories.VideoInputDevice, int(source))
	except (AttributeError, ValueError) as e:
		raise ValueError(f'No camera {source}') from e
	return device


@_on_com_thread
def list_cameras() -> List[Tuple[str, str]]:
	"""[(source, friendly name)] for every video input device."""
	names = SystemDeviceEnum().get_available_filters(DeviceCategories.VideoInputDevice)
	return [(str(index), name) for index, name in enumerate(names)]


@_on_com_thread
def list_controls(source) -> Dict[str, Tuple[int, int, int]]:
	"""{name: (min, max, default)} for each control the camera supports."""
	device = _device(source)
	controls = {}
	for name, (interface, prop, is_auto_toggle) in _CONTROLS.items():
		try:
			minimum, maximum, _, default, caps = device.QueryInterface(interface).GetRange(prop)
		except COMError:
			continue
		if is_auto_toggle:
			if caps & _AUTO and caps & _MANUAL:
				controls[name] = (0, 1, 1)
		elif caps & _MANUAL:
			controls[name] = (minimum, maximum, default)
	return controls


@_on_com_thread
def set_controls(source, values) -> set:
	"""
	Set controls ({name: value}) and return the names of those that did not take,
	read back to check, as a value can be ignored while its auto mode is on.
	"""
	device = _device(source)
	failed = set()
	for name, value in values.items():
		if name not in _CONTROLS:
			failed.add(name)
			continue
		interface, prop, is_auto_toggle = _CONTROLS[name]
		value = int(value)
		try:
			control = device.QueryInterface(interface)
			current, flags = control.Get(prop)
			if is_auto_toggle:
				control.Set(prop, current, _AUTO if value else _MANUAL)
				took = bool(control.Get(prop)[1] & _AUTO) == bool(value)
			else:
				# Keep the auto/manual mode, which has its own toggle.
				control.Set(prop, value, flags or _MANUAL)
				took = control.Get(prop)[0] == value
		except COMError:
			took = False
		if not took:
			failed.add(name)
	return failed


def _format_name(subtype) -> str:
	"""FOURCC (e.g. 'MJPG', 'YUY2', 'NV12') or name of a media subtype."""
	guid = str(subtype).upper()
	if guid.endswith(_FOURCC_GUID_SUFFIX):
		try:
			return int(guid[1:9], 16).to_bytes(4, 'little').decode('ascii').strip()
		except (ValueError, UnicodeDecodeError):
			pass
	return subtypes.get(guid, guid)


def _fps_choices(intervals) -> List[float]:
	"""Frame rates offered for the frame intervals (100ns units) a format allows."""
	rates = [round(10_000_000 / interval, 2) for interval in intervals if interval > 0]
	if not rates:
		return []
	lowest, highest = min(rates), max(rates)
	return sorted({fps for fps in _COMMON_FPS if lowest <= fps <= highest} | {highest})


def _stream_config(device):
	"""IAMStreamConfig of the camera's capture (first output) pin."""
	pins = device.EnumPins()
	pin, count = pins.Next(1)
	while count > 0:
		if pin.QueryDirection() == 1:  # PINDIR_OUTPUT
			try:
				return pin.QueryInterface(IAMStreamConfig)
			except COMError:
				pass
		pin, count = pins.Next(1)
	return None


# Formats don't change, and querying them while the camera streams may not be possible.
_FORMATS_CACHE = {}


@_on_com_thread
def list_formats(source) -> "dict[str, dict[Tuple[int, int], List[float]]]":
	"""
	{format: {(width, height): [fps, ...]}}, as get_config parses from
	v4l2-ctl --list-formats-ext on Linux. {} if the camera can't be queried.
	"""
	if source in _FORMATS_CACHE:
		return _FORMATS_CACHE[source]

	stream_config = _stream_config(_device(source))
	if stream_config is None:
		return {}

	intervals: "dict[str, dict[Tuple[int, int], set]]" = {}
	count, _ = stream_config.GetNumberOfCapabilities()
	for index in range(count):
		media_type, caps = stream_config.GetStreamCaps(index)
		if GUID(FormatTypes.FORMAT_VideoInfo) != media_type.contents.formattype:
			continue
		header = cast(media_type.contents.pbFormat, POINTER(VIDEOINFOHEADER)).contents.bmi_header
		size = (header.biWidth, abs(header.biHeight))  # negative height means top-down rows
		sizes = intervals.setdefault(_format_name(media_type.contents.subtype), {})
		sizes.setdefault(size, set()).update((caps.MinFrameInterval, caps.MaxFrameInterval))

	_FORMATS_CACHE[source] = {
		format_name: {size: _fps_choices(size_intervals) for size, size_intervals in sizes.items()}
		for format_name, sizes in intervals.items()
	}
	return _FORMATS_CACHE[source]
