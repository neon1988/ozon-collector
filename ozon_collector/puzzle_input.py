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

Ревизия 3.0 (бюджет решения, 2026-10-07): длина пачки больше не задаётся
только случайностью. В подвиревшей вкладке `page.mouse.move` стоит ~0.7 с
на точку (живой лог: зонд 17 точек = 12.34 с), и фиксированные 22-24 точки
съедали весь `PUZZLE_BUDGET_S` до начала доводки. Теперь число точек
подбирается под остаток бюджета (move_cost() — скользящая оценка стоимости
точки), а обрезанная по дедлайну траектория всё равно доставляет конечную
координату отдельной командой: иначе кусок остался бы там, где его оставил
последний промежуточный move, и доводка считала бы неверную позицию.
"""

import math
import random
import time

# Оценка стоимости одной точки траектории (секунд на page.mouse.move) —
# скользящее среднее по последним пачкам. На здоровой вкладке точка стоит
# 0.008-0.02 с, на подвисшей CDP-раундтрип раздувается до 0.5-0.8 с (живой
# лог 2026-10-07: зонд 17 точек = 12.34 с, большой ход 24 точки = 10.86 с).
# Траектория фиксированной длины в такой вкладке САМА съедает бюджет
# решения: доезд просто не начинается (stop=deadline_micro при iters=2).
# Поэтому длина пачки подбирается под остаток бюджета.
_MOVE_COST_S: list = [0.0]

# Границы числа точек траектории. Снизу 3: одна команда нужна ВСЕГДА, иначе
# конечная координата просто не будет доставлена (см. _human_move).
PATH_MIN_POINTS = 3
PATH_MAX_POINTS = 34
PATH_NOMINAL_POINTS = 22
# Доля остатка бюджета, которую можно потратить на одну траекторию: остальное
# нужно на замер позиции и на вердикт после дропа.
PATH_BUDGET_SHARE = 0.35
# Одна точка на 4 px хода: на коротком смещении 30 точек смысла нет, лишние
# события — тоже палево.
PATH_PX_PER_POINT = 4.0
# Нижняя оценка стоимости точки для расчёта (сек): быстрее CDP не отвечает.
_PATH_COST_FLOOR_S = 0.005


def move_cost() -> float:
    """Текущая оценка секунд на одну точку траектории."""
    return _MOVE_COST_S[0]


def _note_move_cost(points: int, seconds: float) -> float:
    """Обновить оценку стоимости точки по факту пачки (0 = неизвестно)."""
    if points > 0 and seconds > 0.0:
        per = seconds / float(points)
        prev = _MOVE_COST_S[0]
        _MOVE_COST_S[0] = per if prev <= 0.0 else 0.5 * (prev + per)
    return _MOVE_COST_S[0]


def _fit_points(left_s: float, dist: float, want: int = PATH_NOMINAL_POINTS) -> int:
    """Сколько точек траектории влезает в остаток бюджета (чистая функция).

    left_s <= 0 — бюджета нет (точечный свип, юнит-тест): возвращаем `want`,
    форма остаётся случайной как раньше. Иначе не больше, чем влезает по
    времени (остаток × PATH_BUDGET_SHARE / стоимость точки), не больше
    PATH_MAX_POINTS и не больше одной точки на PATH_PX_PER_POINT пикселей хода.
    """
    if left_s <= 0.0:
        return max(PATH_MIN_POINTS, int(want))
    cost = max(move_cost(), _PATH_COST_FLOOR_S)
    by_time = int(left_s * PATH_BUDGET_SHARE / cost)
    by_dist = int(max(PATH_MIN_POINTS, dist / PATH_PX_PER_POINT))
    return max(PATH_MIN_POINTS,
               min(int(want), PATH_MAX_POINTS, by_dist, by_time))


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
                     bound: float | None = None,
                     n: int | None = None) -> tuple:
    """Траектория драга с overshoot & correction (ревизия 2.9).

    Возвращает (points, shape) — shape попадает в лог/телеметрию, чтобы
    в данных было видно разнообразие параметров, а не одну и ту же кривую.
    Конечная точка — ровно (x1, y1): overshoot отыгрывается назад, поэтому
    последующая сверка позиции (см. puzzle_drag) работает как раньше.

    bound — правая граница дорожки (max_hx). Если до неё меньше 2px, overshoot
    отменяется: пролететь дальше конца дорожки нельзя.

    n — число точек; None = случайное 14..34. Передаётся явно, когда длина
    подобрана под остаток бюджета (_fit_points).
    """
    rng = rng or random
    n = int(n) if n else rng.randint(14, PATH_MAX_POINTS)
    arc = rng.uniform(2.0, 7.0)
    ease_k = rng.uniform(1.5, 2.8)
    tremor = rng.uniform(0.3, 1.0)
    over = rng.uniform(2.0, 9.0) * (1 if rng.random() < 0.85 else -1)
    if bound is not None and x1 + over > bound - 1.5:
        # У края дорожки перелёта нет: превышать нечего, остаток добирает
        # измерительный цикл (puzzle_drag), а не «угадайка» мышью.
        over = 0.0
    # k=0 при n<=3: диапазон индексов [2..n-2] пуст (PATH_MIN_POINTS=3 — при
    # урезанном бюджете n=3 реален), а прежний max(1, ...) требовал сэмпл из
    # пустого диапазона → ValueError посреди драга (аудит 2026-10-15).
    hold = sorted(rng.sample(range(2, n - 1), k=min(2, max(0, n - 3))))
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


def _human_move(page, pts, delay=(0.008, 0.02), hold_ranges=(),
                deadline: float = 0.0) -> int:
    """Эмитировать траекторию реальными событиями мыши.

    Каждая точка — отдельный mouse.move (КДП-событие), между точками
    случайная пауза (дрожание руки). hold_ranges: индексы точек с длинной
    паузой (dwell, человеческая «правка курсора»). Последняя точка
    фиксируется без паузы после неё (потом down/up мгновенно от цели).

    deadline (абсолютный time.monotonic) — ВАЖНО, иначе одна пачка съедает
    бюджет решения: в подвиревшей вкладке точка стоит 0.7 с, и 24-точечный
    ход = 10.9 с. По дедлайну промежуточные точки прекращаются, но конечная
    координата ДОСТАВЛЯЕТСЯ отдельной командой. Без этого обрезанная
    траектория оставляла бы кусок там, где его оставил последний
    промежуточный move, и доводка считала бы неверную позицию.

    Возвращает число отправленных mouse.move (для телеметрии).
    """
    holds = set(hold_ranges or ())
    if not pts:
        return 0
    last = len(pts) - 1
    t0 = time.monotonic()
    sent = 0
    reached_last = False
    for i, (px, py) in enumerate(pts):
        if i == last:
            reached_last = True
        elif deadline and time.monotonic() > deadline:
            break
        page.mouse.move(px, py)
        sent += 1
        if i != last:
            time.sleep(random.uniform(0.045, 0.11) if i in holds
                       else random.uniform(*delay))
    if not reached_last:
        page.mouse.move(pts[last][0], pts[last][1])
        sent += 1
    _note_move_cost(sent, time.monotonic() - t0)
    return sent


def _human_drag(page, x0: float, y0: float, x1: float, y1: float,
                rng: random.Random | None = None, bound: float | None = None,
                deadline: float = 0.0):
    """Драг кусочка «человеком»: путь + события + паузы; вернёт shape.

    Число точек подбирается под остаток бюджета (deadline), а не только под
    случайность: подвиревшая вкладка не даёт доводке начаться. shape["n_sent"]
    — сколько команд реально ушло, чтобы по паспорту было видно обрезку.
    """
    rng = rng or random
    left = (deadline - time.monotonic()) if deadline else 0.0
    n = _fit_points(left, math.hypot(x1 - x0, y1 - y0),
                    rng.randint(14, PATH_MAX_POINTS))
    pts, shape = _human_drag_path(x0, y0, x1, y1, rng=rng, bound=bound, n=n)
    shape["n_sent"] = _human_move(page, pts, hold_ranges=shape.get("holds", ()),
                                  deadline=deadline)
    return shape


def _human_click(page, x: float, y: float, hold: float = 0.0,
                 deadline: float = 0.0):
    """Клик/нажим в точке с пред-подводом мыши (не телепорт в целевую).

    deadline передаётся в _human_move: подвод = 14 команд, и в подвиревшей
    вкладке он один стоит больше, чем весь калибровочный зонд.
    """
    px0, py0 = x - random.uniform(25, 55), y + random.uniform(-18, 18)
    page.mouse.move(px0, py0)
    time.sleep(random.uniform(0.05, 0.14))
    _human_move(page, _human_path(px0, py0, x, y, n=12, arc=2.0),
                deadline=deadline)
    time.sleep(random.uniform(0.03, 0.08))
    page.mouse.down()
    if hold > 0:
        time.sleep(hold)
