#!/usr/bin/env python3
"""Inspect fields and managed-form controls for one indexed 1C object."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

CONTROL_TYPES = {"Button", "InputField", "CheckBoxField", "RadioButtonField", "Table", "LabelField", "Page", "Popup"}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if local(item.tag) == name), None)


def direct_text(element: ET.Element, name: str) -> str:
    item = direct(element, name)
    return (item.text or "").strip() if item is not None else ""


def localized(element: ET.Element, name: str) -> str:
    container = direct(element, name)
    if container is None:
        return ""
    fallback = ""
    for item in container:
        lang = direct_text(item, "lang")
        content = direct_text(item, "content")
        if content and not fallback:
            fallback = content
        if lang == "ru" and content:
            return content
    return fallback


def flatten(children: list[dict]) -> list[dict]:
    result = []
    for item in children:
        result.append({key: value for key, value in item.items() if key != "children"})
        result.extend(flatten(item.get("children", [])))
    return result


def find_record(index: Path, qualified_name: str) -> dict:
    with index.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("qualified_name") == qualified_name:
                return record
    raise SystemExit(f"Metadata object not found: {qualified_name}")


def inspect_form(path: Path, terms: list[str], limit: int) -> dict:
    root = ET.parse(path).getroot()
    controls = []
    for element in root.iter():
        kind = local(element.tag)
        if kind not in CONTROL_TYPES:
            continue
        item = {
            "kind": kind,
            "name": element.attrib.get("name", ""),
            "title": localized(element, "Title"),
            "data_path": direct_text(element, "DataPath"),
            "command": direct_text(element, "CommandName"),
        }
        haystack = json.dumps(item, ensure_ascii=False).casefold()
        if terms and not any(term in haystack for term in terms):
            continue
        if not any(item.values()):
            continue
        controls.append(item)
        if len(controls) >= limit:
            break
    commands = []
    commands_container = next((item for item in root if local(item.tag) == "Commands"), None)
    if commands_container is not None:
        for element in commands_container:
            if local(element.tag) != "Command":
                continue
            item = {
                "name": element.attrib.get("name", ""),
                "title": localized(element, "Title"),
                "action": direct_text(element, "Action"),
            }
            haystack = json.dumps(item, ensure_ascii=False).casefold()
            if terms and not any(term in haystack for term in terms):
                continue
            commands.append(item)
            if len(commands) >= limit:
                break
    return {"source_path": str(path).replace("\\", "/"), "controls": controls, "commands": commands}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("qualified_name")
    parser.add_argument("--config", type=Path, default=Path("config"))
    parser.add_argument("--index", type=Path, default=Path("metadata/index/objects.ndjson"))
    parser.add_argument("--terms", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    terms = [term.casefold() for term in args.terms]
    record = find_record(args.index, args.qualified_name)
    fields = flatten(record.get("child_objects", []))
    if terms:
        fields = [item for item in fields if any(term in json.dumps(item, ensure_ascii=False).casefold() for term in terms)]
    forms = []
    for relative in record.get("related_paths", {}).get("forms", []):
        path = args.config / relative
        if path.exists():
            forms.append(inspect_form(path, terms, args.limit))
    result = {
        "object": {key: record.get(key) for key in ("id", "qualified_name", "synonym", "presentations", "uuid", "configuration_version", "source_path")},
        "fields": fields[: args.limit],
        "forms": forms,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
