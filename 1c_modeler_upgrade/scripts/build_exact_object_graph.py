#!/usr/bin/env python3
"""Derive the exact-release object graph from the XML semantic graph."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


STRUCTURAL_RELATIONSHIPS = {
    "contains",
    "has_attribute",
    "has_command",
    "has_dimension",
    "has_form",
    "has_resource",
    "has_tabular_section",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic-graph", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.semantic_graph.read_text(encoding="utf-8-sig"))
    edges = [
        edge for edge in source.get("edges", [])
        if edge.get("relationship") in STRUCTURAL_RELATIONSHIPS
    ]
    used = {
        value
        for edge in edges
        for value in (edge.get("source"), edge.get("target"))
        if value
    }
    nodes = {
        node_id: node
        for node_id, node in (source.get("nodes") or {}).items()
        if node_id in used
    }
    graph = {
        "configuration": source.get("configuration", ""),
        "release": source.get("release", ""),
        "status": source.get("status", "НЕПОЛНЫЙ"),
        "verification_status": source.get("verification_status", "inferred"),
        "scope": (
            "Объекты, реквизиты, табличные части, измерения, ресурсы, формы и "
            "команды, непосредственно извлечённые из XML точного релиза."
        ),
        "provenance": source.get("provenance", {}),
        "nodes": nodes,
        "edges": edges,
        "build_report": {
            "nodes": len(nodes),
            "edges": len(edges),
            "relationship_counts": {
                relationship: sum(
                    1 for edge in edges if edge.get("relationship") == relationship
                )
                for relationship in sorted(STRUCTURAL_RELATIONSHIPS)
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(graph["build_report"], ensure_ascii=False))


if __name__ == "__main__":
    main()
