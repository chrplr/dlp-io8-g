#!/usr/bin/env python3
"""Regress the realised pulse width on the requested one.

Takes the emitter's log (cmd/pulsetrain) and a free-running AD3 capture
(ad3-capture.py), pairs them, and fits

    measured_width = intercept + slope * target_width

The question the fit answers is not "are they correlated" -- they will be, at
r ~ 1, and that is uninformative. It is whether the slope is 1 and the intercept
is 0, and how large the residual scatter is around them. A slope below 1 means
long pulses lose proportionally more than short ones; a non-zero intercept means
a fixed overhead on every pulse regardless of width. Those are different faults
with different causes, and only a regression over a range of widths separates
them, which is why the widths are random rather than a handful of fixed values.

Three fits are reported, and the comparison between them is the point:

  measured ~ target   what the instrument saw against what was asked for
  host     ~ target   what the host's own clock saw between its two writes
  measured ~ host     what the device and the USB path added to the host's view

If measured~target is poor but host~target is good, the host asked for the right
thing and the device or the link did not deliver it. If both are poor in the
same way, this process was descheduled and the device is not at fault.

Usage:

    ./analyse-pulsetrain.py train.csv capture.npz [--out paired.csv]
"""

import argparse
import csv
import math
import sys

import numpy as np


def ols(x, y):
    """Least squares fit of y on x, with standard errors.

    Returns a dict rather than a tuple because the caller wants to print most of
    it, and positional unpacking of seven values is where transcription errors
    live.
    """
    n = len(x)
    if n < 3:
        raise ValueError(f"need at least 3 points to fit, got {n}")
    xbar, ybar = x.mean(), y.mean()
    sxx = ((x - xbar) ** 2).sum()
    if sxx == 0:
        raise ValueError("every x is identical, so no slope is estimable")
    slope = ((x - xbar) * (y - ybar)).sum() / sxx
    intercept = ybar - slope * xbar
    resid = y - (intercept + slope * x)
    rss = (resid ** 2).sum()
    tss = ((y - ybar) ** 2).sum()
    s2 = rss / (n - 2)
    return {
        "n": n,
        "slope": slope,
        "intercept": intercept,
        "se_slope": math.sqrt(s2 / sxx),
        "se_intercept": math.sqrt(s2 * (1 / n + xbar ** 2 / sxx)),
        "r2": 1 - rss / tss if tss > 0 else float("nan"),
        "resid_sd": math.sqrt(s2),
        "resid": resid,
    }


def two_sided_p(t):
    """Normal approximation to the two-sided p-value. At n~1000, t is z."""
    return math.erfc(abs(t) / math.sqrt(2))


def report(name, x, y, xlabel, ylabel):
    f = ols(x, y)
    t_slope = (f["slope"] - 1) / f["se_slope"]
    t_icept = f["intercept"] / f["se_intercept"]
    print(f"\n  {name}   ({ylabel} on {xlabel}, n={f['n']})")
    print(f"    slope       {f['slope']:.5f} +/- {f['se_slope']:.5f}"
          f"   vs 1: t={t_slope:+.2f}, p={two_sided_p(t_slope):.3g}")
    print(f"    intercept   {f['intercept']:+.4f} +/- {f['se_intercept']:.4f} ms"
          f"   vs 0: t={t_icept:+.2f}, p={two_sided_p(t_icept):.3g}")
    print(f"    residual SD {f['resid_sd']:.4f} ms      R^2 {f['r2']:.6f}")
    err = y - x
    print(f"    error       median {np.median(err):+.4f}  "
          f"p05 {np.quantile(err, .05):+.4f}  p95 {np.quantile(err, .95):+.4f}  "
          f"max |{np.abs(err).max():.4f}| ms")
    return f


def align(target, measured):
    """Find where the emitted sequence starts inside the measured one.

    The capture is started before the emitter and stopped after it, so the
    measured sequence can carry extra pulses at either end. The widths are
    random over a wide range, which makes the sequence a signature: the correct
    offset is the one where the two agree, and every other offset disagrees
    grossly. Slide and take the minimum sum of squared differences.
    """
    n, m = len(target), len(measured)
    if m < n:
        raise SystemExit(
            f"  the capture holds {m} pulses but the emitter fired {n}. The "
            f"capture has to span the whole emission, and loss is fatal to the "
            f"pairing. Re-run with a longer --seconds, started first.")
    best, best_cost = 0, float("inf")
    for k in range(m - n + 1):
        cost = float(((measured[k:k + n] - target) ** 2).sum())
        if cost < best_cost:
            best, best_cost = k, cost
    rms = math.sqrt(best_cost / n)
    return best, rms


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("train", help="CSV written by cmd/pulsetrain")
    p.add_argument("capture", help=".npz written by ad3-capture.py")
    p.add_argument("--out", help="write the paired per-trial rows here")
    p.add_argument("--max-rms", type=float, default=2.0,
                   help="reject the alignment if its RMS error exceeds this (ms)")
    args = p.parse_args()

    with open(args.train, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"{args.train} has no rows")

    target = np.array([float(r["target_width_ms"]) for r in rows])
    target_isi = np.array([float(r["target_isi_ms"]) for r in rows])
    host = np.array([float(r["host_width_ms"]) for r in rows])
    meta = rows[0]

    cap = np.load(args.capture)
    rise, fall, rate = cap["rise"], cap["fall"], float(cap["rate"])

    # Each rising edge pairs with the first falling edge after it. A trailing
    # rise with no fall (the capture ended mid-pulse) is dropped here and shows
    # up as a count mismatch, which is the honest failure.
    widths, rises, falls = [], [], []
    for r in rise:
        later = fall[fall > r]
        if later.size:
            widths.append((later[0] - r) * 1000)
            rises.append(r)
            falls.append(later[0])
    measured = np.array(widths)
    rises = np.array(rises)
    falls = np.array(falls)

    print(f"  emitter        {len(target)} pulses, condition {meta['condition']}, "
          f"{meta['policy']} priority {meta['priority']}, seed {meta['seed']}")
    print(f"  capture        {rate/1e3:.0f} kS/s, {len(rise)} rising / "
          f"{len(fall)} falling edges, {len(measured)} complete pulses")

    k, rms = align(target, measured)
    print(f"  alignment      offset {k}, RMS {rms:.4f} ms")
    if rms > args.max_rms:
        sys.exit(f"\n  the best alignment still disagrees by {rms:.3f} ms RMS, "
                 f"above --max-rms {args.max_rms}. That is not an offset problem: "
                 f"the two files are probably from different runs, or the capture "
                 f"lost pulses in the middle. Nothing below would be meaningful.")
    if len(measured) > len(target):
        print(f"                 dropped {len(measured) - len(target)} pulses "
              f"outside the emission window")

    measured = measured[k:k + len(target)]
    rises = rises[k:k + len(target)]
    falls = falls[k:k + len(target)]

    print("\n  --- widths ------------------------------------------------")
    fit = report("measured ~ target", target, measured, "target", "measured")
    report("host     ~ target", target, host, "target", "host clock")
    report("measured ~ host  ", host, measured, "host clock", "measured")

    # Onset-to-onset, not the requested gap: the ISI the emitter asked for is
    # the low period, so the realised cycle is width + isi. Comparing measured
    # onset spacing against target width+isi keeps the two definitions aligned.
    if len(rises) > 1:
        print("\n  --- intervals ---------------------------------------------")
        meas_cycle = np.diff(rises) * 1000
        targ_cycle = (target + target_isi)[:-1]
        report("onset-to-onset   ", targ_cycle, meas_cycle, "target", "measured")

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["condition", "policy", "priority", "seed", "trial",
                        "target_width_ms", "host_width_ms", "measured_width_ms",
                        "residual_ms", "rise_s", "fall_s"])
            for i in range(len(target)):
                w.writerow([meta["condition"], meta["policy"], meta["priority"],
                            meta["seed"], i,
                            f"{target[i]:.4f}", f"{host[i]:.4f}",
                            f"{measured[i]:.4f}", f"{fit['resid'][i]:.4f}",
                            f"{rises[i]:.6f}", f"{falls[i]:.6f}"])
        print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
