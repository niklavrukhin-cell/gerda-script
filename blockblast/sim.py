#!/usr/bin/env python3
"""Симулятор Block Blast для подбора весов оценки в bot.py (без Мака и экрана).

    python3 sim.py --games 20            # оценить текущие веса
    python3 sim.py --tune 40 --games 12  # случайный поиск весов
"""
import argparse
import json
import random
import time
from multiprocessing import Pool

import bot


def _shape(rows):
    return tuple((r, c) for r, line in enumerate(rows) for c, ch in enumerate(line) if ch == '#')


def _rotations(cells):
    out = set()
    cur = cells
    for _ in range(4):
        cur = tuple(sorted((c, -r) for r, c in cur))
        mr = min(r for r, _ in cur)
        mc = min(c for _, c in cur)
        cur = tuple(sorted((r - mr, c - mc) for r, c in cur))
        out.add(cur)
        out.add(tuple(sorted((r, max(c for _, c in cur) - c) for r, c in cur)))
    return out


BASE = [['#'], ['##'], ['###'], ['####'], ['#####'], ['##', '##'], ['###', '###', '###'],
        ['###', '###'], ['##', '#.'], ['###', '#..'], ['###', '#..', '#..'], ['###', '.#.'],
        ['##.', '.##']]
PIECES = sorted({p for rows in BASE for p in _rotations(_shape(rows))})


def play(seed, max_rounds=200):
    rnd = random.Random(seed)
    board, moves, lines_total, score = 0, 0, 0, 0
    for rnd_i in range(max_rounds):
        pieces = {i: rnd.choice(PIECES) for i in range(3)}
        while pieces:
            _, seq = bot.plan(board, pieces)
            if not seq:
                return rnd_i, moves, lines_total, score
            s, m, _, _ = seq[0]
            board, lines = bot.apply(board, m)
            moves += 1
            lines_total += lines
            score += len(pieces[s]) + 10 * lines * lines
            del pieces[s]
    return max_rounds, moves, lines_total, score


def run(args):
    weights, gain, seeds, max_rounds = args
    bot.set_weights(weights)
    bot.GAIN.update(gain)
    return [play(s, max_rounds) for s in seeds]


def bench(pool, weights, gain, games, max_rounds, seed0=0):
    seeds = list(range(seed0, seed0 + games))
    chunks = [(weights, gain, seeds[i::4], max_rounds) for i in range(4)]
    res = [r for part in pool.map(run, chunks) for r in part]
    rounds = sorted(r[0] for r in res)
    return sum(rounds) / len(rounds), rounds[len(rounds) // 2], res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--games', type=int, default=20)
    ap.add_argument('--rounds', type=int, default=200, help='предел раундов (по 3 фигуры) на игру')
    ap.add_argument('--tune', type=int, default=0, help='сколько кандидатов перебрать')
    args = ap.parse_args()
    with Pool(4) as pool:
        best_w, best_g = dict(bot.WEIGHTS), dict(bot.GAIN)
        t = time.time()
        best, med, _ = bench(pool, best_w, best_g, args.games, args.rounds)
        print(f'база: среднее {best:.1f} раундов, медиана {med}, {time.time() - t:.0f}с', flush=True)
        rnd = random.Random(1)
        for i in range(args.tune):
            w = {k: v * rnd.uniform(0.6, 1.6) if rnd.random() < 0.4 else v for k, v in best_w.items()}
            g = {k: v * rnd.uniform(0.6, 1.6) if rnd.random() < 0.3 else v for k, v in best_g.items()}
            avg, med, _ = bench(pool, w, g, args.games, args.rounds)
            mark = ''
            if avg > best:
                best, best_w, best_g, mark = avg, w, g, '  ← лучше'
            print(f'{i + 1}: среднее {avg:.1f}, медиана {med}{mark}', flush=True)
        if args.tune:
            print(json.dumps({'weights': best_w, 'gain': best_g}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
