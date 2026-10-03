"""
scanCam - view and adjust USB and Pi cameras from a web page.

Set up the venv once with install.py, then run:
	./run.sh  (or venv/bin/python code/scanCam.py)
	run.bat on Windows
"""

# This is to supress the noisy libcam
# MUST BE AT THE VERY START OF THE SCRIPT
import os
os.environ["LIBCAMERA_LOG_LEVELS"] = "*:ERROR"

import sys

import logging
import socket
import signal
import shutil
import subprocess
import webbrowser

from werkzeug.serving import make_server

from defaults import IS_WINDOWS
from routes import app, add_camera, set_camera_settings, set_in_use_cameras, set_exit_on_page_close

from camera_settings import get_camera_settings
from config_common import highlight_print
from get_config import get_installed_cameras, get_detected_cameras, configure_cameras


# Listen on every interface, so the cameras can be viewed on this machine
# (localhost) as well as from the network.
LISTEN_ADDRESS = '0.0.0.0'


def port_free(port):
	#  A port is free if it can be bound; another server on any interface would prevent that
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		#  The web server sets SO_REUSEADDR too, so a port left in TIME_WAIT counts as free.
		#  Not on Windows, where it would allow binding a port that is in use.
		if not IS_WINDOWS:
			sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		try:
			sock.bind((LISTEN_ADDRESS, port))
			return True
		except OSError:
			return False


def get_ip_address():
	#  The address other machines on the network reach this one at
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		s.connect(('10.255.255.255', 1))  # doesn't even have to be reachable
		return s.getsockname()[0]
	except OSError:
		print('No network found - the cameras can only be viewed on this machine')
		return '127.0.0.1'
	finally:
		s.close()


def find_port(start_port=17800, max_tries=100):
	#  Use the first free port starting at start_port
	for port in range(start_port, start_port + max_tries):
		if port_free(port):
			print(f'''Port {port} is available''')
			return port
	print(f'''No free port found between {start_port} and {start_port + max_tries - 1}''')
	force_quit(1)


def open_browser(url):
	#  Only with a desktop - otherwise (e.g. run by DSF) a text browser could take over the terminal.
	#  Windows always has one.
	if not IS_WINDOWS and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
		print(f'''No display - not opening {url} in a browser''')
		return
	try:
		#  xdg-open uses the desktop's default browser; webbrowser has its own order of preference
		if not IS_WINDOWS and shutil.which('xdg-open'):
			subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		elif not webbrowser.open(url):
			print(f'''No browser available to open {url}''')
	except Exception as e:
		print(f'''Could not open {url} in a browser - {e}''')


CAMERA_TYPE_LABELS = {'USB': 'USB camera', 'PICAMERA': 'Pi camera'}


def report_in_use_cameras(cameras):
	#  Cameras skipped because another program had them open; they can be used after a restart
	if not cameras:
		return
	lines = ['The following cameras are in use by another program and cannot be connected to at this time:']
	for camera in cameras:
		lines.append(f"   {CAMERA_TYPE_LABELS.get(camera['cameratype'], camera['cameratype'])} {camera['source']}")
	lines.append('Close the other program and restart scanCam to use them.')
	highlight_print(lines)


def force_quit(code):
	print(f'''Terminating the program with exit code {code}''')
	sys.exit(code)

def sig_handler(signum, frame):
	signame = signal.Signals(signum).name
	print(f'Shutting down.  Recieved signal {signame} ({signum})')
	force_quit(0)


if __name__ == "__main__":

	# shutdown with SIGINT (kill -2 <pid> or SIGTERM)
	signal.signal(signal.SIGINT, sig_handler)
	signal.signal(signal.SIGTERM, sig_handler)

	global progName, progVersion
	progName = os.path.splitext(os.path.basename(sys.argv[0]))[0]
	progVersion = '1.0.0'

	# Show each message straight away when output goes to a pipe or file
	sys.stdout.reconfigure(line_buffering=True)

	# Werkzeug logs every request (each stream and snapshot); only show its problems
	logging.getLogger('werkzeug').setLevel(logging.WARNING)
	print(f'''{progName} -- {progVersion}''')

	this_ip_address = get_ip_address()
	PORT = find_port()

	# Use every detected camera with default settings
	try:
		installed_cameras = get_installed_cameras()
		cameras_to_use, cameras_to_use_configs = get_detected_cameras(installed_cameras)
		configured_cameras, in_use_cameras = configure_cameras(installed_cameras,cameras_to_use, cameras_to_use_configs)
	except Exception as e:
		print(f'{e}')
		force_quit(1)

	# Values shown on the index page
	try:
		set_camera_settings(configured_cameras, get_camera_settings(configured_cameras))
	except Exception as e:
		print(f'Index page will not show camera settings - {e}')
		set_camera_settings(configured_cameras, {})

	# Use case sensitive order based on keys
	for camera_name, camera_settings in sorted(configured_cameras.items()):
		try:
			add_camera(camera_settings)
			print(f"Added {camera_name} with source '{camera_settings['source']}'")
		except Exception as e:
			print(f"Error adding camera {camera_name}: {e}")
	set_in_use_cameras(in_use_cameras)

	# Each request (e.g. each open stream) is handled in its own thread
	try:
		server = make_server(LISTEN_ADDRESS, PORT, app, threaded=True)
	except Exception as e:
		print(f'Could not start the web server on port {PORT} - {e}')
		force_quit(1)

	report_in_use_cameras(in_use_cameras)
	print('-------------------------------------------------------\n')
	print(f"View cameras at http://{this_ip_address}:{PORT}")
	print(f"or on this machine at http://localhost:{PORT}\n")

	open_browser(f"http://{this_ip_address}:{PORT}")

	# Closing the last open page (not just hiding it) stops the server, ending serve_forever.
	set_exit_on_page_close(server.shutdown)
	server.serve_forever()
	force_quit(0)
