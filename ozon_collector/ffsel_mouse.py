"""page.mouse поверх W3C Actions (нужно puzzle.py для drag/click по координатам).

Playwright даёт абсолютные координаты указателя; W3C Actions — смещение
относительно in-view центра элемента, поэтому координаты пересчитываются
относительно видимого центра body. Промежуточная точка (steps>1)
имитирует человеческое ведение мыши короткими паузами.
"""
from __future__ import annotations

from . import log


class SeleniumMouse:
    """Абсолютные координаты указателя через move_to_element_with_offset."""

    def __init__(self, driver):
        self._d = driver
        self._x = 0.0
        self._y = 0.0

    def _body_center(self) -> tuple:
        try:
            c = self._d.execute_script(
                "const r = document.body.getBoundingClientRect();"
                "return [Math.min(Math.max(r.left + r.width/2, 0),"
                " window.innerWidth), Math.min(Math.max(r.top + r.height/2, 0),"
                " window.innerHeight)];")
            return float(c[0]), float(c[1])
        except Exception:
            return 0.0, 0.0

    def _body(self):
        try:
            return self._d.find_element("tag name", "body")
        except Exception:
            return None

    def move(self, x: float, y: float, steps: int = 1) -> None:
        from selenium.webdriver.common.action_chains import ActionChains

        body = self._body()
        if body is None:
            return
        cx, cy = self._body_center()
        n = max(int(steps or 1), 1)
        if n == 1:
            ActionChains(self._d).move_to_element_with_offset(
                body, int(round(x - cx)), int(round(y - cy))).perform()
            self._x, self._y = float(x), float(y)
            return
        chain = ActionChains(self._d)
        chain.move_to_element_with_offset(
            body, int(round(self._x - cx)), int(round(self._y - cy)))
        dx = (float(x) - self._x) / n
        dy = (float(y) - self._y) / n
        for _ in range(n):
            chain.move_by_offset(int(round(dx)), int(round(dy))).pause(0.008)
        chain.perform()
        self._x, self._y = float(x), float(y)

    def down(self) -> None:
        from selenium.webdriver.common.action_chains import ActionChains

        ActionChains(self._d).click_and_hold().perform()

    def up(self) -> None:
        from selenium.webdriver.common.action_chains import ActionChains

        try:
            ActionChains(self._d).release().perform()
        except Exception as e:
            log.debug(f"mouse.up: {type(e).__name__}: {e}")
