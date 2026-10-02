#!/usr/bin/env python3
"""
One-time setup for scanCam: asks for an install directory, copies the
program there, then installs system packages and creates the Python venv.

Run from anywhere with: python3 install.py
Running it again updates the program files and recreates the venv.
"""
import shutil
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
default_target = Path.home() / 'scanCam'
# Copied into the install directory; the venv and run.sh are created there.
program_files = ['README.md', 'code']
run_script = """#!/bin/bash
# Start scanCam using its venv (recreate it with python3 install.py).
cd "$(dirname "$0")"
exec venv/bin/python -u code/scanCam.py "$@"
"""


def ask_target() -> Path:
	try:
		answer = input(f"Enter the directory to install scanCam into [{default_target}]: ").strip()
	except EOFError:
		# No terminal to answer from (e.g. piped input), so take the default.
		answer = ''
	return Path(answer).expanduser().resolve() if answer else default_target


def copy_program(target: Path):
	target.mkdir(parents=True, exist_ok=True)
	if target != here:
		print(f"Copying scanCam into {target}")
		for name in program_files:
			source = here / name
			if source.is_dir():
				shutil.copytree(source, target / name, dirs_exist_ok=True,
					ignore=shutil.ignore_patterns('__pycache__'))
			else:
				shutil.copy2(source, target / name)
	run = target / 'run.sh'
	run.write_text(run_script)
	run.chmod(0o755)


def is_raspberry_pi() -> bool:
	try:
		return 'raspberry pi' in Path('/proc/device-tree/model').read_text().lower()
	except OSError:
		return False


def is_installed(package: str) -> bool:
	result = subprocess.run(['dpkg', '-s', package], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	return result.returncode == 0


def install_system_packages():
	packages = ['python3-venv', 'v4l-utils']
	# Pi cameras need picamera2 / libcamera, which come from apt (not pip).
	if is_raspberry_pi():
		packages += ['python3-picamera2', 'python3-libcamera']

	missing = [package for package in packages if not is_installed(package)]
	if missing:
		print(f"Installing system packages: {' '.join(missing)}")
		subprocess.run(['sudo', 'apt-get', 'update'], check=True)
		subprocess.run(['sudo', 'apt-get', 'install', '-y', *missing], check=True)


def create_venv(target: Path):
	# --system-site-packages lets the venv use picamera2 / libcamera installed with apt.
	# Use the base Python, since sys.executable may be inside the venv being deleted.
	python = Path(sys.base_prefix) / 'bin' / 'python3'
	venv = target / 'venv'
	shutil.rmtree(venv, ignore_errors=True)
	subprocess.run([str(python), '-m', 'venv', '--system-site-packages', str(venv)], check=True)
	subprocess.run([str(venv / 'bin' / 'pip'), 'install', '-r', str(target / 'code' / 'requirements.txt')], check=True)
	print('venv ready')


def main():
	target = ask_target()
	try:
		copy_program(target)
		install_system_packages()
		create_venv(target)
	except subprocess.CalledProcessError as e:
		sys.exit(f"Install failed: {' '.join(e.cmd)} exited with {e.returncode}")
	except OSError as e:
		sys.exit(f"Install failed: {e}")
	print()
	print(f"scanCam is installed in {target}. Start it with: {target / 'run.sh'}")


if __name__ == '__main__':
	main()
