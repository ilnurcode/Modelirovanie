# Правила работы с NewAgent 4.1

## Основной runtime

Codex, OpenCode и Pi — только интерфейсы. Канонический request path:

`интерфейс → consultant.cmd → Python workflow → Python hybrid graph search → Wormsoft API roles`.

MCP не требуется и не должен запускаться. Смысловые артефакты создают только роли,
заданные в `agent-runtime-policy.json`:

- `erp-translator` → `wormsoft/agent/low`;
- `erp-process-planner` → `wormsoft/agent/medium`;
- `instruction-writer` → `wormsoft/agent/high`.

Используй существующий `NEWAGENT_API_KEY`/`WORMSOFT_API_KEY`; Python безопасно ищет
его в окружении и разрешённых локальных `.env`, не печатает и не копирует секрет.
Не запускай вложенный Codex/OpenCode ради предметной генерации.

## Граф и доказательства

Используй `consultant.cmd --repo . --json graph-status`, `graph-search` и `preflight`.
Python читает опубликованный четырёхслойный SQLite-граф Яны read-only: L1 сценарии,
L2 уточнения, L3 метаданные/UI, L4 ИТС; поиск — FTS5+sparse semantic+rerank с typed
expansion и `source_ref`. Ранг не является доказательством.

Если `graph-status` возвращает `ready: false`, четырёхслойный sidecar не подключён.
Не утверждай L3/L4-подтверждение: используй portable Modeler только для candidates/GAP
и потребуй подходящий graph package или `ERP_GRAPH_DATABASE`.

- `verified_source` — L4/первичный источник точного продукта и релиза;
- `verified_metadata` — L3/XML точного релиза;
- `user_decision` — точный ответ пользователя;
- `candidate`, `inferred`, `unresolved` запрещены в подтверждённых шагах.

## Полный lifecycle

1. Создай проект через `new` с форматом `process`, `consultant` или `vanessa`.
2. Выполни бесплатный `preflight`, затем `run`: translator/low формирует вопросы.
3. Сохрани ответы и требуй явный `approve requirements`.
4. `run`: planner/medium формирует solution model, ветви, GAP, traceability и tests.
5. Требуй явный `approve design`.
6. `run`: writer/high формирует ответ, JSON-contract и Modeler проверяются локально.
7. Покажи Markdown из `answers_md` и спроси: «Всё ли устраивает?».
8. Только явный `approve instruction` переводит проект в `successful`.

Изменение решения, контекста или замечание создаёт ревизию, отзывает затронутые
approvals и исключает старую инструкцию из успешных примеров. Не редактируй историю
проекта вручную. Все проектные JSON сохраняй в `results/<project-id>/agent_artifacts/`,
ответы — в `results/<project-id>/answers_md/`.

## Формат ответа

Design и instruction начинаются с `Общая последовательность документов`: основной
сквозной маршрут, затем параллельные, альтернативные и возвратные ветви. Для
`consultant` нужны предварительные настройки и verified UI-действия; для `vanessa` —
дополнительно Gherkin без выдуманных селекторов. Диаграммы — Mermaid.

После изменения базы запускай `$env:PYTHONPATH='src'; py -3 -m unittest discover -s tests -v`
и `py -3 scripts/validate_repository.py`.
