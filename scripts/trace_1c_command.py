#!/usr/bin/env python3
"""Trace an object command through 1C subsystem command interfaces."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if local(item.tag) == name), None)


def direct_text(element: ET.Element, name: str) -> str:
    item = direct(element, name)
    return (item.text or "").strip() if item is not None else ""


def subsystem_chain(relative: Path) -> list[str]:
    parts = list(relative.parts)
    return [parts[index + 1] for index, part in enumerate(parts[:-1]) if part == "Subsystems" and parts[index + 1] != "Ext"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("qualified_name", help="Example: Document.ЗаказПоставщику")
    parser.add_argument("--command", default="StandardCommand.OpenList")
    parser.add_argument("--config", type=Path, default=Path("config"))
    args = parser.parse_args()
    target = f"{args.qualified_name}.{args.command}"
    results = []
    for path in args.config.glob("Subsystems/**/Ext/CommandInterface.xml"):
        root = ET.parse(path).getroot()
        for command in root.iter():
            if local(command.tag) != "Command" or command.attrib.get("name") != target:
                continue
            visibility = direct(command, "Visibility")
            common = ""
            roles = []
            if visibility is not None:
                for item in visibility:
                    if local(item.tag) == "Common":
                        common = (item.text or "").strip()
                    elif local(item.tag) == "Value" and (item.text or "").strip() == "true":
                        roles.append(item.attrib.get("name", ""))
            results.append({
                "command": target,
                "subsystem_chain": subsystem_chain(path.relative_to(args.config)),
                "command_group": direct_text(command, "CommandGroup"),
                "placement": direct_text(command, "Placement"),
                "common_visibility": common,
                "visible_for_roles": roles,
                "source_path": str(path.relative_to(args.config)).replace("\\", "/"),
            })
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
