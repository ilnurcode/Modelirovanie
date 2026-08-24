#!/usr/bin/env python3
"""Normalize an exported Markdown page into a knowledge article draft."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path


def quote_yaml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def normalize(text: str, title: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = text.replace("\u200b", "")
    lines = [line.rstrip() for line in text.split("\n")]
    lines = [line for line in lines if not re.match(r"^_Source:\s*.+_$", line)]
    while True:
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].strip().lstrip("#").strip() == title.strip():
            lines.pop(0)
            continue
        break
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return f"# {title}\n\n{body}\n" if body else f"# {title}\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--product", default="1C:ERP")
    parser.add_argument("--version", default="2.5")
    parser.add_argument("--accessed-at", default=date.today().isoformat())
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.id):
        parser.error("--id must be lowercase kebab-case")
    body = normalize(args.source.read_text(encoding="utf-8-sig"), args.title)
    frontmatter = "\n".join(["---", f"id: {args.id}", f"title: {quote_yaml(args.title)}", f"product: {quote_yaml(args.product)}", f"version: {quote_yaml(args.version)}", "verification_status: unresolved", f"source_url: {args.source_url}", f"accessed_at: {args.accessed_at}", "tags: []", "---", ""])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(frontmatter + body, encoding="utf-8", newline="\n")
    print(f"Created draft: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
