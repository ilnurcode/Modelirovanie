#!/usr/bin/env python3
"""Build an evidence-backed semantic graph from an exact 1C XML dump.

The graph deliberately describes metadata facts, not assumed business behaviour:

* ``creates_based_on`` comes only from the object's ``Properties/BasedOn`` list;
* ``declares_register_records`` comes only from ``RegisterRecords``;
* structure and reference types come from the corresponding child-object XML.

Every edge retains a repository-relative primary source and an XML location.
"""
from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Iterable


REFERENCE_TYPES = {
    "DocumentRef": "Document",
    "CatalogRef": "Catalog",
    "EnumRef": "Enum",
    "BusinessProcessRef": "BusinessProcess",
    "TaskRef": "Task",
    "ChartOfCharacteristicTypesRef": "ChartOfCharacteristicTypes",
    "ChartOfAccountsRef": "ChartOfAccounts",
    "ChartOfCalculationTypesRef": "ChartOfCalculationTypes",
    "ExchangePlanRef": "ExchangePlan",
}

CHILD_RELATIONS = {
    "Attribute": "has_attribute",
    "Dimension": "has_dimension",
    "Resource": "has_resource",
    "TabularSection": "has_tabular_section",
    "Form": "has_form",
    "Command": "has_command",
}

SEMANTIC_OBJECT_TYPES = {
    "AccountingRegister",
    "AccumulationRegister",
    "BusinessProcess",
    "CalculationRegister",
    "Catalog",
    "ChartOfAccounts",
    "ChartOfCalculationTypes",
    "ChartOfCharacteristicTypes",
    "Constant",
    "Document",
    "Enum",
    "ExchangePlan",
    "FunctionalOption",
    "InformationRegister",
    "Task",
}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next((item for item in element if local(item.tag) == name), None)


def child_text(element: ET.Element | None, name: str) -> str:
    item = child(element, name)
    return (item.text or "").strip() if item is not None else ""


def values(element: ET.Element | None) -> Iterable[ET.Element]:
    return tuple(element) if element is not None else ()


def reference_from_type(value: str) -> str:
    """Convert a generated cfg type such as DocumentRef.X to metadata notation."""
    value = value.removeprefix("cfg:").strip()
    if "." not in value:
        return ""
    prefix, name = value.split(".", 1)
    metadata_type = REFERENCE_TYPES.get(prefix)
    return f"{metadata_type}.{name}" if metadata_type else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--metadata-index", required=True, type=Path)
    parser.add_argument("--configuration-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    repo = args.repo.resolve()
    config = args.config.resolve()
    manifest = json.loads(args.configuration_manifest.read_text(encoding="utf-8-sig"))
    records: dict[str, dict] = {}
    with args.metadata_index.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                item = json.loads(line)
                records[str(item["qualified_name"])] = item

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    edge_keys: set[tuple[str, str, str, str]] = set()
    relation_counts: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    parsed = 0

    def source_ref(path: Path) -> str:
        try:
            return path.resolve().relative_to(repo).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def add_metadata_node(qualified_name: str) -> bool:
        if qualified_name in nodes:
            return True
        record = records.get(qualified_name)
        if not record:
            return False
        nodes[qualified_name] = {
            "id": qualified_name,
            "label": record.get("synonym") or record.get("internal_name") or qualified_name,
            "type": record.get("metadata_type") or qualified_name.split(".", 1)[0],
            "properties": {
                "technical_name": record.get("internal_name", ""),
                "source_xml": record.get("source_path", ""),
                "verification_status": "verified_metadata",
            },
        }
        return True

    def add_child_node(node_id: str, label: str, node_type: str, xml_ref: str) -> None:
        nodes.setdefault(
            node_id,
            {
                "id": node_id,
                "label": label or node_id.rsplit(".", 1)[-1],
                "type": node_type,
                "properties": {
                    "technical_name": node_id.rsplit(".", 1)[-1],
                    "source_xml": xml_ref,
                    "verification_status": "verified_metadata",
                },
            },
        )

    def add_edge(
        source: str,
        target: str,
        relationship: str,
        xml_ref: str,
        xpath: str,
        evidence: str,
    ) -> None:
        key = (source, target, relationship, xml_ref)
        if key in edge_keys:
            return
        edge_keys.add(key)
        relation_counts[relationship] += 1
        edges.append(
            {
                "id": f"sem-{len(edges) + 1:06d}",
                "source": source,
                "target": target,
                "relationship": relationship,
                "verification_status": "verified_metadata",
                "source_ref": xml_ref,
                "source_xpath": xpath,
                "evidence": evidence,
            }
        )

    def walk_children(
        container: ET.Element | None,
        parent_id: str,
        xml_ref: str,
        source_xml: str,
        xpath: str,
    ) -> None:
        for item in values(container):
            kind = local(item.tag)
            properties = child(item, "Properties")
            name = child_text(properties, "Name") or (item.text or "").strip()
            if not name:
                continue
            child_id = f"{parent_id}.{kind}.{name}"
            add_child_node(child_id, name, kind, source_xml)
            relationship = CHILD_RELATIONS.get(kind, "contains")
            add_edge(
                parent_id,
                child_id,
                relationship,
                xml_ref,
                f"{xpath}/ChildObjects/{kind}[Name='{name}']",
                f"{parent_id} содержит {kind} {name}",
            )
            type_element = child(properties, "Type")
            for type_item in type_element.iter() if type_element is not None else ():
                target = reference_from_type((type_item.text or "").strip())
                if target and add_metadata_node(target):
                    add_edge(
                        child_id,
                        target,
                        "references_type",
                        xml_ref,
                        f"{xpath}/ChildObjects/{kind}[Name='{name}']/Properties/Type",
                        f"Тип {child_id}: {target}",
                    )
            walk_children(
                child(item, "ChildObjects"),
                child_id,
                xml_ref,
                source_xml,
                f"{xpath}/ChildObjects/{kind}[Name='{name}']",
            )

    top_level = [
        record for record in records.values()
        if record.get("source_path")
        and record.get("metadata_type") in SEMANTIC_OBJECT_TYPES
        and re.fullmatch(r"[^/]+/[^/]+\.xml", str(record["source_path"]))
    ]
    print(
        json.dumps(
            {"indexed_objects": len(records), "candidate_top_level_objects": len(top_level)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    for record in top_level:
        qualified_name = str(record["qualified_name"])
        path = config / str(record["source_path"])
        if not path.is_file():
            failures.append({"object": qualified_name, "error": "source XML is missing"})
            continue
        try:
            root = ET.parse(path).getroot()
            obj = root[0]
        except (ET.ParseError, OSError, IndexError) as exc:
            failures.append({"object": qualified_name, "error": str(exc)})
            continue
        parsed += 1
        add_metadata_node(qualified_name)
        xml_ref = source_ref(path)
        object_type = local(obj.tag)
        properties = child(obj, "Properties")
        base_xpath = f"/MetaDataObject/{object_type}/Properties"

        for item in values(child(properties, "BasedOn")):
            base = (item.text or "").strip()
            if base and add_metadata_node(base):
                add_edge(
                    base,
                    qualified_name,
                    "creates_based_on",
                    xml_ref,
                    f"{base_xpath}/BasedOn",
                    f"{qualified_name} BasedOn {base}",
                )

        for item in values(child(properties, "RegisterRecords")):
            register = (item.text or "").strip()
            if register and add_metadata_node(register):
                add_edge(
                    qualified_name,
                    register,
                    "declares_register_records",
                    xml_ref,
                    f"{base_xpath}/RegisterRecords",
                    f"{qualified_name} declares register records {register}",
                )

        walk_children(
            child(obj, "ChildObjects"),
            qualified_name,
            xml_ref,
            str(record["source_path"]),
            f"/MetaDataObject/{object_type}",
        )

    status = "ГОТОВ" if not failures and parsed == len(top_level) else "НЕПОЛНЫЙ"
    graph = {
        "configuration": manifest.get("synonym") or "1С:ERP Управление предприятием 2",
        "release": manifest.get("version", ""),
        "status": status,
        "verification_status": "verified_metadata" if status == "ГОТОВ" else "inferred",
        "scope": (
            "Полный набор структурных семантических связей, явно объявленных в XML: "
            "BasedOn, RegisterRecords, состав объектов и ссылочные типы реквизитов. "
            "Граф не предполагает фактическое выполнение условных движений и не "
            "добавляет бизнес-переходы, отсутствующие в метаданных."
        ),
        "provenance": {
            "configuration_manifest": source_ref(args.configuration_manifest),
            "metadata_index": source_ref(args.metadata_index),
            "configuration_sha256": manifest.get("configuration_sha256", ""),
            "dump_info_sha256": manifest.get("dump_info_sha256", ""),
        },
        "nodes": nodes,
        "edges": edges,
        "build_report": {
            "indexed_objects": len(records),
            "candidate_top_level_objects": len(top_level),
            "parsed_top_level_objects": parsed,
            "failed_objects": failures,
            "relationship_counts": dict(sorted(relation_counts.items())),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": status,
                "nodes": len(nodes),
                "edges": len(edges),
                "parsed": parsed,
                "failed": len(failures),
                "relationships": graph["build_report"]["relationship_counts"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
