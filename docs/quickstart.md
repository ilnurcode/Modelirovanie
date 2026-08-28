# Быстрый запуск

## Проверка без LLM

```powershell
cd C:\Users\Y.Karpova\Desktop\NewAgent
consultant.cmd --repo . --json runtime-status
consultant.cmd --repo . --json graph-status
consultant.cmd --repo . --json graph-search "заказ клиента производство отгрузка"
```

Ожидается Python request path, три модели low/medium/high и опубликованный
четырёхслойный граф. Команды не выводят API-ключ.

## Новый проект

```powershell
consultant.cmd --repo . new demo --prompt "Описание процесса и требуемой инструкции" --product "1С:ERP Управление предприятием 2" --release "2.5"
consultant.cmd --repo . preflight demo
consultant.cmd --repo . run demo
consultant.cmd --repo . questions demo
consultant.cmd --repo . answer demo --set Q-...="ответ"
consultant.cmd --repo . approve demo requirements --by "Заказчик" --evidence "Требования утверждаю"
consultant.cmd --repo . run demo
consultant.cmd --repo . approve demo design --by "Заказчик" --evidence "Проект решения утверждаю"
consultant.cmd --repo . run demo
consultant.cmd --repo . approve demo instruction --by "Заказчик" --evidence "Всё устраивает"
```

Вместо вставки текста можно загрузить исходное ТЗ из UTF-8 Markdown. Исходник будет
сохранён в проекте как `00-source.md`:

```powershell
consultant.cmd --repo . new demo --file "C:\Users\User\Desktop\tz.md" --product "1С:ERP Управление предприятием 2" --release "2.5.27.49"
```

После design approval следующий `run` вызывает writer/high и сохраняет draft в
`results/demo/answers_md`. Только `approve demo instruction` после ответа «Всё
устраивает» переводит проект в successful.

Новый проект по умолчанию имеет формат `hybrid`: итог включает и полный сквозной
процесс, и подробную инструкцию консультанта. После каждого вызова в Markdown
показываются токены и ссылка на точный prompt в `agent_artifacts`.

Сохранённый вопрос проекту:

```powershell
consultant.cmd --repo . ask demo "Опиши ветки доставки" --kind process
consultant.cmd --repo . ask demo "Подготовь сценарий" --kind vanessa
```

## Интерфейсы

- Codex/OpenCode: открыть корень и работать через команды из `AGENTS.md`.
- Терминал: `consultant.cmd` без параметров открывает меню.

## Проверки

```powershell
$env:PYTHONPATH = "src"
py -3 -m unittest discover -s tests -v
py -3 scripts\validate_repository.py
```
