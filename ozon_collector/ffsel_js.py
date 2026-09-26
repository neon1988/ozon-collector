"""JS-интероп Selenium ↔ Playwright: нормализация строк evaluate и запуск.

Playwright-строки JS приходят в двух видах: функция (`() => …`, `(dy) => …`,
`async () => …`) и выражение (`1+1`). Selenium execute_script умеет только
выражение, а промисы не разрешает. Этот модуль сводит оба вида к одному
async-вызову, который возвращает значение (как Playwright) или ошибку.

Ошибка JS пробрасывается как JsError, а не как selenium.WebDriverException:
вызывающий код (collect/puzzle) отличает ошибку страницы от падения драйвера.
"""
from __future__ import annotations

from typing import Any

OK = "__ozon_ok"
VAL = "__ozon_v"
ERR = "__ozon_e"

_ASYNC_FN = """
var __done = arguments[arguments.length - 1];
var __args = Array.prototype.slice.call(arguments, 0, -1);
var __f = %s;
try {
  Promise.resolve(__f.apply(null, __args)).then(function (v) {
    __done({"%s": 1, "%s": v});
  }, function (e) {
    __done({"%s": 1, "%s": String((e && e.stack) || e)});
  });
} catch (e) {
  __done({"%s": 1, "%s": String((e && e.stack) || e)});
}
""" % ("{FN}", OK, VAL, OK, ERR, OK, ERR)


class JsError(RuntimeError):
    """Ошибка JS, выполненного в контексте страницы."""


def js_is_function(js: str) -> bool:
    """Строка JS — функция (вызвать с аргументами) или выражение.

    Функция определяется по началу строки: `function`, `async` либо `(` в
    сочетании с `=>`. Последнее условие обязательно: иначе `(a + b)` был бы
    принят за функцию с параметром `a` и тихо вернул бы undefined.
    """
    s = (js or "").strip()
    if s.startswith("function") or s.startswith("async"):
        return True
    return s.startswith("(") and "=>" in s


def function_script(js: str) -> str:
    """Обёртка функции под execute_async_script (Playwright-await семантика).

    Promise.resolve снаружи — поэтому и `async () => …`, и обычная функция,
    возвращающая промис (цепочка fetch), дают значение, а не сам Promise.
    """
    return _ASYNC_FN.replace("{FN}", js.strip())


def expression_script(js: str) -> str:
    return "return (%s);" % js.strip()


def unwrap(res: Any) -> Any:
    """Развернуть маркер-обёртку; ошибка JS → JsError."""
    if isinstance(res, dict) and ERR in res:
        raise JsError("JS error: %s" % res[ERR])
    if isinstance(res, dict) and OK in res:
        return res.get(VAL)
    return res


def run(driver, js: str, args: tuple = (), timeout: float | None = None) -> Any:
    """Выполнить js с аргументами и вернуть значение (Playwright-семантика)."""
    if timeout is not None:
        driver.set_script_timeout(max(timeout, 1.0))
    try:
        if js_is_function(js):
            res = driver.execute_async_script(function_script(js), *args)
        else:
            res = driver.execute_script(expression_script(js), *args)
    except JsError:
        raise
    except Exception as e:
        msg = str(e)
        if "javascript error" in msg.lower() or "Error:" in msg:
            raise JsError("JS error: %s" % msg) from e
        raise
    return unwrap(res)
