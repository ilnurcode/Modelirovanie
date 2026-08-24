#!/usr/bin/env python3
"""Validate a generic role-filterable 1C instruction."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


STEP_HEADING = re.compile(r"^### (P\d{2,})\.", re.MULTILINE)
STEP_META = re.compile(
    r"<!--\s*step_id:\s*(P\d{2,});\s*roles:\s*\[([^]]*)];\s*evidence:\s*\[([^]]*)]\s*-->"
)
ACTION = re.compile(r"^\d+\.\s+", re.MULTILINE)
REQUIRED_FIELDS = (
    "**Роль:**",
    "**Предусловие:**",
    "**Результат:**",
    "**Проверка:**",
    "**Источник:**",
)


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    raw = text.split("---\n", 2)[1]
    result = {}
    for line in raw.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip('"')
    return result


def step_blocks(text: str) -> dict[str, str]:
    matches = list(STEP_HEADING.finditer(text))
    result = {}
    for index, match in enumerate(matches):
        next_step = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = re.search(r"^##\s+", text[match.end():next_step], re.MULTILINE)
        end = match.end() + section.start() if section else next_step
        result[match.group(1)] = text[match.start():end]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("instruction", type=Path)
    args = parser.parse_args()
    text = args.instruction.read_text(encoding="utf-8")
    meta = frontmatter(text)
    blocks = step_blocks(text)
    errors: list[str] = []
    warnings: list[str] = []

    if not blocks:
        errors.append("instruction contains no PNN steps")
    if len(blocks) != len(STEP_HEADING.findall(text)):
        errors.append("step identifiers must be unique")

    diagram_text = text
    design_path = args.instruction.parent / "02-design.md"
    if "```mermaid" not in diagram_text and design_path.exists():
        diagram_text = design_path.read_text(encoding="utf-8")
    diagram_match = re.search(r"```mermaid\s*(.*?)```", diagram_text, re.DOTALL)
    if not diagram_match:
        errors.append("Mermaid diagram is missing")
    diagram = diagram_match.group(1) if diagram_match else ""

    action_count = 0
    for step_id, block in blocks.items():
        action_count += len(ACTION.findall(block))
        metadata = STEP_META.search(block)
        if not metadata or metadata.group(1) != step_id:
            errors.append(f"{step_id}: missing or mismatched step metadata")
        for field in REQUIRED_FIELDS:
            if field not in block:
                errors.append(f"{step_id}: missing {field}")
        if (
            "**Путь:**" not in block
            and "**Маршрут:**" not in block
            and "→" not in block
        ):
            warnings.append(f"{step_id}: interface path is not explicit")
        if step_id not in diagram:
            warnings.append(f"{step_id}: identifier is absent from Mermaid diagram")

    broken_links = []
    for target in re.findall(r"\[[^]]+]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#")):
            continue
        local_target = target.split("#", 1)[0]
        if local_target and not (args.instruction.parent / local_target).resolve().exists():
            broken_links.append(target)
    if broken_links:
        errors.append(f"broken local links: {', '.join(sorted(set(broken_links)))}")

    unresolved = len(re.findall(r"`(?:unresolved|inferred|needs_review)`", text, re.IGNORECASE))
    if meta.get("status") == "verified" and unresolved:
        errors.append("verified instruction contains unresolved evidence")

    result = {
        "status": meta.get("status", ""),
        "steps": len(blocks),
        "numbered_user_actions": action_count,
        "unresolved_evidence": unresolved,
        "broken_local_links": sorted(set(broken_links)),
        "warnings": warnings,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
