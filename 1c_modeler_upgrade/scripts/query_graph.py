#!/usr/bin/env python3
"""Search graph labels, ids and indexed source text using only stdlib."""
import argparse
import json
from pathlib import Path


def as_list(nodes):
    return list(nodes.values()) if isinstance(nodes, dict) else (nodes or [])


def main():
    parser = argparse.ArgumentParser(description="Query a 1C modeler knowledge graph")
    parser.add_argument("--graph", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--depth", type=int, default=1, choices=range(0, 6))
    parser.add_argument("--limit", type=int, default=25, choices=range(1, 101))
    parser.add_argument("--type", action="append", dest="types")
    args = parser.parse_args()
    graph = json.loads(args.graph.read_text(encoding="utf-8-sig"))
    needle = args.query.casefold(); nodes = as_list(graph.get("nodes"))
    by_id = {node.get("id"): node for node in nodes if isinstance(node, dict) and node.get("id")}
    matches = []
    for node in nodes:
        if not isinstance(node, dict) or (args.types and node.get("type") not in args.types):
            continue
        properties = node.get("properties", {})
        node_id = str(node.get("id", "")); label = str(node.get("label", "")); technical_name = str(properties.get("technical_name", ""))
        text = " ".join((node_id, label, technical_name, str(properties.get("search_text", "")))).casefold()
        if needle not in text:
            continue
        node_id_folded = node_id.casefold()
        score = 100
        if node_id_folded == needle: score = 1200
        elif node_id_folded.endswith("." + needle): score = 1200 if node_id.count(".") == 1 else 700
        elif technical_name.casefold() == needle: score = 1100 if node_id.count(".") == 1 else 600
        elif label.casefold() == needle: score = 800
        matches.append((score, node_id, node))
    matches.sort(key=lambda item: (-item[0], item[1]))
    all_matches = [item[2] for item in matches]
    matched = all_matches[:args.limit]
    visited, frontier, related = {node["id"] for node in matched}, {node["id"] for node in matched}, []
    for _ in range(args.depth):
        next_frontier = set()
        for edge in graph.get("edges", []):
            if edge.get("source") in frontier or edge.get("target") in frontier:
                if edge not in related: related.append(edge)
                for node_id in (edge.get("source"), edge.get("target")):
                    if node_id not in visited: visited.add(node_id); next_frontier.add(node_id)
        frontier = next_frontier
        if not frontier: break
    print(json.dumps({"query": args.query, "total_matched": len(all_matches), "truncated": len(all_matches) > len(matched), "configuration": graph.get("configuration"), "release": graph.get("release"), "matched_nodes": matched, "related_nodes": [by_id[node_id] for node_id in visited if node_id in by_id], "related_edges": related}, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
