# Совместимость интерфейсов

Канонический backend один: `consultant.cmd → Python workflow → Wormsoft API roles`.
Интерфейс не выбирает предметную модель и не пишет артефакты самостоятельно.

| Интерфейс | Адаптер | Расход предметной генерации |
|---|---|---|
| Pi/Herdr | `.pi/extensions/newagent-workspace.ts`, `/erp` | Wormsoft API key |
| Codex | `AGENTS.md`, `.codex/config.toml` | Wormsoft API key через Python |
| OpenCode | `AGENTS.md` и CLI | Wormsoft API key через Python |
| Терминал | `consultant.cmd` | Wormsoft API key |

Внешние CLI-профили Codex/OpenCode сохранены только как диагностическая совместимость;
основной workflow их не вызывает. Секреты не записываются в project.yaml, telemetry
или Markdown. Все интерфейсы работают с одним каталогом `results/`, поэтому
approvals, revisions, Modeler и ответы не расходятся.
