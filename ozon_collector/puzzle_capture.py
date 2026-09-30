"""Съёмка кадров: живой кадр фона с лункой + геометрия полей.

Лунка нарисована CSS-оверлеем и есть ТОЛЬКО в живом кадре, поэтому всё
живёт вокруг page.screenshot(clip=rect): прячем кусочек, снимаем элемент
фона, повторяем при несовпадении размеров И при неотрисованном (однотонном)
кадре. Причины неудач копятся в _shot_bg_reason() — их показывает solve_puzzle
в диагностике провала.

Перенесено из puzzle.py без изменения кода (механический сплит); позже
добавлены единая точка съёмки _shoot_rect и гейт _frame_painted.
"""

import io
import time
import urllib.request
from typing import Optional

import numpy as np

from ozon_collector import log

from .puzzle_dom import _capture_info

# Однотонный кадр = неотрисованный композитор (свёрнутое/минимизированное окно,
# гонка compositor). Скриншот при этом валиден (PNG декодируется, размеры верны),
# но std(grayscale)≈0: детекторы читают такой кадр как «лунки нет» и попытка
# тихо сгорает. Порог 3.0 — у реальных фонов капчи (фото) std десятки.
FLAT_STD = 3.0


def _fetch(url: str) -> io.BytesIO:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0"})
    return io.BytesIO(urllib.request.urlopen(req, timeout=15).read())
_SHOT_BG_REASON: list[str] = [""]
def _shot_bg_reason() -> str:
    return _SHOT_BG_REASON[0]


def _shoot_rect(page, hide: str, show: str, clip: dict,
                settle: float = 0.15) -> Optional[bytes]:
    """Скрыть кусочек → снять прямоугольник → вернуть кусочек на место.

    Единая точка съёмки для всех попыток _shot_bg_clean: раньте hide/sleep/
    screenshot/show был скопирован трижды, и во второй ветке ещё и без
    animations/timeout — расхождение параметров между попытками.
    """
    try:
        page.evaluate(hide)
        time.sleep(settle)
        return page.screenshot(clip=clip, timeout=5000,
                               animations="disabled")
    except Exception as e:
        log.debug(f"shot_rect failed clip={clip} settle={settle}: "
                  f"{type(e).__name__}: {e}")
        return None
    finally:
        try:
            page.evaluate(show)
        except Exception:
            pass


def _decode_gray(shot: Optional[bytes]) -> Optional[np.ndarray]:
    """PNG-байты → 2D grayscale float (None на битой картинке)."""
    if not shot:
        return None
    from PIL import Image
    try:
        return np.asarray(Image.open(io.BytesIO(shot)).convert("L")).astype(float)
    except Exception as e:
        log.debug(f"decode_gray failed bytes={len(shot)}: {type(e).__name__}: {e}")
        return None


def _frame_painted(bg_l: Optional[np.ndarray]) -> tuple[bool, str]:
    """Похож ли кадр на реальную картинку (не пусто/не однотонно)."""
    if bg_l is None or bg_l.size == 0:
        return False, "empty_frame"
    std = float(np.std(bg_l))
    if std < FLAT_STD:
        return False, f"flat_frame(std={std:.1f})"
    return True, f"std={std:.1f}"


def _bring_to_front(page, reason_box=None) -> bool:
    """Попытка поднять окно перед пересъёмкой однотонного кадра.

    Chromium под Windows отдаёт однотонную заливку (std≈0), если окно
    свёрнуто/закрыто другим окном: браузер отдаёт буфер, не рисуя кадр.
    Пассивная пересъёмка после паузы это не лечит, а вызывает новый таймаут и
    сжигает лимит попыток. Поэтому перед reshoot пробуем Page.bringToFront —
    это не «пробуем угадать», а убираем известную причину.

    best-effort: нет API/ошибка — продолжаем как раньше, причина пишется в
    reason_box, чтобы по логу было видно, что кадр был плоским из-за окна.
    """
    try:
        page.bring_to_front()
        time.sleep(0.25)
        if reason_box is not None:
            reason_box[0] += " front=ok"
        return True
    except Exception as e:
        if reason_box is not None:
            reason_box[0] += f" front_err={type(e).__name__}"
        else:
            log.debug(f"bring_to_front failed: {type(e).__name__}: {e}")
        return False


def _shot_bg_clean(page, info):
    """Скролл к капче, скрыть кусочек, снять чистый кадр фона (viewport clip).

    Возвращает dict {bg_l, bg_r, pz_r} (как в текущем DOM после скролла) или
    None. Кадр контролируется по размеру: если он меньше прямоугольника bg
    (элемент частично за пределами viewport) — повторный скролл+съёмка.

    Важно: clip-кадр берётся строго ПО АКТУАЛЬНОМУ bg_r (пересъёмка DOM прямо
    перед screenshot). Гонки скролла (scrollIntoView ещё «плывёт») дают кадр
    не с пазла, а с соседнего контента страницы — лечится стабилизацией
    координат: повторная съёмка rect до тех пор, пока позиция не устоится.
    """
    _SHOT_BG_REASON[0] = ""
    # стабилизация координат капчи (smooth-scroll может докатываться)
    try:
        for _stab in range(6):
            py0 = page.evaluate("""() => {
              const c=document.querySelector('#captcha')||document.querySelector('[class*=captcha]');
              if (c) c.scrollIntoView({block:'center', behavior:'instant'});
              const el=document.querySelector('#captcha img.image')||
                        [...document.querySelectorAll('#captcha img')].find(e=>/image/.test(e.className||''));
              return el? el.getBoundingClientRect().y : null; }""")
            if py0 is None:
                break
            time.sleep(0.25)
            py1 = page.evaluate("""() => {
              const el=document.querySelector('#captcha img.image')||
                        [...document.querySelectorAll('#captcha img')].find(e=>/image/.test(e.className||''));
              return el? el.getBoundingClientRect().y : null; }""")
            if py1 is None:
                break
            if abs(py1 - py0) < 1.5:
                break
        else:
            _SHOT_BG_REASON[0] += " scroll_unstable"
    except Exception as e:
        _SHOT_BG_REASON[0] = f"scroll_err={e!r}"
    info2 = _capture_info(page)
    bg = (info2.get("bg") or {}) if info2 else {}
    pz = (info2.get("puzzle") or {}) if info2 else {}
    if not bg.get("r") or not pz.get("r"):
        _SHOT_BG_REASON[0] = (f"no_elem bg={bool(bg.get('r'))} pz={bool(pz.get('r'))} "
                              f"after_scroll has={bool(info2 and info2.get('has'))}")
        return None
    hide = """() => { let n=0;
      document.querySelectorAll('img').forEach(el=>{
        if (/puzzle/.test(el.className||'')) { el.__v=el.style.visibility; el.style.visibility='hidden'; n++; } });
      return n; }"""
    show = """() => { document.querySelectorAll('img').forEach(el=>{
      if (/puzzle/.test(el.className||'')) el.style.visibility = el.__v||''; }); }"""
    # ждём полной загрузки картинки фона (иначе кадр снимается пустым белым
    # местом, а детектор лунки не находит ничего)
    try:
        page.evaluate("""() => {
          const img = [...document.querySelectorAll('#captcha img')]
            .find(el => /image/.test(el.className||''));
          if (img) { if (!img.complete) { img.loading='eager'; img.src=img.src; }
                     return [img.complete, img.naturalWidth, img.currentSrc]; }
          return null; }""")
        for _ in range(20):
            done = page.evaluate("""() => {
              const img = [...document.querySelectorAll('#captcha img')]
                .find(el => /image/.test(el.className||''));
              return img ? (img.complete && img.naturalWidth > 0) : false; }""")
            if done:
                break
            time.sleep(0.25)
        else:
            _SHOT_BG_REASON[0] += " bg_img_not_loaded"
    except Exception as e:
        _SHOT_BG_REASON[0] += f" imgwait_err={e!r}"
    x, y, w, h = bg["r"]
    _IP = lambda v: int(round(v))
    cx, cy, cw, ch = _IP(x), _IP(y), _IP(w), _IP(h)
    clip = {"x": cx, "y": cy, "width": cw, "height": ch}
    shot = _shoot_rect(page, hide, show, clip)
    if not shot:
        _SHOT_BG_REASON[0] = (f"shot1_none cls_bg={bg.get('r')} "
                              f"clip=({cx},{cy},{cw},{ch}) bytes=0")
        # гонка/пустой кадр — повторная съёмка через 0.5с (страница могла ещё грузиться)
        for _retry in range(2):
            shot = _shoot_rect(page, hide, show, clip, settle=0.5)
            if shot:
                _SHOT_BG_REASON[0] += f" retry{_retry + 1}=ok({len(shot)})"
                break
            _SHOT_BG_REASON[0] += f" retry{_retry + 1}=empty"
        if not shot:
            return None
    bg_l = _decode_gray(shot)
    if bg_l is None:
        _SHOT_BG_REASON[0] += " decode1_err"
        return None
    if (bg_l.shape[1] != cw or bg_l.shape[0] != ch) and abs(bg_l.shape[1] - cw) > 2:
        _SHOT_BG_REASON[0] = (f"size_mismatch got=({bg_l.shape[1]}x{bg_l.shape[0]}) "
                              f"want=({cw}x{ch})")
        # кадр меньше прямоугольника — кусок был за краем вьюпорта; повтор
        try:
            page.evaluate("""() => { const c=document.querySelector('#captcha');
              if(c) c.scrollIntoView({block:'center'}); }""")
        except Exception as e:
            _SHOT_BG_REASON[0] += f" scroll2_err={e!r}"
        shot = _shoot_rect(page, hide, show, clip, settle=0.4)
        bg_l = _decode_gray(shot)
        if bg_l is None:
            _SHOT_BG_REASON[0] += " shot2_none" if not shot else " decode2_err"
            return None
    # Кадр валиден, но может быть НЕОТРИСОВАННЫМ: свёрнутое/минимизированное
    # окно отдаёт однотонную заливку, которую детекторы читают как «лунки нет».
    # Одна пересъёмка с длинной паузой; если и она пустая — честный отказ.
    painted, why = _frame_painted(bg_l)
    if not painted:
        _SHOT_BG_REASON[0] += f" {why}"
        _bring_to_front(page, _SHOT_BG_REASON)
        shot = _shoot_rect(page, hide, show, clip, settle=0.6)
        bg_l = _decode_gray(shot)
        if bg_l is not None and (bg_l.shape[0] != ch
                                 or abs(bg_l.shape[1] - cw) > 2):
            bg_l = None
        if bg_l is None:
            _SHOT_BG_REASON[0] += " reshoot=empty"
            return None
        painted, why = _frame_painted(bg_l)
        _SHOT_BG_REASON[0] += f" reshoot={why}"
    if not painted:
        return None
    _SHOT_BG_REASON[0] += f" ok_sz=({bg_l.shape[1]}x{bg_l.shape[0]})"
    return {"bg_l": bg_l, "bg_r": list(bg["r"]), "pz_r": list(pz["r"]),
            "orig": shot}


def _shot_piece_margin(page, info) -> Optional[int]:
    """Замерить РЕНДЕРНУЮ маржу кусочка из живого кадра (левая прозрачная зона).

    CDN-PNG маржа (_sil_left) ненадёжна: браузер отрисовывает кусок через
    object-fit, из-за чего силуэт в css-боксе смещается относительно прямого
    resize PNG в бокс. Валидировано 2026-09-23: у 008 и 020 PNG-маски
    идентичны, а рендерные маржи разные (3 и 10) — маржу можно измерить
    только по живому кадру.

    Алгоритм: два скриншота по clip=rect кусочка — кусок виден и спрятан
    (visibility hidden). |diff| = силуэт куска в боксе; левый столбец, где
    diff значимо отличен от фона, в координатах css-бокса и есть render sil.

    Возвращает int (пикселей от левого края css-бокса кусочка) или None.
    """
    try:
        pz = (info or {}).get("puzzle") or {}
        r = pz.get("r")
        if len(r or ()) != 4 or r[2] < 8 or r[3] < 8:
            return None
        return _shot_piece_margin_rect(page, [int(round(v)) for v in r])
    except Exception:
        return None


def _shot_piece_margin_rect(page, rect) -> Optional[int]:
    """Съёмка кусочка по явному rect [x,y,w,h] (независимо от info)."""
    from PIL import Image
    # Прячем кусок по ТОМУ ЖЕ признаку, по которому его находит остальной код
    # (_read_sl_piece, _capture_info): id === 'puzzle' ИЛИ className ~ /puzzle/.
    # Раньше здесь матчился только className, и если живой кусок опознаётся по
    # id, hide() не прятал НИЧЕГО: diff был нулевой, mx<8, функция молча
    # возвращала None, а solve_puzzle откатывался на silL из CDN-альфы —
    # заведомо ненадёжную маржу. Именно это давало промах детектора: успехи
    # шли при марже 22, провалы при 12-15 (живой замер 2026-10-01).
    hide = """() => { let n=0;
        document.querySelectorAll('img').forEach(el=>{
          const c = String(el.className||'');
          if (el.id === 'puzzle' || /puzzle/.test(c)) {
            el.__v = el.style.visibility;
            el.style.visibility = 'hidden'; n++; } });
        return n; }"""
    show = """() => { document.querySelectorAll('img').forEach(el=>{
          const c = String(el.className||'');
          if (el.id === 'puzzle' || /puzzle/.test(c))
            el.style.visibility = el.__v||''; }); }"""
    do_shot = lambda: np.asarray(Image.open(io.BytesIO(page.screenshot(
        clip={"x": rect[0], "y": rect[1], "width": rect[2], "height": rect[3]},
        timeout=5000, animations="disabled"))).convert("L")).astype(float)
    vis = do_shot()
    try:
        page.evaluate(hide)
        time.sleep(0.15)
        clean = do_shot()
    except Exception:
        return None
    finally:
        try:
            page.evaluate(show)
        except Exception:
            pass
    if vis.shape != clean.shape or vis.shape[0] == 0 or vis.shape[1] == 0:
        return None
    d = np.abs(vis - clean)
    colmax = d.max(axis=0)
    mx = float(colmax.max())
    if mx < 8.0:
        return None
    thr = max(16.0, 0.30 * mx)
    nz = np.where(colmax > thr)[0]
    if not len(nz):
        return None
    return int(nz[0])
