#!/usr/bin/env python3
"""Extract exact user-interface routes from interface Markdown."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def cells(line):
    marker = "__PIPE__"
    return [item.strip().replace(marker, "|") for item in line.replace("\\|", marker).strip().strip("|").split("|")]


def main():
    parser = argparse.ArgumentParser(description="Build exact interface route graph")
    parser.add_argument("--interface", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(); root = args.interface.resolve(); nodes, edges, skipped = {}, [], []
    nodes["ROOT_ROUTES"] = {"id": "ROOT_ROUTES", "label": "Пользовательские маршруты 1С:ERP", "type": "Root", "properties": {}}
    for directory, _, names in os.walk(root / "01_Пользовательский_интерфейс", onerror=lambda error: skipped.append(str(error))):
        for name in names:
            if not name.lower().endswith(".md"): continue
            file = Path(directory) / name
            try: lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as error: skipped.append(str(error)); continue
            for line in lines:
                if not line.startswith("|") or line.startswith("|---"): continue
                row = cells(line)
                if len(row) != 3 or row[0] in {"№", ""} or not row[2] or row[2] == "Путь": continue
                route_id = "Route." + hashlib.sha1(row[2].encode("utf-8")).hexdigest()[:16]
                nodes.setdefault(route_id, {"id": route_id, "label": row[1], "type": "InterfaceRoute", "properties": {"path": row[2], "source_path": str(file.resolve())}})
                edges.append({"source": "ROOT_ROUTES", "target": route_id, "relationship": "contains", "source_ref": str(file.resolve())})
    args.output.write_text(json.dumps({"configuration": "1С:ERP 2.5", "status": "ГОТОВ", "nodes": nodes, "edges": edges, "build_report": {"skipped_files": skipped}}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"nodes": len(nodes), "edges": len(edges), "skipped_files": len(skipped)}, ensure_ascii=False))


if __name__ == "__main__": main()
