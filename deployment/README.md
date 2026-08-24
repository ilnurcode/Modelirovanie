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
  -BaseUrl "https://github.com/ilnurcode/Modelirovanie/releases/download/v0.7.0" `
  -ApplicationDirectory application-packages
```

Если Python вызывается не через `py -3`, передайте
`-Python "C:\path\python.exe" -PythonArguments @()`.

Нативный application ZIP создаёт `scripts/build_application.py`. Финальный скрипт
проверяет наличие пяти пакетов, вычисляет SHA-256 и записывает manifest. Installer
по умолчанию берёт manifest последнего Release.

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

Для двойного клика скачайте `1c-consultant-setup-<ОС>-<архитектура>.zip`.
Windows запускает `.cmd`, macOS — `.command`, Linux — `.sh`. На Linux окружение
рабочего стола может один раз запросить разрешение на запуск файла.

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
