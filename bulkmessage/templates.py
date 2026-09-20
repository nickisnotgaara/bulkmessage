"""Message templates and category normalization.

Phase 3.1: поддержка spin-tax (variant_index) для избежания паттерн-детекции.
Поддерживает два формата в Message_script.md:
1. {opt1|opt2|opt3} — inline spin (внутри одной строки)
2. ## Вариант 1 / ## Вариант 2 / ## Вариант 3 — multi-block
Если ни один — возвращается базовый текст.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Optional

from . import config


_SPINTAX_PATTERN = re.compile(r"\{([^{}|]+(?:\|[^{}]+)+)\}")


def _expand_spintax(text: str, variant_index: Optional[int] = None) -> str:
    """Раскрывает {opt1|opt2|opt3} в один вариант.

    Распознаёт ТОЛЬКО паттерны с разделителем | (настоящий spin-tax).
    Обычные плейсхолдеры {имя} НЕ трогаются — это ответственность _format_template.

    Если variant_index задан — выбирает по индексу (round-robin).
    Если None — случайно.
    """
    def _replace_one(match: re.Match) -> str:
        options = match.group(1).split("|")
        options = [o.strip() for o in options if o.strip()]
        if not options:
            return ""
        if variant_index is not None:
            idx = variant_index % len(options)
        else:
            idx = random.randrange(len(options))
        return options[idx]

    return _SPINTAX_PATTERN.sub(_replace_one, text)


def load_templates(path: Optional[str] = None) -> dict[str, str]:
    """Парсит Message_script.md: {категория: шаблон} с плейсхолдером {имя}.

    Если в файле несколько шаблонов на категорию (## Вариант 1/2/3),
    возвращает ТОЛЬКО первый (для backward compat).
    Для spin-tax используйте load_all_variants().
    """
    p = Path(path or config.TEMPLATES_PATH)
    text = p.read_text(encoding="utf-8")
    templates: dict[str, str] = {}
    current_category: Optional[str] = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # Поддержка обоих форматов: "Покупатели" и "## Покупатели".
        cat_name = stripped.lstrip("#").strip()
        if cat_name in config.TEMPLATE_MAP:
            if current_category and current_lines:
                templates[current_category] = " ".join(current_lines)
            current_category = cat_name
            current_lines = []
        elif current_category and stripped.startswith("1)"):
            clean = re.sub(r"^1\)\s*", "", stripped)
            current_lines.append(clean)
        # Phase 3.1: "## Вариант N" — пропускаем (берём только первый 1) вариант)

    if current_category and current_lines:
        templates[current_category] = " ".join(current_lines)
    return templates


def load_all_variants(path: Optional[str] = None) -> dict[str, list[str]]:
    """Возвращает {category: [variant_1_text, variant_2_text, ...]}.

    Парсит:
    - категорию (одна из TEMPLATE_MAP)
    - внутри — несколько блоков "1) ..." (как в load_templates) OR
      "## Вариант N" маркеры для явного разделения.

    Возвращает все варианты (минимум SPINTAX_VARIANT_COUNT_MIN).
    """
    p = Path(path or config.TEMPLATES_PATH)
    text = p.read_text(encoding="utf-8")
    by_cat: dict[str, list[str]] = {}
    current_category: Optional[str] = None
    current_lines: list[str] = []

    def _commit():
        nonlocal current_lines
        if current_category and current_lines:
            txt = " ".join(current_lines).strip()
            if txt:
                by_cat.setdefault(current_category, []).append(txt)
            current_lines = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        cat_name = stripped.lstrip("#").strip()
        if cat_name in config.TEMPLATE_MAP:
            _commit()
            current_category = cat_name
        elif current_category and stripped.startswith("1)"):
            clean = re.sub(r"^1\)\s*", "", stripped)
            current_lines.append(clean)
    _commit()
    return by_cat


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


def build_message(
    contact: dict,
    templates: dict[str, str],
    variant_index: Optional[int] = None,
) -> str:
    """Строит финальное сообщение с подстановкой имени и spin-tax.

    variant_index: если None — выбирает случайно (для spin-tax).
                   если int — выбирает по индексу (round-robin).
    """
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

    raw: Optional[str] = None
    if normalized in templates:
        raw = templates[normalized]
    else:
        for tpl_key, cat in config.TEMPLATE_MAP.items():
            if cat == category and tpl_key in templates:
                raw = templates[tpl_key]
                break

    if raw is None:
        raw = (
            f"Здравствуйте, {{имя}}. Приглашаю поучаствовать в проекте под 25% годовых."
        )

    # Phase 3.1: если есть inline spin-tax {opt1|opt2|opt3} — раскрываем.
    raw = _expand_spintax(raw, variant_index=variant_index)

    return _clean_greeting(_format_template(raw, name))
