#!/usr/bin/env python3
"""Runnable stdlib check for output validator."""
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    good = """## Статус
Статус: ГОТОВ_ПЛАН
## Схема процесса
## План-инструкция
| № | Блок ERP | Шаг | Объект и тип | Путь | Действие | Карточка | Зависимости | Источник | Проверка |
| 1 | Продажи | Тест | Документ | Путь | Создать | Поля | Нет | E-001 | Создан |
## Открытые вопросы
## Доказательства
| ID | Файл/раздел | Подтверждаемый факт |
| E-001 | test | Тест |
"""
    with tempfile.TemporaryDirectory() as directory:
        plan = Path(directory) / "plan.md"; plan.write_text(good, encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("validate_instruction.py")), "--stage", "plan", "--input", str(plan)], check=False)
    return result.returncode


if __name__ == "__main__": sys.exit(main())
