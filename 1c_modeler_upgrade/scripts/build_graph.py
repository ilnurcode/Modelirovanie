#!/usr/bin/env python3
"""Build a portable evidence graph from Markdown and an optional object graph."""
import argparse
import json
import os
import re
from pathlib import Path


def add_node(nodes, node_id, label, node_type, properties):
    nodes.setdefault(node_id, {"id": node_id, "label": label, "type": node_type, "properties": properties})


def add_edge(edges, source, target, relationship, source_ref):
    edges.append({"source": source, "target": target, "relationship": relationship, "source_ref": source_ref})


def markdown_files(root, skipped):
    for directory, _, names in os.walk(root, onerror=lambda error: skipped.append(str(error))):
        for name in names:
            if name.lower().endswith(".md"):
                yield Path(directory) / name


def scan_markdown(root, prefix, root_id, root_label, classifier, nodes, edges, skipped):
    root = root.resolve(); add_node(nodes, root_id, root_label, "Root", {})
    for file in markdown_files(root, skipped):
        try:
            relative = file.relative_to(root); content = file.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            skipped.append(str(error)); continue
        node_id = prefix + relative.as_posix()
        search_text = re.sub(r"\s+", " ", content)[:6000]
        add_node(nodes, node_id, file.stem, classifier(relative), {"source_path": str(file.resolve()), "path": relative.as_posix(), "search_text": search_text})
        add_edge(edges, root_id, node_id, "contains", str(file.resolve()))
        if prefix == "IFACE_":
            for match in re.finditer(r"\[[^\]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)", content):
                target_file = (file.parent / match.group(1)).resolve()
                if target_file.is_file() and target_file.is_relative_to(root):
                    add_edge(edges, node_id, prefix + target_file.relative_to(root).as_posix(), "references", str(file.resolve()))


def main():
    parser = argparse.ArgumentParser(description="Build 1C modeler evidence graph")
    parser.add_argument("--raw", required=True, type=Path, help="ITS Markdown directory")
    parser.add_argument("--interface", required=True, type=Path, help="UI Markdown directory")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--configuration", default="1С:ERP 2.5")
    parser.add_argument("--release", default="[указать релиз]")
    parser.add_argument("--object-graph", type=Path, help="Optional typed object graph JSON")
    args = parser.parse_args()
    nodes, edges, skipped = {}, [], []
    scan_markdown(args.interface, "IFACE_", "ROOT_INTERFACE", "Интерфейс 1С:ERP", lambda p: "FunctionalOption" if "Функциональные_опции" in str(p) else "Metadata" if "Технические_метаданные" in str(p) else "InterfaceRoute", nodes, edges, skipped)
    scan_markdown(args.raw, "RAW_", "ROOT_RAW", "Методология ИТС 1С:ERP", lambda _: "MethodologyArticle", nodes, edges, skipped)
    if args.object_graph:
        external = json.loads(args.object_graph.read_text(encoding="utf-8-sig"))
        external_nodes = external.get("nodes", {}).values() if isinstance(external.get("nodes"), dict) else external.get("nodes", [])
        for node in external_nodes: add_node(nodes, node["id"], node["label"], node["type"], node.get("properties", {}))
        for edge in external.get("edges", []): add_edge(edges, edge["source"], edge["target"], edge["relationship"], edge.get("source_ref", "[не указан]"))
    args.output.write_text(json.dumps({"configuration": args.configuration, "release": args.release, "nodes": nodes, "edges": edges, "build_report": {"skipped_files": skipped}}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"nodes": len(nodes), "edges": len(edges), "skipped_files": len(skipped)}, ensure_ascii=False))


if __name__ == "__main__": main()
