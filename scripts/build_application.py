from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


EXCLUDED = {
    ".git", ".venv", "application-packages", "build", "consultant.exe", "deployment", "dist",
    "release", "results", "tests",
}


def copy_application(root: Path, stage: Path) -> None:
    for source in root.iterdir():
        if source.name in EXCLUDED:
            continue
        destination = stage / source.name
        if source.name == "1c_modeler_upgrade":
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("graphs"))
        elif source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


def write_integrity(stage: Path) -> None:
    lines = []
    for path in sorted(item for item in stage.rglob("*") if item.is_file()):
        if path.name == "FILES.sha256":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(stage).as_posix()}")
    (stage / "FILES.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_archive(archive: Path, executable: str) -> None:
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        if executable not in names or any(name.startswith("1c_modeler_upgrade/graphs/") for name in names):
            raise RuntimeError("application archive has invalid contents")
        for line in package.read("FILES.sha256").decode("utf-8").splitlines():
            expected, name = line.split("  ", 1)
            if hashlib.sha256(package.read(name)).hexdigest() != expected:
                raise RuntimeError(f"integrity check failed for {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("windows", "linux", "macos"), required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))["application_version"]
    executable = "consultant.exe" if args.platform == "windows" else "consultant"
    dist = root / "dist" / f"{args.platform}-{args.arch}"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", "consultant", "--paths", str(root / "src"),
            "--distpath", str(dist),
            "--workpath", str(root / "build" / f"pyinstaller-{args.platform}-{args.arch}"),
            "--specpath", str(root / "build"),
            str(root / "src" / "consultant_cli" / "__main__.py"),
        ],
        check=True,
    )
    binary = dist / executable
    binary.chmod(binary.stat().st_mode | 0o755)
    actual_version = subprocess.check_output([binary, "--version"], text=True).strip()
    if actual_version != version:
        raise RuntimeError(f"application version {actual_version}, expected {version}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="1c-consultant-application-") as temp:
        stage = Path(temp)
        copy_application(root, stage)
        shutil.copy2(binary, stage / executable)
        (stage / executable).chmod(0o755)
        write_integrity(stage)
        with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                package.write(path, path.relative_to(stage).as_posix())
    verify_archive(args.output, executable)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
