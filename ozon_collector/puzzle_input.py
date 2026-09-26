"""Человекоподобное движение мыши для драга кусочка.

Траектория сглаженная (квадратичный bezier + ease-in-out) плюс микрошум и
разная длительность шагов: равномерный move — самый заметный для антибота
признак автоматизации. Никакой логики пазла здесь нет.

Перенесено из puzzle.py без изменения кода (механический сплит).

Ревизия 2.9 (биометрия траекторий): «человечность» больше не сводится к
ease-in-out + симметричный белый шум, потому что такая кривая даёт узнаваемый
профиль ускорения:

- форма eased-сегмента рандомизируется **на каждый драг** (экспонента ease,
  дуга, число точек, амплитуда тремора) — нет повторяющейся математической
  кривой на тысячах решений;
- добавлены низкочастотный дрейф + асимметричный микрошум (белый шум с
  одинаковым законом легко фильтруется усреднением);
- микро-паузы (dwell) в 1-2 точках траектории — человеческая «поправка
  курсора», а не идеальная дуга без остановок;
- overshoot & correction: цель пролетается на few px дальше, микропауза,
  возврат назад — живой человек доигрывает позицию рукой, а не идеально
  останавливается ровно на пикселе.
"""

import math
import random
import time


def _human_path(x0: float, y0: float, x1: float, y1: float,
                n: int = 22, arc: float = 5.0) -> list:
    """Точки траектории мыши «человеком»: Безье + ease-in-out + jitter.

    Анти-бот палит равномерные прямые линейные move (steps=N Playwright
    даёт идеально ровные промежуточные сэмплы). Человек: разгон → полёт →
    торможение, небольшая дуга по Y (нет идеальных горизонталей), мелкий
    тремор (±0.8px), завершение точно в (x1, y1) без «телепорта».
    """
    dx = x1 - x0
    dy = y1 - y0
    dist = math.hypot(dx, dy)
    if dist:
        arc = max(1.5, min(arc, dist * 0.12))
    else:
        arc = 0.0
    pts = []
    for i in range(n + 1):
        t = i / n
        # ease-in-out (платовидное ускорение и торможение на концах)
        s = t * t * (3 - 2 * t)
        # контрольная точка дуги: максимум прогиба посередине
        bx = x0 + dx * s
        by = y0 + dy * s + arc * 4 * (s - s * s)
        jx = random.uniform(-0.9, 0.9) * (1 - 4 * (s - 0.5) ** 2)
        jy = random.uniform(-0.9, 0.9) * (1 - 4 * (s - 0.5) ** 2)
        pts.append((bx + jx, by + jy))
    pts[-1] = (x1, y1)
    return pts


def _ease(t: float, k: float) -> float:
    """Обобщённый smoothstep с рандомизированной экспонентой k.

    k=2 — классический ease-in-out (s=t²(3-2t)). k≠2 меняет профиль
    ускорения/торможения, поэтому у каждого драга своя форма кривой.
    """
    kk = max(0.4, k)
    return (t ** kk) / ((t ** kk) + ((1.0 - t) ** kk)) if 0 < t < 1 else t


def _drift(rng, n: int) -> list:
    """Низкочастотный дрейф: сумма 2 синусов с рандомной фазой/периодом.

    Обычный белый шум усредняется фильтром Калмана за несколько сэмплов;
    медленный дрейф — нет.
    """
    a1, a2 = rng.uniform(0.3, 0.8), rng.uniform(0.2, 0.6)
    p1 = rng.uniform(2.0, 4.0) * math.pi
    p2 = rng.uniform(4.0, 8.0) * math.pi
    f1, f2 = rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)
    return [a1 * math.sin(p1 * i / max(1, n - 1) + f1) +
            a2 * math.sin(p2 * i / max(1, n - 1) + f2) for i in range(n + 1)]


def _human_drag_path(x0: float, y0: float, x1: float, y1: float,
                     rng: random.Random | None = None,
                     bound: float | None = None) -> tuple:
    """Траектория драга с overshoot & correction (ревизия 2.9).

    Возвращает (points, shape) — shape попадает в лог/телеметрию, чтобы в
    данных было видно разнообразие параметров, а не одну и ту же кривую.
    Конечная точка — ровно (x1, y1): overshoot отыгрывается назад, поэтому
    последующая сверка позиции (см. puzzle_drag) работает как раньше.

    bound — правая граница дорожки (max_hx). Если до неё меньше 2px, overshoot
    отменяется: пролететь дальше конца дорожки нельзя.
    """
    rng = rng or random
    n = rng.randint(14, 34)
    arc = rng.uniform(2.0, 7.0)
    ease_k = rng.uniform(1.5, 2.8)
    tremor = rng.uniform(0.3, 1.0)
    over = rng.uniform(2.0, 9.0) * (1 if rng.random() < 0.85 else -1)
    if bound is not None and x1 + over > bound - 1.5:
        # У края дорожки перелёта нет: превышать нечего, остаток добирает
        # измерительный цикл (puzzle_drag), а не «угадайка» мышью.
        over = 0.0
    hold = sorted(rng.sample(range(2, n - 1), k=min(2, max(1, n - 3))))
    dx = x1 - x0
    dy1 = y1 - y0
    dist = math.hypot(dx, dy1)
    if dist:
        arc = max(1.5, min(arc, dist * 0.12))
    else:
        arc = over = 0.0
    drift = _drift(rng, n)
    tx = x1 + over
    ty = y1 + rng.uniform(-1.0, 1.0)
    pts = []
    for i in range(n + 1):
        t = i / n
        s = _ease(t, ease_k)
        # основной eased-сегмент до точки перелёта
        bx = x0 + (tx - x0) * s
        by = y0 + dy1 * s + arc * 4 * (s - s * s)
        taper = 1 - 4 * (s - 0.5) ** 2      # шум максимален в середине
        # асимметричный микрошум: разные амплитуды/сдвиги для x и y
        jx = (rng.uniform(-tremor, tremor * 0.6) + drift[i] * 0.7) * taper
        jy = (rng.uniform(-tremor * 0.6, tremor) - drift[i] * 0.5) * taper
        pts.append((bx + jx, by + jy))
    for i in hold:                          # микро-пауза «поправки курсора»
        px, py = pts[i]
        pts[i] = (px + rng.uniform(-0.6, 0.6), py + rng.uniform(-0.6, 0.6))
    # overshoot & correction: 2-3 точки возвратного сегмента ty→y1
    corr = []
    for k in (1, 2):
        f = k / 3.0
        cx = tx + (x1 - tx) * f + rng.uniform(-0.4, 0.4)
        cy = ty + (y1 - ty) * f + rng.uniform(-0.4, 0.4)
        corr.append((cx, cy))
    pts.extend(corr)
    pts[-1] = (x1, y1)
    shape = {"n": n, "arc": round(arc, 2), "ease_k": round(ease_k, 2),
             "tremor": round(tremor, 2), "overshoot": round(over, 2),
             "holds": hold}
    return pts, shape


def _human_move(page, pts, delay=(0.008, 0.02), hold_ranges=()):
    """Эмитировать траекторию реальными событиями мыши.

    Каждая точка — отдельный mouse.move (КДП-событие), между точками
    случайная пауза (дрожание руки). hold_ranges: индексы точек с длинной
    паузой (dwell, человеческая «поправка курсора»). Последняя точка
    фиксируется без паузы после неё (потом down/up мгновенно от цели).
    """
    holds = set(hold_ranges or ())
    last = len(pts) - 1
    for i, (px, py) in enumerate(pts):
        page.mouse.move(px, py)
        if i != last:
            time.sleep(random.uniform(0.045, 0.11) if i in holds
                       else random.uniform(*delay))


def _human_drag(page, x0: float, y0: float, x1: float, y1: float,
                rng: random.Random | None = None, bound: float | None = None):
    """Драг кусочка «человеком»: путь + события + паузы; вернёт shape."""
    pts, shape = _human_drag_path(x0, y0, x1, y1, rng=rng, bound=bound)
    _human_move(page, pts, hold_ranges=shape.get("holds", ()))
    return shape


def _human_click(page, x: float, y: float, hold: float = 0.0):
    """Клик/нажим в точке с пред-подводом мыши (не телепорт в целевую)."""
    px0, py0 = x - random.uniform(25, 55), y + random.uniform(-18, 18)
    page.mouse.move(px0, py0)
    time.sleep(random.uniform(0.05, 0.14))
    _human_move(page, _human_path(px0, py0, x, y, n=12, arc=2.0))
    time.sleep(random.uniform(0.03, 0.08))
    page.mouse.down()
    if hold > 0:
        time.sleep(hold)
