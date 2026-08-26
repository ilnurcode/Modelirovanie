from __future__ import annotations

import argparse
import os
import json
import sys
from pathlib import Path
from typing import Any

from consultant_cli import __version__
from consultant_cli.app import build_application
from consultant_cli.console import open_external_agent, run_menu
from consultant_cli.errors import ConsultantError, InvalidConfigurationError
from consultant_cli.infrastructure.settings import AgentProfile
from consultant_cli.domain.models import project_state


def configure_console() -> None:
    """Use UTF-8 consistently in Windows terminals and redirected output."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except (AttributeError, OSError):
            pass
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


def read_markdown_input(path: Path) -> tuple[str, str, bytes]:
    """Read an external Markdown TZ without copying its absolute path into a project."""
    source = path.expanduser().resolve()
    if source.suffix.casefold() not in {".md", ".markdown"}:
        raise InvalidConfigurationError("Файл ТЗ должен иметь расширение .md или .markdown.")
    if not source.is_file():
        raise InvalidConfigurationError(f"Файл ТЗ не найден: {source}")
    if source.stat().st_size > 5 * 1024 * 1024:
        raise InvalidConfigurationError("Файл ТЗ больше 5 МБ.")
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidConfigurationError("Файл ТЗ должен быть сохранён в UTF-8.") from exc
    if not text.strip():
        raise InvalidConfigurationError("Файл ТЗ пуст.")
    return text, source.name, raw


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="consultant", description="Консультант 1С")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--repo", type=Path, help="Путь к корню базы знаний")
    root.add_argument("--json", action="store_true", dest="json_output")
    commands = root.add_subparsers(dest="command")

    commands.add_parser("menu", help="Открыть интерактивное меню")

    new = commands.add_parser("new", help="Создать проект")
    new.add_argument("title")
    new_source = new.add_mutually_exclusive_group(required=True)
    new_source.add_argument("--prompt", help="Текст ТЗ")
    new_source.add_argument("--file", type=Path, help="Путь к ТЗ в UTF-8 Markdown")
    new.add_argument("--product", default="not_configured")
    new.add_argument("--edition", default="")
    new.add_argument("--release", default="")
    new.add_argument("--agent", default="")
    new.add_argument("--project-id")
    new.add_argument("--detail", choices=["concise", "balanced", "detailed"], default="balanced")
    new.add_argument(
        "--deliverable",
        choices=["hybrid", "process", "consultant", "vanessa"],
        default="hybrid",
        help="Формат результата; по умолчанию единый процесс и инструкция консультанта",
    )

    commands.add_parser("list", help="Список проектов")
    delete = commands.add_parser("delete", help="Удалить проект из списка в корзину")
    delete.add_argument("project_id")
    delete.add_argument(
        "--confirm",
        required=True,
        help="Для подтверждения повторите project-id",
    )
    open_parser = commands.add_parser("open", help="Показать папку проекта")
    open_parser.add_argument("project_id")
    status = commands.add_parser("status", help="Статус проекта")
    status.add_argument("project_id")
    run = commands.add_parser("run", help="Продолжить текущий этап")
    run.add_argument("project_id")
    recover = commands.add_parser(
        "recover", help="Повторно обработать последний сохранённый ответ без вызова API"
    )
    recover.add_argument("project_id")
    ask = commands.add_parser("ask", help="Задать вопрос сохранённому проекту и записать Markdown")
    ask.add_argument("project_id")
    ask.add_argument("question")
    ask.add_argument(
        "--kind",
        choices=["process", "consultant", "vanessa", "implementation"],
        default="process",
    )
    preflight = commands.add_parser(
        "preflight", help="Выполнить предварительные Python-проверки без LLM"
    )
    preflight.add_argument("project_id")
    preflight.add_argument("--focus", default="")

    configure = commands.add_parser("configure", help="Изменить параметры проекта")
    configure.add_argument("project_id")
    configure.add_argument("--agent")
    configure.add_argument("--product")
    configure.add_argument("--edition")
    configure.add_argument("--release")
    configure.add_argument("--detail", choices=["concise", "balanced", "detailed"])
    configure.add_argument(
        "--deliverable", choices=["hybrid", "process", "consultant", "vanessa"]
    )
    configure.add_argument(
        "--internet-policy",
        choices=["forbidden", "official_only", "official_and_allowed_web"],
    )

    questions = commands.add_parser("questions", help="Показать вопросы")
    questions.add_argument("project_id")
    answer = commands.add_parser("answer", help="Сохранить ответы")
    answer.add_argument("project_id")
    answer.add_argument("--set", action="append", default=[], metavar="ID=ANSWER")
    answer.add_argument("--file", type=Path, help="JSON-файл вида {ID: answer}")

    analyze = commands.add_parser("analyze", help="Пакетно найти доказательства и обновить аналитическую модель")
    analyze.add_argument("project_id")
    analysis_status = commands.add_parser("analysis-status", help="Показать унифицированную аналитическую модель")
    analysis_status.add_argument("project_id")
    decision = commands.add_parser("decision", help="Записать точный ответ на бизнес-вопрос")
    decision.add_argument("project_id")
    decision.add_argument("question_id")
    decision.add_argument("--answer", required=True)
    modeler = commands.add_parser("modeler", help="Запустить независимую проверку аналитической модели")
    modeler.add_argument("project_id")
    telemetry = commands.add_parser("telemetry", help="Показать агрегированную телеметрию проекта")
    telemetry.add_argument("project_id")
    commands.add_parser("runtime-status", help="Проверить Python/API runtime без вывода секрета")
    commands.add_parser("graph-status", help="Проверить опубликованный четырёхслойный граф")
    graph_search = commands.add_parser("graph-search", help="Выполнить Python hybrid search по ERP-графу")
    graph_search.add_argument("query")
    graph_search.add_argument("--limit", type=int, default=12)
    graph_search.add_argument("--layer", type=int, action="append", default=[])
    migrate = commands.add_parser("migrate-analysis", help="Импортировать аналитические артефакты как данные")
    migrate.add_argument("project_id")
    migrate.add_argument("--requirements", type=Path)
    migrate.add_argument("--evidence", type=Path)
    migrate.add_argument("--bootstrap-kirill", action="store_true")

    approve = commands.add_parser("approve", help="Явно согласовать этап")
    approve.add_argument("project_id")
    approve.add_argument("stage", choices=["requirements", "design", "instruction"])
    approve.add_argument("--by", required=True)
    approve.add_argument("--evidence", required=True)

    changes = commands.add_parser("request-changes", help="Отправить инструкцию на доработку")
    changes.add_argument("project_id")
    changes.add_argument("--reason", required=True)
    changes.add_argument("--by", default="Консультант")
    design_changes = commands.add_parser(
        "revise-design", help="Вернуть неутверждённый проект и схему на доработку"
    )
    design_changes.add_argument("project_id")
    design_changes.add_argument("--reason", required=True)
    design_changes.add_argument("--by", default="Консультант")
    revise = commands.add_parser("revise", help="Создать цикл доработки инструкции")
    revise.add_argument("project_id")
    revise.add_argument("--reason", required=True)
    revise.add_argument("--by", default="Консультант")
    revoke = commands.add_parser("revoke-approval", help="Отозвать успешность инструкции")
    revoke.add_argument("project_id")
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--by", default="Консультант")
    draft = commands.add_parser("save-draft", help="Сохранить результат без подтверждения")
    draft.add_argument("project_id")

    sources = commands.add_parser("sources", help="Показать источники проекта")
    sources.add_argument("project_id")
    validate = commands.add_parser("validate", help="Проверить проект или репозиторий")
    validate.add_argument("project_id", nargs="?")
    export = commands.add_parser("export", help="Экспортировать результат")
    export.add_argument("project_id")
    export.add_argument("--format", choices=["md", "json", "html"], default="html")
    promote = commands.add_parser("promote-example", help="Пересобрать запись успешного примера")
    promote.add_argument("project_id")
    external = commands.add_parser("open-agent", help="Открыть проект во внешнем агенте")
    external.add_argument("project_id")
    external.add_argument("--agent", choices=["codex", "claude", "opencode"], required=True)
    external.add_argument("--launch", action="store_true")

    agent = commands.add_parser("agent", help="Управление AI-подключениями")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("list")
    agent_commands.add_parser("detect")
    add = agent_commands.add_parser("add")
    add.add_argument("name")
    add.add_argument(
        "--kind",
        required=True,
        choices=[
            "codex_cli",
            "claude_cli",
            "opencode_cli",
            "custom_cli",
            "openai_api",
            "openai_compatible",
        ],
    )
    add.add_argument("--command", dest="executable_command", default="")
    add.add_argument("--arg", action="append", default=[])
    add.add_argument("--endpoint", default="")
    add.add_argument("--model", default="")
    add.add_argument("--secret-env", default="")
    add.add_argument("--protocol", default="")
    add.add_argument("--reasoning-effort", default="")
    add.add_argument("--default", action="store_true")
    test = agent_commands.add_parser("test")
    test.add_argument("name")
    test.add_argument("--remote", action="store_true")
    enable = agent_commands.add_parser("enable")
    enable.add_argument("name")
    disable = agent_commands.add_parser("disable")
    disable.add_argument("name")
    return root


def emit(value: Any, as_json: bool = False) -> None:
    if as_json:
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(value)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parser().parse_args(argv)
    try:
        app = build_application(args.repo)
        if not args.command or args.command == "menu":
            return run_menu(app)
        result = dispatch(app, args)
        if result is not None:
            emit(result, args.json_output)
        return 0
    except ConsultantError as exc:
        emit({"error": str(exc), "exit_code": exc.exit_code}, getattr(args, "json_output", False))
        return exc.exit_code
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        emit({"error": str(exc), "exit_code": 2}, getattr(args, "json_output", False))
        return 2
    except KeyboardInterrupt:
        print("\nОперация прервана пользователем.", file=sys.stderr)
        return 130


def dispatch(app, args) -> Any:
    command = args.command
    if command == "new":
        prompt, source_name, source_bytes = (
            read_markdown_input(args.file)
            if args.file is not None
            else (args.prompt, "", None)
        )
        project = app.workflow.create_project(
            title=args.title,
            prompt=prompt,
            mode="full",
            product=args.product,
            edition=args.edition,
            release=args.release,
            agent_profile=args.agent,
            detail_level=args.detail,
            deliverable=args.deliverable,
            project_id=args.project_id,
            source_name=source_name,
            source_bytes=source_bytes,
        )
        return project.to_dict()
    if command == "list":
        records = []
        for project in app.store.list():
            record = project.to_dict()
            record["project_state"] = project_state(project.status)
            records.append(record)
        return records
    if command == "delete":
        if args.confirm != args.project_id:
            raise ValueError("Значение --confirm должно точно совпадать с project-id")
        destination = app.workflow.delete_project(args.project_id)
        return {
            "project_id": args.project_id,
            "deleted_from_list": True,
            "recoverable_path": str(destination),
        }
    if command == "open":
        return {"project_id": args.project_id, "path": str(app.store.project_dir(args.project_id))}
    if command == "status":
        project = app.store.load(args.project_id)
        return {
            "project": project.to_dict(),
            "project_state": project_state(project.status),
            "validation": app.validation.project(args.project_id),
        }
    if command == "run":
        project, artifact = app.workflow.run(args.project_id)
        return {
            "project_id": project.project_id,
            "status": project.status.value,
            "artifact": str(artifact),
            "next_action": "Ответьте, всё ли вас устраивает"
            if project.status.value == "feedback_pending"
            else "Выполните явный апрув текущего этапа",
        }
    if command == "recover":
        project, artifact = app.workflow.recover_latest_generation(args.project_id)
        next_action = {
            "requirements_pending": "Ответьте на вопросы и выполните явный апрув requirements",
            "design_pending": "Проверьте проект решения и выполните явный апрув design",
            "feedback_pending": "Проверьте инструкцию и ответьте, всё ли вас устраивает",
        }.get(project.status.value, "Продолжите lifecycle из текущего статуса проекта")
        return {
            "project_id": project.project_id,
            "status": project.status.value,
            "artifact": str(artifact),
            "recovered_without_api": True,
            "next_action": next_action,
        }
    if command == "ask":
        return app.workflow.ask_project(args.project_id, args.question, args.kind)
    if command == "preflight":
        return app.workflow.preflight(args.project_id, args.focus)
    if command == "configure":
        values = {}
        mapping = {
            "agent": "agent_profile",
            "product": "product",
            "edition": "edition",
            "release": "release",
            "detail": "detail_level",
            "deliverable": "deliverable",
            "internet_policy": "internet_policy",
        }
        for source, target in mapping.items():
            value = getattr(args, source)
            if value is not None:
                values[target] = value
        return app.workflow.configure(args.project_id, values).to_dict()
    if command == "questions":
        return app.workflow.questions(args.project_id)
    if command == "answer":
        answers = {}
        if args.file:
            answers.update(json.loads(args.file.read_text(encoding="utf-8")))
        for value in args.set:
            if "=" not in value:
                raise ValueError("Формат ответа: --set ID=ANSWER")
            key, answer = value.split("=", 1)
            answers[key] = answer
        if not answers:
            raise ValueError("Передайте --set или --file")
        return {"path": str(app.workflow.save_answers(args.project_id, answers))}
    if command == "analyze":
        return app.analytics.analyze_evidence(args.project_id)
    if command == "analysis-status":
        return app.analytics.ensure(args.project_id).to_dict()
    if command == "decision":
        return app.workflow.record_decision(args.project_id, args.question_id, args.answer)
    if command == "modeler":
        return app.analytics.run_modeler(args.project_id)
    if command == "telemetry":
        return app.telemetry.aggregate(args.project_id)
    if command == "runtime-status":
        return {
            "api": app.agents.api_runtime_status(),
            "graph": app.workflow.graph.status(),
            "request_path": "python",
            "interfaces": ["pi", "opencode", "codex"],
        }
    if command == "graph-status":
        return app.workflow.graph.status()
    if command == "graph-search":
        return app.workflow.graph.search(
            args.query, layers=args.layer, limit=max(1, min(args.limit, 50))
        )
    if command == "migrate-analysis":
        if args.bootstrap_kirill:
            return app.migrations.bootstrap_kirill_project(args.project_id)
        if not args.requirements:
            raise ValueError("Передайте --requirements или --bootstrap-kirill")
        return app.migrations.import_yana_artifacts(
            args.project_id, args.requirements, args.evidence
        )
    if command == "approve":
        return app.workflow.approve(
            args.project_id, args.stage, args.by, args.evidence
        ).to_dict()
    if command == "revise-design":
        return app.workflow.revise_design(args.project_id, args.reason, args.by).to_dict()
    if command in {"request-changes", "revoke-approval", "revise"}:
        return app.workflow.request_changes(args.project_id, args.reason, args.by).to_dict()
    if command == "save-draft":
        return app.workflow.save_draft(args.project_id).to_dict()
    if command == "sources":
        directory = app.store.project_dir(args.project_id)
        result = {}
        for name in ("source-route.json", "evidence.ndjson"):
            path = directory / name
            result[name] = path.read_text(encoding="utf-8") if path.exists() else ""
        return result
    if command == "validate":
        return app.validation.project(args.project_id) if args.project_id else app.validation.repository()
    if command == "export":
        return {"path": str(app.exports.export(args.project_id, args.format))}
    if command == "promote-example":
        return app.examples.promote(args.project_id)
    if command == "open-agent":
        return {
            "command": open_external_agent(app, args.project_id, args.agent, args.launch),
            "internal_api_required": False,
        }
    if command == "agent":
        return dispatch_agent(app, args)
    raise ValueError(f"Неизвестная команда: {command}")


def dispatch_agent(app, args) -> Any:
    if args.agent_command == "list":
        return [
            {
                "name": profile.name,
                "kind": profile.kind,
                "enabled": profile.enabled,
                "command": profile.command,
                "endpoint": profile.endpoint,
                "model": profile.model,
                "secret_env": profile.secret_env,
            }
            for profile in app.settings.agents.values()
        ]
    if args.agent_command == "detect":
        return [item.to_dict() for item in app.agents.detect()]
    if args.agent_command == "add":
        profile = AgentProfile(
            name=args.name,
            kind=args.kind,
            command=args.executable_command,
            args=args.arg,
            endpoint=args.endpoint,
            model=args.model,
            secret_env=args.secret_env,
            protocol=args.protocol,
            reasoning_effort=args.reasoning_effort,
        )
        app.agents.add_profile(profile, make_default=args.default)
        return {"added": args.name, "default": app.settings.default_agent}
    if args.agent_command == "test":
        return app.agents.test(args.name, args.remote).to_dict()
    if args.agent_command in {"enable", "disable"}:
        enabled = args.agent_command == "enable"
        app.agents.set_enabled(args.name, enabled)
        return {"name": args.name, "enabled": enabled}
    raise ValueError("Неизвестная команда agent")


if __name__ == "__main__":
    raise SystemExit(main())
