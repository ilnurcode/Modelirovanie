#!/usr/bin/env python3
"""Build evidence-backed ERP object graph from functional-option Markdown."""
import argparse
import json
import os
from pathlib import Path


TYPE_MAP = {
    "Документ": "Document", "Справочник": "Catalog", "Регистр сведений": "InformationRegister",
    "Регистр накопления": "AccumulationRegister", "Регистр бухгалтерии": "AccountingRegister",
    "Перечисление": "Enum", "План видов характеристик": "ChartOfCharacteristicTypes",
    "Константа": "Constant", "Бизнес-процесс": "BusinessProcess", "Задача": "Task",
    "Обработка": "Processor", "Отчет": "Report", "Функциональная опция": "FunctionalOption",
}
CHILD_KINDS = {"Attribute": "has_attribute", "Command": "has_command", "TabularSection": "has_tabular_section"}
PREFIX_TYPES = {"Document": "Document", "Catalog": "Catalog", "InformationRegister": "InformationRegister", "AccumulationRegister": "AccumulationRegister", "AccountingRegister": "AccountingRegister", "DataProcessor": "Processor", "Report": "Report", "BusinessProcess": "BusinessProcess", "Task": "Task"}


def markdown_files(root, skipped):
    for directory, _, names in os.walk(root, onerror=lambda error: skipped.append(str(error))):
        for name in names:
            if name.lower().endswith(".md"):
                yield Path(directory) / name


def cells(line):
    marker = "__PIPE__"
    return [part.strip().replace(marker, "|") for part in line.replace("\\|", marker).strip().strip("|").split("|")]


def parent_ref(reference):
    for child_kind, relation in CHILD_KINDS.items():
        marker = f".{child_kind}."
        if marker in reference:
            return reference.split(marker, 1)[0], relation
    return None, None


def main():
    parser = argparse.ArgumentParser(description="Build ERP object graph from UI evidence")
    parser.add_argument("--interface", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--configuration", default="1С:ERP 2.5")
    parser.add_argument("--release", default="[релиз не подтвержден]")
    args = parser.parse_args(); root = args.interface.resolve(); nodes, edges, skipped, edge_keys = {}, [], [], set()

    def add_node(node_id, label, node_type, properties):
        current = nodes.get(node_id)
        if current is None or (current["type"] == "MetadataContainer" and node_type != "MetadataContainer"):
            nodes[node_id] = {"id": node_id, "label": label, "type": node_type, "properties": properties}

    def add_edge(source, target, relationship, source_ref):
        key = (source, target, relationship, source_ref)
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({"source": source, "target": target, "relationship": relationship, "source_ref": source_ref})

    options_root = root / "03_Функциональные_опции"
    if not options_root.is_dir():
        raise SystemExit(f"Каталог функциональных опций не найден: {options_root}")
    for file in markdown_files(options_root, skipped):
        relative = file.relative_to(root)
        try:
            with file.open(encoding="utf-8", errors="replace") as handle:
                lines = []
                for number, line in enumerate(handle):
                    if number >= 500:
                        break
                    lines.append(line.rstrip("\n"))
        except OSError as error:
            skipped.append(str(error)); continue
        option_id = "FunctionalOption." + file.stem.split("_", 1)[-1]
        source_ref = str(file.resolve())
        add_node(option_id, file.stem, "FunctionalOption", {"source_path": source_ref, "path": relative.as_posix()})
        for line in lines:
            if not line.startswith("|") or line.startswith("|---"):
                continue
            row = cells(line)
            if len(row) < 8 or not row[6] or row[6] == "Ссылка метаданных":
                continue
            reference = row[6]
            if "." not in reference:
                continue
            technical_type = row[2]
            node_type = TYPE_MAP.get(technical_type, technical_type or "Metadata")
            label = row[4] or row[3] or reference.rsplit(".", 1)[-1]
            add_node(reference, label, node_type, {"technical_path": row[1], "technical_name": row[3], "source_xml": row[7], "source_path": source_ref})
            add_edge(option_id, reference, "available_when", source_ref)
            parent, relation = parent_ref(reference)
            if parent:
                parent_type = PREFIX_TYPES.get(parent.split(".", 1)[0], "MetadataContainer") if parent.count(".") == 1 else "MetadataContainer"
                add_node(parent, parent.rsplit(".", 1)[-1], parent_type, {"source_path": source_ref, "evidence": f"Родитель явной ссылки {reference}"})
                add_edge(parent, reference, relation, source_ref)

    graph = {"configuration": args.configuration, "release": args.release, "status": "ГОТОВ", "scope": "Объекты, технические пути, реквизиты и функциональные опции, явно указанные в интерфейсных источниках.", "nodes": nodes, "edges": edges, "build_report": {"skipped_files": skipped}}
    args.output.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"nodes": len(nodes), "edges": len(edges), "skipped_files": len(skipped)}, ensure_ascii=False))


if __name__ == "__main__": main()
