"""
The she-bang is not required if called with fully qualified paths
The plugin manager does this.  Otherwise use ...
Standard python install e.g.
#!/usr/bin/python3 -u
Venv python install e.g.
#! <path-to-virtual-environment/bin>python -u
"""

# This is to supress the noisy libcam
# MUST BE AT THE VERY START OF THE SCRIPT
import os
os.environ["LIBCAMERA_LOG_LEVELS"] = "*:ERROR"

from pathlib import Path
import sys

import httpx
import threading
import time
import uvicorn
import socket
import signal
import shutil
import subprocess
import webbrowser

from routes import app, start_cameras, set_camera_settings

from logger_module import setup_log


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
		logger.critical(f'''Unknown error trying to get the local IP address''')
		logger.critical(f'''{e}''')
		force_quit(1)
	finally:
		s.close()

	#  Use the first free port starting at start_port
	for port in range(start_port, start_port + max_tries):
		if not port_in_use(this_ip_address, port):
			break
	else:
		logger.critical(f'''No free port found between {start_port} and {start_port + max_tries - 1}''')
		force_quit(1)

	logger.info(f'''IP address {this_ip_address} with port {port} is available''')
	return this_ip_address, port


def open_browser(url):
	#  Only with a desktop - otherwise (e.g. run by DSF) a text browser could take over the terminal
	if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
		logger.debug(f'''No display - not opening {url} in a browser''')
		return
	try:
		#  xdg-open uses the desktop's default browser; webbrowser has its own order of preference
		if shutil.which('xdg-open'):
			subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
		elif not webbrowser.open(url):
			logger.debug(f'''No browser available to open {url}''')
	except Exception as e:
		logger.debug(f'''Could not open {url} in a browser - {e}''')


def force_quit(code):
	logger.critical(f'''Terminating the program with exit code {code}''')
	sys.exit(code)

def sig_handler(signum, frame):
	signame = signal.Signals(signum).name
	logger.info(f'Shutting down.  Recieved signal {signame} ({signum})')
	force_quit(0)


if __name__ == "__main__":

	# shutdown with SIGINT (kill -2 <pid> or SIGTERM)
	signal.signal(signal.SIGINT, sig_handler)
	signal.signal(signal.SIGTERM, sig_handler)

	global progName, progVersion
	progName = os.path.splitext(os.path.basename(sys.argv[0]))[0]
	progVersion = '1.0.0'

	LOGFILENAME = Path(__file__).parent / f"{progName}.log"

	if not setup_log(progName,LOGFILENAME):
		logger.error(f"Failed to setup logging to {LOGFILENAME}. Please ensure the file is writable.")
		sys.exit(1)

	from logger_module import logger # Need to import after setup_logging is called
	logger.info(f'''Log file for {progName} -- {progVersion}''')

	from get_config import (get_installed_cameras, get_detected_cameras, configure_cameras,
						get_camera_settings)

	this_ip_address, PORT = find_port()

	# Use every detected camera with default settings
	try:
		installed_cameras = get_installed_cameras()
		cameras_to_use, cameras_to_use_configs = get_detected_cameras(installed_cameras)
		configured_cameras = configure_cameras(installed_cameras,cameras_to_use, cameras_to_use_configs)
	except Exception as e:
		logger.info(f'{e}')
		force_quit(1)

	# Values shown on the index page
	try:
		set_camera_settings(configured_cameras, get_camera_settings(configured_cameras))
	except Exception as e:
		logger.warning(f'Index page will not show camera settings - {e}')
		set_camera_settings(configured_cameras, {})

	# Start uvicorn in a background thread
	def run_server():
		uvicorn.run(
			app,
			host=this_ip_address,
			port=PORT,
			reload=False,
			log_config=None
		)

	server_thread = threading.Thread(target=run_server, daemon=True)
	server_thread.start()

	logger.info("Waiting for server to be ready")
	time.sleep(2)

	with httpx.Client() as client:
			# Use case sensitive order based on keys		
			for camera_name, camera_settings in sorted(configured_cameras.items()):
				try:
					camera_payload = camera_settings
					response = client.post(
						f"http://{this_ip_address}:{PORT}/api/add-camera",
						json=camera_payload,
					)
					if response.is_success and response.json().get("status") == "success":
						logger.info(f"Added {camera_name} with source '{camera_payload['source']}'")
					else:
						logger.error(f"Error adding camera {camera_name}: {response.text}")
				except Exception as e:
					logger.error(f"Error adding camera {camera_name}: {e}")

	# Start all cameras after registration
	try:
		start_cameras()
	except Exception as e:
		logger.critical(f"{e}")
		force_quit(1)

	logger.info('-------------------------------------------------------\n')
	logger.info(f"View cameras at http://{this_ip_address}:{PORT}\n")

	open_browser(f"http://{this_ip_address}:{PORT}")

	# Keep the main thread alive
	server_thread.join()
