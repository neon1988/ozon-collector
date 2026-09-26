"""Чистый CV по кадрам пазла: силуэт, детекторы лунки, выбор кандидатов.

Ни одной зависимости от DOM/мыши/состояния страницы — на вход numpy-массивы
(живой кадр фона bg_l и/или CDN-кадр bg_cdn) и маска кусочка. Все пороги
живого формата (лунка рисуется оверлеем только в живом кадре) вынесены сюда.

Перенесено из puzzle.py без изменения кода (механический сплит).
"""

import io

import numpy as np


def _resample_lanczos():
    """Крос-версийный LANCZOS (новые Pillow — Image.Resampling.LANCZOS)."""
    from PIL import Image
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:
        return Image.LANCZOS
def _silhouette(piece_bytes: bytes, W: int, H: int) -> np.ndarray:
    """Альфа-маска кусочка (CDN PNG) → bool силуэт css-размера (W x H)."""
    from PIL import Image
    im = np.asarray(Image.open(io.BytesIO(piece_bytes)).convert("RGBA"))
    al = (im[..., 3].astype(np.float32) / 255.0 * 255.0).astype(np.uint8)
    m = np.asarray(Image.fromarray(al).resize(
        (int(round(W)), int(round(H))), _resample_lanczos())) > 60
    return m


def _sil_left(mask: np.ndarray) -> int:
    """Левый непрозрачный край силуэта (пикселей от левого края css-бокса).

    Кусок Ozon — прозрачный силуэт с маржей слева: img.left css-бокса НЕ
    совпадает с началом фигуры. Чтобы фигура встала в лунку, img.left должен
    быть на эту маржу МЕНЬШЕ левого края лунки (диагноз 2026-09-20: 34/36
    дропов сходились к детектору |err|≤0.9, но пазл не снят — img.left ставился
    равным грани лунки, а силуэт оказывался правее на silL).
    """
    if mask is None or getattr(mask, "size", 0) == 0:
        return 0
    mcol = mask.any(axis=0)
    if not mcol.any():
        return 0
    return int(np.argmax(mcol))


def _grip_point(mask) -> tuple:
    """Точка хвата ВНУТРИ непрозрачного силуэта (не геом. центр rect).

    Геометрический центр css-бокса часто попадает в прозрачную зону силуэта
    (бочонок/барель), и mousedown там не зацепляет кусок: кусок «не едет».
    Хватаем центр масс непрозрачных пикселей — он гарантированно в силаете.
    Возвращает (gx, gy) локально к маске (0..W, 0..H).
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return mask.shape[1] / 2.0, mask.shape[0] / 2.0
    gx = float(xs.mean())
    gy = float(ys.mean())
    # съём чуть вверх по вертикали (рука держит кусок за верхнюю половину)
    h = float(mask.shape[0])
    gy = min(max(0.35 * h, gy * 0.8), 0.8 * h)
    return gx, gy
def _detect_slot_topk(bg_l: np.ndarray, mask: np.ndarray, yrow: int, k: int = 3):
    """Топ-k кандидатов лунки по Варианту A (сортировка по score убыв.).

    yrow клампится в границы кадра (защита от сдвига после скролла).
    """
    h, w = mask.shape
    if yrow + h > bg_l.shape[0]:
        yrow = max(1, bg_l.shape[0] - h - 2)
    if w + 80 >= bg_l.shape[1] or h >= bg_l.shape[0]:
        return []
    gm = np.abs(np.gradient(bg_l, axis=0)) + np.abs(np.gradient(bg_l, axis=1))
    inner = np.zeros((h, w), dtype=bool)
    inner[4:h - 4, 4:w - 4] = ~mask[4:h - 4, 4:w - 4]
    sub = gm[yrow:yrow + h]
    scores = []
    for x in range(40, bg_l.shape[1] - w - 40):
        win = sub[:, x:x + w]
        eb = float(win[mask].mean()) if mask.any() else 0.0
        ib = float(win[inner].mean()) if inner.any() else 0.0
        scores.append((x, eb - ib))
    scores.sort(key=lambda t: -t[1])
    best = scores[0][1] if scores else None
    out = []
    for x, sc in scores[:k]:
        if sc is not None and (best is None or sc >= max(0.1, 0.6 * best)):
            out.append((x, sc))
    return out


# Новое поколение капчи (2026-09): лунка рисуется CSS-оверлеем поверх фона
# ТОЛЬКО в живом кадре (CDN-PNG её не содержит). Для плоских фонов (bg в
# основном ровный, std~5) градиентный Variant A слеп, а diff(shot, cdn)
# локализует лунку. Пороги по 53 живым кадрам 2026-09-13 (8 уверенных):
# уверенные пики имеют med<=23/ratio>=1.69; сорвавшиеся кадры (гонка скролла)
# дают med>=83 — отсекаются по _DIFF_MED_MAX.
_DIFF_RATIO_MIN = 1.6    # пик/медиана маск-взвеш. diff в полосе ряда
_DIFF_MED_MAX = 60.0     # медиана score-окон: выше = кадр не совпал с CDN
_DIFF_OVER_MULT = 1.6    # изолированность: ширина пика <= 1.6 * ширины кусочка
_DIFF_OVER_MAX = 0.6     # и <= 60% ширины кадра


def _diff_score_profile(bg_live, bg_cdn, mask, yrow):
    """Скоринг-профиль |live - cdn| по полосе yrow..yrow+h (маск-взвеш.).

    Возвращает (scores, med, best) или None при невалидных границах. diff
    бокс-блюрится по ширине (r=2), чтобы сгладить 1px-шум антиалиасинга.
    """
    h, w = mask.shape
    if yrow + h > bg_live.shape[0]:
        yrow = max(1, bg_live.shape[0] - h - 2)
    if (w + 80 >= bg_live.shape[1] or h >= bg_live.shape[0]
            or bg_live.shape != bg_cdn.shape):
        return None
    diff = np.abs(bg_live - bg_cdn)
    kern = np.ones(2, dtype=np.float32) / 2
    diff = np.pad(diff, ((0, 0), (0, 1)), mode="edge")
    diff = np.apply_along_axis(lambda v: np.convolve(v, kern, mode="same"), 1, diff)
    sub = diff[yrow:yrow + h]
    scores = [(x, float(sub[:, x:x + w][mask].mean()))
              for x in range(40, bg_live.shape[1] - w - 40)]
    if not scores:
        return None
    vals = np.array([s for _, s in scores])
    return scores, float(np.median(vals)), float(vals.max())


def _detect_slot_diff(bg_live: np.ndarray, bg_cdn: np.ndarray,
                      mask: np.ndarray, yrow: int, k: int = 3):
    """Топ-k лунки по diff живого кадра и CDN-фона (гонка скролла учтена).

    Возвращает уверенные (x, score) или []: если пик слабый/неизолированный
    или кадр не совпал с CDN (med-гейт) — тогда лунку локализует только
    фолбэк Variant A по CDN.

    Gated по 53 живым кадрам 2026-09-13: на уверенных кадрах пик
    изолированный (n_over<=1.6*w) и ratio>=1.69; на сорвавшихся (не тот
    регион страницы) med>=83 — детектор возвращает [].
    """
    out = _diff_score_profile(bg_live, bg_cdn, mask, yrow)
    if out is None:
        return []
    scores, med, best = out
    if med <= 1e-9 or med > _DIFF_MED_MAX:
        return []
    if best / med < _DIFF_RATIO_MIN:
        return []
    vals = np.array([s for _, s in scores])
    thr = best * 0.5
    over = int((vals >= thr).sum())
    if over > _DIFF_OVER_MULT * mask.shape[1] or over > _DIFF_OVER_MAX * bg_live.shape[1]:
        return []
    idx = np.argsort(vals)[::-1]
    return [(scores[i][0], float(vals[i])) for i in idx[:k]]


def _detect_slot_edges(bg_live: np.ndarray, bg_cdn: np.ndarray,
                       mask: np.ndarray, yrow: int, k: int = 3):
    """Лунка по колонному профилю diff |live-cdn| в полосе ряда кусочка.

    Маск-взвешенный _detect_slot_diff доминирует прозрачным силуэтом
    (среднее сдвигается к центру массы альфы и может отставать от реального
    левого края на ~90px — валидировано 2026-09-19: slot_diff дал x=332,
    тогда как колонная грань лунки на кадре стоит на x~240-247). Здесь
    считаем профиль среднего diff по СТОЛБЦАМ (не по маске) и ищем пару
    резких граней (левая + правая), ширина лунки ≈ ширине кусочка.

    Возвращает топ-k (x_top_left, score) — score = высота левой грани.
    """
    h, w = mask.shape
    if yrow + h > bg_live.shape[0]:
        yrow = max(1, bg_live.shape[0] - h - 2)
    if (w + 80 >= bg_live.shape[1] or h >= bg_live.shape[0]
            or bg_live.shape != bg_cdn.shape):
        return []
    diff = np.abs(bg_live - bg_cdn)
    kern = np.ones(2, dtype=np.float32) / 2
    diff = np.pad(diff, ((0, 0), (0, 1)), mode="edge")
    diff = np.apply_along_axis(lambda v: np.convolve(v, kern, mode="same"), 1, diff)
    sub = diff[yrow:yrow + h]
    col = sub.mean(axis=0)
    med = float(np.median(col[40:bg_live.shape[1] - 40]))
    if med <= 1e-9 or med > _DIFF_MED_MAX:
        return []
    peak = float(col.max())
    if peak / med < _DIFF_RATIO_MIN or peak <= 1e-6:
        return []
    thr = med + (peak - med) * 0.45
    strong = col >= thr
    idx = np.where(strong)[0]
    if len(idx) == 0:
        return []
    groups = []
    s = idx[0]
    for i in range(1, len(idx) + 1):
        if i == len(idx) or idx[i] > idx[i - 1] + 2:
            groups.append((s, idx[i - 1]))
            if i < len(idx):
                s = idx[i]
    cands = []
    # грани лунки узкие (бортик ~6-10px) на расстоянии ≈ ширины кусочка:
    # ищем пары групп (левая грань, правая грань), left-edge = начало левой.
    for gi, (gs, ge) in enumerate(groups):
        if ge - gs + 1 < 2 or ge - gs + 1 > w * 1.2:
            continue
        lo, hi = int(round(w * 0.55)), int(round(w * 1.55))
        right = None
        for (rs, re) in groups[gi + 1:]:
            gap = rs - ge
            if lo <= gap <= hi:
                right = (rs, re)
                break
            if gap > hi:
                break
        score = float(col[gs])
        if right is not None:
            # уверенная пара: сила обеих граней плюс правый край ~ левый+ширина
            score = float(col[gs]) + float(col[right[0]]) * 0.5
            score = round(score, 2)
            cands.append((int(gs), score))
            cands.append((int(right[0] - w), score * 0.7))
        else:
            cands.append((int(gs), round(score, 2)))
    cands.sort(key=lambda t: -t[1])
    out = []
    for x, sc in cands[:k]:
        if 1 <= x < bg_live.shape[1] - w - 1:
            out.append((x, sc))
    return out
def _solve_cands(mask, edge_cands, diff_cands, va_cands, silL):
    """Построить финальный порядок кандидатов лунки.

    Логика (вынесена из solve_puzzle для переиспользования в калибровке):
    edges скорректированы на silL (маржу куска), затем + diff + va (без
    дублей), дедуп ±1px по score, консенсус edges∩diff — в топ-1. СЫРЫЕ
    edges дописываются с весом 0.6: silL из CDN-маски ненадёжен (ловит
    выступ/тень вместо реальной маржи рендера), поэтому на кадрах с
    лживым silL raw-край остаётся доступным для микропоправок и top-3
    (валидировано 2026-09-23 на 15 truth-кадрах: top-1 9/15, top-3 11/15;
    без raw — top-3 10/15)."""
    edge_fixed = [(x - silL, sc) for x, sc in edge_cands
                  if x - silL >= 1]
    seen = {x for x, _ in edge_fixed}
    seen.update(x for x, _ in diff_cands)
    cands = edge_fixed + diff_cands + \
        [c for c in va_cands if c[0] not in seen]
    cands = cands + [(x, sc * 0.6) for x, sc in edge_cands if x >= 1]

    def _dedup1(lst):
        out = []
        for x, s in lst:
            if x < 1:
                continue
            merged = False
            for i, (ox, oscore) in enumerate(out):
                if abs(x - ox) <= 1:
                    if s > oscore + 1e-6 or (abs(s - oscore) <= 1e-6 and x < ox):
                        out[i] = (x, s)
                    merged = True
                    break
            if not merged:
                out.append((x, s))
        out.sort(key=lambda e: (-e[1], e[0]))
        return out

    cands_dedup = _dedup1(cands)

    try:
        exs = {x for x, _ in edge_cands}
        dxs = {x for x, _ in diff_cands}
        best_c = None
        for ex in exs:
            tx = ex - silL
            if tx < 1:
                continue
            for dx in dxs:
                if abs(tx - dx) <= 1:
                    best_c = (tx, max(s for x, s in edge_cands if abs(x - ex) < 0.1),
                              max(s for x, s in diff_cands if abs(x - dx) < 0.1))
                    break
            if best_c:
                break
        if best_c and cands_dedup:
            txb, _, _ = best_c
            cd2 = []
            for x, s in cands_dedup:
                if abs(x - txb) > 1:
                    cd2.append((x, s))
            cd2.insert(0, (txb, best_c[1] + best_c[2]))
            cands_dedup = cd2
    except Exception:
        pass
    return cands_dedup
