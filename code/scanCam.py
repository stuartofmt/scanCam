"""
scanCam - view and adjust USB and Pi cameras from a web page.

Set up the venv once with ./createVenv.sh, then run:
	venv/bin/python scanCam.py
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

from routes import app, add_camera, set_camera_settings

from get_config import (get_installed_cameras, get_detected_cameras, configure_cameras,
						get_camera_settings)


def port_in_use(ip_address, port):
	#  A successful connection means something is already listening there
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		sock.settimeout(1)
		return sock.connect_ex((ip_address, port)) == 0


def find_port(start_port=17800, max_tries=100):
	#  Get the local ip address
	this_ip_address = ''
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		s.connect(('10.255.255.255', 1))  # doesn't even have to be reachable
		this_ip_address = s.getsockname()[0]
	except Exception as e:
		print(f'''Unknown error trying to get the local IP address''')
		print(f'''{e}''')
		force_quit(1)
	finally:
		s.close()

	#  Use the first free port starting at start_port
	for port in range(start_port, start_port + max_tries):
		if not port_in_use(this_ip_address, port):
			break
	else:
		print(f'''No free port found between {start_port} and {start_port + max_tries - 1}''')
		force_quit(1)

	print(f'''IP address {this_ip_address} with port {port} is available''')
	return this_ip_address, port


def open_browser(url):
	#  Only with a desktop - otherwise (e.g. run by DSF) a text browser could take over the terminal
	if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
		print(f'''No display - not opening {url} in a browser''')
		return
	try:
		#  xdg-open uses the desktop's default browser; webbrowser has its own order of preference
		if shutil.which('xdg-open'):
			subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		elif not webbrowser.open(url):
			print(f'''No browser available to open {url}''')
	except Exception as e:
		print(f'''Could not open {url} in a browser - {e}''')


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

	this_ip_address, PORT = find_port()

	# Use every detected camera with default settings
	try:
		installed_cameras = get_installed_cameras()
		cameras_to_use, cameras_to_use_configs = get_detected_cameras(installed_cameras)
		configured_cameras = configure_cameras(installed_cameras,cameras_to_use, cameras_to_use_configs)
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

	# Each request (e.g. each open stream) is handled in its own thread
	try:
		server = make_server(this_ip_address, PORT, app, threaded=True)
	except Exception as e:
		print(f'Could not start the web server on port {PORT} - {e}')
		force_quit(1)

	print('-------------------------------------------------------\n')
	print(f"View cameras at http://{this_ip_address}:{PORT}\n")

	open_browser(f"http://{this_ip_address}:{PORT}")

	server.serve_forever()
