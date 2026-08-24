#!/usr/bin/env python3
"""Build a compact searchable node index for the Modeler graph bundle."""

from __future__ import annotations

import argparse
import gzip
import io
import json
from pathlib import Path


FIELDS = (
    "path",
    "technical_path",
    "technical_name",
    "source_xml",
    "source_path",
    "search_text",
    "source",
    "target",
    "relationship",
    "source_ref",
    "source_xpath",
    "verification_status",
    "evidence",
)


def node_values(value):
    return value.values() if isinstance(value, dict) else (value or [])


def iter_records(graph_path: Path, graph_kind: str):
    graph = json.loads(graph_path.read_text(encoding="utf-8-sig"))
    header = {
        "configuration": str(graph.get("configuration") or ""),
        "release": str(graph.get("release") or ""),
        "graph_status": str(graph.get("status") or ""),
    }
    for node in node_values(graph.get("nodes")):
        if not isinstance(node, dict) or not node.get("id"):
            continue
        properties = node.get("properties") or {}
        selected = {
            field: str(properties.get(field) or "")[:1600]
            for field in FIELDS
            if properties.get(field)
        }
        yield {
            "graph": graph_kind,
            **header,
            "id": str(node.get("id")),
            "label": str(node.get("label") or ""),
            "type": str(node.get("type") or ""),
            "properties": selected,
        }
    if graph_kind != "semantic":
        return
    for number, edge in enumerate(graph.get("edges") or [], 1):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        relationship = str(edge.get("relationship") or "")
        if not source or not target or not relationship:
            continue
        properties = {
            field: str(edge.get(field) or "")[:1600]
            for field in FIELDS
            if edge.get(field)
        }
        properties.update(
            {"source": source, "target": target, "relationship": relationship}
        )
        yield {
            "graph": graph_kind,
            **header,
            "id": str(edge.get("id") or f"semantic-edge-{number:06d}"),
            "label": f"{source} —{relationship}→ {target}",
            "type": "SemanticRelation",
            "properties": properties,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "graphs" / "search-index.ndjson.gz"
    inputs = (
        ("source", root / "1c_erp_2_5_source_graph.json"),
        ("object", root / "graphs" / "1c_erp_2_5_object_graph.json"),
        ("route", root / "graphs" / "1c_erp_2_5_route_graph.json"),
        ("semantic", root / "graphs" / "1c_erp_2_5_semantic_graph.json"),
    )
    missing = [str(path) for _, path in inputs if not path.exists()]
    if missing:
        raise SystemExit("Missing graph files: " + ", ".join(missing))
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as stream:
                for graph_kind, graph_path in inputs:
                    for record in iter_records(graph_path, graph_kind):
                        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                        stream.write("\n")
                        count += 1
    print(f"Indexed {count} nodes into {output}")


if __name__ == "__main__":
    main()
