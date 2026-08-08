#!/usr/bin/env python3
"""Plot trigger-to-light delay against trial number, with its distribution.

    ./plot-onset-sequence.py onsets.npz "my panel" sequence.png ["footnote"]

Takes the .npz that carries a per-trial `onset_ms` (and optionally `ttl_ms`),
or any raw two-channel capture that plot-onset.py can read.

# Why this plot and not a summary statistic

A standard deviation over a run assumes the run is one population. This delay is
not: it sits on a plateau where consecutive trials differ by tens of
microseconds, and then steps to another plateau several milliseconds away. The
sd is a description of how often it steps, which is not what it looks like it
is describing.

Two consequences, both visible here and neither visible in a number:

  * A short pilot samples one plateau and reports a spread ten times too small.
    The left edge of this plot is what a thirteen-trial run sees.
  * The jitter is not gaussian noise to be averaged down. It is a small number
    of discrete events, so a between-conditions comparison can be biased by
    where the steps happen to fall.

The marginal histogram shares the y-axis with the sequence, so the modal
plateau and the tails are read off the same scale. It is not a second measure
and not a second axis.
"""
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8985"
SERIES, GRID = "#2a78d6", "#e9e8e5"

if len(sys.argv) < 4:
    sys.exit(__doc__)
z = np.load(sys.argv[1])
title, out = sys.argv[2], sys.argv[3]
foot = sys.argv[4] if len(sys.argv) > 4 else ""

if "onset_ms" not in z.files:
    sys.exit(f"{sys.argv[1]} has no onset_ms; keys are {', '.join(z.files)}")
y = np.asarray(z["onset_ms"], dtype=np.float64)
x = np.arange(1, len(y) + 1)

step = np.abs(np.diff(y))
n_big = int((step > 1.0).sum())
minutes = (z["ttl_ms"][-1] - z["ttl_ms"][0]) / 60000 if "ttl_ms" in z.files else None

fig = plt.figure(figsize=(11.5, 5.4), facecolor=SURFACE)
gs = fig.add_gridspec(1, 2, width_ratios=(4.4, 1), wspace=0.03)
ax = fig.add_subplot(gs[0], facecolor=SURFACE)
ah = fig.add_subplot(gs[1], facecolor=SURFACE, sharey=ax)

for a in (ax, ah):
    a.set_axisbelow(True)
    for s in ("top", "right"):
        a.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        a.spines[s].set_color("#d6d5d1")
    a.tick_params(colors=INK2, labelsize=9)
ax.grid(True, color=GRID, lw=0.8)
ah.grid(True, axis="x", color=GRID, lw=0.8)

# Line and points together: the line carries the plateau structure, the points
# keep single-trial excursions from being smoothed into a spike that reads as
# thicker than one trial.
ax.plot(x, y, color=SERIES, lw=1.0, alpha=0.55, zorder=2)
ax.plot(x, y, ".", color=SERIES, ms=2.6, zorder=3)

med = float(np.median(y))
ax.axhline(med, color=MUTED, lw=1, ls=(0, (4, 3)), zorder=1)
ax.text(len(y) * 0.997, med, f" median {med:.1f} ms", color=INK2, fontsize=9,
        va="bottom", ha="right")

ax.set_xlim(0, len(y) + 1)
ax.set_xlabel("trial", color=INK2, fontsize=10)
ax.set_ylabel("trigger to light, ms", color=INK2, fontsize=10)

ah.hist(y, bins=48, orientation="horizontal", color=SERIES, alpha=0.85)
ah.set_xlabel("trials", color=INK2, fontsize=10)
ah.tick_params(labelleft=False)

sub = (f"n = {len(y)}"
       + (f" over {minutes:.1f} min" if minutes is not None else "")
       + f"   ·   sd {y.std(ddof=1):.2f} ms   ·   range {y.min():.1f}–{y.max():.1f} ms"
       + f"   ·   median trial-to-trial step {np.median(step):.2f} ms,"
       + f" {n_big} steps above 1 ms")
fig.text(0.5, 0.985, title, color=INK, fontsize=13, ha="center", va="top")
fig.text(0.5, 0.925, sub, color=INK2, fontsize=9.5, ha="center", va="top")
fig.text(0.008, 0.005, foot, color=MUTED, fontsize=8.5)
fig.subplots_adjust(top=0.86)
fig.savefig(out, dpi=150, facecolor=SURFACE, bbox_inches="tight")
print(f"  n={len(y)}  sd {y.std(ddof=1):.3f} ms  range {y.min():.2f}–{y.max():.2f}  "
      f"median step {np.median(step):.3f} ms  steps>1ms {n_big}  -> {out}")
