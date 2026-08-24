#!/usr/bin/env python3
"""Check required Markdown structure for 1C modeler output."""
import argparse
import re
import sys
from pathlib import Path


REQUIRED = {
    "settings": [
        "## Статус", "Статус: ОЖИДАЕТ_ПОДТВЕРЖДЕНИЯ_НАСТРОЕК",
        "## Границы процесса", "## Сопоставление шагов", "## Начальные настройки",
        "| Блок ERP | Настройка | Основание | Источник |", "## Вопросы", "## Противоречия",
    ],
    "plan": [
        "## Статус", "Статус: ГОТОВ_ПЛАН", "## Схема процесса", "## План-инструкция",
        "| № | Блок ERP | Шаг | Объект и тип | Путь | Действие | Карточка | Зависимости | Источник | Проверка |",
        "## Открытые вопросы", "## Доказательства", "| ID | Файл/раздел | Подтверждаемый факт |",
    ],
}


def main():
    parser = argparse.ArgumentParser(description="Validate 1C modeler Markdown structure")
    parser.add_argument("--stage", required=True, choices=REQUIRED)
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args(); text = args.input.read_text(encoding="utf-8-sig")
    missing = [item for item in REQUIRED[args.stage] if item not in text]
    out_of_order = []
    headings = [item for item in REQUIRED[args.stage] if item.startswith("## ")]
    positions = [text.find(item) for item in headings]
    if positions != sorted(positions): out_of_order.append("Заголовки нарушают обязательный порядок.")
    errors = missing + out_of_order
    if args.stage == "plan" and not missing:
        evidence = text.split("## Доказательства", 1)[1]
        defined = set(re.findall(r"^\|\s*(E-\d+)\s*\|", evidence, flags=re.MULTILINE))
        used = set(re.findall(r"\bE-\d+\b", text.split("## Доказательства", 1)[0]))
        undefined = sorted(used - defined)
        if undefined: errors.append("Не определены доказательства: " + ", ".join(undefined))
    if errors:
        print("ОШИБКА:\n" + "\n".join(f"- {item}" for item in errors)); return 1
    print("OK: структура соответствует стадии " + args.stage); return 0


if __name__ == "__main__": sys.exit(main())
