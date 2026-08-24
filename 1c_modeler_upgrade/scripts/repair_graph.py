#!/usr/bin/env python3
"""Remove dangling edges without inventing graph facts."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Repair dangling graph references")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--configuration", default="1С:ERP 2.5")
    parser.add_argument("--release", default="[релиз не подтвержден]")
    args = parser.parse_args(); graph = json.loads(args.input.read_text(encoding="utf-8-sig"))
    values = list(graph.get("nodes", {}).values()) if isinstance(graph.get("nodes"), dict) else graph.get("nodes", [])
    ids = {node.get("id") for node in values if isinstance(node, dict) and node.get("id")}
    edges = [edge for edge in graph.get("edges", []) if isinstance(edge, dict) and edge.get("source") in ids and edge.get("target") in ids and edge.get("relationship")]
    args.output.write_text(json.dumps({"configuration": args.configuration, "release": args.release, "nodes": graph.get("nodes", {}), "edges": edges}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__": main()
