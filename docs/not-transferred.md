# Возможности, сознательно не включённые в основной runtime

- Node MCP и MCP-host orchestration: legacy-файлы не включены в portable-репозиторий
  и основной runtime не запускает MCP.
- Копия тяжёлого graph sidecar, первичного XML и ИТС: используются read-only из
  RAGAgent, чтобы не дублировать сотни мегабайт.
- NetworkX/NumPy/SciPy/scikit-learn в request path: опубликованный SQLite уже содержит
  индексы, стандартной библиотеки Python достаточно.
- Автоматические LLM retry, writer/verifier loops и неограниченные деревья субагентов.
- Автоматическое повышение candidate/inferred до verified или автоматический successful.
- Секреты, чаты и готовые ответы исходных проектов.
- `consultant.exe`: source CLI остаётся воспроизводимым.

Для несовпадающего релиза L3/XML остаётся candidate; Modeler и final approval честно
блокируют неподтверждённые UI-шаги.
