from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def dumps(data: dict[str, Any]) -> str:
    lines: list[str] = []

    def emit(mapping: dict[str, Any], indent: int) -> None:
        prefix = " " * indent
        for key, value in mapping.items():
            if isinstance(value, dict):
                lines.append(f"{prefix}{key}:")
                emit(value, indent + 2)
            else:
                lines.append(f"{prefix}{key}: {_scalar(value)}")

    emit(data, 0)
    return "\n".join(lines) + "\n"


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        if value.casefold() == "true":
            return True
        if value.casefold() == "false":
            return False
        if value.casefold() in {"null", "none"}:
            return None
        return value


def loads(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise ValueError(f"Некорректный отступ YAML в строке {number}")
        line = raw.strip()
        if ":" not in line:
            raise ValueError(f"Ожидалось key: value в строке {number}")
        key, value = line.split(":", 1)
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        key = key.strip()
        if value.strip():
            parent[key] = _parse_scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def load(path: Path) -> dict[str, Any]:
    return loads(path.read_text(encoding="utf-8"))


def dump(path: Path, data: dict[str, Any]) -> None:
    path.write_text(dumps(data), encoding="utf-8", newline="\n")

