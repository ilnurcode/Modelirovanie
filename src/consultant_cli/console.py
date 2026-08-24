from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from consultant_cli.app import Application
from consultant_cli.domain.models import ProjectStatus, project_state
from consultant_cli.infrastructure.settings import AgentProfile
from consultant_cli.ui import ConsoleUI


ui = ConsoleUI()


def ask(prompt: str, default: str = "") -> str:
    value = input(ui.prompt_label(prompt, default)).strip()
    return value or default


def ask_required(prompt: str) -> str:
    while True:
        value = ask(prompt).strip()
        if value:
            return value
        ui.error("Это поле обязательно. Введите значение.")


def yes_no(prompt: str, default: bool = False) -> bool:
    marker = "Д/н" if default else "д/Н"
    value = input(ui.prompt_label(f"{prompt} ({marker})")).strip().casefold()
    if not value:
        return default
    return value in {"д", "да", "y", "yes"}


def _project_progress(project) -> tuple[list[str], int]:
    labels = ["Вопросы", "Требования", "Схема", "Инструкция", "Готово"]
    current = {
        ProjectStatus.CONFIGURED: 0,
        ProjectStatus.REQUIREMENTS_PENDING: 0,
        ProjectStatus.REQUIREMENTS_APPROVED: 1,
        ProjectStatus.DESIGN_PENDING: 2,
        ProjectStatus.DESIGN_APPROVED: 3,
        ProjectStatus.GENERATING: 3,
        ProjectStatus.FEEDBACK_PENDING: 3,
        ProjectStatus.DRAFT: 3,
        ProjectStatus.NEEDS_REVISION: 3,
        ProjectStatus.SUCCESSFUL: 5,
    }.get(project.status, 0)
    return labels, current


def project_summary(project) -> None:
    mode = "Полный"
    configuration = " ".join(
        value for value in (project.configuration.product, project.configuration.release) if value
    )
    ui.panel(
        project.title or "Проект без названия",
        [
            f"ID: {project.project_id}",
            f"Режим: {mode}    Статус: {ui.status_text(project.status.value)}",
            f"Состояние результата: {ui.project_state_text(project_state(project.status))}",
            f"Конфигурация: {configuration or 'не указана'}",
            f"AI: {project.agent_profile or 'не подключён'}",
        ],
        "blue",
    )
    labels, current = _project_progress(project)
    ui.progress(labels, current)
    ui.write()


def run_menu(app: Application) -> int:
    while True:
        ui.clear()
        ui.header(
            "КОНСУЛЬТАНТ 1С",
            "Инструкции и схемы на основе проверяемой базы знаний",
            f"База: {app.paths.root}",
        )
        projects = app.store.list()
        ui.panel(
            "Состояние",
            [
                f"Проектов: {len(projects)}",
                f"AI: {app.settings.default_agent or 'не подключён'}",
                "ERP XML: индекс 2.5.27.49 подключён",
            ],
            "cyan",
        )
        ui.menu(
            [
                ("1", "Новый проект", "Мастер создания инструкции и схемы"),
                ("2", "Мои проекты", "Выбрать существующий проект"),
                ("3", "Подключить AI", "Настроить способ генерации"),
                ("4", "Система и справка", "База знаний, проверка и помощь"),
                ("0", "Выход", "Закрыть приложение"),
            ]
        )
        choice = ask("Выберите действие")
        if choice == "0":
            ui.clear()
            ui.success("До следующей консультации.")
            return 0
        if choice == "1":
            project = new_project_wizard(app)
            project_menu(app, project.project_id)
        elif choice == "2":
            project_id = choose_project(app)
            if project_id:
                project_menu(app, project_id)
        elif choice == "3":
            agent_menu(app)
        elif choice == "4":
            system_menu(app)


def new_project_wizard(app: Application):
    ui.clear()
    ui.header("Новый проект", "Шаг 1 из 2 — задача", "Главное меню › Новый проект")
    title = ask_required("Название проекта")
    prompt = ask("Что нужно сделать")
    ui.clear()
    ui.header("Новый проект", "Шаг 2 из 2 — конфигурация", "Главное меню › Новый проект")
    ui.panel(
        "Полный режим",
        [
            "Обязательные вопросы → апрув требований → проект и схема → апрув → инструкция.",
            "Режим оптимизирован: агент задаёт только вопросы, влияющие на решение.",
        ],
        "cyan",
    )
    ui.info("Редакцию и релиз можно указать позже в дополнительных настройках.")
    product = ask("Конфигурация 1С", "1С:ERP Управление предприятием 2")
    with ui.spinner("Создаю папку и настройки проекта"):
        project = app.workflow.create_project(
            title=title,
            prompt=prompt,
            mode="full",
            product=product,
            edition="",
            release="",
            agent_profile=app.settings.default_agent,
        )
    ui.success(f"Проект создан: results/{project.project_id}")
    if not app.settings.default_agent:
        ui.warning(
            "AI пока не подключён. Проект сохранён; перед генерацией выберите "
            "«Подключить AI» в главном меню."
        )
    ui.pause("Открыть проект")
    return project


def show_projects(app: Application) -> None:
    ui.clear()
    ui.header("Все проекты", breadcrumb="Главное меню › Проекты")
    projects = app.store.list()
    rows = [
        [
            project.project_id,
            project.title,
            "Полный",
            ui.project_state_text(project_state(project.status)),
            ui.status_text(project.status.value),
        ]
        for project in projects
    ]
    ui.table(["ID", "Название", "Режим", "Состояние", "Точный этап"], rows)
    ui.pause()


def choose_project(app: Application) -> str:
    ui.clear()
    ui.header("Выбор проекта", breadcrumb="Главное меню › Продолжить")
    projects = app.store.list()
    if not projects:
        ui.warning("Проектов пока нет. Создайте первый проект в главном меню.")
        ui.pause()
        return ""
    rows = [
        [
            index,
            project.title or "(без названия)",
            project.project_id,
            ui.project_state_text(project_state(project.status)),
            ui.status_text(project.status.value),
        ]
        for index, project in enumerate(projects, 1)
    ]
    ui.table(["№", "Название", "ID", "Состояние", "Точный этап"], rows)
    ui.info("Удаление: откройте проект, затем выберите «Ещё» → «Удалить проект».")
    ui.info("Введите 0, чтобы вернуться назад.")
    value = ask("Номер или ID проекта")
    if value == "0":
        return ""
    if value.isdigit() and 1 <= int(value) <= len(projects):
        return projects[int(value) - 1].project_id
    return value


def project_menu(app: Application, project_id: str) -> None:
    while True:
        project = app.store.load(project_id)
        ui.clear()
        title = project.title or "Проект без названия"
        ui.header("Проект", breadcrumb=f"Главное меню › {title}")
        project_summary(project)
        ui.menu(
            [
                ("1", primary_action_label(project), "Рекомендуемое следующее действие"),
                ("2", "Материалы", "Инструкция, схема и источники"),
                ("3", "Изменить", "Настройки или замечания"),
                ("4", "Экспортировать", "Сохранить результат в файл"),
                ("5", "Ещё", "Проверка и внешний агент"),
                ("0", "Назад", "Вернуться в главное меню"),
            ]
        )
        choice = ask("Выберите действие")
        try:
            if choice == "0":
                return
            if choice == "1":
                handle_primary_action(app, project_id)
            elif choice == "2":
                project_materials_menu(app, project_id)
            elif choice == "3":
                project_changes_menu(app, project_id)
            elif choice == "4":
                fmt = ask("Формат md/json/html", "html")
                with ui.spinner("Готовлю экспорт"):
                    path = app.exports.export(project_id, fmt)
                ui.success(f"Экспорт готов: {path}")
                ui.pause()
            elif choice == "5":
                if project_more_menu(app, project_id):
                    return
        except Exception as exc:
            ui.error(str(exc))
            ui.pause()


def primary_action_label(project) -> str:
    return {
        ProjectStatus.CONFIGURED: "Начать работу",
        ProjectStatus.REQUIREMENTS_PENDING: "Ответить на вопросы",
        ProjectStatus.REQUIREMENTS_APPROVED: "Создать проект и схему",
        ProjectStatus.DESIGN_PENDING: "Проверить и утвердить схему",
        ProjectStatus.DESIGN_APPROVED: "Создать инструкцию",
        ProjectStatus.FEEDBACK_PENDING: "Оценить инструкцию",
        ProjectStatus.DRAFT: "Продолжить доработку",
        ProjectStatus.SUCCESSFUL: "Открыть готовую инструкцию",
        ProjectStatus.NEEDS_REVISION: "Исправить инструкцию",
        ProjectStatus.ERROR: "Повторить попытку",
    }.get(project.status, "Продолжить")


def handle_primary_action(app: Application, project_id: str) -> None:
    project = app.store.load(project_id)
    if project.status is ProjectStatus.REQUIREMENTS_PENDING:
        answer_questions_wizard(app, project_id)
        return
    if project.status is ProjectStatus.DESIGN_PENDING:
        show_artifact(app, project_id, "02-design.md", "Проект и схема")
        approve_current_wizard(app, project_id)
        return
    if project.status in {ProjectStatus.FEEDBACK_PENDING, ProjectStatus.SUCCESSFUL}:
        show_instruction(app, project_id)
        return
    with ui.spinner("AI формирует следующий артефакт"):
        project, artifact = app.workflow.run(project_id)
    ui.success(f"Создано: {artifact.name}")
    if project.status is ProjectStatus.FEEDBACK_PENDING:
        feedback_prompt(app, project_id)
    else:
        ui.info("Проверьте новый материал. Следующее действие появится в пункте 1.")
        ui.pause()


def project_materials_menu(app: Application, project_id: str) -> None:
    while True:
        ui.clear()
        ui.header("Материалы проекта", breadcrumb=f"Проект › {project_id} › Материалы")
        ui.menu(
            [
                ("1", "Текущий результат", "Вопросы, схема или инструкция"),
                ("2", "Источники", "Доказательства и совместимость конфигурации"),
                ("0", "Назад", "Вернуться в проект"),
            ]
        )
        choice = ask("Выберите действие")
        if choice == "0":
            return
        if choice == "1":
            project = app.store.load(project_id)
            if project.instruction_version:
                show_instruction(app, project_id)
            elif project.design_version:
                show_artifact(app, project_id, "02-design.md", "Проект и схема")
                ui.pause()
            elif project.requirements_version:
                show_artifact(app, project_id, "01-requirements.md", "Вопросы и требования")
                ui.pause()
            else:
                ui.info("Материалы появятся после начала работы.")
                ui.pause()
        elif choice == "2":
            show_sources(app, project_id)
            ui.pause()


def project_changes_menu(app: Application, project_id: str) -> None:
    ui.clear()
    ui.header("Изменить проект", breadcrumb=f"Проект › {project_id} › Изменить")
    ui.menu(
        [
            ("1", "Настройки", "Конфигурация и дополнительные параметры"),
            (
                "2",
                "Сообщить замечания",
                "Доработать проект, схему или готовую инструкцию",
            ),
            ("0", "Назад", "Вернуться в проект"),
        ]
    )
    choice = ask("Выберите действие")
    if choice == "1":
        configure_project_wizard(app, project_id)
    elif choice == "2":
        reason = ask_required("Что нужно исправить")
        project = app.store.load(project_id)
        if project.status is ProjectStatus.DESIGN_PENDING:
            app.workflow.revise_design(project_id, reason)
            ui.success("Замечания сохранены. Проект и схема возвращены на доработку.")
        else:
            app.workflow.request_changes(project_id, reason)
            ui.success("Замечания сохранены. Инструкция возвращена на доработку.")
        ui.pause()


def project_more_menu(app: Application, project_id: str) -> bool:
    ui.clear()
    ui.header("Дополнительно", breadcrumb=f"Проект › {project_id} › Ещё")
    ui.menu(
        [
            ("1", "Проверить проект", "Проверка файлов и статусов"),
            ("2", "Открыть во внешнем агенте", "Codex, Claude Code или OpenCode"),
            ("3", "Удалить проект", "Убрать из списка и перенести в корзину"),
            ("0", "Назад", "Вернуться в проект"),
        ]
    )
    choice = ask("Выберите действие")
    if choice == "1":
        show_project_validation(app, project_id)
    elif choice == "2":
        agent = ask("Агент codex/claude/opencode", "codex")
        command = open_external_agent(app, project_id, agent, yes_no("Запустить сейчас", False))
        ui.info("Команда: " + subprocess.list2cmdline(command))
        ui.info("Внутреннее AI-подключение приложения здесь не требуется.")
        ui.pause()
    elif choice == "3":
        project = app.store.load(project_id)
        ui.warning("Проект исчезнет из списка, но его папка сохранится в results/.trash/.")
        confirmation = ask(f"Для удаления введите ID проекта: {project.project_id}")
        if confirmation != project.project_id:
            ui.info("Удаление отменено.")
            ui.pause()
            return False
        destination = app.workflow.delete_project(project_id)
        ui.success(f"Проект удалён из списка. Резервная копия: {destination}")
        ui.pause()
        return True
    return False


def show_artifact(
    app: Application, project_id: str, filename: str, title: str
) -> None:
    ui.clear()
    ui.header(title, breadcrumb=f"Проект › {project_id} › {title}")
    path = app.store.artifact_path(project_id, filename)
    if not path.exists():
        ui.warning("Материал ещё не сформирован.")
        return
    ui.panel("Файл", [str(path)], "green")
    if yes_no("Показать полный текст в терминале", False):
        ui.write()
        ui.write(path.read_text(encoding="utf-8"))


def feedback_prompt(app: Application, project_id: str) -> None:
    project = app.store.load(project_id)
    if project.status is not ProjectStatus.FEEDBACK_PENDING:
        return
    ui.write()
    ui.panel(
        "Проверка результата",
        [
            "1. Всё устраивает — инструкция станет успешным примером.",
            "2. Есть замечания — результат вернётся на доработку.",
            "3. Проверить позже — сохранить как черновик.",
        ],
        "yellow",
    )
    answer = ask("Ваше решение", "3")
    if answer == "1":
        by = ask("Кто подтверждает", "Консультант")
        app.workflow.approve(project_id, "instruction", by, "Инструкция устраивает")
        ui.success("Инструкция подтверждена и добавлена в успешные примеры.")
    elif answer == "2":
        app.workflow.request_changes(project_id, ask("Перечислите замечания"))
        ui.warning("Инструкция не считается успешной и отправлена на доработку.")
    else:
        app.workflow.save_draft(project_id)
        ui.info("Результат сохранён как черновик и не используется как пример.")
    ui.pause()


def configure_project_wizard(app: Application, project_id: str) -> None:
    project = app.store.load(project_id)
    ui.clear()
    ui.header("Настройки", breadcrumb=f"Проект › {project.title or project_id} › Настройки")
    ui.menu(
        [
            ("1", "Основные", "Конфигурация и детализация ответа"),
            ("2", "Дополнительные", "Редакция, релиз, схема и AI"),
            ("0", "Назад", "Вернуться в проект"),
        ]
    )
    choice = ask("Что изменить")
    if choice == "1":
        values = {
            "product": ask("Конфигурация", project.configuration.product),
            "detail_level": ask(
                "Детализация concise/balanced/detailed", project.generation.detail_level
            ),
        }
    elif choice == "2":
        ui.info("Редакция и релиз необязательны. Они нужны для точной проверки XML.")
        values = {
            "edition": ask("Редакция (необязательно)", project.configuration.edition),
            "release": ask("Релиз (необязательно)", project.configuration.release),
        }
        selected_agent = choose_ai_connection(app, project.agent_profile)
        if selected_agent is not None:
            values["agent_profile"] = selected_agent
    else:
        return
    app.workflow.configure(project_id, values)
    ui.success("Настройки сохранены.")
    ui.pause()


def choose_ai_connection(app: Application, current: str = "") -> str | None:
    profiles = list(app.settings.agents.values())
    ui.write()
    ui.panel(
        "Что такое AI-подключение?",
        [
            "Это сохранённый способ связи с AI: установленный Codex/Claude/OpenCode "
            "или API. Здесь не нужно вводить ключ, модель или произвольный текст.",
            "Подключения создаются один раз в главном меню «Подключить AI».",
        ],
        "cyan",
    )
    if not profiles:
        ui.warning("Сохранённых AI-подключений нет. Текущее значение не изменено.")
        return None
    ui.table(
        ["№", "Название", "Тип", "Состояние"],
        [
            [index, profile.name, profile.kind, "включено" if profile.enabled else "отключено"]
            for index, profile in enumerate(profiles, 1)
        ],
    )
    value = ask("Номер AI-подключения или 0 — не менять", "0")
    if value == "0" or not value:
        return None
    if value.isdigit() and 1 <= int(value) <= len(profiles):
        return profiles[int(value) - 1].name
    ui.warning("Выбрано неизвестное подключение. Текущее значение сохранено.")
    return None


def answer_questions_wizard(app: Application, project_id: str) -> None:
    ui.clear()
    ui.header("Обязательные вопросы", breadcrumb=f"Проект › {project_id} › Вопросы")
    questions = app.workflow.questions(project_id)
    answers = {}
    for index, question in enumerate(questions, 1):
        question_id = str(question.get("id"))
        required = "обязательно" if question.get("required", True) else "необязательно"
        lines = [f"{question.get('text')} ({required})"]
        if question.get("impact"):
            lines.append("Зачем: " + str(question["impact"]))
        if question.get("options"):
            lines.append("Варианты: " + "; ".join(question["options"]))
        ui.panel(f"Вопрос {index} из {len(questions)} · {question_id}", lines, "cyan")
        answers[question_id] = (
            ask_required("Ответ") if question.get("required", True) else ask("Ответ")
        )
    path = app.workflow.save_answers(project_id, answers)
    ui.success(f"Ответы сохранены: {path.name}")
    if yes_no("Утвердить требования и перейти к проектированию", False):
        approve_current_wizard(app, project_id)
    else:
        ui.pause()


def approve_current_wizard(app: Application, project_id: str) -> None:
    project = app.store.load(project_id)
    stage = {
        ProjectStatus.REQUIREMENTS_PENDING: ("requirements", "требования"),
        ProjectStatus.DESIGN_PENDING: ("design", "проект и схему"),
        ProjectStatus.FEEDBACK_PENDING: ("instruction", "итоговую инструкцию"),
    }.get(project.status)
    if not stage:
        ui.warning("Сейчас нет этапа, ожидающего апрув.")
        ui.pause()
        return
    stage_id, label = stage
    ui.warning(f"Вы подтверждаете {label}. Апрув будет записан в артефакт и журнал.")
    if not yes_no("Продолжить", False):
        return
    by = ask("Кто согласовывает", "Консультант")
    evidence = ask("Формулировка подтверждения", f"Утверждаю {label}")
    app.workflow.approve(project_id, stage_id, by, evidence)
    ui.success(f"Этап «{label}» утверждён.")
    ui.pause()


def agent_menu(app: Application) -> None:
    while True:
        ui.clear()
        ui.header("Подключение AI", breadcrumb="Главное меню › AI")
        ui.panel(
            "Что здесь настраивается",
            [
                "AI-подключение — это сохранённый способ вызвать AI из приложения.",
                "Основное подключение: " + (app.settings.default_agent or "не выбрано"),
                "При работе прямо в Codex/Claude/OpenCode подключение внутри приложения не нужно.",
            ],
            "cyan",
        )
        ui.menu(
            [
                ("1", "Подключить установленный агент", "Найти Codex, Claude Code или OpenCode"),
                ("2", "Подключить по API", "OpenAI или совместимый сервис"),
                ("3", "Мои подключения", "Посмотреть, выбрать или проверить"),
                ("0", "Назад", "Вернуться в главное меню"),
            ]
        )
        choice = ask("Выберите действие")
        if choice == "0":
            return
        try:
            if choice == "1":
                with ui.spinner("Проверяю доступные команды"):
                    diagnostics = app.agents.detect()
                ui.table(
                    ["Агент", "Готов", "Версия", "Комментарий"],
                    [
                        [item.name, "да" if item.available else "нет", item.version or "—", item.message]
                        for item in diagnostics
                    ],
                )
                available = [item for item in diagnostics if item.available]
                if available:
                    ui.table(
                        ["№", "Доступный агент"],
                        [[index, item.name] for index, item in enumerate(available, 1)],
                    )
                    selected = ask("Номер агента или 0 — отмена", "0")
                    if selected.isdigit() and 1 <= int(selected) <= len(available):
                        detected = available[int(selected) - 1]
                        name = ask("Название подключения", f"{detected.name}-local")
                        profile = AgentProfile(
                            name=name,
                            kind=detected.kind,
                            command=detected.executable,
                        )
                        app.agents.add_profile(
                            profile, make_default=yes_no("Использовать по умолчанию", True)
                        )
                        ui.success("AI-подключение добавлено.")
                else:
                    ui.warning("Готовых CLI-агентов не найдено. Можно подключиться по API.")
            elif choice == "2":
                ui.panel(
                    "Название подключения",
                    [
                        "Это понятное вам имя настройки, например «openai-work». "
                        "Потом приложение будет выбирать это подключение автоматически."
                    ],
                    "cyan",
                )
                name = ask_required("Название подключения")
                provider = ask("Сервис: 1 — OpenAI, 2 — совместимый API", "1")
                if provider == "1":
                    profile = AgentProfile(
                        name=name,
                        kind="openai_api",
                        endpoint="https://api.openai.com/v1",
                        model=ask_required("Название модели"),
                        secret_env=ask("Переменная окружения с ключом", "OPENAI_API_KEY"),
                        protocol="responses",
                    )
                else:
                    profile = AgentProfile(
                        name=name,
                        kind="openai_compatible",
                        endpoint=ask_required("Адрес API endpoint"),
                        model=ask_required("Название модели"),
                        secret_env=ask("Переменная окружения с ключом", "AI_API_KEY"),
                        protocol=ask("Протокол responses/chat_completions", "chat_completions"),
                    )
                app.agents.add_profile(
                    profile, make_default=yes_no("Использовать по умолчанию", True)
                )
                ui.success("AI-подключение сохранено. Сам ключ в файл не записан.")
            elif choice == "3":
                manage_ai_connections(app)
                continue
            ui.pause()
        except Exception as exc:
            ui.error(str(exc))
            ui.pause()


def manage_ai_connections(app: Application) -> None:
    ui.clear()
    ui.header("Мои AI-подключения", breadcrumb="Главное меню › AI › Подключения")
    profiles = list(app.settings.agents.values())
    if not profiles:
        ui.info("Подключений пока нет.")
        ui.pause()
        return
    ui.table(
        ["№", "Название", "Тип", "Состояние", "Основное"],
        [
            [
                index,
                profile.name,
                profile.kind,
                "включено" if profile.enabled else "отключено",
                "●" if profile.name == app.settings.default_agent else "",
            ]
            for index, profile in enumerate(profiles, 1)
        ],
    )
    selected = ask("Номер подключения или 0 — назад", "0")
    if not selected.isdigit() or not 1 <= int(selected) <= len(profiles):
        return
    profile = profiles[int(selected) - 1]
    ui.menu(
        [
            ("1", "Сделать основным", "Использовать в новых проектах"),
            ("2", "Проверить", "Сначала выполнить бесплатную локальную проверку"),
            ("3", "Включить или отключить", "Изменить доступность подключения"),
            ("0", "Назад", "Ничего не менять"),
        ]
    )
    action = ask("Действие")
    if action == "1":
        if not profile.enabled:
            app.agents.set_enabled(profile.name, True)
        app.agents.add_profile(profile, make_default=True)
        ui.success(f"Основное AI-подключение: {profile.name}")
    elif action == "2":
        remote = yes_no("Дополнительно выполнить модельный запрос с расходом лимита", False)
        with ui.spinner("Проверяю подключение"):
            result = app.agents.test(profile.name, remote)
        (ui.success if result.available else ui.error)(result.message)
    elif action == "3":
        app.agents.set_enabled(profile.name, not profile.enabled)
        ui.success("Подключение включено." if not profile.enabled else "Подключение отключено.")
    ui.pause()


def show_sources(app: Application, project_id: str) -> None:
    ui.clear()
    ui.header("Источники", breadcrumb=f"Проект › {project_id} › Источники")
    directory = app.store.project_dir(project_id)
    route_path = directory / "source-route.json"
    evidence_path = directory / "evidence.ndjson"
    modeler_path = directory / "03-modeler-review.json"
    if route_path.exists():
        route = json.loads(route_path.read_text(encoding="utf-8"))
        ui.panel(
            "Маршрут",
            [
                f"Совместимость: {route.get('compatibility', 'not_checked')}",
                f"Использовать XML: {'да' if route.get('use_xml') else 'нет'}",
                f"Требуется веб-поиск: {'да' if route.get('web_search_required') else 'нет'}",
            ] + [f"Предупреждение: {item}" for item in route.get("warnings", [])],
            "yellow" if route.get("warnings") else "green",
        )
    else:
        ui.info("Маршрут ещё не сформирован.")
    records = []
    if evidence_path.exists():
        records = [
            json.loads(line)
            for line in evidence_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    ui.table(
        ["Источник", "Статус", "Ссылка"],
        [
            [
                item.get("title") or item.get("id"),
                item.get("verification_status", "unresolved"),
                item.get("url") or item.get("local_ref") or "—",
            ]
            for item in records
        ],
    )
    if modeler_path.exists():
        modeler = json.loads(modeler_path.read_text(encoding="utf-8"))
        summary = modeler.get("summary", {})
        ui.panel(
            "Независимая проверка 1C Modeler",
            [
                f"Результат: {modeler.get('verdict', 'not_checked')}",
                f"Совместимость: {modeler.get('compatibility', 'not_checked')}",
                "Пути: "
                f"verified {summary.get('verified', 0)}, "
                f"inferred {summary.get('inferred', 0)}, "
                f"unresolved {summary.get('unresolved', 0)}",
                f"Отчёт: {modeler_path.with_suffix('.md')}",
            ],
            "yellow" if modeler.get("verdict") != "passed" else "green",
        )


def show_instruction(app: Application, project_id: str) -> None:
    ui.clear()
    ui.header("Инструкция", breadcrumb=f"Проект › {project_id} › Результат")
    path = app.store.artifact_path(project_id, "03-instruction.md")
    if not path.exists():
        ui.warning("Инструкция ещё не сформирована.")
        ui.pause()
        return
    ui.panel(
        "Файл результата",
        [str(path), "Можно открыть в редакторе или просмотреть текст прямо здесь."],
        "green",
    )
    if yes_no("Показать полный текст в терминале", False):
        ui.write()
        ui.write(path.read_text(encoding="utf-8"))
    was_pending = app.store.load(project_id).status is ProjectStatus.FEEDBACK_PENDING
    feedback_prompt(app, project_id)
    if not was_pending:
        ui.pause()


def show_project_validation(app: Application, project_id: str) -> None:
    ui.clear()
    ui.header("Проверка проекта", breadcrumb=f"Проект › {project_id} › Проверка")
    result = app.validation.project(project_id)
    (ui.success if result["valid"] else ui.error)(
        "Проект прошёл проверку." if result["valid"] else "Проект содержит ошибки."
    )
    for error in result["errors"]:
        ui.error(error)
    for warning in result["warnings"]:
        ui.warning(warning)
    ui.pause()


def system_menu(app: Application) -> None:
    while True:
        ui.clear()
        ui.header("Система и справка", breadcrumb="Главное меню › Система")
        ui.menu(
            [
                ("1", "База знаний", "Статьи, процессы и XML конфигурации"),
                ("2", "Проверить систему", "Целостность файлов и правил"),
                ("3", "Как работать", "Короткая памятка"),
                ("0", "Назад", "Вернуться в главное меню"),
            ]
        )
        choice = ask("Выберите действие")
        if choice == "0":
            return
        if choice == "1":
            show_knowledge(app)
            ui.pause()
        elif choice == "2":
            ui.clear()
            ui.header("Проверка системы", breadcrumb="Главное меню › Система › Проверка")
            with ui.spinner("Проверяю структуру, ссылки и статусы"):
                result = app.validation.repository()
            (ui.success if result["valid"] else ui.error)(result["output"])
            ui.pause()
        elif choice == "3":
            show_help(app)


def show_knowledge(app: Application) -> None:
    ui.clear()
    ui.header("База знаний", breadcrumb="Главное меню › База знаний")
    manifest = app.paths.root / "metadata" / "index" / "configuration.json"
    articles = len(list((app.paths.root / "knowledge" / "articles").glob("**/*.md")))
    processes = len(list((app.paths.root / "processes").glob("**/*.md")))
    lines = [f"Корень: {app.paths.root}", f"Статей: {articles}", f"Процессов: {processes}"]
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        lines.extend(
            [
                f"XML: {data.get('synonym', 'неизвестно')}",
                f"Релиз: {data.get('version', 'неизвестно')}",
                f"Объектов метаданных: {data.get('indexed_objects', 0):,}".replace(",", " "),
            ]
        )
    ui.panel("Подключённые данные", lines, "cyan")


def show_help(app: Application) -> None:
    ui.clear()
    ui.header("Справка", breadcrumb="Главное меню › Справка")
    ui.panel(
        "Как начать",
        [
            "1. Подключите AI в разделе «Подключение AI».",
            "2. Создайте проект и точно укажите конфигурацию/релиз 1С.",
            "3. Пройдите этапы выбранного режима.",
            "4. Подтвердите инструкцию только после проверки.",
            "5. При замечании верните результат на доработку.",
        ],
        "green",
    )
    ui.info(f"Подробное руководство: {app.paths.root / 'docs' / 'consultant-cli.md'}")
    ui.pause()


def open_external_agent(
    app: Application, project_id: str, agent: str, launch: bool = False
) -> list[str]:
    project_dir = app.store.project_dir(project_id)
    command_name = {"codex": "codex", "claude": "claude", "opencode": "opencode"}.get(agent)
    if not command_name:
        raise ValueError(f"Неизвестный агент: {agent}")
    executable = shutil.which(command_name)
    command = [executable or command_name]
    if agent == "codex":
        command.extend(["-C", str(project_dir)])
    elif agent == "opencode":
        command.append(str(project_dir))
    if launch:
        if not executable:
            raise FileNotFoundError(f"{command_name} не найден в PATH")
        subprocess.run(command, cwd=app.paths.root, check=False)
    return command
