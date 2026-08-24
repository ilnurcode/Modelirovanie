#!/usr/bin/env python3
"""Validate portable skills, knowledge metadata and the lightweight graph."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from consultant_cli.infrastructure import yamlio
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NODE_TYPES = {"process", "catalog", "document", "register", "report", "setting", "role", "mechanism", "article", "subsystem", "form", "command", "attribute"}
EDGE_TYPES = {"uses", "requires", "creates", "reads", "writes", "performed_by", "documented_by", "alternative_to", "has_form", "has_command", "has_attribute", "has_tabular_section", "included_in", "grants_access", "implemented_by", "sourced_from"}


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError("unclosed YAML frontmatter")
    result = {}
    for line in parts[1].splitlines():
        if not line.strip() or line.startswith((" ", "-")):
            continue
        if ":" not in line:
            raise ValueError(f"invalid metadata line: {line}")
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"')
    return result


def list_value(raw: str) -> list[str]:
    raw = raw.strip()
    if raw == "[]":
        return []
    if raw.startswith("[") and raw.endswith("]"):
        return [x.strip().strip("'\"") for x in raw[1:-1].split(",") if x.strip()]
    return []


def main() -> int:
    errors = []
    ids = {}
    article_ids = set()
    process_meta = []

    def fail(path: Path, message: str) -> None:
        errors.append(f"{path.relative_to(ROOT)}: {message}")

    specs = [
        ("knowledge/articles/**/*.md", {"id", "title", "product", "version", "verification_status", "source_url", "accessed_at"}),
        ("processes/**/*.md", {"id", "title", "product", "version", "status", "knowledge_refs", "entity_refs"}),
    ]
    for pattern, required in specs:
        for path in ROOT.glob(pattern):
            try:
                meta = frontmatter(path)
            except ValueError as exc:
                fail(path, str(exc))
                continue
            missing = required - meta.keys()
            if missing:
                fail(path, f"missing metadata: {', '.join(sorted(missing))}")
            item_id = meta.get("id", "")
            if not ID_PATTERN.fullmatch(item_id):
                fail(path, "id must be lowercase kebab-case")
            elif item_id in ids:
                fail(path, f"duplicate id also used by {ids[item_id].relative_to(ROOT)}")
            else:
                ids[item_id] = path
            if pattern.startswith("knowledge"):
                article_ids.add(item_id)
                if meta.get("verification_status") not in {"verified", "inferred", "unresolved"}:
                    fail(path, "invalid verification_status")
                if meta.get("verification_status") == "verified" and not meta.get("source_url", "").startswith(("http://", "https://")):
                    fail(path, "verified article needs an HTTP(S) source_url")
            else:
                process_meta.append((path, meta))

    nodes = {}
    edges = []
    for filename in ("nodes.ndjson", "edges.ndjson"):
        path = ROOT / "graph" / filename
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                fail(path, f"line {number}: invalid JSON: {exc.msg}")
                continue
            if filename == "nodes.ndjson":
                required = {"id", "type", "name", "source_refs"}
                if not required <= item.keys():
                    fail(path, f"line {number}: missing {sorted(required - item.keys())}")
                elif item["id"] in nodes:
                    fail(path, f"line {number}: duplicate node id {item['id']}")
                else:
                    nodes[item["id"]] = item
                    if item.get("type") not in NODE_TYPES:
                        fail(path, f"line {number}: invalid node type {item.get('type')}")
            else:
                item["_line"] = number
                edges.append(item)

    for edge in edges:
        for key in {"from", "to", "type"}:
            if key not in edge:
                fail(ROOT / "graph/edges.ndjson", f"line {edge['_line']}: missing {key}")
        for endpoint in ("from", "to"):
            if edge.get(endpoint) not in nodes:
                fail(ROOT / "graph/edges.ndjson", f"line {edge['_line']}: missing {endpoint} node {edge.get(endpoint)}")
        if edge.get("type") not in EDGE_TYPES:
            fail(ROOT / "graph/edges.ndjson", f"line {edge['_line']}: invalid edge type {edge.get('type')}")

    config_manifest_path = ROOT / "metadata" / "index" / "configuration.json"
    config_manifest = {}
    if config_manifest_path.exists():
        try:
            config_manifest = json.loads(config_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(config_manifest_path, f"invalid JSON: {exc.msg}")
        source_root = config_manifest.get("source_root", "")
        config_root = Path(source_root) if source_root else ROOT / "local" / "configurations"
        if not config_root.is_absolute():
            config_root = ROOT / config_root
        if not config_root.exists():
            fail(config_manifest_path, f"configuration source_root is missing: {config_root}")
        else:
            try:
                config_root.resolve().relative_to(ROOT.resolve())
            except ValueError:
                fail(config_manifest_path, "configuration source_root must be inside repository workspace")
        if config_manifest.get("failed_files") != 0:
            fail(config_manifest_path, "metadata index contains failed files")
        if config_manifest.get("indexed_objects") != config_manifest.get("candidate_xml_files"):
            fail(config_manifest_path, "metadata index is incomplete")
        for node in nodes.values():
            if node.get("verification_status") == "verified_metadata":
                if not node.get("configuration_version") or not node.get("source_path"):
                    fail(ROOT / "graph/nodes.ndjson", f"metadata node {node.get('id')} lacks version or source_path")
                    continue
                if node.get("configuration_version") != config_manifest.get("version"):
                    fail(ROOT / "graph/nodes.ndjson", f"metadata node {node.get('id')} has a different configuration version")
                if config_root.exists() and not (config_root / node["source_path"]).exists():
                    fail(ROOT / "graph/nodes.ndjson", f"metadata source missing for {node.get('id')}: {node['source_path']}")

    for path, meta in process_meta:
        for ref in list_value(meta.get("knowledge_refs", "[]")):
            if ref not in article_ids:
                fail(path, f"missing knowledge_ref {ref}")
        for ref in list_value(meta.get("entity_refs", "[]")):
            if ref not in nodes:
                fail(path, f"missing entity_ref {ref}")

    for result_dir in (ROOT / "results").glob("*") if (ROOT / "results").exists() else []:
        if not result_dir.is_dir():
            continue
        requirement_path = result_dir / "01-requirements.md"
        design_path = result_dir / "02-design.md"
        instruction_path = result_dir / "03-instruction.md"
        project_path = result_dir / "project.yaml"
        project_data = yamlio.load(project_path) if project_path.exists() else {}
        mode = project_data.get("mode", "full")
        if project_path.exists() and mode != "full":
            fail(project_path, "project mode must be full")
        requirement_meta = frontmatter(requirement_path) if requirement_path.exists() else {}
        design_meta = frontmatter(design_path) if design_path.exists() else {}
        if requirement_meta and requirement_meta.get("approval_status") not in {"pending_approval", "approved", "rejected"}:
            fail(requirement_path, "invalid approval_status")
        if design_meta and design_meta.get("approval_status") not in {"pending_approval", "approved", "rejected"}:
            fail(design_path, "invalid approval_status")
        if design_path.exists() and requirement_meta.get("approval_status") != "approved":
            fail(design_path, "design exists before requirements approval")
        if instruction_path.exists():
            if requirement_meta.get("approval_status") != "approved":
                fail(instruction_path, "instruction exists before requirements approval")
            if design_meta.get("approval_status") != "approved":
                fail(instruction_path, "instruction exists before design approval")
            instruction_meta = frontmatter(instruction_path)
            if instruction_meta.get("status") not in {"draft", "review", "verified"}:
                fail(instruction_path, "invalid instruction status")
            validation_path = result_dir / "03-instruction-validation.md"
            if not validation_path.exists():
                fail(instruction_path, "instruction validation report is missing")
            if instruction_meta.get("status") == "verified" and "needs_review" in instruction_path.read_text(encoding="utf-8"):
                fail(instruction_path, "verified instruction contains needs_review")
            if instruction_meta.get("approval_status", "") not in {"pending_approval", "approved", "revoked", ""}:
                fail(instruction_path, "invalid instruction approval_status")
            if project_data.get("status") == "successful" and instruction_meta.get("approval_status") != "approved":
                fail(instruction_path, "successful project has no approved instruction")

    for path in (ROOT / "skills").glob("*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        try:
            meta = frontmatter(path)
        except ValueError as exc:
            fail(path, str(exc))
            continue
        if set(meta) != {"name", "description"}:
            fail(path, "skill frontmatter must contain only name and description")
        if meta.get("name") != path.parent.name:
            fail(path, "skill name must match directory")
        if "TODO" in text:
            fail(path, "contains TODO placeholder")
        if not (path.parent / "agents/openai.yaml").exists():
            fail(path, "missing agents/openai.yaml")

    for path in (ROOT / "schemas").glob("*.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(path, f"invalid JSON schema: {exc.msg}")

    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Validation passed: {len(article_ids)} articles, {len(process_meta)} processes, {len(nodes)} graph nodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
