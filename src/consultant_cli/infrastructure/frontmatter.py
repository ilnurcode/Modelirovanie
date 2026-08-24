from __future__ import annotations

from typing import Any

from consultant_cli.infrastructure import yamlio


def parse(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    metadata = yamlio.loads(text[4:end])
    return metadata, text[end + 5 :]


def render(metadata: dict[str, Any], body: str) -> str:
    return f"---\n{yamlio.dumps(metadata)}---\n\n{body.lstrip()}"


def update(text: str, **values: Any) -> str:
    metadata, body = parse(text)
    metadata.update(values)
    return render(metadata, body)
