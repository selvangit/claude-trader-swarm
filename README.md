# claude-trader-swarm

**An evolution harness for paper-trading strategy agents.**

Build 100 mini trading agents. Let them fight. Kill the bottom 50. Breed the
survivors. Repeat.

Testing 100 strategies by hand takes years. A population, a scoreboard and a
selection loop do it in an afternoon — and the loop is the interesting part,
not any single strategy.

```
FIGHT  ->  every agent trades the same paper sessions and gets scored
KILL   ->  the bottom half of the population is eliminated
BREED  ->  survivors' parameters are mixed + mutated into a new generation
REPEAT ->  weak strategies die out, strong ones evolve
```

## The agents

Each agent is one micro-strategy with its own parameter "genome":

| Family | Idea | Evolvable knobs |
| --- | --- | --- |
| `rsi_reversion` | Buy oversold, exit overbought | RSI period, oversold/overbought levels, stop, target |
| `vwap_bounce` | Buy a dip back up toward VWAP | Band %, stop, target |
| `breakout` | Buy a break above the recent high | Lookback bars, stop, target |
| `momentum` | Ride a fast/slow average crossover | Fast/slow periods, stop, target |
| `opening_range` | Buy a break of the opening range | Opening-range bars, stop, target |

Scoring is explicit: `fitness = P&L − 0.5 × max drawdown + 25 × win rate`.
Reward profit and consistency, charge for drawdown. Tune it and say what
you changed.

Breeding: the top 10% pass through unchanged (elites). The rest are bred
from two survivor parents of the same family — each parameter is picked
from one parent, with a 25% chance of a small mutation.

## Run it

Stdlib only, Python 3.8+. Nothing to install, no API key, no orders — the
built-in market is a deterministic synthetic feed, so a run is identical
for anyone with the same seed.

```bash
python3 swarm.py                              # 100 agents, 5 generations
python3 swarm.py --agents 40 --generations 3  # a quick taste
python3 swarm.py --seed 7 --out runs/demo     # write reports
```

Every generation prints a one-line report (fought / killed / best agent),
and `--out` writes `history.json`, `champion.json` (the best genome, ready
to map onto real strategy knobs) and a human-readable `summary.md`.

## Pairing with the day-trader skills

This harness is the selection layer that sits *above* a strategy pack like
[day-trader-skill](https://github.com/selvangit/day-trader-skill) (11 Claude
skills for scanning, entries, exits and risk — paper trading by default,
nothing places an order on its own). The workflow:

1. Evolve genomes here until a family + parameter set keeps winning.
2. Take `champion.json` and set those knobs on the matching real strategy.
3. Paper-trade the champion on real bars before believing anything.

`MarketFeed` in `swarm.py` is the seam: replace the synthetic feed with
real paper bars (the day-trader pack reads free Yahoo Finance data) and the
same fight → kill → breed loop runs on the market instead of a simulation.

## Honest caveats

- **Paper trading only.** No broker, no orders, no real money — by design.
- A champion on a synthetic feed has proven it fits *that feed*. Evolution
  is a powerful overfitter: a few good sessions can crown a lucky genome.
  More sessions per generation and out-of-sample checks are the antidote.
- Past paper performance is not future performance. Most people who day
  trade lose money; a harness does not change that. This is a learning and
  experimentation tool, not financial advice.

## License

MIT — see [LICENSE](LICENSE).
