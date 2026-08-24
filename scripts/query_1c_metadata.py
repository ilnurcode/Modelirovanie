#!/usr/bin/env python3
"""Query the compact 1C metadata index without loading it into agent context."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("terms", nargs="+")
    parser.add_argument("--index", type=Path, default=Path("metadata/index/objects.ndjson"))
    parser.add_argument("--type")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    terms = [term.casefold() for term in args.terms]
    matches = []
    with args.index.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if args.type and record.get("metadata_type", "").casefold() != args.type.casefold():
                continue
            main_text = " ".join([record.get("qualified_name", ""), record.get("internal_name", ""), record.get("synonym", "")]).casefold()
            full_text = main_text + " " + json.dumps(record.get("child_objects", []), ensure_ascii=False).casefold()
            if not all(term in full_text for term in terms):
                continue
            score = sum(20 if term in main_text else 1 for term in terms)
            if any(main_text == term for term in terms):
                score += 100
            matches.append((score, record))
    matches.sort(key=lambda item: (-item[0], item[1]["qualified_name"].casefold()))
    for _, record in matches[: args.limit]:
        if args.full:
            print(json.dumps(record, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({key: record.get(key) for key in ("id", "qualified_name", "metadata_type", "internal_name", "synonym", "presentations", "uuid", "configuration_version", "source_path", "related_paths")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
