from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TextIO


ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True, slots=True)
class Palette:
    reset: str = "\033[0m"
    bold: str = "\033[1m"
    dim: str = "\033[2m"
    cyan: str = "\033[38;5;44m"
    blue: str = "\033[38;5;75m"
    green: str = "\033[38;5;78m"
    yellow: str = "\033[38;5;221m"
    red: str = "\033[38;5;203m"
    gray: str = "\033[38;5;245m"
    white: str = "\033[38;5;255m"


class Spinner(AbstractContextManager["Spinner"]):
    def __init__(self, ui: "ConsoleUI", message: str):
        self.ui = ui
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Spinner":
        if not self.ui.interactive:
            self.ui.write(f"… {self.message}")
            return self
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def _animate(self) -> None:
        frames = "◐◓◑◒"
        index = 0
        while not self._stop.wait(0.12):
            frame = self.ui.paint(frames[index % len(frames)], "cyan")
            self.ui.stream.write(f"\r  {frame} {self.message}")
            self.ui.stream.flush()
            index += 1

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self.ui.stream.write("\r" + " " * min(self.ui.width, len(self.message) + 8) + "\r")
            self.ui.stream.flush()
        return False


class ConsoleUI:
    def __init__(self, stream: TextIO | None = None, color: bool | None = None):
        self.stream = stream or sys.stdout
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.color = self.interactive and not os.getenv("NO_COLOR") if color is None else color
        self.palette = Palette()
        if os.name == "nt" and self.interactive:
            self._enable_windows_vt()

    @staticmethod
    def _enable_windows_vt() -> None:
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)
        except (AttributeError, OSError):
            pass

    @property
    def width(self) -> int:
        return max(58, min(100, shutil.get_terminal_size((86, 24)).columns))

    def paint(self, text: object, tone: str = "white", bold: bool = False) -> str:
        value = str(text)
        if not self.color:
            return value
        prefix = getattr(self.palette, tone, self.palette.white)
        if bold:
            prefix = self.palette.bold + prefix
        return f"{prefix}{value}{self.palette.reset}"

    def write(self, text: object = "") -> None:
        self.stream.write(str(text) + "\n")
        self.stream.flush()

    def clear(self) -> None:
        if self.interactive:
            self.stream.write("\033[2J\033[H")
            self.stream.flush()
        else:
            self.write()

    def header(self, title: str, subtitle: str = "", breadcrumb: str = "") -> None:
        width = self.width
        self.write(self.paint("═" * width, "cyan"))
        self.write("  " + self.paint(title, "cyan", bold=True))
        if subtitle:
            self.write("  " + self.paint(subtitle, "gray"))
        if breadcrumb:
            self.write("  " + self.paint(breadcrumb, "blue"))
        self.write(self.paint("─" * width, "cyan"))

    def menu(self, items: list[tuple[str, str, str]]) -> None:
        key_width = max((len(key) for key, _, _ in items), default=1)
        for key, title, description in items:
            marker = self.paint(key.rjust(key_width), "cyan", bold=True)
            self.write(f"  {marker}  {self.paint(title, 'white', bold=True)}")
            if description:
                self.write(" " * (key_width + 6) + self.paint(description, "gray"))
        self.write()

    def panel(self, title: str, lines: list[str], tone: str = "blue") -> None:
        inner = self.width - 4
        self.write("  " + self.paint(f"┌─ {title} " + "─" * max(0, inner - len(title) - 3) + "┐", tone))
        for raw in lines or [""]:
            for line in self._wrap(str(raw), inner - 2):
                padding = " " * max(0, inner - 2 - self.visible_len(line))
                self.write("  " + self.paint("│", tone) + f" {line}{padding} " + self.paint("│", tone))
        self.write("  " + self.paint("└" + "─" * inner + "┘", tone))

    def table(self, headers: list[str], rows: list[list[object]]) -> None:
        if not rows:
            self.info("Записей пока нет.")
            return
        column_count = len(headers)
        available = self.width - column_count * 3 - 1
        natural = [len(header) for header in headers]
        for row in rows:
            for index, value in enumerate(row[:column_count]):
                natural[index] = max(natural[index], min(38, self.visible_len(str(value))))
        total = sum(natural) or 1
        widths = [max(8, int(available * size / total)) for size in natural]
        while sum(widths) > available:
            widest = max(range(len(widths)), key=widths.__getitem__)
            if widths[widest] <= 8:
                break
            widths[widest] -= 1

        def line(values: list[object], header: bool = False) -> str:
            cells = []
            for index, value in enumerate(values):
                cell = self.truncate(str(value), widths[index]).ljust(widths[index])
                cells.append(self.paint(cell, "cyan" if header else "white", bold=header))
            return "  " + self.paint("│", "gray") + self.paint("│", "gray").join(
                f" {cell} " for cell in cells
            ) + self.paint("│", "gray")

        self.write("  " + self.paint("┌" + "┬".join("─" * (width + 2) for width in widths) + "┐", "gray"))
        self.write(line(headers, True))
        self.write("  " + self.paint("├" + "┼".join("─" * (width + 2) for width in widths) + "┤", "gray"))
        for row in rows:
            self.write(line(row))
        self.write("  " + self.paint("└" + "┴".join("─" * (width + 2) for width in widths) + "┘", "gray"))

    def status(self, value: str) -> str:
        labels = {
            "configured": ("настроен", "blue"),
            "requirements_pending": ("ожидаются ответы", "yellow"),
            "requirements_approved": ("требования утверждены", "green"),
            "design_pending": ("схема на согласовании", "yellow"),
            "design_approved": ("схема утверждена", "green"),
            "generating": ("формируется", "cyan"),
            "feedback_pending": ("ожидается оценка", "yellow"),
            "draft": ("черновик", "gray"),
            "successful": ("подтверждён", "green"),
            "needs_revision": ("нужна доработка", "red"),
            "error": ("ошибка", "red"),
        }
        label, tone = labels.get(value, (value, "gray"))
        return self.paint(f"● {label}", tone, bold=True)

    def status_text(self, value: str) -> str:
        labels = {
            "configured": "настроен",
            "requirements_pending": "ожидаются ответы",
            "requirements_approved": "требования утверждены",
            "design_pending": "схема на согласовании",
            "design_approved": "схема утверждена",
            "generating": "формируется",
            "feedback_pending": "ожидается оценка",
            "draft": "черновик",
            "successful": "подтверждён",
            "needs_revision": "нужна доработка",
            "error": "ошибка",
        }
        return labels.get(value, value)

    def project_state_text(self, value: str) -> str:
        labels = {
            "in_development": "в разработке",
            "unconfirmed": "не подтверждён",
            "confirmed": "подтверждён",
        }
        return labels.get(value, value)

    def progress(self, labels: list[str], current: int) -> None:
        parts = []
        for index, label in enumerate(labels):
            if index < current:
                parts.append(self.paint(f"● {label}", "green"))
            elif index == current:
                parts.append(self.paint(f"◉ {label}", "cyan", bold=True))
            else:
                parts.append(self.paint(f"○ {label}", "gray"))
        self.write("  " + self.paint(" ─ ", "gray").join(parts))

    def success(self, message: str) -> None:
        self.write("  " + self.paint("✓", "green", bold=True) + " " + message)

    def warning(self, message: str) -> None:
        self.write("  " + self.paint("!", "yellow", bold=True) + " " + message)

    def error(self, message: str) -> None:
        self.write("  " + self.paint("×", "red", bold=True) + " " + message)

    def info(self, message: str) -> None:
        self.write("  " + self.paint("•", "blue", bold=True) + " " + message)

    def prompt_label(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        return "  " + self.paint("›", "cyan", bold=True) + f" {prompt}{self.paint(suffix, 'gray')}: "

    def pause(self, message: str = "Нажмите Enter, чтобы продолжить") -> None:
        if self.interactive:
            input(self.prompt_label(message))

    def spinner(self, message: str) -> Spinner:
        return Spinner(self, message)

    @staticmethod
    def visible_len(text: str) -> int:
        return len(ANSI.sub("", text))

    @staticmethod
    def truncate(text: str, width: int) -> str:
        return text if len(text) <= width else text[: max(1, width - 1)] + "…"

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        if not text:
            return [""]
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word[:width]
        if current:
            lines.append(current)
        return lines or [""]
