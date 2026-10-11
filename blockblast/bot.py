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
try:
    import Quartz
except ImportError:  # для симулятора на машине без macOS
    Quartz = None

WORK = '/tmp/bb'
SHOT = os.path.join(WORK, 'shot.png')
STOP = os.path.join(WORK, 'stop')
CALIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calib.json')
OWNERS = ('Видеоповтор iPhone', 'iPhone Mirroring')

# Механика перетаскивания (замерено): центр фигуры = точка касания + LIFT*cell
# + GAIN*(мышь - точка касания). То есть фигура висит над пальцем и едет в 1.5 раза быстрее.
DEFAULT_CALIB = {'gain': 1.5, 'lift_x': 0.0, 'lift_y': -2.26}
# Геометрия поля в долях окна «Видеоповтора» (одинакова во всех темах игры).
DEFAULT_BOARD = (50 / 632, 381 / 1392, 532 / 632)
TRAY_CELL = 0.459  # размер клетки фигуры в лотке относительно клетки поля
TRAY_DIFF = 28  # насколько цвет пикселя должен отличаться от фона лотка (сумма по RGB)

N = 8
FULL = (1 << 64) - 1
ROW = [0xFF << (8 * r) for r in range(N)]
COL = [sum(1 << (r * 8 + c) for r in range(N)) for c in range(N)]
NOT_C0, NOT_C7 = FULL & ~COL[0], FULL & ~COL[7]


LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
_logf = None


def log(*a):
    line = ' '.join(str(x) for x in (time.strftime('%H:%M:%S'),) + a)
    print(line, flush=True)
    if _logf:
        _logf.write(line + '\n')
        _logf.flush()


def snapshot(tag):
    """Копия последнего снимка окна рядом с логом — чтобы потом разобрать, что случилось."""
    if _logf:
        dst = _logf.name[:-4] + f'-{time.strftime("%H%M%S")}-{tag}.png'
        try:
            os.replace(SHOT, dst)
            log('снимок:', os.path.basename(dst))
        except OSError:
            pass


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


def read_board(img, geom):
    """Клетка занята, если у неё есть объёмная рамка фигуры: верхний край заметно светлее нижнего.
    Пустые клетки плоские. Работает в любой теме, цвет фигур и поля не важен."""
    x0, y0, cell = geom
    L = lum(img)
    k = int(cell * 0.38)
    hw = k // 2
    bits = 0
    for i in range(N):
        for j in range(N):
            cx, cy = int(x0 + (j + .5) * cell), int(y0 + (i + .5) * cell)
            top = L[cy - k:cy - k + 4, cx - hw:cx + hw].mean()
            bottom = L[cy + k - 4:cy + k, cx - hw:cx + hw].mean()
            if top - bottom > 20:
                bits |= 1 << (i * 8 + j)
    return bits


def screen_ok(img, geom):
    """Похоже ли на игру: пустые клетки поля заметно отличаются по яркости от фона под полем
    (в светлых темах они темнее фона, в чёрной — светлее).
    Отсекает «iPhone используется», «Подключение прервано» и т.п."""
    x0, y0, cell = geom
    yb = y0 + N * cell
    strip = lum(img[int(yb + 0.2 * cell):int(yb + 0.35 * cell), int(x0):int(x0 + N * cell)])
    r = max(2, int(cell * 0.18))
    darkest = min(lum(img[int(y0 + (i + .5) * cell) - r:int(y0 + (i + .5) * cell) + r,
                          int(x0 + (j + .5) * cell) - r:int(x0 + (j + .5) * cell) + r]).mean()
                  for i in range(N) for j in range(N))
    return abs(np.median(strip) - darkest) > 12


def read_tray(img, geom):
    """Три слота под полем. Для каждого: None или (cells, (cx, cy) центр в px, (h, w))."""
    x0, y0, cell = geom
    H, W, _ = img.shape
    ty0 = int(y0 + N * cell + 0.5 * cell)
    ty1 = int(min(H, y0 + N * cell + 4.8 * cell))
    reg = img[ty0:ty1]
    bg = np.median(reg[:, int(x0):int(x0 + N * cell)].reshape(-1, 3), axis=0)
    mask = np.abs(reg - bg).sum(axis=2) > TRAY_DIFF
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


# Веса признаков доски (подобраны симулятором sim.py).
WEIGHTS = {
    'free': 1.0,      # свободные клетки
    'perim': -0.9,    # границы пустых клеток с занятыми/стенами — «изрезанность»
    'holes': -12.0,   # одиночные дырки, закрытые со всех сторон
    'pockets': -2.5,  # клетки, закрытые с трёх сторон
    'sq3': 39.0,      # влезает 3×3
    'h5': 22.0,       # влезает 1×5
    'v5': 22.0,       # влезает 5×1
    'sq2': 44.0,      # влезает 2×2
    'rect23': 13.0,   # влезает 2×3 или 3×2
    'n_sq3': 1.2,     # сколько мест под 3×3 (до 6)
    'n_5': 0.6,       # сколько мест под 1×5/5×1 (до 8)
}
# Награда за снос линий в серии: за линию, за несколько сразу, за комбо подряд.
GAIN = {'line': 10.0, 'multi': 10.0, 'streak': 6.0}
FEATS = tuple(WEIGHTS)
_W = [WEIGHTS[k] for k in FEATS]
_EVAL = {}


def set_weights(w):
    global _W
    WEIGHTS.update(w)
    _W = [WEIGHTS[k] for k in FEATS]
    _EVAL.clear()


def evaluate(b):
    v = _EVAL.get(b)
    if v is None:
        if len(_EVAL) > 2_000_000:
            _EVAL.clear()
        v = _EVAL[b] = sum(w * f for w, f in zip(_W, features(b)))
    return v


def features(b):
    """Признаки доски в порядке FEATS."""
    free = FULL & ~b
    L = ((b << 1) & NOT_C0 & FULL) | COL[0]
    R = ((b >> 1) & NOT_C7) | COL[7]
    U = ((b << 8) & FULL) | ROW[0]
    D = (b >> 8) | ROW[7]
    perim = popcount(free & L) + popcount(free & R) + popcount(free & U) + popcount(free & D)
    holes = popcount(free & L & R & U & D)
    pockets = popcount(free & L & R & U) + popcount(free & L & R & D) \
        + popcount(free & U & D & L) + popcount(free & U & D & R) - 4 * holes
    h2, h3, h5 = _runs_h(free, 2), _runs_h(free, 3), _runs_h(free, 5)
    sq3 = _runs_v(h3, 3)
    sq2 = _runs_v(h2, 2)
    v5 = _runs_v(free, 5)
    rect23 = _runs_v(h3, 2) | _runs_v(h2, 3)
    return (popcount(free), perim, holes, pockets, sq3 != 0, h5 != 0, v5 != 0, sq2 != 0,
            rect23 != 0, min(popcount(sq3), 6), min(popcount(h5) + popcount(v5), 8))


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
                gain = (GAIN['line'] * lines + GAIN['multi'] * lines * (lines - 1)
                        + GAIN['streak'] * streak) if lines else 0
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


def fmt_turn(board, pieces, seq):
    """Поле (куда встанет первый ход — цифрой слота) и фигуры в лотке, всё текстом."""
    s, m, _, _ = seq[0]
    rows = [''.join(str(s) if m >> (r * 8 + c) & 1 else '#' if board >> (r * 8 + c) & 1 else '.'
                    for c in range(N)) for r in range(N)]
    shapes = []
    for k, cells in sorted(pieces.items()):
        h = max(r for r, _ in cells) + 1
        w = max(c for _, c in cells) + 1
        shapes.append([f'{k}:' + ''.join('#' if (r, c) in cells else '.' for c in range(w)) if r == 0 else
                       '  ' + ''.join('#' if (r, c) in cells else '.' for c in range(w)) for r in range(h)])
    for i in range(max(len(x) for x in shapes)):
        rows[i] += '   ' + '  '.join((x[i] if i < len(x) else '').ljust(8) for x in shapes)
    return '\n'.join(rows)


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


def stable_state(wid, geom, tries=15, need=3):
    """Ждёт, пока картинка успокоится (анимации, надписи «Brilliant Escape» и т.п.).
    Возвращает (img, доска, лоток); (None, None, None) — не игра или не успокоилось."""
    prev, same = None, 0
    for _ in range(tries):
        img = capture(wid)
        if not screen_ok(img, geom):
            return None, None, None
        tray = read_tray(img, geom)
        st = (read_board(img, geom), tuple(s and s[0] for s in tray))
        same = same + 1 if st == prev else 1
        if same >= need:
            return img, st[0], tray
        prev = st
        time.sleep(0.2)
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--moves', type=int, default=100000, help='максимум ходов')
    ap.add_argument('--dry', action='store_true', help='только распознать и показать план')
    args = ap.parse_args()
    global _logf
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    _logf = open(os.path.join(LOG_DIR, time.strftime('%Y%m%d-%H%M%S') + '.log'), 'w')
    log('лог:', _logf.name)
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
    # Раскладка игры одинакова во всех темах, поэтому берём геометрию поля в долях окна.
    rx, ry, rs = DEFAULT_BOARD
    geom = (rx * W, ry * H, rs * W / N)
    log('окно', dict(wb), 'поле px', tuple(round(v, 1) for v in geom))
    calib = load_calib()
    last_mouse = None
    moves = 0
    idle_since = None
    warned = False
    fails = 0

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
        pieces = {i: s[0] for i, s in enumerate(tray) if s} if img is not None else {}
        if not pieces:
            # фигур нет: идёт анимация, или вылезло окно/реклама, или отвалился Видеоповтор
            idle_since = idle_since or time.time()
            idle = time.time() - idle_since
            if idle > 8 and not warned:
                log('игры не видно 8 с — реклама, «iPhone используется» или «Подключение прервано»? жду')
                snapshot('net-figur')
                warned = True
            if idle > 120:
                log('2 минуты без игры — выхожу')
                break
            time.sleep(0.5)
            continue
        idle_since = None
        warned = False
        t0 = time.time()
        val, seq = plan(board, pieces)
        if not seq:
            log('ни одна фигура не влезает — игра окончена\n' + fmt(board))
            snapshot('konec')
            break
        s, m, r0, c0 = seq[0]
        cells, (tx, ty), (nh, nw) = tray[s]
        log(f'ход {moves + 1}: слот {s} → ряд {r0} кол {c0}; план {len(seq)}/{len(pieces)}, '
            f'оценка {val:.1f}, {time.time() - t0:.2f}с\n' + fmt_turn(board, pieces, seq))
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
        expected, cleared = apply(board, m)
        time.sleep(1.1 if cleared else 0.35)  # после сноса линий поверх поля летят надписи
        img2, board2, tray2 = stable_state(wid, geom)
        if img2 is None:
            continue
        if board2 == expected:
            fails = 0
            continue
        if tray2[s] is not None and board2 == board:
            fails += 1
            log(f'фигура не встала (вернулась в лоток), подряд: {fails}')
            snapshot('ne-vstala')
            if fails >= 3:
                log('3 раза подряд не встаёт — видимо, неверно читаю экран, останавливаюсь')
                break
            continue
        fails = 0
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
            log('доска отличается от ожидаемой, перечитываю\nждал:\n' + fmt(expected) + '\nвижу:\n' + fmt(board2))
            snapshot('ne-to')
    log(f'сделано ходов: {moves}')


if __name__ == '__main__':
    main()
