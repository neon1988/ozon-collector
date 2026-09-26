"""Перевод селекторов Playwright в форму, понимаемую Firefox/Selenium.

Три вида, которые реально использует ozon_collector:
  * CSS — нативно, но Playwright-псевдоклассы (`:visible`) Firefox не знает;
  * XPath — если селектор начинается с `/` или `(`, иначе ищем By.XPATH;
  * get_by_role — доступность-роли в XPath: сам Selenium их не умеет.
"""
from __future__ import annotations


def xq(value: str) -> str:
    """Строка в XPath-литерал (одиночные кавычки экранируются)."""
    if "'" not in value:
        return "'%s'" % value
    if '"' not in value:
        return '"%s"' % value
    parts = value.split("'")
    return "concat(%s)" % ", \"'\", ".join("'%s'" % p for p in parts)


def css_to_xpath(selector: str) -> str:
    """CSS-селектор → XPath-совместимая строка для find_element(s).

    Раскрывается только используемое подмножество: `:visible` отбрасывается
    (видимость проверяется отдельно через is_displayed), остальное уходит в
    find_elements как есть — так незнакомые Firefox конструкции не ломаются
    молча, а падают видимой ошибкой.
    """
    s = (selector or "").strip()
    if ":visible" in s:
        s = s.replace(":visible", "")
    return s


def is_xpath(selector: str) -> bool:
    """Селектор задан XPath-ом (а не CSS)."""
    s = (selector or "").strip()
    return s.startswith("/") or s.startswith("(")


def role_xpath(role: str, name: str | None = None) -> str:
    """get_by_role(role, name) → XPath по @role/тегу и имени."""
    parts = ["//*[@role=%s or self::%s]" % (xq(role), role)]
    if name:
        lit = xq(name)
        parts.append(
            "[contains(normalize-space(string(.)), %s)"
            " or contains(@aria-label, %s)]" % (lit, lit))
    return "".join(parts)
