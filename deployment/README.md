# Развёртывание 1C-Consultant

Каталог содержит одну кодовую базу Go-установщика и скрипты подготовки Release.
Текущий пакет приложения проверен только для Windows x64, поэтому генерируемый
`manifest.json` публикует только эту платформу. Installer-бинарники собираются для
Windows, Linux и macOS; новые платформы следует добавлять в manifest только после
появления и проверки соответствующих пакетов приложения.

## Подготовка Release

Требования для локальной сборки: PowerShell 7, Python 3.11 и готовый `consultant.exe`.
GitHub Actions собирает `consultant.exe` из исходников автоматически.

```powershell
./deployment/scripts/build-release.ps1 `
  -BaseUrl "https://github.com/ilnurcode/Modelirovanie/releases/download/v0.6.0"
```

Если Python вызывается не через `py -3`, передайте
`-Python "C:\path\python.exe" -PythonArguments @()`.

Скрипт проверяет версию приложения и репозиторий, создаёт application/graph ZIP,
вычисляет размер и SHA-256, затем записывает `release/manifest.json`. Installer
по умолчанию берёт manifest последнего Release; `--manifest` позволяет заменить URL.

Для offline bundle положите `manifest.json` в корень, application ZIP — в
`application/`, graph ZIP — в `graphs/`, installer-бинарники — в `installer/`.

## Сборка installer

Требуется Go 1.22+.

```powershell
./deployment/scripts/build-installers.ps1
```

Скрипт запускает `go test`, собирает пять целевых бинарников в `deployment/dist/`
и создаёт `SHA256SUMS`. Подпись Windows-кода и Apple notarization выполняются после
сборки корпоративными средствами: ключи и сертификаты в проект не включаются.

## Использование

```powershell
# Online
./1c-consultant-installer-windows-x64.exe install `
  --application --graphs "erp-2.5.27.49" --non-interactive

# Offline
./1c-consultant-installer-windows-x64.exe install `
  --offline-path "D:\1c-consultant-offline-bundle" `
  --application --graphs "erp-2.5.27.49" --non-interactive

./1c-consultant-installer-windows-x64.exe status
./1c-consultant-installer-windows-x64.exe check
./1c-consultant-installer-windows-x64.exe rollback
```

Архив скачивается во временный каталог, проверяется по размеру и SHA-256,
распаковывается без разрешения path traversal и symlink, проверяется запуском
`consultant.exe --version`, после чего атомарно переключается active version.
Предыдущая версия сохраняется для rollback. Состояние находится в
`config/installed.json`, журнал без URL query/fragment — в `logs/installer.log`.
Launcher передаёт приложению каталог данных, поэтому Modeler использует активный
внешний граф. Если состояние отсутствует, приложение пробует встроенный граф.

Локальные секреты подключения к 1С не входят ни в manifest, ни в installed.json.
