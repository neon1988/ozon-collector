"""Авто-решение слайдер-пазла Ozon (анти-бот «Сопоставьте пазл»).

Интегрируется в check_challenge (collect.py): при слайдер-пазле НЕ перезагружаем
вслепую (FAB-reload), а решаем пазл: скачиваем фон и кусочек, находим выемку
сопоставлением шаблона (OpenCV matchTemplate) и перетаскиваем слайдер.

Процесс-локальный счётчик неудач (PUZZLE_FAILURES): если решатель не смог снять
пазл за PUZZLE_MAX_ATTEMPTS, шард об этом узнаёт (recovery.challenge_storm) —
оркестратор/демон понимает, что это не «голый» challenge, а нерешаемый пазл
(перезапуск/cooldown-логика отстраивается по дополнительному признаку).
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import time
import urllib.request
from datetime import datetime
from typing import Optional

import numpy as np

from ozon_collector import log

# Сколько попыток решения даёт воркер на один инцидент пазла, прежде чем
# признать его нерешаемым (fallback → FAB-reload → FSM).
PUZZLE_MAX_ATTEMPTS = 2

# Процесс-локал (list как изменяемый синглтон, чтобы reset() работал в тестах).
_PUZZLE_FAILURES: list[int] = [0]
# Статистика кадра фона в последнем решении (mean/std/max/min) — для диагностики.
_PBG_STATS: list = [None]


def reset() -> None:
    """Сбросить счётчик неудач (вызывается при старте сбора/воркера)."""
    _PUZZLE_FAILURES[0] = 0


def failures() -> int:
    """Число неудачных попыток решения пазла в текущем сборе."""
    return _PUZZLE_FAILURES[0]


# Куда писать пер-попыточный JSONL (машиночитаемо, для классификации
# fail-reason: detect/drag/token/UI). Диагностика, НЕ в репозиторий.
_ATTEMPT_LOG_DIR = os.environ.get("OZON_ATTEMPT_LOG",
                                  r"D:/Temp/opencode/puzzle_attempts")


def _attempt(kind: str, **kw) -> None:
    """Дописать строку JSONL с попыткой решения (тихая, без исключений)."""

    def _js_default(o):
        if isinstance(o, np.floating):
            return round(float(o), 3)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.bool_):
            return bool(o)
        return str(o)

    try:
        os.makedirs(_ATTEMPT_LOG_DIR, exist_ok=True)
        path = os.path.join(_ATTEMPT_LOG_DIR,
                            time.strftime("%Y%m%d") + ".jsonl")
        rec = {"ts": datetime.now().strftime("%H%M%S.%f")[:-3], "kind": kind}
        rec.update(kw)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False,
                               default=_js_default) + "\n")
    except Exception:
        pass


def is_puzzle(signature: Optional[str]) -> bool:
    """Сигнатура challenge указывает на слайдер-пазл (а не FAB-заглушку)."""
    return bool(signature and ("пазл" in signature or "ползунок" in signature))


def looks_like_puzzle(page) -> bool:
    """Есть ли на странице элементы слайдер-пазла (фон + кусочек) по DOM.

    Не полагается на текстовую сигнатуру title/body: если detect_challenge
    вернул общий анти-бот маркер (например 'antibot captcha'), но в DOM есть
    фон и кусочек — это пазл, который можно решить, а не глухой FAB-reload.
    """
    info = _capture_info(page)
    return bool(info and info.get("has"))


def _find_notch(bg: np.ndarray, piece: np.ndarray) -> Optional[int]:
    """x-top-left выемки в фоне (в пикселях картинки) или None.

    Кусочек Ozon-пазла — уменьшенная копия области фона: template matching
    по краям даёт пик совпадения в правильной позиции.
    """
    bx, _conf = _notch_match(bg, piece)
    return bx


def _notch_match(bg: np.ndarray, piece: np.ndarray) -> (Optional[int], float):
    """Классический поиск выемки template-matching'ом.

    Возвращает (x_top_left или None, корреляция максимума). Корреляция ~1 —
    выемка почти наверняка найдена (старый формат: кусочек = копия области
    фона). Слабая корреляция (новый формат: фон инпейнчен/под «порт-холом»)
    — выемку ищет _find_notch_v2.
    """
    try:
        import cv2
    except Exception:
        return None, 0.0
    if bg.ndim != 2 and bg.ndim != 3:
        return None, 0.0
    bg_g = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY) if bg.ndim == 3 else bg
    p_g = cv2.cvtColor(piece, cv2.COLOR_RGB2GRAY) if piece.ndim == 3 else piece
    if bg_g.shape[0] < p_g.shape[0] or bg_g.shape[1] < p_g.shape[1]:
        return None, 0.0
    res = cv2.matchTemplate(bg_g, p_g, cv2.TM_CCOEFF_NORMED)
    _minv, maxv, _minl, maxloc = cv2.minMaxLoc(res)
    return int(maxloc[0]), float(maxv)


def _find_notch_v2(bg: np.ndarray, piece: np.ndarray, slot_y: float) -> Optional[int]:
    """Поиск выемки в новом формате (кусочек — прозрачный силуэт-вырезка).

    В новом формате содержимое кусочка в фоне замазано/убрано, поэтому
    template-matching не работает. Выемка видна как «порт-хол»: под прозрачной
    её частью однотонный равномерный участок на фоне фактурной фотографии.

    slot_y — вертикаль центра выемки в координатах картинки фона (по геометрии
    DOM вертикаль кусочка не меняется при драге). Ищем горизонталь с минимальной
    дисперсией внутри силуэта кусочка; результат принимаем, только если окно
    резко однороднее окружающей текстуры (иначе None — сигнал слабый).
    """
    try:
        import cv2
    except Exception:
        return None
    if bg.ndim != 2 and bg.ndim != 3 or piece.ndim not in (2, 3):
        return None
    bgray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY).astype(np.float32) if bg.ndim == 3 else bg.astype(np.float32)
    if piece.ndim == 3:
        sil = (piece.max(axis=2) > 40).astype(np.uint8)
    else:
        sil = (piece > 40).astype(np.uint8)
    h, w = sil.shape
    if h >= bgray.shape[0] or w >= bgray.shape[1]:
        return None
    # силуэт почти сплошной (>=92% пикселей непрозрачны) — старый формат,
    # «порт-хол» тут не ищем
    if sil.mean() >= 0.92:
        return None
    inner = sil > 0
    if inner.sum() < 16:
        return None
    y = int(round(slot_y - h / 2))
    y = max(0, min(bgray.shape[0] - h, y))
    vars_, xs = [], []
    for x in range(0, bgray.shape[1] - w + 1):
        roi = bgray[y:y + h, x:x + w]
        vals = roi[inner]
        vars_.append(float(vals.std()))
        xs.append(x + w // 2)
    if not vars_:
        return None
    vars_ = np.asarray(vars_)
    i_min = int(np.argmin(vars_))
    stdev_min = float(vars_[i_min])
    lo, p50 = float(np.percentile(vars_, 5)), float(np.percentile(vars_, 50))
    span = max(1e-6, float(np.percentile(vars_, 90)) - lo)
    sharp = 1.0 - (stdev_min - lo) / span if span > 1e-6 else 1.0
    if stdev_min < 4.0 and p50 > 8.0 and sharp >= 0.55:
        return int(xs[i_min] - w // 2)
    return None


def _fetch(url: str) -> io.BytesIO:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0"})
    return io.BytesIO(urllib.request.urlopen(req, timeout=15).read())


def _capture_info(page) -> Optional[dict]:
    """Извлечь элементы пазла (фон, кусочек, слайдер) из DOM страницы."""
    try:
        return page.evaluate(
            """() => {
              const out = {bg:null, puzzle:null, slider:null, has:false};
              document.querySelectorAll('img').forEach(el => {
                const r = el.getBoundingClientRect();
                const c = el.className || '';
                if (c.includes('image') && r.width > 200)
                  out.bg = {src: el.currentSrc||el.src,
                            r:[r.x,r.y,r.width,r.height]};
                if (c.includes('puzzle'))
                  out.puzzle = {src: el.currentSrc||el.src,
                                r:[r.x,r.y,r.width,r.height]};
              });
              document.querySelectorAll(
                '[class*=slider],[class*=slide],[class*=handler],[class*=track],[class*=drag],[class*=ckbox]'
              ).forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 30 && r.height > 15)
                  out.slider = {r:[r.x,r.y,r.width,r.height]};
              });
              out.has = !!(out.bg && out.puzzle);
              return out;
            }""")
    except Exception:
        return None


def _still_challenge(page) -> bool:
    """Остался ли на странице ЛЮБОЙ анти-бот челлендж (пазл, FAB, VPN-блок).

    Не считаем «решённым», если после драга страница оказалась другой
    заглушкой (например, 'выключите vpn'): пазл исчез, но выдача не открыта —
    это НЕ успех, а подмена челленджа.
    """
    if looks_like_puzzle(page):
        return True
    from .collect import detect_challenge
    return bool(detect_challenge(page))


def _find_slider(page) -> Optional[dict]:
    """Найти ручку и трек слайдера по геометрии (у Ozon классы пустые).

    Возвращает {track:[x,y,w,h], handle:[x,y,w,h]} в экранных px или None.
    Трек — самый широкий тонкий DIV ниже фона; ручка — DIV ~40x32 у левого
    края трека (содержит svg-иконку).
    """
    try:
        return page.evaluate(
            """() => {
              let bg = null;
              [...document.querySelectorAll('img')].forEach(i => {
                const r = i.getBoundingClientRect();
                const c = String(i.className||'');
                if (c.includes('image') && r.width>200) bg = r;
              });
              if (!bg) return null;
              const all = [...document.querySelectorAll('div')];
              const belowY = bg.y + bg.height;
              let track=null, handle=null;
              all.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width<=0 || r.height<=0) return;
                if (r.y >= belowY-5 && r.y < belowY+200
                    && r.width>=300 && r.height<=6)
                  if (!track || r.width>track)
                    track=[r.x, r.y, r.width, r.height];
              });
              if (!track) return null;
              const ty = track[1] + track[3]/2;
              all.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width<=0 || r.height<=0) return;
                const cy = r.y + r.height/2;
                if (Math.abs(cy-ty) < 30
                    && Math.abs(r.width-40) <= 15
                    && Math.abs(r.height-32) <= 18)
                  if (!handle || r.x < handle[0])
                    handle=[r.x, r.y, r.width, r.height];
              });
              if (!handle) return null;
              return {track: track, handle: handle};
            }""")
    except Exception:
        return None


def _log_slider(page) -> None:
    """Резервная диагностика структуры слайдера (debug-уровень)."""
    try:
        _probe = page.evaluate("""() => {
              const rect = el => { const r=el.getBoundingClientRect();
                return [Math.round(r.x),Math.round(r.y),
                        Math.round(r.width),Math.round(r.height)]; };
              const puz=[...document.querySelectorAll('img')]
                    .find(i=>(i.className||'').includes('puzzle'));
              if (!puz) return {error:'no puzzle img'};
              const ancSet=[];
              let anc=puz.parentElement;
              for (let d=0; d<5 && anc; d++) {
                const kids=[];
                [...anc.querySelectorAll('*')].forEach(el=>{
                  const r=el.getBoundingClientRect();
                  if (r.width>0 && r.height>0)
                    kids.push({cls:String(el.className||'').slice(0,40),
                               tag:el.tagName, r:rect(el)});
                });
                ancSet.push({parent_cls:String(anc.className||'').slice(0,60),
                             parent_r:rect(anc), kids:kids.slice(0,12)});
                anc=anc.parentElement;
              }
              return {puzzle_rect:rect(puz), chain:ancSet};
            }""")
        log.debug(f"slider_probe={_probe}")
    except Exception as e:
        log.debug(f"slider_probe_err={e!r}")


"""Живой метод решения (валидирован на живых капчах, новое поколение пазлов):

Кусочек НЕ является вырезом фона (template-match мёртв), а выемка-лунка
нарисована ДОМ/CSS-оверлеем ПОВЕРХ фона — на CDN-PNG её нет, она видна только
в реальном кадре страницы. Поэтому:
  1) прячем кусочек (visibility hidden) и снимаем именно элемент фона
     через page.screenshot(clip=rect bg) — кадр содержит лунку;
  2) силуэт кусочка = альфа его CDN-картинки, ресайз к css-боксу;
  3) детектор ВариаНТ A по строке кусочка (дорога горизонтальная,
     top кусочка == top лунки): argmax(mean(gm по силуэту) - mean(gm по
     внутреннему прямоугольнику фона вне силуэта));
  4) цель: viewport_x кусочка = bg.x + det_x;
  5) k-калибровка драгом +40px (кусок отстаёт от мыши: k 0.4..3), затем
     доводка до |err|<=2px и отпускание.

Пороговые константы взяты из живой прогонки 2026-09-08 (12 авто-решений).
"""


_SHOT_BG_REASON: list[str] = [""]


def _shot_bg_reason() -> str:
    return _SHOT_BG_REASON[0]


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
    shot = None
    try:
        page.evaluate(hide)
        time.sleep(0.15)
        shot = page.screenshot(clip={"x": cx, "y": cy, "width": cw, "height": ch},
                               timeout=15000, animations="disabled")
    except Exception as e:
        _SHOT_BG_REASON[0] = f"shot1_err={e!r}"
        shot = None
    finally:
        try:
            page.evaluate(show)
        except Exception:
            pass
    if not shot:
        _SHOT_BG_REASON[0] = (f"shot1_none cls_bg={bg.get('r')} "
                              f"clip=({cx},{cy},{cw},{ch}) bytes={len(shot) if shot else 0}")
        # гонка/пустой кадр — повторная съёмка через 0.5с (страница могла ещё грузиться)
        for _retry in range(2):
            try:
                page.evaluate(hide)
                time.sleep(0.5)
                shot = page.screenshot(clip={"x": cx, "y": cy, "width": cw, "height": ch},
                                       timeout=15000, animations="disabled")
            except Exception:
                shot = None
            finally:
                try:
                    page.evaluate(show)
                except Exception:
                    pass
            if shot:
                _SHOT_BG_REASON[0] += f" retry{_retry + 1}=ok({len(shot)})"
                break
            _SHOT_BG_REASON[0] += f" retry{_retry + 1}=empty"
        if not shot:
            return None
    from PIL import Image
    try:
        bg_l = np.asarray(Image.open(io.BytesIO(shot)).convert("L")).astype(float)
    except Exception as e:
        _SHOT_BG_REASON[0] = f"decode1_err={e!r}"
        return None
    if (bg_l.shape[1] != cw or bg_l.shape[0] != ch) and abs(bg_l.shape[1] - cw) > 2:
        _SHOT_BG_REASON[0] = (f"size_mismatch got=({bg_l.shape[1]}x{bg_l.shape[0]}) "
                              f"want=({cw}x{ch})")
        # кадр меньше прямоугольника — кусок был за краем вьюпорта; повтор
        try:
            page.evaluate("""() => { const c=document.querySelector('#captcha');
              if(c) c.scrollIntoView({block:'center'}); }""")
            time.sleep(0.4)
            page.evaluate(hide)
            time.sleep(0.15)
            shot = page.screenshot(clip={"x": cx, "y": cy, "width": cw, "height": ch})
        except Exception as e:
            _SHOT_BG_REASON[0] += f" shot2_err={e!r}"
            shot = None
        finally:
            try:
                page.evaluate(show)
            except Exception:
                pass
        if not shot:
            _SHOT_BG_REASON[0] += " shot2_none"
            return None
        try:
            bg_l = np.asarray(Image.open(io.BytesIO(shot)).convert("L")).astype(float)
        except Exception as e:
            _SHOT_BG_REASON[0] += f" decode2_err={e!r}"
            return None
    _SHOT_BG_REASON[0] += f" ok_sz=({bg_l.shape[1]}x{bg_l.shape[0]})"
    return {"bg_l": bg_l, "bg_r": list(bg["r"]), "pz_r": list(pz["r"]),
            "orig": shot}


def _silhouette(piece_bytes: bytes, W: int, H: int) -> np.ndarray:
    """Альфа-маска кусочка (CDN PNG) → bool силуэт css-размера (W x H)."""
    from PIL import Image
    im = np.asarray(Image.open(io.BytesIO(piece_bytes)).convert("RGBA"))
    al = (im[..., 3].astype(np.float32) / 255.0 * 255.0).astype(np.uint8)
    m = np.asarray(Image.fromarray(al).resize(
        (int(round(W)), int(round(H))), Image.LANCZOS)) > 60
    return m


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


def _human_move(page, pts, delay=(0.008, 0.02)):
    """Эмитировать траекторию из _human_path реальными событиями мыши.

    Каждая точка — отдельный mouse.move (КДП-событие), между точками
    случайная пауза (дрожание руки). Последняя точка фиксируется без
    паузы после неё (потом down/up мгновенно от целевой позиции).
    """
    last = len(pts) - 1
    for i, (px, py) in enumerate(pts):
        page.mouse.move(px, py)
        if i != last:
            time.sleep(random.uniform(*delay))


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


def _detect_slot(bg_l: np.ndarray, mask: np.ndarray, yrow: int):
    """Вариант A: выемка по строке кусочка. Возвращает (x_top_left, score)."""
    h, w = mask.shape
    if yrow + h > bg_l.shape[0] or w + 40 >= bg_l.shape[1] or yrow < 0:
        return None, None
    gm = np.abs(np.gradient(bg_l, axis=0)) + np.abs(np.gradient(bg_l, axis=1))
    inner = np.zeros((h, w), dtype=bool)
    inner[4:h - 4, 4:w - 4] = ~mask[4:h - 4, 4:w - 4]
    sub = gm[yrow:yrow + h]
    best_x, best = None, None
    for x in range(40, bg_l.shape[1] - w - 40):
        win = sub[:, x:x + w]
        eb = float(win[mask].mean()) if mask.any() else 0.0
        ib = float(win[inner].mean()) if inner.any() else 0.0
        sc = eb - ib
        if best is None or sc > best:
            best, best_x = sc, x
    return best_x, best


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


def _read_sl_piece(page):
    """(slider.x, piece.viewport_x) или None.

    Позиция слайдера читается из getBoundingClientRect (надёжно при любой
    реализации: css-класс, transform или inline style). Раньше читался
    style.left — при transform/классной вёрстке он пуст (NaN) и вся
    k-калибровка слайдера падала (диагноз 2026-09-17: систематический
    all_cands_failed при живом слайдере)."""
    try:
        return page.evaluate("""() => {
          const cap=document.querySelector('#captcha');
          const pzz=cap? document.querySelectorAll('#captcha img'):[];
          let px=null, ps=null;
          [...pzz].forEach(el=>{ if (/puzzle/.test(el.className||'')){
            const r=el.getBoundingClientRect(); px=r.x;
            const v=parseFloat(el.style.left); if(!isNaN(v)) ps=v; } });
          let s=null, ss=null;
          const sl=document.querySelector('#slider');
          if (sl) { const r=sl.getBoundingClientRect();
                    if (r.width>0 && r.height>0) s=r.x;
                    const v=parseFloat(sl.style.left); if(!isNaN(v)) ss=v; }
          return {s, px, ps, ss};
        }""")
    except Exception:
        return None


def _drag_to(page, target_vx: float, info, bx, score,
             grip: Optional[tuple] = None) -> bool:
    """Драг кусочка к target_vx с жёсткой обратной связью по реальному px.

    Самое слабое звено живой реализации — точность попадания: если детектор
    дал верную лунку, но кусочек не встал в слот, пазл остаётся (валидировано
    2026-09-17: один и тот же пазл решён в 13:16 и не решён в 18:58 при том же
    кандидате). Здесь на каждой микро-коррекции читается фактическое position
    кусочка (getBoundingClientRect), и шаг строится не по модели, а по реальной
    ошибке; финальный вердикт — «challenge снят» (_still_challenge == False),
    а не «кусочек исчез»: после неудачного дропа DOM пазла часто пересобирается
    (ложное «gone»), поэтому решает только фактическое снятие challenge.

    Новое поколение (нет трека/слайдера в DOM): перетаскиваем САМ кусочек
    (img.puzzle). Хват — в точке внутри силуэта (_grip_point), иначе mousedown
    в прозрачной зоне бочонка «не везёт» кусок. Вертикаль держим исходную.
    Все перемещения мыши — человеческие траектории (_human_path/_human_move),
    анти-бот палит равномерные линейные move (телепорт + идеальная прямая).
    """
    ACC = 0.9
    try:
        page.evaluate("""() => {
          const c=document.querySelector('#captcha');
          if (c) c.scrollIntoView({block:'center', behavior:'instant'});
          return true; }""")
    except Exception:
        pass
    sl = _find_slider(page)
    bg_r = (info.get("bg") or {}).get("r")
    pz_r = (info.get("puzzle") or {}).get("r")
    if sl:
        track, handle = sl["track"], sl["handle"]
        max_hx = track[0] + track[2] - handle[2] - 1
        gx = handle[0] + handle[2] / 2.0
        gy = handle[1] + handle[3] / 2.0
        grip_mode = "slider"
    elif bg_r and pz_r and len(bg_r) == 4 and len(pz_r) == 4:
        # новый формат: тянем сам кусочек; трек = прямоугольник фона
        track = list(bg_r)
        max_hx = track[0] + track[2] - pz_r[2] - 1
        gx = pz_r[0] + pz_r[2] / 2.0
        gy = pz_r[1] + pz_r[3] / 2.0
        # сместим хват в непрозрачную зону силуэта (центр бокса часто прозрачный)
        if grip:
            ogx, ogy = grip
            gx = pz_r[0] + ogx
            gy = pz_r[1] + ogy
        grip_mode = "piece"
    else:
        _attempt("drag", note="no_anchor", bx=bx, score=score,
                 target_vx=round(target_vx, 1))
        return False
    log.debug(f"drag start target_vx={target_vx:.1f} bx={bx} sl={sl} "
              f"grip={grip_mode} gx={gx:.1f} gy={gy:.1f} max_hx={max_hx:.1f} "
              f"bg_r={bg_r} pz_r={pz_r}")
    rng_x = random.uniform(-6, 6)
    last = {}
    for _try in range(3):
        r0 = _read_sl_piece(page)
        log.debug(f"drag try={_try} gx={gx:.1f} gy={gy:.1f} r0={r0}")
        if not r0 or r0.get("px") is None:
            _attempt("drag", note="no_piece", bx=bx, score=score,
                     target_vx=round(target_vx, 1))
            return True  # кусочка уже нет — пазл снят
        # подвод мыши к точке хвата (визуально-естественно) затем нажим
        _human_click(page, gx + rng_x, gy, hold=random.uniform(0.06, 0.16))
        time.sleep(random.uniform(0.05, 0.12))
        # калибровочный сдвиг +40px человеческой траекторией
        _human_move(page, _human_path(gx + rng_x, gy, gx + 40 + rng_x, gy,
                                      n=16, arc=3.0))
        time.sleep(random.uniform(0.1, 0.22))
        r1 = _read_sl_piece(page)
        k = None
        if r1 and r1.get("px") is not None:
            dpx = (r1.get("px") or 0) - (r0.get("px") or 0)
            if abs(dpx) <= 1.0:
                k = None  # кусочек не поехал — хват не удался, retry
            else:
                if grip_mode == "slider":
                    dsl = (r1.get("s") or 0) - (r0.get("s") or 0)
                    k = dpx / dsl if dsl > 0.01 else 1.0
                else:
                    k = dpx / 40.0  # тянем кусок мышью 1:1 (движение мыши == 40px)
        log.debug(f"drag r1={r1} dpx={(r1.get('px') - r0.get('px')) if r1 else None} "
                  f"k={k}")
        if k is None or not (0.4 <= k <= 3.0):
            page.mouse.up()
            time.sleep(random.uniform(0.3, 0.5))
            _attempt("drag", note="grip_fail", bx=bx, score=score,
                     target_vx=round(target_vx, 1), k=k, grip=grip_mode)
            rng_x = random.uniform(-9, 9)
            continue  # хват не удался — пробуем другую точку
        px_now = r1.get("px") or r0.get("px")
        err = px_now - target_vx
        if abs(err) <= ACC:
            time.sleep(random.uniform(0.15, 0.3))
            page.mouse.up()
            time.sleep(1.5)
            _solved = not _still_challenge(page)
            _attempt("drag", note="acc_ok", bx=bx, score=score,
                     target_vx=round(target_vx, 1), k=k, settled=True,
                     solved=_solved, grip=grip_mode)
            return _solved
        # большой сдвиг в ожидаемую позицию (остаточная ошибка докручивается)
        d_sl = -err / k
        dest = max(track[0] + 1, min(max_hx, gx + 40 + d_sl))
        log.debug(f"drag to dest={dest:.1f} (err={err:.1f} d_sl={d_sl:.1f})")
        _human_move(page, _human_path(gx + 40 + rng_x, gy, dest, gy,
                                      n=28, arc=6.0))
        time.sleep(random.uniform(0.15, 0.3))
        cur = dest
        settled = False
        iters = 0
        for _i in range(18):
            iters = _i + 1
            rr = _read_sl_piece(page)
            if not rr or rr.get("px") is None:
                log.debug(f"drag i={_i} piece gone")
                settled = True
                break
            last = rr
            err = (rr.get("px") or 0) - target_vx
            log.debug(f"drag i={_i} px={rr.get('px')} s={rr.get('s')} err={err:+.2f}")
            if abs(err) <= ACC:
                settled = True
                break
            step_px = -err / k if k else -err
            step_px = max(-12.0, min(12.0, step_px))
            _dest = max(track[0] + 1, min(max_hx, cur + step_px))
            if abs(_dest - cur) < 0.3:
                _dest = cur  # больше не двигаем — мертвая зона
            _human_move(page, _human_path(cur, gy, _dest, gy,
                                          n=10, arc=1.2))
            time.sleep(random.uniform(0.1, 0.24))
            cur = _dest
        log.debug(f"drag settle={settled} cur={cur:.1f}")
        time.sleep(random.uniform(0.25, 0.4))
        page.mouse.up()
        time.sleep(1.5)
        _solved = not _still_challenge(page)
        _err_last = None
        if last.get("px") is not None:
            _err_last = round((last.get("px") or 0) - target_vx, 1)
        _attempt("drag", note="settle_end", bx=bx, score=score,
                 target_vx=round(target_vx, 1), k=k, settled=settled,
                 solved=_solved, err=_err_last, cur=round(cur, 1),
                 iters=iters, px=last.get("px"), s=last.get("s"),
                 ps=last.get("ps"), ss=last.get("ss"), grip=grip_mode)
        return _solved
    try:
        page.mouse.up()
    except Exception:
        pass
    _attempt("drag", note="grip_exhausted", bx=bx, score=score,
             target_vx=round(target_vx, 1))
    return False


def _piece_grip_offset(page):
    """Смещение хвата внутри силуэта кусочка (локально к css-боксу)."""
    try:
        return page.evaluate("""() => {
          const cap=document.querySelector('#captcha');
          const img=cap? [...cap.querySelectorAll('img')].find(
            el=>/puzzle/.test(el.className||'')) : null;
          if (!img) return null;
          const r=img.getBoundingClientRect();
          if (r.width<=0) return null;
          const can=document.createElement('canvas');
          can.width=Math.max(1,Math.floor(r.width));
          can.height=Math.max(1,Math.floor(r.height));
          const g=can.getContext('2d');
          try { g.drawImage(img,0,0,can.width,can.height); }
          catch(e){ return null; }
          let d;
          try { d=g.getImageData(0,0,can.width,can.height).data; }
          catch(e){ return null; }
          let sx=0, sy=0, n=0;
          for(let i=0;i<d.length;i+=4){
            if(d[i+3]>80){ sx+= (i/4)%can.width; sy+= Math.floor((i/4)/can.width); n++; }
          }
          if(!n) return null;
          return [sx/n, Math.min(can.height*0.8, Math.max(can.height*0.35, (sy/n)*0.85))];
        }""")
    except Exception:
        return None


def _puzzle_piece_gone(page) -> bool:
    """Кусочек пазла исчез из DOM (снят) — True; иначе False."""
    try:
        rr = _read_sl_piece(page)
    except Exception:
        return False
    if not rr:
        return False
    return rr.get("px") is None


def _solve_classic(page, info) -> Optional[bool]:
    """Старый fallback: CDN-картинки + template-match (для старых форматов)."""
    try:
        from PIL import Image
        bsrc = (info.get("bg") or {}).get("src")
        psrc = (info.get("puzzle") or {}).get("src")
        b_r = (info.get("bg") or {}).get("r")
        p_r = (info.get("puzzle") or {}).get("r")
        if not (bsrc and psrc and b_r and p_r):
            return None
        bg = np.asarray(Image.open(_fetch(bsrc)).convert("RGB"))
        piece = np.asarray(Image.open(_fetch(psrc)).convert("RGB"))
        bx = _find_notch(bg, piece)
        if bx is None:
            return None
        b_x0, _b_y0, b_w, _b_h = b_r
        scale_x = b_w / bg.shape[1]
        dx_px = b_x0 + bx * scale_x - p_r[0]
        sl = _find_slider(page)
        if not sl:
            return None
        track, handle = sl["track"], sl["handle"]
        track_x0, _ty, track_w, _th = track
        hx, hy, hw, hh = handle
        usable = max(1, track_w - hw)
        target_left = min(track_x0 + usable, max(track_x0, hx + dx_px))
        grab_x = hx + min(6, max(1, hw * 0.2))
        grab_y = hy + hh / 2
        page.mouse.move(grab_x, grab_y)
        page.mouse.down()
        page.mouse.move(grab_x + (target_left - hx), grab_y, steps=30)
        time.sleep(0.4)
        page.mouse.up()
        return True
    except Exception:
        return None


def _solve_diag(page, info, stage: str, detail: str = "", bg=None, piece=None,
                bg_stats=None, shot=None, fullshot=None) -> None:
    """Сохранить диагностику НЕУДАЧИ решателя в D:/Temp/opencode/puzzle_diag/.

    Одноразовый отладочный хук (проблема «пазл не решается» 2026-09-07):
    сохраняет текущий DOM-срез (все img + элементы slider/puzzle/track),
    найденные bg/piece-картинки (np.save), статистику кадра фона (bg_stats),
    сырой PNG кадра (shot), полный скриншот вьюпорта (fullshot) и стадию
    отказа. Никак не влияет на поведение (внутренний try/except, только
    запись файлов).
    """
    try:
        import json
        import os
        d = os.path.join("D:/Temp/opencode", "puzzle_diag")
        os.makedirs(d, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        rec = {"stage": stage, "detail": detail, "ts": ts}
        if bg_stats is not None:
            rec["bg_stats"] = {
                "mean": float(bg_stats[0]), "std": float(bg_stats[1]),
                "max": float(bg_stats[2]), "min": float(bg_stats[3]),
            }
        if shot:
            try:
                with open(os.path.join(d, f"{ts}_shot.png"), "wb") as f:
                    f.write(shot)
                rec["shot_png"] = f"{ts}_shot.png"
            except Exception as e:
                rec["shot_err"] = str(e)
        if fullshot:
            try:
                with open(os.path.join(d, f"{ts}_full.png"), "wb") as f:
                    f.write(fullshot)
                rec["full_png"] = f"{ts}_full.png"
            except Exception as e:
                rec["full_err"] = str(e)
        try:
            rec["info"] = info
        except Exception:
            pass
        try:
            dom = page.evaluate(
                """() => {
                  const out=[];
                  document.querySelectorAll('img,div,button').forEach(el=>{
                    const r=el.getBoundingClientRect();
                    if (r.width<=0 && r.height<=0) return;
                    const tag=el.tagName;
                    const c=String(el.className||'');
                    if (tag!=='IMG' &&
                        !/image|puzzle|slider|slide|handler|track|drag|ckbox/.test(c)) return;
                    out.push({
                      tag, cls:c.slice(0,80),
                      r:[Math.round(r.x),Math.round(r.y),
                         Math.round(r.width),Math.round(r.height)],
                      src:(el.currentSrc||el.src||'').slice(0,120)
                    });
                  });
                  return out;
                }""")
            rec["dom"] = dom
        except Exception as e:
            rec["dom_err"] = str(e)
        for name, arr in (("bg", bg), ("piece", piece)):
            if arr is not None:
                try:
                    np.save(os.path.join(d, f"{ts}_{name}.npy"), arr)
                    rec[f"{name}_shape"] = list(arr.shape)
                except Exception as e:
                    rec[f"{name}_err"] = str(e)
        with open(os.path.join(d, f"{ts}.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, indent=2, default=str))
    except Exception:
        pass


def _bg_from_cdn(info):
    """Фоновый кадр из CDN-картинки (без скриншота окна).

    Скриншот-путь страдает гонкой скролла/свёрнутым окном: кадр снимается не
    с пазла, а с соседнего контента (diff с CDN 75+ по всему кадру). Детекция
    по CDN src напрямую не зависит от отрисовки страницы.

    Возвращает dict {bg_l, bg_r, pz_r} или None. bg_l ресайзится под
    CSS-размер bg_r[2]x[3], поэтому координаты лунки сразу в экранных px.
    """
    try:
        bg = info.get("bg") or {}
        pz = info.get("puzzle") or {}
        if not bg.get("src") or not pz.get("src"):
            return None
        b_r, p_r = bg["r"], pz["r"]
        if len(b_r) != 4 or len(p_r) != 4 or b_r[2] <= 0 or b_r[3] <= 0:
            return None
        from PIL import Image
        bw, bh = int(round(b_r[2])), int(round(b_r[3]))
        im = Image.open(_fetch(bg["src"])).convert("RGBA")
        bg_l = np.asarray(im.resize((bw, bh), Image.LANCZOS).convert("L")).astype(float)
        return {"bg_l": bg_l, "bg_r": b_r, "pz_r": p_r}
    except Exception:
        return None


def solve_puzzle(page, attempts: int = PUZZLE_MAX_ATTEMPTS) -> bool:
    """Решить слайдер-пазл на странице. True — пазл снят.

    Живой метод: прятки кусочка + силуэтный детектор Вариант A по кадру фона.
    Фон сначала берётся из CDN src (надёжно, независимо от скролла/свёрнутого
    окна), при неудаче — фолбэк на скриншот окна. При любой ошибке (нет DOM,
    нет картинок, не нашлась выемка, обрыв CDP) — False. Счётчик неудач растёт
    за каждый неудачный заход.
    """
    for _ in range(max(1, int(attempts))):
        try:
            info = _capture_info(page)
        except Exception:
            _PUZZLE_FAILURES[0] += 1
            return False
        if not info or not info.get("bg") or not info.get("puzzle"):
            # пазла нет (уже снят / это не пазл) — считаем успехом
            return not _still_challenge(page) or False
        try:
            psrc = (info["puzzle"] or {}).get("src")
            if not psrc:
                _PUZZLE_FAILURES[0] += 1
                return False
            cdn = _bg_from_cdn(info)
            shot = None
            grip = None
            if cdn is not None:
                # Кандидаты: diff(живой кадр, CDN) ПЕРВИЧНЫ — лунка рисуется
                # CSS-оверлеем только в живом кадре (новый формат), на плоских
                # фонах градиентный Variant A слеп. Variant A по CDN — фолбэк.
                bg_l, b_r, p_r = cdn["bg_l"], cdn["bg_r"], cdn["pz_r"]
                mask = _silhouette(_fetch(psrc).read(), p_r[2], p_r[3])
                grip = _grip_point(mask)
                yrow = int(round(p_r[1] - b_r[1]))
                _PBG_STATS[0] = (float(bg_l.mean()), float(bg_l.std()),
                                 float(bg_l.max()), float(bg_l.min()))
                shot = _shot_bg_clean(page, info)
                diff_cands = []
                if shot is not None and shot["bg_l"].shape == bg_l.shape:
                    diff_cands = _detect_slot_diff(shot["bg_l"], bg_l,
                                                   mask, yrow, k=3)
                edge_cands = []
                if shot is not None and shot["bg_l"].shape == bg_l.shape:
                    edge_cands = _detect_slot_edges(shot["bg_l"], bg_l,
                                                    mask, yrow, k=3)
                va_cands = _detect_slot_topk(bg_l, mask, yrow, k=3)
                seen = {x for x, _ in diff_cands}
                seen.update(x for x, _ in edge_cands)
                cands = edge_cands + diff_cands + \
                    [c for c in va_cands if c[0] not in seen]
                log.debug(f"solve cdn bg={bg_l.shape} piece={mask.shape} p_r={p_r} "
                          f"yrow={yrow} edges={[x for x, _ in edge_cands]} "
                          f"diff={[x for x, _ in diff_cands]} "
                          f"va={[x for x, _ in va_cands]} cands={cands} "
                          f"shot_reason={_shot_bg_reason()}")
                _attempt("det", diff=[x for x, _ in diff_cands],
                         va=[x for x, _ in va_cands],
                         edges=[x for x, _ in edge_cands],
                         cands=[x for x, _ in cands],
                         shot_reason=_shot_bg_reason())
                if edge_cands or diff_cands:
                    _solve_diag(page, info, "slot_diff",
                                detail=(f"bg={bg_l.shape} piece={mask.shape} "
                                        f"yrow={yrow} "
                                        f"edges={[x for x, _ in edge_cands]} "
                                        f"diff={[x for x, _ in diff_cands]} "
                                        f"va={[x for x, _ in va_cands]}"),
                                bg_stats=_PBG_STATS[0], bg=bg_l,
                                shot=shot.get("orig") if shot else None)
                elif cands:
                    _solve_diag(page, info, "slot_cdn",
                                detail=f"bg={bg_l.shape} piece={mask.shape} "
                                       f"yrow={yrow} cands={cands[:2]}",
                                bg_stats=_PBG_STATS[0], bg=bg_l)
            else:
                cands = None
            if cands is None:
                shot = _shot_bg_clean(page, info)
                if shot is None:
                    _solve_diag(page, info, "no_bg_shot",
                                detail=f"reason={_shot_bg_reason()}")
                    okc = _solve_classic(page, info)
                    if okc is None:
                        _PUZZLE_FAILURES[0] += 1
                        return False
                else:
                    bg_l = shot["bg_l"]
                    b_r = shot["bg_r"]
                    p_r = shot["pz_r"]
                    if len(p_r) != 4 or len(b_r) != 4:
                        _PUZZLE_FAILURES[0] += 1
                        return False
                    mask = _silhouette(_fetch(psrc).read(), p_r[2], p_r[3])
                    yrow = int(round(p_r[1] - b_r[1]))
                    cands = _detect_slot_topk(bg_l, mask, yrow, k=3)
                    _PBG_STATS[0] = (float(bg_l.mean()), float(bg_l.std()),
                                     float(bg_l.max()), float(bg_l.min()))
            if not cands:
                slot_fb = (112, 98.0)
                log.info(f"solve: детекторы пусты → fallback slot_x={slot_fb[0]} "
                         f"(из живого дампа 131847)")
                _attempt("fallback", note="slot_x_no_detect",
                         cands=[slot_fb[0]])
                cands = [slot_fb]
            if not cands:
                fullshot = None
                try:
                    fullshot = page.screenshot(timeout=10000, animations="disabled")
                except Exception:
                    pass
                _solve_diag(page, info, "no_slot",
                            detail=(f"bg={bg_l.shape} piece={mask.shape} yrow={yrow} "
                                    f"reason={_shot_bg_reason()}"),
                            bg_stats=_PBG_STATS[0], bg=bg_l,
                            shot=shot.get("orig") if shot else None,
                            fullshot=fullshot)
                _PUZZLE_FAILURES[0] += 1
                return False
            # Детектор лунки ошибается на ~±4px, а Ozon принимает попадание
            # только в узком окне (~±3px): свипуем мелкую сетку вокруг
            # каждого кандидата (наблюдение live: bx=225 не снял, bx=221 снял).
            sweep = (0, -2, 2, -4, 4)
            cand_pos = []
            seen = set()
            for bx0, score0 in cands:
                for off in sweep:
                    x = bx0 + off
                    if x >= 1 and x not in seen:
                        seen.add(x)
                        cand_pos.append((x, score0))
            for bx, score in cand_pos:
                target_vx = b_r[0] + bx
                log.debug(f"solve cand bx={bx} score={score:.3f} target_vx={target_vx:.1f}")
                okc = _drag_to(page, target_vx, info, bx, score, grip=grip)
                still = _still_challenge(page)
                _attempt("cand", bx=bx, score=score, okc=bool(okc),
                         still=still, target_vx=round(target_vx, 1))
                log.debug(f"solve drag okc={okc} still_challenge={still}")
                if okc and not still:
                    _attempt("solve", ok=True)
                    return True
            _solve_diag(page, info, "all_cands_failed",
                        detail=f"cands={cands}")
            _attempt("solve", ok=False, cands=[c[0] for c in cands])
            # Пазл часто МЕНЯЕТСЯ после неудачного дропа (новый фон/слайдер):
            # устаревшие кандидаты бесполезны — выходим на повторный захват.
            continue
        except Exception as exc:
            _solve_diag(page, info, "exception",
                        detail=f"{type(exc).__name__}: {exc}")
            _PUZZLE_FAILURES[0] += 1
            return False
        if not _still_challenge(page):
            return True
    _PUZZLE_FAILURES[0] += 1
    return False