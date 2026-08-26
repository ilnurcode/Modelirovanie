# Происхождение компонентов

## Кирилл

Источник: `../ГОТОВО_К_ОТПРАВКЕ_1C-Consultant`, исходная лицензия MIT.
Сохранён integrity manifest `provenance/kirill-FILES.sha256`. Канонической основой
остались Python application/store/workflow, ProjectStatus, approvals, revisions,
answers_md, validation и независимый Modeler. Бинарный `consultant.exe` не переносился.

## Яна

Источник: `../RAGAgent`, внутренний/private проект пользователя. В NewAgent 4.1
адаптированы алгоритм четырёхслойного поиска FTS5+sparse semantic+rerank, typed
expansion, source_ref, document-flow contract, безопасное обнаружение существующего
Wormsoft-ключа, Pi/Herdr UX и role policy. Опубликованный 487-МиБ SQLite sidecar,
ERP XML и ИТС не копируются: Python читает их read-only из RAGAgent.

Legacy Node MCP сохранён только для истории/сравнительных тестов и не входит в
request path. Готовые ответы исходных проектов не используются как эталон результата.

## Новые модули NewAgent 4.1

- `services/graph_search.py`: прямой Python reader опубликованного графа;
- role routing в `services/agents.py` и `WorkflowService`;
- `preflight`, `ask`, runtime/graph diagnostics;
- `document_flow`, deliverables process/consultant/vanessa;
- `.pi/extensions/newagent-workspace.ts` и provider adapter;
- проверка L3 document IDs/точных названий в верхней цепочке.
