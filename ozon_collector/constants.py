"""Общие константы пакета, не тянущие тяжёлых зависимостей (CDP, Playwright и т.п.).

Вынесены из collect.py, чтобы лёгкие модули (например report.py для рендера
Markdown) могли использовать их без импорта тяжёлого сборщика.
"""

import re

# Маркеры заголовков анти-бот страниц/челленджей Ozon («Похоже, нет соединения»,
# «Доступ ограничен», капча и т.п.). Используется для распознавания того, что
# вместо карточки/выдачи пришла challenge-страница.
CHALLENGE_TITLE_MARKERS = (
    "похоже, нет соединения",
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "подтвердите, что вы человек",
    "проверка, что вы не робот",
    "antibot captcha",
    "captcha",
)

# Текстовые маркеры (кроме голого «captcha») ищутся как подстроки.
_SUBSTRING_MARKERS = tuple(m for m in CHALLENGE_TITLE_MARKERS if m != "captcha")

# «captcha» — только по границам слова: иначе товар с «CaptchaMaster»/«captchax»
# в названии уходил в бесконечный retry (находка аудита 2026-10-06).
_CAPTCHA_WORD_RE = re.compile(r"(?<!\w)captcha(?!\w)")


def title_challenge_marker(title: str) -> str | None:
    """Маркер антибот-челленджа в title или None (регистр не важен)."""
    t = (title or "").lower()
    for m in _SUBSTRING_MARKERS:
        if m in t:
            return m
    if _CAPTCHA_WORD_RE.search(t):
        return "captcha"
    return None
