# Отчёт проверки NewAgent 4.0

Дата: 2026-08-19, timezone Asia/Tomsk.

## Автоматические проверки

- `py -3 -B -m unittest discover -s tests -v`: 41/41 passed.
- `npm.cmd run test:mcp`: 3/3 passed, включая реальный STDIO handshake, approvals, ambiguous-answer guard и release mismatch.
- `npm.cmd run mcp:doctor`: ready; graph schema v3, 97 353 nodes, 563 612 edges, 13 131 L3↔L4 links.
- `npm.cmd run mcp:smoke`: passed; hybrid search возвращает L3/L4 узлы для производства, отгрузки и оплаты.
- `py -3 -B scripts\validate_repository.py`: passed; 5 articles, 2 processes, 32 graph nodes.
- `py -3 -B scripts\validate_analysis.py`: passed; 5 проектов.
- Repo skills: 3/3 прошли `skill-creator/quick_validate.py`.
- Все 16 обязательных аналитических инвариантов находятся в `tests/test_unified_analytics.py` и прошли.

## Хлебозавод

Актуальный прогон: `results/bakery-regression-4`.

- 120 атомарных требований, 8 кластеров, 2 бизнес-вопроса.
- 62 дедуплицированных evidence candidates.
- Вместо 120 механических шагов создано 8 операционных групп при сохранении traceability всех 120 требований.
- Повторный поиск: 8 cache hits, 0 misses.
- Неоднозначный ответ «подтверждаю настройки» по вариантам оплаты не записан как Decision.
- 0 модельных вызовов, 0 input/cached/output/reasoning tokens, 0 model time; wall time regression 406 ms при тёплом кэше.
- Все structural/invariant checks в regression report имеют значение `true`.
- Offline Python Modeler вернул `needs_revision`: 23 critical GAP, потому что его portable graph содержит кандидаты. Это корректно блокирует `successful`; основной Codex runtime отдельно использует опубликованный граф Яны и точный route graph Ильнура.

## Целостность источников

- Кирилл: все 142 записи исходного `FILES.sha256` совпали.
- Яна: выбранные исходные MCP/config/skill-файлы не имеют diff; существовавшие до интеграции прочие modified/untracked файлы не изменялись этой работой.

## Оставшиеся ограничения

1. Проектная `.codex/config.toml` начинает действовать после открытия trusted-папки NewAgent и перезапуска Codex; текущий чат в другом workspace не может проверить UI-индикатор этого нового подключения.
2. Sidecar и исходные L4/XML сейчас читаются из соседнего RAGAgent. Для автономного переноса на другой компьютер нужно скопировать данные или изменить две переменные в `.codex/config.toml`.
3. Перенесённые внутренние MCP/skills Яны остаются private до отдельного решения по внешней лицензии.
4. Token usage CLI-провайдеров сохраняется нулём, если сам CLI не возвращает usage; MCP journal хранит фактическую модель, параметры, длительность и артефакты.
5. Offline Python regression консервативнее основного MCP и не служит готовым ответом по хлебозаводу; он проверяет инварианты и запрет выдуманных значений.
