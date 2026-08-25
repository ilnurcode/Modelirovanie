#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from consultant_cli.domain.analytics import AnalysisBundle
from consultant_cli.services.analytics import validate_bundle


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Deterministic unified-analysis validator")
    value.add_argument("project_id", nargs="?")
    return value


def main() -> int:
    args = parser().parse_args()
    base = ROOT / "results"
    paths = [base / args.project_id / "analysis" / "analysis.json"] if args.project_id else list(base.glob("*/analysis/analysis.json"))
    errors: list[str] = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != "1.0.0":
                errors.append(f"{path}: unsupported schema_version")
                continue
            bundle = AnalysisBundle.from_dict(raw)
            semantic_errors, _ = validate_bundle(bundle)
            errors.extend(f"{path}: {message}" for message in semantic_errors)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        print("Analysis validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Analysis validation passed: {len(paths)} project(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
