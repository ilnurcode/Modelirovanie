from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from consultant_cli.infrastructure.store import atomic_write_text


@dataclass(slots=True)
class AgentProfile:
    name: str
    kind: str
    enabled: bool = True
    command: str = ""
    args: list[str] = field(default_factory=list)
    endpoint: str = ""
    model: str = ""
    secret_env: str = ""
    protocol: str = ""
    reasoning_effort: str = ""
    timeout_seconds: int = 600
    max_output_tokens: int = 30000

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "AgentProfile":
        return cls(
            name=name,
            kind=str(data.get("kind", "custom_cli")),
            enabled=bool(data.get("enabled", True)),
            command=str(data.get("command", "")),
            args=[str(item) for item in data.get("args", [])],
            endpoint=str(data.get("endpoint", "")),
            model=str(data.get("model", "")),
            secret_env=str(data.get("secret_env", "")),
            protocol=str(data.get("protocol", "")),
            reasoning_effort=str(data.get("reasoning_effort", "")),
            timeout_seconds=int(data.get("timeout_seconds", 600)),
            max_output_tokens=int(data.get("max_output_tokens", 30000)),
        )


@dataclass(slots=True)
class AppSettings:
    default_agent: str = ""
    results_dir: str = "results"
    default_output: list[str] = field(
        default_factory=lambda: ["markdown", "json", "html"]
    )
    mask_sensitive_data: bool = True
    allow_external_ai: bool = True
    agents: dict[str, AgentProfile] = field(default_factory=dict)


def load_settings(path: Path) -> AppSettings:
    if not path.exists():
        return AppSettings()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    privacy = data.get("privacy", {})
    profiles = {
        name: AgentProfile.from_dict(name, values)
        for name, values in data.get("agents", {}).items()
    }
    return AppSettings(
        default_agent=str(data.get("default_agent", "")),
        results_dir=str(data.get("results_dir", "results")),
        default_output=[str(item) for item in data.get("default_output", [])],
        mask_sensitive_data=bool(privacy.get("mask_sensitive_data", True)),
        allow_external_ai=bool(privacy.get("allow_external_ai", True)),
        agents=profiles,
    )


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def save_settings(path: Path, settings: AppSettings) -> None:
    lines = [
        f"default_agent = {_toml_scalar(settings.default_agent)}",
        f"results_dir = {_toml_scalar(settings.results_dir)}",
        f"default_output = {_toml_scalar(settings.default_output)}",
        "",
        "[privacy]",
        f"mask_sensitive_data = {_toml_scalar(settings.mask_sensitive_data)}",
        f"allow_external_ai = {_toml_scalar(settings.allow_external_ai)}",
    ]
    for name in sorted(settings.agents):
        profile = settings.agents[name]
        lines.extend(["", f"[agents.{json.dumps(name, ensure_ascii=False)}]"])
        values = asdict(profile)
        values.pop("name", None)
        for key, value in values.items():
            if value is None or value == "":
                continue
            if value == []:
                continue
            lines.append(f"{key} = {_toml_scalar(value)}")
    atomic_write_text(path, "\n".join(lines) + "\n")
