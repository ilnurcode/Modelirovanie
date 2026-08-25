# NewAgent 4.1.2

- Для Intel и Apple Silicon автоматически собираются отдельные unsigned `.pkg`.
- PKG устанавливает обычное `/Applications/1C-Consultant.app`; `.command` для запуска не нужен.
- Первый запуск приложения открывает CLI installer в Terminal, последующие — установленный сервис.
- Managed app mode не создаёт дубликат приложения в `~/Applications`.
