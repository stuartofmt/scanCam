"""
Finds the attached USB and Pi cameras and configures each one at startup.

The details live in usb_config.py and pi_config.py; camera_settings.py
handles the settings shown and changed in the web page.
"""

from config_common import CameraInUseError, highlight_print
from defaults import DefaultCameraSettings
from pi_config import (PI_REAL_TO_CANONICAL, find_pi_cameras, get_camera_options_picam,
						set_controls_picam, validate_pi_camera_configs)
from usb_config import (find_usb_cameras, get_camera_options_usb, set_controls_usb,
						validate_usb_camera_configs)


def default_camera_config(name, source, cameratype):
	"""
	Build the default settings dictionary for a single camera.
	"""
	camera_data = {
		"name": name,
		"source": source,
		"cameratype": cameratype,
		**{setting.name: setting.value for setting in DefaultCameraSettings},
	}
	if cameratype == 'USB': # Set MPEG as default
		camera_data['format'] = 'MPEG'
	return camera_data


def get_detected_cameras(installed_cameras):
	"""
	Build default settings for every installed USB and Pi camera.

	Cameras are named Camera1, Camera2, ... in the order they were detected
	(USB cameras first, then Pi cameras).

	Returns:
		(CAMERAS, CAMERA_CONFIG), where CAMERAS maps camera name to its
		settings and CAMERA_CONFIG maps camera name to its requested
		AllowedOptions (always empty, so each control stays at its default).
	"""
	CAMERAS = {}
	for number, source in enumerate(installed_cameras, start=1):
		name = f"Camera{number}"
		cameratype = "USB" if isinstance(source, str) else "PICAMERA"
		CAMERAS[name] = default_camera_config(name, source, cameratype)
	CAMERA_CONFIG = {name: {} for name in CAMERAS}

	camera_results = ["Requested Settings"]
	for name, options in CAMERAS.items():
		for option, value in options.items():
			if option == 'name':
				camera_results.append(f'\n{value}')
			else:
				camera_results.append(f'\t--{option} = {value}')
	if not CAMERAS:
		camera_results.append("No cameras detected")
	highlight_print(camera_results)

	return CAMERAS, CAMERA_CONFIG

def get_installed_cameras():
	"""
	Detect attached cameras and log what was found.

	Returns USB device paths (e.g. "/dev/video0") followed by Pi camera
	Picamera2 slots.
	"""
	usb_cameras = find_usb_cameras()
	pi_cameras = find_pi_cameras()

	return usb_cameras + pi_cameras

	
def configure_cameras(installed_cameras,camera_list,requested_options):
		"""
		Apply the requested options to each configured camera and validate
		its format, resolution and fps against what the device supports.

		Args:
			installed_cameras: sources returned by get_installed_cameras.
			camera_list: camera settings keyed by camera name, as returned
				by get_detected_cameras. Updated in place.
			requested_options: requested AllowedOptions, keyed by
				camera name.

		Returns:
			(camera_list, in_use_cameras). camera_list has USB and Pi cameras that
			are not installed, or that could not be set up (e.g. busy in another
			process), removed, and the remaining settings adjusted to their actual
			values. Pi cameras with configured options also get a 'controls' entry.
			in_use_cameras lists the cameras skipped because another process is
			using them, as {"name", "source", "cameratype"}.
			The actual settings are logged.
		"""
			# Assemble the camera configurations
		try:
			# Option values as applied to each camera (after clamping), keyed by camera name.
			applied_options = {}
			# Cameras that could not be set up (e.g. busy in another process); removed below
			# so the remaining cameras still start.
			failed_cameras = set()
			# The failed cameras that are busy in another process, reported to the user.
			in_use_cameras = []
			for name, details in camera_list.items():
				if details['source'] not in installed_cameras:
					print(f'Camera source {details["source"]} is not installed')
					continue

				try:
					# Get and set camera options
					if details['cameratype'] == 'USB':
						camera_options = get_camera_options_usb(name,details['source'])
						applied = set_controls_usb(camera_options,requested_options[name])
						# With no options configured every control is set to its default; only report requested ones.
						applied_options[name] = {
							key: value
							for key, value in applied[details['source']].items()
							if key in requested_options[name]
						}

					elif details['cameratype'] == 'PICAMERA':
						camera_options = get_camera_options_picam(name,details['source'])
						resolved = set_controls_picam(camera_options,requested_options[name])
						applied_options[name] = {
							PI_REAL_TO_CANONICAL[real_name]: value
							for real_name, value in resolved[details['source']].items()
						}
						# Only carry controls when some are configured, so the camera dict
						# otherwise matches the USB one; picam treats missing as {}.
						if resolved[details['source']]:
							camera_list[name]['controls'] = resolved[details['source']]

					else:
						print(f'Unrecognized camera type {details["cameratype"]}')
						continue
				except CameraInUseError:
					print(f'[{name}] Camera {details["source"]} is in use by another process; skipping it')
					failed_cameras.add(name)
					in_use_cameras.append({key: details[key] for key in ("name", "source", "cameratype")})
					continue
				except Exception as e:
					print(f'[{name}] Could not set up camera {details["source"]}; skipping it - {e}')
					failed_cameras.add(name)
					continue

			# Remove any cameras that are not present or could not be set up
			for camera, details in list(camera_list.items()):
				if details['source'] not in installed_cameras:
					camera_list.pop(camera)
					print(f'Camera {camera} with source {details["source"]} removed')
					continue
				if camera in failed_cameras:
					camera_list.pop(camera)
					print(f'Camera {camera} with source {details["source"]} removed')
					continue

				# Validate this camera's format, resolution, and fps.
				try:
					if details['cameratype'] == 'USB':
						validated_camera = validate_usb_camera_configs({camera: details}, applied_options)
						camera_list[camera] = validated_camera[camera]
					elif details['cameratype'] == 'PICAMERA':
						validated_camera = validate_pi_camera_configs({camera: details}, applied_options)
						camera_list[camera] = validated_camera[camera]
				except Exception as e:
					print(f'[{camera}] Could not validate camera {details["source"]}; skipping it - {e}')
					camera_list.pop(camera)



			camera_results = []

			camera_results.append("Actual Camera Settings")
			for name, options in camera_list.items():
				for option, value in options.items():
					if option == 'name':
						camera_results.append(f'\n{value}')
					else:
						if option != 'controls': # Dont want this displayed
							camera_results.append(f'\t--{option} = {value}')

			highlight_print(camera_results)

			return camera_list, in_use_cameras
		except Exception as e:
			print('Camera option setup failed')
			raise Exception(f'Error setting camera options {e}') from e
