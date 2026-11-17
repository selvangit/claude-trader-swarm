#!/usr/bin/env python3
"""
claude-trader-swarm — an evolution harness for paper-trading strategy agents.

The idea (one loop, repeated):
  FIGHT  -> every agent trades the same paper sessions and gets scored
  KILL   -> the bottom half of the population is eliminated
  BREED  -> survivors' parameters are mixed + mutated into the next generation
  REPEAT -> weak strategies die out, strong ones evolve

Each agent is one micro-strategy (RSI reversion, VWAP bounce, breakout,
momentum, opening range) with its own parameter "genome". Agents are scored
on paper P&L, win rate and drawdown. Nothing here places a real order, ever:
the built-in market is a deterministic synthetic feed so the loop runs
end-to-end out of the box, and `MarketFeed` is the seam where a real paper
data source (e.g. the free Yahoo bars used by the day-trader-skill pack)
plugs in.

Stdlib only. Python 3.8+.

  python3 swarm.py                      # 100 agents, 5 generations
  python3 swarm.py --agents 40 --generations 3 --seed 7
  python3 swarm.py --out runs/demo      # also write JSON + summary reports
"""

import argparse
import json
import math
import os
import random
import statistics
import sys

# --------------------------------------------------------------------------
# Market: deterministic synthetic intraday bars (the demo feed)
# --------------------------------------------------------------------------

BARS_PER_DAY = 78  # 5-minute bars in a 6.5h session


class MarketFeed:
    """One trading day of OHLC bars for one synthetic symbol.

    A seeded random walk with a regime (trend + volatility) per day, so a
    generation's agents all face identical conditions and scores are
    comparable. Swap this class for a real paper feed later; the harness
    only needs `.days()` -> list of days, each a list of bar dicts.
    """

    def __init__(self, seed, days=5, symbols=("ALPHA", "BRAVO", "COMET", "DELTA")):
        self.seed = seed
        self.n_days = days
        self.symbols = symbols

    def _day_bars(self, rng, start_price):
        trend = rng.uniform(-0.0006, 0.0006)          # drift per bar
        vol = rng.uniform(0.0012, 0.0045)             # per-bar volatility
        price = start_price
        bars = []
        for _ in range(BARS_PER_DAY):
            open_ = price
            close = open_ * math.exp(rng.gauss(trend, vol))
            high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
            low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
            bars.append({"open": open_, "high": high, "low": low,
                         "close": close, "volume": rng.randint(50_000, 900_000)})
            price = close
        return bars

    def days(self):
        """Return a list of days; each day maps symbol -> bars."""
        out = []
        for d in range(self.n_days):
            rng = random.Random(self.seed * 1000 + d)
            day = {}
            for s in self.symbols:
                day[s] = self._day_bars(rng, rng.uniform(40, 320))
            out.append(day)
        return out


# --------------------------------------------------------------------------
# Indicators (tiny, on-the-fly)
# --------------------------------------------------------------------------

def sma(values, period, i):
    if i + 1 < period:
        return None
    return sum(values[i + 1 - period:i + 1]) / period


def rsi(closes, period, i):
    if i < period:
        return None
    gains, losses = 0.0, 0.0
    for j in range(i - period + 1, i + 1):
        ch = closes[j] - closes[j - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - 100 / (1 + rs)


def vwap(bars, i):
    tpv, vol = 0.0, 0
    for b in bars[:i + 1]:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        tpv += tp * b["volume"]
        vol += b["volume"]
    return tpv / vol if vol else bars[i]["close"]


# --------------------------------------------------------------------------
# Strategy genomes: five families, each a dict of evolvable parameters
# --------------------------------------------------------------------------

GENOME_RANGES = {
    "rsi_reversion": {
        "rsi_period": (7, 21), "oversold": (20, 40), "overbought": (60, 80),
        "stop_pct": (0.3, 2.5), "target_pct": (0.5, 4.0),
    },
    "vwap_bounce": {
        "band_pct": (0.1, 1.2), "stop_pct": (0.3, 2.5), "target_pct": (0.5, 4.0),
    },
    "breakout": {
        "lookback": (10, 40), "stop_pct": (0.3, 2.5), "target_pct": (0.5, 5.0),
    },
    "momentum": {
        "fast": (3, 10), "slow": (15, 40), "stop_pct": (0.3, 2.5),
        "target_pct": (0.5, 5.0),
    },
    "opening_range": {
        "or_bars": (3, 12), "stop_pct": (0.3, 2.5), "target_pct": (0.5, 5.0),
    },
}

FAMILIES = list(GENOME_RANGES.keys())
INT_PARAMS = {"rsi_period", "lookback", "fast", "slow", "or_bars"}


def random_genome(rng, family):
    g = {}
    for k, (lo, hi) in GENOME_RANGES[family].items():
        v = rng.uniform(lo, hi)
        g[k] = int(round(v)) if k in INT_PARAMS else round(v, 3)
    if family == "momentum" and g["fast"] >= g["slow"]:
        g["fast"], g["slow"] = max(2, g["slow"] // 2), g["slow"]
    return g


def clamp_genome(family, g):
    for k, (lo, hi) in GENOME_RANGES[family].items():
        g[k] = min(hi, max(lo, g[k]))
        g[k] = int(round(g[k])) if k in INT_PARAMS else round(g[k], 3)
    if family == "momentum" and g["fast"] >= g["slow"]:
        g["fast"] = max(2, g["slow"] - 1)
    return g


# --------------------------------------------------------------------------
# One agent trading one symbol for one day -> list of trade P&Ls (in $)
# --------------------------------------------------------------------------

NOTIONAL = 1000.0  # paper dollars per trade


def run_agent_day(family, g, bars):
    closes = [b["close"] for b in bars]
    trades = []
    pos = None  # (entry_price, entry_index)
    or_high = or_low = None
    if family == "opening_range":
        n = int(g["or_bars"])
        seg = bars[:n]
        or_high = max(b["high"] for b in seg)
        or_low = min(b["low"] for b in seg)

    def exit_check(i, price):
        entry, _ = pos
        chg = (price - entry) / entry * 100
        if chg <= -g["stop_pct"]:
            return price, "stop"
        if chg >= g["target_pct"]:
            return price, "target"
        return None

    for i, bar in enumerate(bars):
        price = bar["close"]
        if pos is not None:
            hit = exit_check(i, price)
            reason = hit[1] if hit else None
            if family == "rsi_reversion":
                r = rsi(closes, int(g["rsi_period"]), i)
                if r is not None and r >= g["overbought"]:
                    reason = reason or "signal"
            if family == "momentum":
                f, s = sma(closes, int(g["fast"]), i), sma(closes, int(g["slow"]), i)
                if f is not None and s is not None and f < s:
                    reason = reason or "signal"
            if reason or i == len(bars) - 1:
                entry, _ = pos
                trades.append((price - entry) / entry * NOTIONAL)
                pos = None
            continue

        # flat -> look for an entry
        enter = False
        if family == "rsi_reversion":
            r = rsi(closes, int(g["rsi_period"]), i)
            enter = r is not None and r <= g["oversold"]
        elif family == "vwap_bounce":
            v = vwap(bars, i)
            near = abs(price - v) / v * 100 <= g["band_pct"]
            enter = price < v and near and i > 0 and price > bars[i - 1]["close"]
        elif family == "breakout":
            n = int(g["lookback"])
            if i >= n:
                enter = price > max(b["high"] for b in bars[i - n:i])
        elif family == "momentum":
            f, s = sma(closes, int(g["fast"]), i), sma(closes, int(g["slow"]), i)
            pf, ps = sma(closes, int(g["fast"]), i - 1), sma(closes, int(g["slow"]), i - 1)
            enter = None not in (f, s, pf, ps) and pf <= ps and f > s
        elif family == "opening_range":
            enter = i >= int(g["or_bars"]) and price > or_high
        if enter:
            pos = (price, i)
    return trades


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score_agent(family, genome, days):
    all_trades = []
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for day in days:
        for _sym, bars in day.items():
            for pnl in run_agent_day(family, genome, bars):
                all_trades.append(pnl)
                equity += pnl
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)
    n = len(all_trades)
    wins = sum(1 for t in all_trades if t > 0)
    pnl = sum(all_trades)
    win_rate = wins / n if n else 0.0
    # Fitness is explicit and arbitrary on purpose: reward profit and
    # consistency, charge for drawdown. Tune it and say what you changed.
    fitness = pnl - 0.5 * max_dd + 25.0 * win_rate
    return {"family": family, "genome": genome, "trades": n, "pnl": round(pnl, 2),
            "win_rate": round(win_rate, 3), "max_drawdown": round(max_dd, 2),
            "fitness": round(fitness, 2)}


# --------------------------------------------------------------------------
# Evolution: kill the bottom half, breed the survivors
# --------------------------------------------------------------------------

def breed(population, survivors, rng, size):
    by_family = {}
    for s in survivors:
        by_family.setdefault(s["family"], []).append(s)
    children = []
    # Elites: top 10% pass through unchanged.
    elites = survivors[:max(1, size // 10)]
    children.extend({"family": e["family"], "genome": dict(e["genome"])}
                    for e in elites)
    while len(children) < size:
        fam = rng.choice(list(by_family.keys()))
        pool = by_family[fam]
        p1 = rng.choice(pool)
        p2 = rng.choice(pool)
        child = {}
        for k in GENOME_RANGES[fam]:
            v = p1["genome"][k] if rng.random() < 0.5 else p2["genome"][k]
            if rng.random() < 0.25:  # mutation
                lo, hi = GENOME_RANGES[fam][k]
                v = v + rng.gauss(0, (hi - lo) * 0.08)
            child[k] = v
        children.append({"family": fam, "genome": clamp_genome(fam, child)})
    return children


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Evolution harness for paper-trading agents")
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--generations", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--days", type=int, default=5, help="paper sessions per generation")
    ap.add_argument("--out", default=None, help="directory for JSON + summary reports")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    population = []
    for _ in range(args.agents):
        fam = rng.choice(FAMILIES)
        population.append({"family": fam, "genome": random_genome(rng, fam)})

    history = []
    champion = None
    for gen in range(1, args.generations + 1):
        feed = MarketFeed(seed=args.seed + gen, days=args.days)
        days = feed.days()
        scored = [score_agent(a["family"], a["genome"], days) for a in population]
        scored.sort(key=lambda s: s["fitness"], reverse=True)
        survivors = scored[:len(scored) // 2]
        champion = scored[0]
        total_pnl = round(sum(s["pnl"] for s in scored), 2)
        fam_alive = {f: sum(1 for s in survivors if s["family"] == f) for f in FAMILIES}
        print(f"Gen {gen}: fought {len(scored)} agents | swarm paper P&L ${total_pnl:+.2f} "
              f"| killed {len(scored) - len(survivors)} | best: {champion['family']} "
              f"fitness {champion['fitness']} P&L ${champion['pnl']:+.2f} "
              f"win {champion['win_rate']:.0%} trades {champion['trades']}")
        history.append({"generation": gen, "swarm_pnl": total_pnl,
                        "survivors_by_family": fam_alive,
                        "leaderboard": scored[:5], "champion": champion})
        if gen < args.generations:
            population = breed(population, survivors, rng, args.agents)

    print("\nChampion genome (map these knobs onto your paper-trading skills):")
    print(json.dumps(champion, indent=2))

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "history.json"), "w") as f:
            json.dump(history, f, indent=2)
        with open(os.path.join(args.out, "champion.json"), "w") as f:
            json.dump(champion, f, indent=2)
        lines = ["# Trader Swarm — run summary", "",
                 f"Agents per generation: {args.agents} | Generations: {args.generations} "
                 f"| Paper sessions per generation: {args.days} | Seed: {args.seed}", ""]
        for h in history:
            c = h["champion"]
            lines.append(f"- Gen {h['generation']}: swarm P&L ${h['swarm_pnl']:+.2f}; "
                         f"champion {c['family']} (fitness {c['fitness']}, "
                         f"P&L ${c['pnl']:+.2f}, win {c['win_rate']:.0%}, "
                         f"max DD ${c['max_drawdown']:.2f})")
        lines += ["", "Paper trading on a synthetic feed. A champion here has "
                  "proven it fits this feed, nothing more — see README caveats."]
        with open(os.path.join(args.out, "summary.md"), "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\nReports written to {args.out}/ (history.json, champion.json, summary.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
