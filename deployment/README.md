# Развёртывание 1C-Consultant

Пользовательские сценарии установки, обновления приложения и сопровождения графов:
[руководство по эксплуатации](../docs/service-operations.md).

Каталог содержит одну кодовую базу Go-установщика и скрипты подготовки Release.
GitHub Actions нативно собирает приложение и installer для Windows x64,
Linux x64/ARM64 и macOS x64/ARM64. `manifest.json` содержит все пять платформ.

## Подготовка Release

Обычный выпуск выполняет `.github/workflows/release.yml`. Пять runner’ов собирают
PyInstaller-пакеты приложения на своих ОС. Финальный Windows job запускает `go build`
для пяти installer-бинарников, собирает граф, manifest и общий offline bundle.

```powershell
./deployment/scripts/build-release.ps1 `
  -BaseUrl "https://github.com/ilnurcode/Modelirovanie/releases/download/v4.1.0" `
  -ApplicationDirectory application-packages `
  -GraphDatabase "C:\Users\Ilnur\Desktop\graph_rag_data\erp_graph_mcp.sqlite" `
  -GraphDatabaseSha256 "8947dbca6a355792417ca95b91833dcf035bcea5da55fc92b03915a59e812773"
```

Если Python вызывается не через `py -3`, передайте
`-Python "C:\path\python.exe" -PythonArguments @()`.

Нативный application ZIP создаёт `scripts/build_application.py`. Финальный скрипт
проверяет наличие пяти пакетов, вычисляет SHA-256, записывает manifest и добавляет в
отдельный graph ZIP только SQLite и необходимые лёгкие графы. Исходные `chunks`,
pickle и TF-IDF-файлы туда не попадают. В GitHub Actions SQLite скачивается как
`erp_graph_mcp.sqlite.zip` из отдельного Release `graph-v<graph_version>`, распаковывается
и проверяется по SHA-256 из
`PACKAGE_MANIFEST.json`. Дополнительные secrets не нужны. Installer по умолчанию
берёт manifest последнего Release приложения.

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

## Использование без команд

Откройте installer своей ОС. Без аргументов появляется меню установки, обновления,
проверки версий, отката и удаления. В меню можно выбрать интернет или offline bundle.
URL manifest и параметры командной строки обычному пользователю не нужны.

Для двойного клика на Windows/Linux скачайте
`1c-consultant-setup-<ОС>-<архитектура>.zip`. Windows запускает `.cmd`, Linux — `.sh`.
Для macOS скачайте `1c-consultant-installer-macos-<архитектура>.pkg`, откройте пакет,
затем запустите `1C-Consultant` из Applications.

После установки Windows получает ярлыки на рабочем столе и в меню «Пуск» текущего
пользователя. На macOS PKG устанавливает `/Applications/1C-Consultant.app` и открывает
CLI в Terminal. PKG потребует пароль администратора и, пока он не подписан, может
показать предупреждение Gatekeeper.

## Автоматизация

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
