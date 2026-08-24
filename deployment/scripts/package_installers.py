from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


TARGETS = {
    "windows-x64": ("1c-consultant-installer-windows-x64.exe", "Install 1C-Consultant.cmd"),
    "linux-x64": ("1c-consultant-installer-linux-x64", "Install 1C-Consultant.sh"),
    "linux-arm64": ("1c-consultant-installer-linux-arm64", "Install 1C-Consultant.sh"),
    "macos-x64": ("1c-consultant-installer-macos-x64", "Install 1C-Consultant.command"),
    "macos-arm64": ("1c-consultant-installer-macos-arm64", "Install 1C-Consultant.command"),
}


def add_bytes(package: zipfile.ZipFile, name: str, data: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    package.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def launcher(platform: str, binary: str) -> str:
    if platform == "windows":
        return f'@echo off\r\ncd /d "%~dp0"\r\n"%~dp0{binary}"\r\npause\r\n'
    return (
        '#!/bin/sh\nDIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        f'chmod +x "$DIR/{binary}"\nexec "$DIR/{binary}"\n'
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    for target, (binary_name, launcher_name) in TARGETS.items():
        binary = args.directory / binary_name
        if not binary.is_file():
            raise FileNotFoundError(binary)
        platform = target.split("-", 1)[0]
        archive = args.directory / f"1c-consultant-setup-{target}.zip"
        with zipfile.ZipFile(archive, "w") as package:
            add_bytes(package, binary_name, binary.read_bytes(), 0o100755)
            add_bytes(package, launcher_name, launcher(platform, binary_name).encode("utf-8"), 0o100755)
        with zipfile.ZipFile(archive) as package:
            if set(package.namelist()) != {binary_name, launcher_name}:
                raise RuntimeError(f"invalid setup archive: {archive}")
        print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
