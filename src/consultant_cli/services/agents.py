from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from consultant_cli.errors import AgentError, GenerationValidationError, NotFoundError
from consultant_cli.infrastructure.settings import AgentProfile, AppSettings, save_settings
from consultant_cli.infrastructure.store import RepositoryPaths


KNOWN_AGENTS = {
    "codex": ("codex_cli", "codex"),
    "claude": ("claude_cli", "claude"),
    "opencode": ("opencode_cli", "opencode"),
}


def add_common_agent_paths() -> None:
    """Make user-installed Windows CLI agents visible to the application."""
    if os.name != "nt":
        return
    appdata = os.getenv("APPDATA")
    if not appdata:
        return
    npm_bin = str(Path(appdata) / "npm")
    if not Path(npm_bin).is_dir():
        return
    parts = [item for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    parts = [
        item
        for item in parts
        if os.path.normcase(item.rstrip("\\/")) != os.path.normcase(npm_bin)
    ]
    os.environ["PATH"] = os.pathsep.join([npm_bin, *parts])


@dataclass(slots=True)
class AgentDiagnostic:
    name: str
    kind: str
    available: bool
    executable: str = ""
    version: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available,
            "executable": self.executable,
            "version": self.version,
            "message": self.message,
        }


class AgentService:
    def __init__(
        self, paths: RepositoryPaths, settings: AppSettings, settings_path: Path
    ):
        add_common_agent_paths()
        self.paths = paths
        self.settings = settings
        self.settings_path = settings_path
        self.last_usage: dict[str, int] = {}
        self.last_profile: AgentProfile | None = None
        self.runtime_policy = self._runtime_policy()

    def _runtime_policy(self) -> dict[str, Any]:
        path = self.paths.root / "agent-runtime-policy.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentError(f"Некорректный agent-runtime-policy.json: {exc}") from exc

    @staticmethod
    def _dotenv_value(path: Path, names: list[str]) -> tuple[str, str] | None:
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            name = name.strip()
            if name not in names:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                return name, value
        return None

    def _role_secret(self) -> tuple[str, str]:
        names = [str(value) for value in self.runtime_policy.get("api_key_precedence", [])]
        if not names:
            names = ["NEWAGENT_API_KEY", "WORMSOFT_API_KEY"]
        for name in names:
            value = os.getenv(name, "").strip()
            if value:
                return name, value
        files = [self.paths.root / ".env"]
        for value in self.runtime_policy.get("api_key_files", ["../RAGAgent/.env"]):
            candidate = Path(str(value))
            files.append(candidate if candidate.is_absolute() else self.paths.root / candidate)
        for path in files:
            found = self._dotenv_value(path.resolve(), names)
            if found:
                return found
        raise AgentError(
            "API-ключ ролей не найден. Задайте NEWAGENT_API_KEY/WORMSOFT_API_KEY "
            "или сохраните ключ в локальном .env одного из разрешённых источников."
        )

    def role_profile(self, role: str) -> AgentProfile:
        allowed = {str(value) for value in self.runtime_policy.get("allowed_agents", [])}
        if role not in allowed:
            raise AgentError(f"Роль запрещена runtime policy: {role}")
        model = str(self.runtime_policy.get("models_by_agent", {}).get(role, ""))
        if not model:
            raise AgentError(f"Для роли {role} не задана модель")
        reasoning = str(
            self.runtime_policy.get("reasoning_effort_by_role", {}).get(role, "")
        )
        return AgentProfile(
            name=role,
            kind="openai_compatible",
            endpoint=str(
                self.runtime_policy.get("wormsoft_base_url", "https://ai.wormsoft.ru/api/gpt")
            ),
            model=model,
            secret_env="WORMSOFT_API_KEY",
            protocol="chat_completions",
            reasoning_effort=reasoning,
            timeout_seconds=int(self.runtime_policy.get("timeout_sec", 900)),
            max_output_tokens=int(self.runtime_policy.get("max_output_tokens", 30000)),
        )

    def generate_role(
        self,
        role: str,
        prompt: str,
        schema: dict[str, Any],
        allow_web_search: bool = False,
    ) -> dict[str, Any]:
        profile = self.role_profile(role)
        _secret_name, secret = self._role_secret()
        self.last_profile = profile
        return self.generate(
            profile,
            prompt,
            schema,
            allow_web_search=allow_web_search,
            secret_override=secret,
        )

    def api_runtime_status(self) -> dict[str, Any]:
        key_configured = True
        try:
            secret_name, _secret = self._role_secret()
        except AgentError:
            key_configured = False
            secret_name = ""
        return {
            "provider": self.runtime_policy.get("provider", "wormsoft-gateway"),
            "key_configured": key_configured,
            "key_source": secret_name,
            "models_by_agent": self.runtime_policy.get("models_by_agent", {}),
            "automatic_retries": int(self.runtime_policy.get("automatic_retries", 0)),
        }

    def detect(self) -> list[AgentDiagnostic]:
        diagnostics = []
        for name, (kind, command) in KNOWN_AGENTS.items():
            executable = shutil.which(command) or ""
            available = False
            version = ""
            message = "Не установлен или отсутствует в PATH"
            if executable:
                try:
                    probe = subprocess.run(
                        [executable, "--version"],
                        cwd=self.paths.root,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                        check=False,
                    )
                    available = probe.returncode == 0
                    version = (probe.stdout or probe.stderr).strip().splitlines()[0]
                    message = "Готов к подключению" if available else "Команда найдена, но проверка завершилась ошибкой"
                except (OSError, subprocess.TimeoutExpired) as exc:
                    message = f"Команда найдена, но недоступна для запуска: {exc}"
            diagnostics.append(
                AgentDiagnostic(
                    name=name,
                    kind=kind,
                    available=available,
                    executable=executable,
                    version=version,
                    message=message,
                )
            )
        return diagnostics

    def add_profile(self, profile: AgentProfile, make_default: bool = False) -> None:
        if profile.kind == "openai_api" and not profile.endpoint:
            profile.endpoint = "https://api.openai.com/v1"
        self.settings.agents[profile.name] = profile
        if make_default or not self.settings.default_agent:
            self.settings.default_agent = profile.name
        save_settings(self.settings_path, self.settings)

    def set_enabled(self, name: str, enabled: bool) -> None:
        profile = self.get_profile(name)
        profile.enabled = enabled
        save_settings(self.settings_path, self.settings)

    def get_profile(self, name: str | None = None) -> AgentProfile:
        selected = name or self.settings.default_agent
        if not selected or selected not in self.settings.agents:
            raise NotFoundError(
                "AI не подключён. В главном меню выберите «Подключить AI» или "
                "для командной строки выполните agent add."
            )
        profile = self.settings.agents[selected]
        if not profile.enabled:
            raise AgentError(f"AI-профиль отключён: {selected}")
        return profile

    def test(self, name: str, remote: bool = False) -> AgentDiagnostic:
        profile = self.get_profile(name)
        healthcheck_schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        if profile.kind.endswith("_cli"):
            executable = shutil.which(profile.command or self._default_command(profile.kind))
            if not executable:
                return AgentDiagnostic(
                    name, profile.kind, False, message="Исполняемый файл не найден"
                )
            version = ""
            try:
                result = subprocess.run(
                    [executable, "--version"],
                    cwd=self.paths.root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                version = (result.stdout or result.stderr).strip().splitlines()[0]
            except (OSError, subprocess.TimeoutExpired) as exc:
                return AgentDiagnostic(name, profile.kind, False, executable, message=str(exc))
            diagnostic = AgentDiagnostic(
                name, profile.kind, True, executable, version, "Локальная проверка пройдена"
            )
            if remote:
                self.generate(
                    profile,
                    "Ответьте JSON: {\"ok\": true}",
                    healthcheck_schema,
                )
                diagnostic.message = "Локальная и модельная проверки пройдены"
            return diagnostic

        if profile.kind in {"openai_api", "openai_compatible"}:
            if not profile.endpoint or not profile.model or not profile.secret_env:
                return AgentDiagnostic(
                    name, profile.kind, False, message="Не заполнены endpoint/model/secret_env"
                )
            if not os.getenv(profile.secret_env):
                return AgentDiagnostic(
                    name,
                    profile.kind,
                    False,
                    message=f"Не задана переменная окружения {profile.secret_env}",
                )
            diagnostic = AgentDiagnostic(
                name, profile.kind, True, message="Настройки и секрет найдены"
            )
            if remote:
                self.generate(
                    profile,
                    "Ответьте JSON: {\"ok\": true}",
                    healthcheck_schema,
                )
                diagnostic.message = "API-проверка пройдена"
            return diagnostic
        return AgentDiagnostic(name, profile.kind, False, message="Неизвестный тип профиля")

    def generate(
        self,
        profile: AgentProfile,
        prompt: str,
        schema: dict[str, Any],
        allow_web_search: bool = False,
        secret_override: str | None = None,
    ) -> dict[str, Any]:
        self.last_usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }
        if profile.kind == "codex_cli":
            return self._codex(profile, prompt, schema)
        if profile.kind == "claude_cli":
            return self._claude(profile, prompt, schema)
        if profile.kind == "opencode_cli":
            return self._opencode(profile, prompt, schema)
        if profile.kind == "custom_cli":
            return self._custom_cli(profile, prompt)
        if profile.kind in {"openai_api", "openai_compatible"}:
            return self._api(profile, prompt, schema, allow_web_search, secret_override)
        raise AgentError(f"Неизвестный тип AI-профиля: {profile.kind}")

    @staticmethod
    def _default_command(kind: str) -> str:
        return {
            "codex_cli": "codex",
            "claude_cli": "claude",
            "opencode_cli": "opencode",
        }.get(kind, "")

    def _run(self, command: list[str], prompt: str, timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                input=prompt,
                cwd=self.paths.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentError(f"Не удалось запустить AI CLI: {exc}") from exc
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            if len(message) > 4000:
                message = "[вывод AI CLI сокращён]\n" + message[-4000:]
            raise AgentError(f"AI CLI завершился с кодом {result.returncode}: {message}")
        return result

    def _codex(
        self, profile: AgentProfile, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        executable = shutil.which(profile.command or "codex")
        if not executable:
            raise AgentError("Codex CLI не найден")
        with tempfile.TemporaryDirectory(prefix="consultant-codex-") as temp:
            temp_path = Path(temp)
            schema_path = temp_path / "schema.json"
            result_path = temp_path / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command = [
                executable,
                "exec",
                "--cd",
                str(temp_path),
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "-",
            ]
            command[2:2] = profile.args
            result = self._run(command, prompt, profile.timeout_seconds)
            text = result_path.read_text(encoding="utf-8") if result_path.exists() else result.stdout
        return extract_json(text)

    def _claude(
        self, profile: AgentProfile, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        executable = shutil.which(profile.command or "claude")
        if not executable:
            raise AgentError("Claude Code CLI не найден")
        command = [
            executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            *profile.args,
        ]
        result = self._run(command, "", profile.timeout_seconds)
        wrapper = extract_json(result.stdout)
        if isinstance(wrapper.get("structured_output"), dict):
            return wrapper["structured_output"]
        if isinstance(wrapper.get("result"), str):
            return extract_json(wrapper["result"])
        return wrapper

    def _opencode(
        self, profile: AgentProfile, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        del schema
        executable = shutil.which(profile.command or "opencode")
        if not executable:
            raise AgentError("OpenCode CLI не найден")
        command = [executable, "run", *profile.args, prompt]
        result = self._run(command, "", profile.timeout_seconds)
        return extract_json(result.stdout)

    def _custom_cli(self, profile: AgentProfile, prompt: str) -> dict[str, Any]:
        executable = shutil.which(profile.command)
        if not executable:
            raise AgentError(f"Команда не найдена: {profile.command}")
        args = [item.replace("{prompt}", prompt) for item in profile.args]
        if not any("{prompt}" in item for item in profile.args):
            args.append(prompt)
        result = self._run([executable, *args], "", profile.timeout_seconds)
        return extract_json(result.stdout)

    def _api(
        self,
        profile: AgentProfile,
        prompt: str,
        schema: dict[str, Any],
        allow_web_search: bool,
        secret_override: str | None = None,
    ) -> dict[str, Any]:
        secret = secret_override or os.getenv(profile.secret_env)
        if not secret:
            raise AgentError(f"Не задана переменная окружения {profile.secret_env}")
        endpoint = profile.endpoint.rstrip("/")
        protocol = profile.protocol or (
            "responses" if profile.kind == "openai_api" else "chat_completions"
        )
        if protocol == "responses":
            url = endpoint if endpoint.endswith("/responses") else endpoint + "/responses"
            payload = {
                "model": profile.model,
                "input": prompt,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "consultant_result",
                        "schema": schema,
                        "strict": False,
                    }
                },
            }
            if allow_web_search:
                payload["tools"] = [{"type": "web_search"}]
        else:
            url = endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"
            schema_prompt = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            payload = {
                "model": profile.model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            prompt
                            + "\n\nОБЯЗАТЕЛЬНАЯ JSON SCHEMA ОТВЕТА:\n"
                            + schema_prompt
                        ),
                    }
                ],
                "temperature": 0,
                "max_tokens": profile.max_output_tokens,
                "response_format": {"type": "json_object"},
            }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=profile.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AgentError(f"Ошибка AI API: {exc}") from exc
        usage = data.get("usage", {}) or {}
        input_details = usage.get("input_tokens_details", {}) or usage.get("prompt_tokens_details", {}) or {}
        output_details = usage.get("output_tokens_details", {}) or usage.get("completion_tokens_details", {}) or {}
        self.last_usage = {
            "input_tokens": int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
            "cached_input_tokens": int(input_details.get("cached_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
            "reasoning_tokens": int(output_details.get("reasoning_tokens", 0) or 0),
        }
        if protocol == "responses":
            text = data.get("output_text", "")
            if not text:
                fragments = []
                for item in data.get("output", []):
                    for content in item.get("content", []):
                        if content.get("type") in {"output_text", "text"}:
                            fragments.append(content.get("text", ""))
                text = "".join(fragments)
        else:
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise AgentError("AI API вернул ответ неизвестного формата") from exc
        return extract_json(text)


def extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    def parse_with_single_missing_comma(value: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            if "Expecting ',' delimiter" not in exc.msg:
                raise
            repaired = value[: exc.pos] + "," + value[exc.pos :]
            return json.loads(repaired)

    try:
        value = parse_with_single_missing_comma(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise GenerationValidationError("AI не вернул JSON-объект")
        try:
            value = parse_with_single_missing_comma(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise GenerationValidationError(f"Некорректный JSON от AI: {exc}") from exc
    if not isinstance(value, dict):
        raise GenerationValidationError("Ожидался JSON-объект верхнего уровня")
    return value
