#!/usr/bin/env python3
"""Автоигра в Block Blast через «Видеоповтор iPhone» (iPhone Mirroring) на Маке.

Запуск (именно из Терминала: ему нужны права «Запись экрана» и «Универсальный доступ»):
    ~/bb-venv/bin/python ~/gerda-script/blockblast/bot.py

Остановка: дёрнуть мышку (бот заметит и встанет), Ctrl+C в Терминале
или `touch /tmp/bb/stop`.
"""
import argparse
import itertools
import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image
import Quartz

WORK = '/tmp/bb'
SHOT = os.path.join(WORK, 'shot.png')
STOP = os.path.join(WORK, 'stop')
CALIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calib.json')
OWNERS = ('Видеоповтор iPhone', 'iPhone Mirroring')

# Механика перетаскивания (замерено): центр фигуры = точка касания + LIFT*cell
# + GAIN*(мышь - точка касания). То есть фигура висит над пальцем и едет в 1.5 раза быстрее.
DEFAULT_CALIB = {'gain': 1.5, 'lift_x': 0.0, 'lift_y': -2.26}
# Геометрия поля в долях окна (запасной вариант, если автопоиск не сработал).
DEFAULT_BOARD = (50 / 632, 381 / 1392, 532 / 632)
TRAY_CELL = 0.459  # размер клетки фигуры в лотке относительно клетки поля

N = 8
FULL = (1 << 64) - 1
ROW = [0xFF << (8 * r) for r in range(N)]
COL = [sum(1 << (r * 8 + c) for r in range(N)) for c in range(N)]
NOT_C0, NOT_C7 = FULL & ~COL[0], FULL & ~COL[7]


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------- экран / мышь

def find_window():
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                               Quartz.kCGNullWindowID):
        b = w.get('kCGWindowBounds')
        if w.get('kCGWindowOwnerName') in OWNERS and b and b['Width'] > 100:
            return w['kCGWindowNumber'], b
    return None, None


def capture(wid):
    subprocess.run(['screencapture', '-x', '-o', '-l', str(wid), SHOT], check=True)
    return np.asarray(Image.open(SHOT).convert('RGB')).astype(np.int16)


def mouse_pos():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def _ev(t, x, y):
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(None, t, (x, y), Quartz.kCGMouseButtonLeft))


def drag(a, b, steps=24, dt=0.008):
    _ev(Quartz.kCGEventMouseMoved, *a)
    time.sleep(0.05)
    _ev(Quartz.kCGEventLeftMouseDown, *a)
    time.sleep(0.09)
    for i in range(1, steps + 1):
        t = i / steps
        _ev(Quartz.kCGEventLeftMouseDragged, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        time.sleep(dt)
    time.sleep(0.09)
    _ev(Quartz.kCGEventLeftMouseUp, *b)


# ---------------------------------------------------------------- распознавание

def lum(img):
    return img.mean(axis=2)


def detect_board(img):
    """Ищет поле по тёмным линиям сетки. Возвращает (x0, y0, cell) в пикселях снимка."""
    H, W, _ = img.shape
    L = lum(img)
    m = (L > 30) & (L < 130)
    m[:int(.15 * H)] = False
    m[int(.75 * H):] = False
    xs = np.nonzero(m.sum(0) > H * 0.15)[0]
    if len(xs) < 2:
        return None
    x0, x1 = xs.min(), xs.max()
    size = x1 - x0 + 1
    if not (0.6 * W < size < 0.98 * W):
        return None
    ys = np.nonzero(m[:, x0:x1].sum(1) > size * 0.3)[0]
    if not len(ys):
        return None
    return float(x0), float(ys.min()), size / N


def read_board(img, geom):
    x0, y0, cell = geom
    r = max(2, int(cell * 0.18))
    vals = np.zeros((N, N))
    for i in range(N):
        for j in range(N):
            cx, cy = int(x0 + (j + .5) * cell), int(y0 + (i + .5) * cell)
            vals[i, j] = lum(img[cy - r:cy + r, cx - r:cx + r]).mean()
    lo, hi = np.percentile(vals, 5), vals.max()
    if hi - lo < 30:  # всё поле одного цвета
        filled = vals > 120
    else:
        filled = vals > lo + max(30, (hi - lo) * 0.35)
    bits = 0
    for i in range(N):
        for j in range(N):
            if filled[i, j]:
                bits |= 1 << (i * 8 + j)
    return bits


def read_tray(img, geom):
    """Три слота под полем. Для каждого: None или (cells, (cx, cy) центр в px, (h, w))."""
    x0, y0, cell = geom
    H, W, _ = img.shape
    ty0 = int(y0 + N * cell + 0.4 * cell)
    ty1 = int(min(H, y0 + N * cell + 4.8 * cell))
    reg = img[ty0:ty1]
    bg = np.median(reg[:, int(x0):int(x0 + N * cell)].reshape(-1, 3), axis=0)
    mask = np.abs(reg - bg).sum(axis=2) > 60
    tc = cell * TRAY_CELL
    slots = []
    bw = N * cell / 3
    for s in range(3):
        sx0, sx1 = int(x0 + s * bw), int(x0 + (s + 1) * bw)
        sub = mask[:, sx0:sx1]
        ys, xs = np.nonzero(sub)
        if len(xs) < tc * tc * 0.5:
            slots.append(None)
            continue
        bx0, bx1, by0, by1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        nw = max(1, int(round((bx1 - bx0) / tc - 0.13)))
        nh = max(1, int(round((by1 - by0) / tc - 0.13)))
        if nw > 5 or nh > 5:
            slots.append(None)
            continue
        cw, ch = (bx1 - bx0) / (nw + 0.13), (by1 - by0) / (nh + 0.13)
        cells = []
        k = max(1, int(tc * 0.15))
        for i in range(nh):
            for j in range(nw):
                px, py = int(bx0 + (j + .5) * cw), int(by0 + (i + .5) * ch)
                if sub[py - k:py + k, px - k:px + k].mean() > 0.5:
                    cells.append((i, j))
        if not cells:
            slots.append(None)
            continue
        cx = sx0 + bx0 + nw * cw / 2
        cy = ty0 + by0 + nh * ch / 2
        slots.append((tuple(cells), (cx, cy), (nh, nw)))
    return slots


# ---------------------------------------------------------------- логика игры

def popcount(x):
    return bin(x).count('1')


def placements(cells):
    h = max(r for r, _ in cells) + 1
    w = max(c for _, c in cells) + 1
    out = []
    for r0 in range(N - h + 1):
        for c0 in range(N - w + 1):
            m = 0
            for r, c in cells:
                m |= 1 << ((r0 + r) * 8 + c0 + c)
            out.append((m, r0, c0))
    return out


_LINES = {}


def apply(board, mask):
    """Ставит фигуру и сносит заполненные ряды/столбцы. Возвращает (доска, сколько линий)."""
    lines = _LINES.get(mask)
    if lines is None:
        lines = _LINES[mask] = [L for L in ROW + COL if L & mask]
    b = board | mask
    clr = 0
    n = 0
    for L in lines:
        if b & L == L:
            clr |= L
            n += 1
    return (b & ~clr if n else b), n


def _runs_h(f, n):
    r = f
    for k in range(1, n):
        f = (f >> 1) & NOT_C7
        r &= f
    return r


def _runs_v(f, n):
    r = f
    for k in range(1, n):
        f >>= 8
        r &= f
    return r


_EVAL = {}


def evaluate(b):
    v = _EVAL.get(b)
    if v is None:
        if len(_EVAL) > 2_000_000:
            _EVAL.clear()
        v = _EVAL[b] = _evaluate(b)
    return v


def _evaluate(b):
    """Чем больше, тем лучше доска для следующих (неизвестных) фигур."""
    free = FULL & ~b
    L = ((b << 1) & NOT_C0 & FULL) | COL[0]
    R = ((b >> 1) & NOT_C7) | COL[7]
    U = ((b << 8) & FULL) | ROW[0]
    D = (b >> 8) | ROW[7]
    perim = popcount(free & L) + popcount(free & R) + popcount(free & U) + popcount(free & D)
    holes = popcount(free & L & R & U & D)
    # «карманы» в одну клетку шириной с трёх сторон закрытые
    pockets = popcount(free & L & R & U) + popcount(free & L & R & D) \
        + popcount(free & U & D & L) + popcount(free & U & D & R) - 4 * holes
    h3, h5 = _runs_h(free, 3), _runs_h(free, 5)
    sq3 = _runs_v(h3, 3)
    h2 = _runs_h(free, 2)
    sq2 = _runs_v(h2, 2)
    v5 = _runs_v(free, 5)
    rect23 = _runs_v(h3, 2) | _runs_v(h2, 3)
    score = popcount(free) * 1.0 - perim * 0.9 - holes * 12 - pockets * 2.5
    score += (14 if sq3 else -25) + (8 if h5 else -14) + (8 if v5 else -14)
    score += (4 if sq2 else -40) + (3 if rect23 else -10)
    score += min(popcount(sq3), 6) * 1.2 + min(popcount(h5) + popcount(v5), 8) * 0.6
    return score


def plan(board, pieces):
    """pieces: {slot: cells}. Возвращает (оценка, [(slot, mask, r0, c0), ...]) лучшей серии."""
    pl = {s: placements(c) for s, c in pieces.items()}
    best = [None, None]
    seen = {}

    def rec(b, left, acc, seq, streak):
        key = (b, left)
        if seen.get(key, -1e18) >= acc:
            return
        seen[key] = acc
        moved = False
        for s in left:
            for m, r0, c0 in pl[s]:
                if b & m:
                    continue
                nb, lines = apply(b, m)
                gain = lines * 10 + (lines * (lines - 1)) * 10 + (streak * 6 if lines else 0)
                moved = True
                rec(nb, left - {s}, acc + gain, seq + [(s, m, r0, c0)], streak + 1 if lines else 0)
        if not moved or not left:
            val = acc + evaluate(b) - 1000 * len(left)
            if best[0] is None or val > best[0]:
                best[0], best[1] = val, seq

    rec(board, frozenset(pieces), 0.0, [], 0)
    return best[0], best[1]


def fmt(b):
    return '\n'.join(''.join('#' if b >> (r * 8 + c) & 1 else '.' for c in range(N)) for r in range(N))


# ---------------------------------------------------------------- основной цикл

def load_calib():
    try:
        with open(CALIB) as f:
            return {**DEFAULT_CALIB, **json.load(f)}
    except Exception:
        return dict(DEFAULT_CALIB)


def save_calib(c):
    with open(CALIB, 'w') as f:
        json.dump(c, f, indent=1)


def stable_state(wid, geom, tries=12):
    prev = None
    for _ in range(tries):
        img = capture(wid)
        st = (read_board(img, geom), tuple(s and s[0] for s in read_tray(img, geom)))
        if st == prev:
            return img, st[0], read_tray(img, geom)
        prev = st
        time.sleep(0.2)
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--moves', type=int, default=100000, help='максимум ходов')
    ap.add_argument('--dry', action='store_true', help='только распознать и показать план')
    args = ap.parse_args()
    os.makedirs(WORK, exist_ok=True)
    if os.path.exists(STOP):
        os.remove(STOP)

    wid, wb = find_window()
    if not wid:
        sys.exit('Не нашёл окно «Видеоповтор iPhone». Открой его и запусти игру.')
    subprocess.run(['osascript', '-e', 'tell application id "com.apple.ScreenContinuity" to activate'])
    time.sleep(0.4)
    img = capture(wid)
    H, W, _ = img.shape
    scale = W / wb['Width']
    geom = detect_board(img)
    if not geom:
        rx, ry, rs = DEFAULT_BOARD
        geom = (rx * W, ry * H, rs * W / N)
        log('поле не нашёл, беру стандартную геометрию')
    log('окно', dict(wb), 'поле px', tuple(round(v, 1) for v in geom))
    calib = load_calib()
    last_mouse = None
    moves = 0

    while moves < args.moves:
        if os.path.exists(STOP):
            log('стоп-файл, выхожу')
            break
        if last_mouse:
            mx, my = mouse_pos()
            if abs(mx - last_mouse[0]) + abs(my - last_mouse[1]) > 25:
                log(f'мышь сдвинули ({mx:.0f},{my:.0f} вместо {last_mouse[0]:.0f},{last_mouse[1]:.0f}) — останавливаюсь')
                break
        wid, wb = find_window()
        if not wid:
            log('окно пропало')
            break
        img, board, tray = stable_state(wid, geom)
        if img is None:
            log('картинка не успокаивается, жду')
            time.sleep(0.5)
            continue
        pieces = {i: s[0] for i, s in enumerate(tray) if s}
        if not pieces:
            time.sleep(0.3)
            continue
        t0 = time.time()
        val, seq = plan(board, pieces)
        if not seq:
            log('ни одна фигура не влезает — игра окончена')
            print(fmt(board))
            break
        s, m, r0, c0 = seq[0]
        cells, (tx, ty), (nh, nw) = tray[s]
        log(f'ход {moves + 1}: слот {s} → ряд {r0} кол {c0}; план {len(seq)}/{len(pieces)}, '
            f'оценка {val:.1f}, {time.time() - t0:.2f}с')
        if args.dry:
            print(fmt(board), '\nфигуры:', pieces, '\nплан:', [(a, b, c) for a, _, b, c in seq])
            break
        x0, y0, cell = geom
        T = (x0 + (c0 + nw / 2) * cell, y0 + (r0 + nh / 2) * cell)
        g = calib['gain']
        M = (tx + (T[0] - tx - calib['lift_x'] * cell) / g,
             ty + (T[1] - ty - calib['lift_y'] * cell) / g)
        a = (wb['X'] + tx / scale, wb['Y'] + ty / scale)
        b = (wb['X'] + M[0] / scale, wb['Y'] + M[1] / scale)
        drag(a, b)
        last_mouse = b
        moves += 1
        expected, _ = apply(board, m)
        time.sleep(0.35)
        img2, board2, tray2 = stable_state(wid, geom)
        if img2 is None or board2 == expected:
            continue
        if tray2[s] is not None and board2 == board:
            log('фигура не встала (вернулась в лоток)')
            continue
        # фигура встала со сдвигом? ищем такую же фигуру рядом и правим калибровку
        for dr, dc in itertools.product((-1, 0, 1), repeat=2):
            if dr == dc == 0:
                continue
            for mm, rr, cc in placements(cells):
                if rr == r0 + dr and cc == c0 + dc and not board & mm and apply(board, mm)[0] == board2:
                    calib['lift_x'] -= dc * 0.5
                    calib['lift_y'] -= dr * 0.5
                    save_calib(calib)
                    log(f'встала со сдвигом ({dr},{dc}), правлю калибровку → {calib}')
        if board2 != expected:
            log('доска отличается от ожидаемой, перечитываю')
    log(f'сделано ходов: {moves}')


if __name__ == '__main__':
    main()
