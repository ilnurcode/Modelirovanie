#!/usr/bin/env python3
"""Build a release-exact 1C interface route graph from configuration XML."""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if local(item.tag) == name), None)


def direct_text(element: ET.Element, name: str) -> str:
    item = direct(element, name)
    return (item.text or "").strip() if item is not None else ""


def subsystem_chain(relative: Path) -> list[str]:
    parts = list(relative.parts)
    result = []
    for index, part in enumerate(parts[:-1]):
        if part != "Subsystems" or parts[index + 1] == "Ext":
            continue
        value = parts[index + 1]
        result.append(Path(value).stem if value.endswith(".xml") else value)
    return result


def read_index(index_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    objects: dict[str, dict[str, Any]] = {}
    subsystems: dict[str, str] = {}
    with index_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            qualified = str(record.get("qualified_name") or "")
            if qualified:
                objects[qualified] = record
            if record.get("metadata_type") == "Subsystem":
                internal = str(record.get("internal_name") or "")
                if internal:
                    subsystems[internal] = str(record.get("synonym") or internal)
    return objects, subsystems


def command_label(command_name: str, objects: dict[str, dict[str, Any]]) -> str:
    if ".StandardCommand." in command_name:
        qualified, standard_command = command_name.split(".StandardCommand.", 1)
        record = objects.get(qualified, {})
        presentations = record.get("presentations") or {}
        base = str(
            presentations.get("ExtendedListPresentation")
            or presentations.get("ListPresentation")
            or record.get("synonym")
            or record.get("internal_name")
            or qualified
        )
        if standard_command == "OpenList":
            return base
        return f"{base} — {standard_command}"
    if ".Command." in command_name:
        qualified, child_name = command_name.split(".Command.", 1)
        record = objects.get(qualified, {})
        for child in record.get("child_objects") or []:
            if child.get("kind") == "Command" and child.get("name") == child_name:
                return str(child.get("synonym") or child_name)
        return str(record.get("synonym") or child_name)
    record = objects.get(command_name, {})
    return str(record.get("synonym") or record.get("internal_name") or command_name)


def iter_commands(path: Path) -> Iterable[ET.Element]:
    root = ET.parse(path).getroot()
    return (
        element
        for element in root.iter()
        if local(element.tag) == "Command" and element.attrib.get("name")
    )


def build_graph(
    repo_root: Path,
    config_root: Path,
    index_path: Path,
    configuration_manifest: Path,
) -> dict[str, Any]:
    manifest = json.loads(configuration_manifest.read_text(encoding="utf-8"))
    objects, subsystem_names = read_index(index_path)
    nodes: dict[str, dict[str, Any]] = {
        "ROOT_ROUTES": {
            "id": "ROOT_ROUTES",
            "label": "Пользовательские маршруты 1С:ERP",
            "type": "Root",
            "properties": {},
        }
    }
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_route(
        display_chain: list[str],
        technical_chain: list[str],
        command_name: str,
        source_xml: str,
        source_path: str,
        command_group: str = "",
        placement: str = "",
        common_visibility: str = "",
        roles: list[str] | None = None,
    ) -> None:
        label = command_label(command_name, objects)
        user_path = " → ".join([*display_chain, label])
        key = (user_path, command_name)
        if key in seen:
            return
        seen.add(key)
        digest = hashlib.sha256("\u241f".join(key).encode("utf-8")).hexdigest()[:16]
        node_id = f"Route.XML.{digest}"
        nodes[node_id] = {
            "id": node_id,
            "label": label,
            "type": "InterfaceRoute",
            "properties": {
                "path": user_path,
                "technical_path": " / ".join(technical_chain),
                "technical_name": command_name,
                "source_xml": source_xml,
                "source_path": source_path,
                "command_group": command_group,
                "placement": placement,
                "common_visibility": common_visibility,
                "visible_for_roles": roles or [],
                "verification_status": "verified_metadata",
            },
        }
        edges.append(
            {"source": "ROOT_ROUTES", "target": node_id, "type": "contains"}
        )

    interface_paths = sorted(config_root.glob("Subsystems/**/Ext/CommandInterface.xml"))
    for interface_path in interface_paths:
        relative_config = interface_path.relative_to(config_root)
        technical_chain = subsystem_chain(relative_config)
        display_chain = [subsystem_names.get(item, item) for item in technical_chain]
        source_path = interface_path.relative_to(repo_root).as_posix()
        source_xml = relative_config.as_posix()
        for command in iter_commands(interface_path):
            command_name = str(command.attrib.get("name") or "")
            if not (
                command_name.endswith(".StandardCommand.OpenList")
                or command_name.startswith("CommonCommand.")
                or ".Command." in command_name
            ):
                continue
            visibility = direct(command, "Visibility")
            roles: list[str] = []
            common_visibility = ""
            if visibility is not None:
                for item in visibility:
                    if local(item.tag) == "Common":
                        common_visibility = (item.text or "").strip()
                    elif local(item.tag) == "Value" and (item.text or "").strip() == "true":
                        roles.append(str(item.attrib.get("name") or ""))
            add_route(
                display_chain,
                technical_chain,
                command_name,
                source_xml,
                source_path,
                direct_text(command, "CommandGroup"),
                direct_text(command, "Placement"),
                common_visibility,
                roles,
            )

    visible_types = {
        "Catalog",
        "Document",
        "InformationRegister",
        "Report",
        "DataProcessor",
        "CommonForm",
    }
    for definition_path in sorted(config_root.glob("Subsystems/**/*.xml")):
        if "Ext" in definition_path.relative_to(config_root).parts:
            continue
        try:
            root = ET.parse(definition_path).getroot()
        except (FileNotFoundError, OSError, ET.ParseError):
            # Some Windows dumps contain paths beyond the legacy path limit or
            # optional files referenced by the dump but absent on disk.
            continue
        content = next((item for item in root.iter() if local(item.tag) == "Content"), None)
        if content is None:
            continue
        relative_config = definition_path.relative_to(config_root)
        technical_chain = subsystem_chain(relative_config)
        display_chain = [subsystem_names.get(item, item) for item in technical_chain]
        source_xml = relative_config.as_posix()
        source_path = definition_path.relative_to(repo_root).as_posix()
        for item in content:
            qualified = (item.text or "").strip()
            record = objects.get(qualified, {})
            if record.get("metadata_type") not in visible_types:
                continue
            command_name = (
                f"{qualified}.StandardCommand.OpenList"
                if record.get("metadata_type") in {"Catalog", "Document", "InformationRegister"}
                else qualified
            )
            add_route(
                display_chain,
                technical_chain,
                command_name,
                source_xml,
                source_path,
            )
    return {
        "configuration": str(manifest.get("synonym") or manifest.get("name") or ""),
        "release": str(manifest.get("version") or ""),
        "configuration_sha256": str(manifest.get("configuration_sha256") or ""),
        "generator": "scripts/build_1c_route_graph.py",
        "source_manifest": configuration_manifest.relative_to(repo_root).as_posix(),
        "status": "ГОТОВ",
        "verification_status": "verified_metadata",
        "nodes": nodes,
        "edges": edges,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--index", type=Path, default=Path("metadata/index/objects.ndjson"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("metadata/index/configuration.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo_root = args.repo.resolve()

    def resolve(value: Path) -> Path:
        return value.resolve() if value.is_absolute() else (repo_root / value).resolve()

    graph = build_graph(
        repo_root,
        resolve(args.config),
        resolve(args.index),
        resolve(args.manifest),
    )
    output = resolve(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Built {len(graph['nodes']) - 1} routes for release {graph['release']} into {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
