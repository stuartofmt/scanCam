#!/usr/bin/env python3
"""
Build dist/scanCam-<version>.zip, which unzips to a scanCam/ folder holding
README.md, install.py and the program in code/.

Run with: python3 make_zip.py  (on Windows: py make_zip.py)
"""
import re
import zipfile
from pathlib import Path

here = Path(__file__).resolve().parent
# Files and folders that go into the zip, relative to here.
release_files = ['README.md', 'install.py', 'code']


def get_version() -> str:
	match = re.search(r"progVersion = '([^']+)'", (here / 'code' / 'scanCam.py').read_text())
	if match is None:
		raise SystemExit("progVersion not found in code/scanCam.py")
	return match.group(1)


def release_paths():
	for name in release_files:
		path = here / name
		if path.is_dir():
			yield from (p for p in sorted(path.rglob('*')) if p.is_file() and '__pycache__' not in p.parts)
		else:
			yield path


def main():
	out = here / 'dist' / f'scanCam-{get_version()}.zip'
	out.parent.mkdir(exist_ok=True)
	with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zip_file:
		for path in release_paths():
			zip_file.write(path, Path('scanCam') / path.relative_to(here))
	print(f"Created {out}")


if __name__ == '__main__':
	main()
