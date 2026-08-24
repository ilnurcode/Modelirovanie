#!/usr/bin/env python3
"""Build a compact searchable index from a 1C configuration XML dump."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


FOLDER_TYPES = {
    "AccountingRegisters": "AccountingRegister",
    "AccumulationRegisters": "AccumulationRegister",
    "BusinessProcesses": "BusinessProcess",
    "CalculationRegisters": "CalculationRegister",
    "Catalogs": "Catalog",
    "CommonCommands": "CommonCommand",
    "CommonForms": "CommonForm",
    "Constants": "Constant",
    "DataProcessors": "DataProcessor",
    "DocumentJournals": "DocumentJournal",
    "Documents": "Document",
    "Enums": "Enum",
    "Forms": "Form",
    "FunctionalOptions": "FunctionalOption",
    "InformationRegisters": "InformationRegister",
    "Reports": "Report",
    "Roles": "Role",
    "Subsystems": "Subsystem",
    "Tasks": "Task",
    "Commands": "Command",
}

GRAPH_TYPES = {
    "AccountingRegister": "register",
    "AccumulationRegister": "register",
    "CalculationRegister": "register",
    "InformationRegister": "register",
    "Catalog": "catalog",
    "Document": "document",
    "DocumentJournal": "document",
    "Report": "report",
    "Role": "role",
    "Subsystem": "subsystem",
    "Form": "form",
    "CommonForm": "form",
    "Command": "command",
    "CommonCommand": "command",
    "Attribute": "attribute",
    "Dimension": "attribute",
    "Resource": "attribute",
    "TabularSection": "attribute",
}

TRANSLIT = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
    "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
    "х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
})

REF_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:\.[^.\s]+)+$")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next((item for item in element if local(item.tag) == name), None)


def child_text(element: ET.Element | None, name: str) -> str:
    item = child(element, name)
    return (item.text or "").strip() if item is not None else ""


def synonym(properties: ET.Element | None) -> str:
    syn = child(properties, "Synonym")
    if syn is None:
        return ""
    fallback = ""
    for item in syn:
        lang = child_text(item, "lang")
        content = child_text(item, "content")
        if content and not fallback:
            fallback = content
        if lang == "ru" and content:
            return content
    return fallback


def localized_value(properties: ET.Element | None, name: str) -> str:
    value = child(properties, name)
    if value is None:
        return ""
    fallback = ""
    for item in value:
        lang = child_text(item, "lang")
        content = child_text(item, "content")
        if content and not fallback:
            fallback = content
        if lang == "ru" and content:
            return content
    return fallback or (value.text or "").strip()


def kebab(value: str) -> str:
    value = re.sub(r"(?<=[а-яёa-z0-9])(?=[А-ЯЁA-Z])", "-", value)
    value = value.lower().translate(TRANSLIT)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def qualified_from_path(relative: Path, object_type: str, name: str) -> str:
    if relative.name == "Configuration.xml":
        return f"Configuration.{name}"
    parts = list(relative.parts)
    result = []
    for index, part in enumerate(parts[:-1]):
        metadata_type = FOLDER_TYPES.get(part)
        if not metadata_type or index + 1 >= len(parts):
            continue
        next_part = parts[index + 1]
        object_name = Path(next_part).stem if next_part.endswith(".xml") else next_part
        if object_name not in {"Ext", "Forms", "Commands", "Subsystems"}:
            result.append(f"{metadata_type}.{object_name}")
    candidate = ".".join(result)
    if candidate.endswith(f"{object_type}.{name}"):
        return candidate
    return f"{object_type}.{name}"


def describe_child(element: ET.Element, scope: str = "") -> dict:
    kind = local(element.tag)
    props = child(element, "Properties")
    name = child_text(props, "Name") or (element.text or "").strip()
    record = {
        "kind": kind,
        "name": name,
        "synonym": synonym(props),
        "uuid": element.attrib.get("uuid", ""),
    }
    if scope:
        record["scope"] = scope
    nested = child(element, "ChildObjects")
    if nested is not None:
        record["children"] = [describe_child(item, name or scope) for item in nested]
    return record


def parse_metadata(path: Path, config_root: Path, configuration_name: str, configuration_version: str) -> dict | None:
    tree = ET.parse(path)
    root = tree.getroot()
    if local(root.tag) != "MetaDataObject" or not len(root):
        return None
    obj = root[0]
    object_type = local(obj.tag)
    properties = child(obj, "Properties")
    name = child_text(properties, "Name")
    if not name:
        return None
    relative = path.relative_to(config_root)
    qualified_name = qualified_from_path(relative, object_type, name)
    child_objects_element = child(obj, "ChildObjects")
    child_objects = [describe_child(item) for item in child_objects_element] if child_objects_element is not None else []
    references = []
    seen = set()
    for element in obj.iter():
        text = (element.text or "").strip()
        if text and REF_PATTERN.fullmatch(text) and text not in seen:
            seen.add(text)
            references.append(text)
            if len(references) >= 500:
                break
    paths = {
        "rights": str(relative.parent / name / "Ext" / "Rights.xml").replace("\\", "/") if object_type == "Role" else "",
        "command_interface": str(relative.parent / name / "Ext" / "CommandInterface.xml").replace("\\", "/") if object_type == "Subsystem" else "",
    }
    forms = [item["name"] for item in child_objects if item["kind"] == "Form" and item["name"]]
    commands = [item["name"] for item in child_objects if item["kind"] == "Command" and item["name"]]
    if forms:
        paths["forms"] = [str(relative.parent / name / "Forms" / form / "Ext" / "Form.xml").replace("\\", "/") for form in forms]
    return {
        "id": f"md-{kebab(object_type)}-{kebab(qualified_name)}",
        "qualified_name": qualified_name,
        "metadata_type": object_type,
        "graph_type": GRAPH_TYPES.get(object_type, "mechanism"),
        "internal_name": name,
        "synonym": synonym(properties),
        "presentations": {
            key: localized_value(properties, key)
            for key in ("ObjectPresentation", "ExtendedObjectPresentation", "ListPresentation", "ExtendedListPresentation")
            if localized_value(properties, key)
        },
        "uuid": obj.attrib.get("uuid", ""),
        "configuration": configuration_name,
        "configuration_version": configuration_version,
        "source_path": str(relative).replace("\\", "/"),
        "verification_status": "verified_metadata",
        "child_objects": child_objects,
        "references": references,
        "related_paths": paths,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def configuration_info(config_root: Path) -> dict:
    path = config_root / "Configuration.xml"
    root = ET.parse(path).getroot()[0]
    props = child(root, "Properties")
    return {
        "name": child_text(props, "Name"),
        "synonym": synonym(props),
        "version": child_text(props, "Version"),
        "vendor": child_text(props, "Vendor"),
        "uuid": root.attrib.get("uuid", ""),
        "format_version": ET.parse(path).getroot().attrib.get("version", ""),
        "configuration_sha256": sha256(path),
        "dump_info_sha256": sha256(config_root / "ConfigDumpInfo.xml") if (config_root / "ConfigDumpInfo.xml").exists() else "",
    }


def candidate_files(config_root: Path) -> list[Path]:
    files = []
    for path in config_root.rglob("*.xml"):
        relative = path.relative_to(config_root)
        if path.name == "ConfigDumpInfo.xml" or "Ext" in relative.parts:
            continue
        files.append(path)
    return sorted(files, key=lambda item: str(item).casefold())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, default=Path("metadata/index"))
    args = parser.parse_args()
    config_root = args.config.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    info = configuration_info(config_root)
    candidates = candidate_files(config_root)
    type_counts = Counter()
    failures = []
    count = 0
    temp = output / "objects.ndjson.tmp"
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        for number, path in enumerate(candidates, 1):
            try:
                record = parse_metadata(path, config_root, info["name"], info["version"])
            except (ET.ParseError, OSError, ValueError) as exc:
                failures.append({"path": str(path.relative_to(config_root)).replace("\\", "/"), "error": str(exc)})
                continue
            if not record:
                continue
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            type_counts[record["metadata_type"]] += 1
            count += 1
            if number % 1000 == 0:
                print(f"Processed {number}/{len(candidates)} candidates; indexed {count}", flush=True)
    temp.replace(output / "objects.ndjson")

    manifest = {
        **info,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(config_root),
        "candidate_xml_files": len(candidates),
        "indexed_objects": count,
        "failed_files": len(failures),
        "type_counts": dict(sorted(type_counts.items())),
    }
    (output / "configuration.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
