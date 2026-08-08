#!/usr/bin/env python3
"""Plot trigger-to-light delay against trial number, with its distribution.

    ./plot-onset-sequence.py --out seq.png --title "my panel" LABEL=onsets.npz
    ./plot-onset-sequence.py --out cmp.png --title "..." \
        "normal priority=a.npz" "SCHED_FIFO 50=b.npz"

Each input is the .npz that extract-onsets.py writes, carrying `onset_ms` and
`ttl_ms`. Give one and you get one panel; give several and you get one row each,
sharing a y-axis so the panels are directly comparable, which is the whole
reason for stacking them rather than putting them in separate files.

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
import argparse
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8985"
GRID = "#e9e8e5"
# Validated categorical pair: blue/orange, CVD-separable. Assigned in fixed
# order, never cycled -- with more than these, the comparison is too crowded to
# read and wants small multiples instead.
SERIES = ["#2a78d6", "#eb6834", "#3f9e5a", "#8a5fc4"]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", metavar="LABEL=FILE",
                   help="an onsets .npz, optionally prefixed with a panel label")
    p.add_argument("--out", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--foot", default="")
    args = p.parse_args()

    if len(args.inputs) > len(SERIES):
        sys.exit(f"at most {len(SERIES)} series: more than that wants small multiples")

    panels = []
    for spec in args.inputs:
        label, _, path = spec.rpartition("=")
        z = np.load(path)
        if "onset_ms" not in z.files:
            sys.exit(f"{path} has no onset_ms; keys are {', '.join(z.files)}")
        y = np.asarray(z["onset_ms"], dtype=np.float64)
        mins = (z["ttl_ms"][-1] - z["ttl_ms"][0]) / 60000 if "ttl_ms" in z.files else None
        panels.append((label, y, mins))

    k = len(panels)
    fig = plt.figure(figsize=(11.5, 3.1 * k + 1.6), facecolor=SURFACE)
    gs = fig.add_gridspec(k, 2, width_ratios=(4.4, 1), wspace=0.03, hspace=0.34)

    # One shared y-range across panels, so a difference in spread is a
    # difference in the picture and not in the axis.
    allv = np.concatenate([y for _, y, _ in panels])
    pad = 0.04 * (allv.max() - allv.min())
    ylim = (allv.min() - pad, allv.max() + pad)

    for i, (label, y, mins) in enumerate(panels):
        colour = SERIES[i]
        x = np.arange(1, len(y) + 1)
        step = np.abs(np.diff(y))
        ax = fig.add_subplot(gs[i, 0], facecolor=SURFACE)
        ah = fig.add_subplot(gs[i, 1], facecolor=SURFACE, sharey=ax)
        for a in (ax, ah):
            a.set_axisbelow(True)
            a.set_ylim(*ylim)
            for s in ("top", "right"):
                a.spines[s].set_visible(False)
            for s in ("left", "bottom"):
                a.spines[s].set_color("#d6d5d1")
            a.tick_params(colors=INK2, labelsize=9)
        ax.grid(True, color=GRID, lw=0.8)
        ah.grid(True, axis="x", color=GRID, lw=0.8)

        # Line and points together: the line carries the plateau structure, the
        # points keep a single-trial excursion from being smoothed into a spike
        # that reads as wider than one trial.
        ax.plot(x, y, color=colour, lw=1.0, alpha=0.55, zorder=2)
        ax.plot(x, y, ".", color=colour, ms=2.6, zorder=3)
        med = float(np.median(y))
        ax.axhline(med, color=MUTED, lw=1, ls=(0, (4, 3)), zorder=1)
        ax.text(len(y) * 0.997, med, f" median {med:.1f} ms ", color=INK2,
                fontsize=9, va="bottom", ha="right")
        ax.set_xlim(0, len(y) + 1)
        ax.set_ylabel("trigger to light, ms", color=INK2, fontsize=10)
        if i == k - 1:
            ax.set_xlabel("trial", color=INK2, fontsize=10)
            ah.set_xlabel("trials", color=INK2, fontsize=10)
        ah.hist(y, bins=48, orientation="horizontal", color=colour, alpha=0.85)
        ah.tick_params(labelleft=False)

        head = (f"{label}   ·   " if label else "")
        head += (f"n = {len(y)}"
                 + (f" over {mins:.1f} min" if mins is not None else "")
                 + f"   ·   sd {y.std(ddof=1):.2f} ms"
                 + f"   ·   range {y.min():.1f}–{y.max():.1f} ms"
                 + f"   ·   {int((step > 1).sum())} steps above 1 ms")
        ax.set_title(head, color=INK if label else INK2, fontsize=10.5,
                     loc="left", pad=7,
                     fontweight="bold" if label else "normal")
        print(f"  {label or path}: n={len(y)}  sd {y.std(ddof=1):.3f} ms  "
              f"range {y.min():.2f}–{y.max():.2f}  median step {np.median(step):.3f} ms  "
              f"steps>1ms {int((step > 1).sum())}")

    if args.title:
        fig.suptitle(args.title, color=INK, fontsize=13, y=0.995)
    fig.text(0.008, 0.004, args.foot, color=MUTED, fontsize=8.5)
    fig.savefig(args.out, dpi=150, facecolor=SURFACE, bbox_inches="tight")
    print(f"  -> {args.out}")


if __name__ == "__main__":
    main()
