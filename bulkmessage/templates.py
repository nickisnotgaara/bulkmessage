"""Message templates and category normalization."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import config


def load_templates(path: Optional[str] = None) -> dict[str, str]:
    """Парсит Message_script.md: {категория: шаблон} с плейсхолдером {имя}."""
    p = Path(path or config.TEMPLATES_PATH)
    text = p.read_text(encoding="utf-8")
    templates: dict[str, str] = {}
    current_category: Optional[str] = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in config.TEMPLATE_MAP:
            if current_category and current_lines:
                templates[current_category] = " ".join(current_lines)
            current_category = stripped
            current_lines = []
        elif current_category and stripped.startswith("1)"):
            clean = re.sub(r"^1\)\s*", "", stripped)
            current_lines.append(clean)

    if current_category and current_lines:
        templates[current_category] = " ".join(current_lines)
    return templates


def normalize_category(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    s_low = s.lower()
    if s_low in config.CATEGORY_ALIASES:
        return config.CATEGORY_ALIASES[s_low]
    if s in config.TEMPLATE_MAP:
        return s
    for key, mapped in config.CATEGORY_ALIASES.items():
        if key in s_low:
            return mapped
    return s


def _safe_name(raw) -> str:
    """Извлекает безопасное имя из контакта.

    Возвращает первое непустое (и не только из пробелов) значение из:
      - contact.get("name", "")
      - первое слово из contact.get("name", ""), если оно разумной длины
    Если ничего нет — возвращает "" (пустую строку); подставляется
    нейтральное обращение ниже в build_message в зависимости от категории.
    Экранирует символы { и }, чтобы .format() не упал.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    s = raw.strip()
    if not s:
        return ""
    # Ограничим длину (защита от очень длинных "имён" вроде описаний из Excel)
    if len(s) > 60:
        s = s[:60].rsplit(" ", 1)[0] or s[:60]
    # Экранируем фигурные скобки, чтобы .format() не интерпретировал их как плейсхолдеры
    s = s.replace("{", "(").replace("}", ")")
    return s


def _format_template(text: str, name: str) -> str:
    """Безопасная подстановка {имя} в шаблон.

    Использует str.replace вместо str.format, чтобы случайные { или } в name
    не ломали шаблон.
    """
    return text.replace("{имя}", name)


def build_message(contact: dict, templates: dict[str, str]) -> str:
    category = contact.get("category", "")
    raw_name = _safe_name(contact.get("name", ""))
    normalized = normalize_category(category)

    # Если имени нет — для риэлторов подставим «коллега», для остальных — пусто.
    if raw_name:
        name = raw_name
    else:
        name = "коллега" if normalized == "Агенты" else ""

    # Подчистим "Здравствуй , " → "Здравствуйте, " когда имени нет
    def _clean_greeting(t: str) -> str:
        t = t.replace("Здравствуй , ", "Здравствуйте, ")
        t = t.replace("Здравствуй, ", "Здравствуйте, ")
        return t.strip()

    if normalized in templates:
        return _clean_greeting(_format_template(templates[normalized], name))
    for tpl_key, cat in config.TEMPLATE_MAP.items():
        if cat == category and tpl_key in templates:
            return _clean_greeting(_format_template(templates[tpl_key], name))
    return _format_template(
        f"Здравствуйте, {name}. Приглашаю поучаствовать в проекте под 25% годовых.",
        name,
    )
