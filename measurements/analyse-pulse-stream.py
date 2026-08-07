#!/usr/bin/env python3
"""Test the measured pulse widths against a model of the firmware that makes them.

The MEG TTL box sets a line high, records `millis() + w`, and drops the line on
the first pass through loop() where millis() has reached it. The obvious model
of that says the realised width is uniform on [w-1, w] -- a flat histogram
exactly 1 ms wide -- and the measured spread is about twice that. This script
asks whether a model of what the AVR actually does accounts for the difference.

It does not tick at 1 ms. Timer0 overflows every 1024 us, and wiring.c carries a
fractional accumulator (FRACT_INC 3, FRACT_MAX 125) that adds a catch-up
millisecond every ~41.7 overflows, so millis() is right on average and advances
by 2 about one time in 42. The consequence for a pulse is that the number of
overflows needed to reach the target depends on the accumulator's phase when the
pulse started: for some phases n overflows suffice, for the rest it takes n+1.
Each case gives a uniform band 1024 us wide, and the realised width is a mixture
of two of them -- roughly 2 ms across, which is what the instrument sees.

The model has no free parameters. Everything in it is fixed by wiring.c and the
16 MHz clock, so the comparison below is a test, not a fit.
"""

import csv
import sys
from collections import defaultdict

import numpy as np

OVERFLOW_US = 256 * 64 / 16e6 * 1e6   # 1024.0 us, timer0 at /64 on a 16 MHz AVR
FRACT_INC, FRACT_MAX = 3, 125         # from wiring.c


def simulate(requested_ms, n=200000, rng=None):
    """Realised widths the firmware would produce, in ms. No free parameters."""
    rng = rng or np.random.default_rng(0)
    # Phase of the fractional accumulator when the pulse starts, and where the
    # start falls inside the current overflow period. Both uniform.
    f0 = rng.integers(0, FRACT_MAX, n)
    u = rng.uniform(0, OVERFLOW_US, n)
    k = np.zeros(n, dtype=int)
    todo = np.ones(n, dtype=bool)
    for steps in range(1, int(requested_ms / 0.9) + 4):
        # millis() advance after `steps` overflows, given the starting phase.
        adv = steps + (f0 + FRACT_INC * steps) // FRACT_MAX
        newly = todo & (adv >= requested_ms)
        k[newly] = steps
        todo &= ~newly
        if not todo.any():
            break
    return (k * OVERFLOW_US - u) / 1000.0


def ks(a, b):
    """Two-sample Kolmogorov-Smirnov statistic, and the 5% critical value."""
    a, b = np.sort(a), np.sort(b)
    allv = np.concatenate([a, b])
    d = np.abs(np.searchsorted(a, allv, "right") / len(a)
               - np.searchsorted(b, allv, "right") / len(b)).max()
    crit = 1.358 * np.sqrt((len(a) + len(b)) / (len(a) * len(b)))
    return d, crit


def main(paths):
    groups = defaultdict(list)
    for p in paths:
        for r in csv.DictReader(open(p)):
            key = (r["device"], r["condition"], float(r["requested_ms"]))
            groups[key].append(float(r["width_ms"]))

    for (device, cond, req), vals in sorted(groups.items()):
        v = np.array(vals)
        print(f"\n=== {device}  {cond}  requested {req:g} ms   n={len(v)}")
        print(f"  measured   min {v.min():8.3f}  p50 {np.median(v):8.3f}  "
              f"max {v.max():8.3f}  spread {v.max() - v.min():.3f} ms")

        if device != "ttlbox":
            # The DLP width is timed by the host busy-wait, not by firmware, so
            # the model above does not apply and no comparison is drawn.
            err = v - req
            print(f"  error      p50 {np.median(err):+8.4f}  p95 "
                  f"{np.percentile(err, 95):+8.4f}  max {err.max():+8.4f} ms")
            continue

        sim = simulate(req)
        print(f"  firmware   min {sim.min():8.3f}  p50 {np.median(sim):8.3f}  "
              f"max {sim.max():8.3f}  spread {sim.max() - sim.min():.3f} ms")
        print(f"  naive      min {req - 1:8.3f}  p50 {req - 0.5:8.3f}  "
              f"max {req:8.3f}  spread 1.000 ms")

        d, crit = ks(v, sim)
        verdict = "consistent" if d < crit else "REJECTED"
        print(f"  KS vs firmware model: D={d:.4f}, 5% critical {crit:.4f}"
              f"  -> {verdict}")

        # Where the two bands meet, and how the trials divide between them.
        bands = sorted({round(x) for x in np.round(sim / OVERFLOW_US * 1000)})
        edges = [b * OVERFLOW_US / 1000 for b in bands]
        for e in edges:
            frac_m = ((v > e - OVERFLOW_US / 1000) & (v <= e)).mean()
            frac_s = ((sim > e - OVERFLOW_US / 1000) & (sim <= e)).mean()
            print(f"    band ({e - OVERFLOW_US / 1000:.3f}, {e:.3f}] ms: "
                  f"measured {frac_m:5.1%}, model {frac_s:5.1%}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip() + "\n\nusage: analyse-pulse-stream.py <csv>...")
    main(sys.argv[1:])
