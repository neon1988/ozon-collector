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
import time
import urllib.request
from typing import Optional

import numpy as np

# Сколько попыток решения даёт воркер на один инцидент пазла, прежде чем
# признать его нерешаемым (fallback → FAB-reload → FSM).
PUZZLE_MAX_ATTEMPTS = 2

# Процесс-локал (list как изменяемый синглтон, чтобы reset() работал в тестах).
_PUZZLE_FAILURES: list[int] = [0]


def reset() -> None:
    """Сбросить счётчик неудач (вызывается при старте сбора/воркера)."""
    _PUZZLE_FAILURES[0] = 0


def failures() -> int:
    """Число неудачных попыток решения пазла в текущем сборе."""
    return _PUZZLE_FAILURES[0]


def is_puzzle(signature: Optional[str]) -> bool:
    """Сигнатура challenge указывает на слайдер-пазл (а не FAB-заглушку)."""
    return bool(signature and ("пазл" in signature or "ползунок" in signature))


def _find_notch(bg: np.ndarray, piece: np.ndarray) -> Optional[int]:
    """x-top-left выемки в фоне (в пикселях картинки) или None.

    Кусочек Ozon-пазла — уменьшенная копия области фона: template matching
    по краям даёт пик совпадения в правильной позиции.
    """
    try:
        import cv2
    except Exception:
        return None
    if bg.ndim != 2 and bg.ndim != 3:
        return None
    bg_g = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY) if bg.ndim == 3 else bg
    p_g = cv2.cvtColor(piece, cv2.COLOR_RGB2GRAY) if piece.ndim == 3 else piece
    if bg_g.shape[0] < p_g.shape[0] or bg_g.shape[1] < p_g.shape[1]:
        return None
    res = cv2.matchTemplate(bg_g, p_g, cv2.TM_CCOEFF_NORMED)
    _minv, _maxv, _minl, maxloc = cv2.minMaxLoc(res)
    return int(maxloc[0])


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
                '[class*=slider],[class*=slide],[class*=handler],
                 [class*=track],[class*=drag],[class*=ckbox]'
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
    """Пазл всё ещё на странице после попыток решения."""
    from .collect import detect_challenge
    return is_puzzle(detect_challenge(page))


def solve_puzzle(page, attempts: int = PUZZLE_MAX_ATTEMPTS) -> bool:
    """Решить слайдер-пазл на странице. True — пазл снят.

    При любой ошибке (нет DOM, нет картинок, не нашлась выемка, обрыв CDP)
    — False. Счётчик неудач растёт за каждый неудачный заход.
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
            from PIL import Image
            bsrc = (info["bg"] or {}).get("src")
            psrc = (info["puzzle"] or {}).get("src")
            b_r = info["bg"]["r"]
            p_r = info["puzzle"]["r"]
            if not (bsrc and psrc):
                _PUZZLE_FAILURES[0] += 1
                return False
            bg = np.asarray(Image.open(_fetch(bsrc)).convert("RGB"))
            piece = np.asarray(Image.open(_fetch(psrc)).convert("RGB"))
            bx = _find_notch(bg, piece)
            if bx is None:
                _PUZZLE_FAILURES[0] += 1
                return False
            b_x0, _b_y0, b_w, _b_h = b_r
            p_x0, p_y0, p_w, p_h = p_r
            scale_x = b_w / bg.shape[1]
            target_cx = b_x0 + bx * scale_x + (p_w * scale_x) / 2
            cur_cx = p_x0 + p_w / 2
            cur_cy = p_y0 + p_h / 2
            dx = target_cx - cur_cx
            page.mouse.move(cur_cx, cur_cy)
            page.mouse.down()
            page.mouse.move(cur_cx + dx, cur_cy, steps=25)
            time.sleep(0.4)
            page.mouse.up()
            time.sleep(1.0)
        except Exception:
            _PUZZLE_FAILURES[0] += 1
            return False
        if not _still_challenge(page):
            return True
    _PUZZLE_FAILURES[0] += 1
    return False