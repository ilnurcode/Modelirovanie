#!/usr/bin/env python3
"""Validate the portable 1C knowledge-graph contract using only stdlib."""
import argparse
import json
import sys
from pathlib import Path


def nodes_as_list(nodes):
    return list(nodes.values()) if isinstance(nodes, dict) else (nodes if isinstance(nodes, list) else [])


def main():
    parser = argparse.ArgumentParser(description="Validate a 1C modeler knowledge graph")
    parser.add_argument("--graph", required=True, type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.graph.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    errors, node_ids = [], set()
    for node in nodes_as_list(data.get("nodes")):
        if not isinstance(node, dict): errors.append("Узел имеет некорректный формат."); continue
        node_id = node.get("id")
        if not node_id: errors.append("Узел без id."); continue
        if node_id in node_ids: errors.append(f"Дублирующийся id узла: {node_id}")
        node_ids.add(node_id)
        for field in ("label", "type"):
            if not node.get(field): errors.append(f"Узел {node_id} без {field}.")
    edges = data.get("edges") if isinstance(data.get("edges"), list) else []
    if not isinstance(data.get("edges"), list): errors.append("Отсутствует или некорректен edges.")
    unresolved = [edge for edge in edges if not isinstance(edge, dict) or edge.get("source") not in node_ids or edge.get("target") not in node_ids]
    errors.extend("Связь без relationship." for edge in edges if isinstance(edge, dict) and not edge.get("relationship"))
    result = {"nodes": len(node_ids), "edges": len(edges), "unresolved_edges": len(unresolved), "valid": not errors and not unresolved, "errors": errors, "unresolved_samples": unresolved[:10]}
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0 if result["valid"] else 1


if __name__ == "__main__": sys.exit(main())
