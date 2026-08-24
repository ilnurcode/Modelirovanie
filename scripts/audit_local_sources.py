#!/usr/bin/env python3
"""Build a compact, content-safe inventory for local source trees."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_inventory(root: Path) -> dict[str, object]:
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.as_posix().casefold(),
    )
    manifest = "\n".join(
        f"{path.relative_to(root).as_posix()}|{path.stat().st_size}"
        for path in files
    )
    return {
        "path": str(root),
        "resolved": str(root.resolve()),
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "manifest_sha256": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
    }


def single_file_inventory(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "resolved": str(path.resolve()),
        "files": 1,
        "bytes": path.stat().st_size,
        "sha256": file_hash(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    records = []
    missing = []
    for path in args.paths:
        if not path.exists():
            missing.append(str(path))
            continue
        records.append(
            tree_inventory(path) if path.is_dir() else single_file_inventory(path)
        )

    print(json.dumps({"records": records, "missing": missing}, ensure_ascii=False, indent=2))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
