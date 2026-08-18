#!/usr/bin/env python3
"""Extract per-trial trigger-to-light onsets from a two-channel AD3 capture.

    ./extract-onsets.py capture.npz onsets.npz

CH1 must carry a TTL trigger and CH2 an optical sensor. For every TTL rising
edge this finds the photodiode's 10% crossing and writes two arrays:

    ttl_ms    the trigger time on the instrument's clock
    onset_ms  the interval from that trigger to the 10% crossing

Both channels are recorded in one acquisition, so the instrument's clock
cancels in `onset_ms` and never has to be reconciled with the host's.

# Why 10% and not 50%

The panel takes 5.5-6.5 ms to go black to white, so "the onset" is a choice of
level, and a different choice shifts every number by milliseconds. 10% is the
earliest level that is safely clear of the baseline, which makes it the closest
available proxy for when the pixel first changes. What matters more than the
choice is that it is the same choice everywhere: the level is recorded in the
output so two files cannot be compared across different ones.

# Why trials are dropped

A trial is skipped when its window runs off either end of the capture, or when
CH2 does not rise by at least `--min-amplitude` of its full swing. The second
case is a photodiode that was not on the stimulus, and averaging it in would
quietly bias the result rather than announce itself. The count is reported.
"""
import argparse
import sys

import os

import numpy as np

from ad3 import logic_levels


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("capture", help="two-channel .npz with raw samples")
    p.add_argument("out", help="output .npz")
    p.add_argument("--level", type=float, default=0.10,
                   help="fraction of each trial's own rise to call the onset (default 0.10)")
    p.add_argument("--pre-ms", type=float, default=8.0, help="baseline window (default 8)")
    p.add_argument("--post-ms", type=float, default=75.0, help="search window (default 75)")
    p.add_argument("--min-amplitude", type=float, default=0.5,
                   help="reject a trial whose CH2 rise is below this fraction of the "
                        "capture's full CH2 swing (default 0.5)")
    args = p.parse_args()

    z = np.load(args.capture)
    if "samples_ch2" not in z.files:
        sys.exit(f"{args.capture} has no samples_ch2; keys are {', '.join(z.files)}")
    rate = float(z["rate"])
    volts = lambda ch: (z[f"offset_v_ch{ch}"]
                        + z[f"range_v_ch{ch}"] * z[f"samples_ch{ch}"].astype(np.float64) / 65536)
    ttl, pd_ = volts(1), volts(2)

    if "rise_ch1" in z.files:
        edges = z["rise_ch1"]
    else:
        # logic_levels, not a percentile: a 1%-duty TTL puts the 99th
        # percentile on the boundary between the two levels. See its docstring.
        lo, hi = logic_levels(ttl)
        above = ttl >= lo + 0.5 * (hi - lo)
        edges = np.flatnonzero(above[1:] & ~above[:-1]) / rate

    # The rejection floor is a fraction of the whole capture's swing, not of the
    # trial's own, because a trial with no stimulus in it has no swing to take a
    # fraction of.
    plo, phi = np.percentile(pd_[::97], (1, 99))
    floor = args.min_amplitude * (phi - plo)

    PRE, POST = int(args.pre_ms * rate / 1000), int(args.post_ms * rate / 1000)
    settle = int(0.001 * rate)
    tail = int(0.005 * rate)
    ttl_ms, onset_ms = [], []
    off_end = low = no_cross = 0
    for x in edges:
        i0 = int(x * rate)
        if i0 - PRE < 0 or i0 + POST >= len(pd_):
            off_end += 1
            continue
        seg = pd_[i0 - PRE:i0 + POST]
        base = np.median(pd_[i0 - PRE:i0 - settle])
        a = np.percentile(seg[-tail:], 90) - base
        if a < floor:
            low += 1
            continue
        k = np.flatnonzero(seg[PRE:] >= base + args.level * a)
        if not k.size:
            no_cross += 1
            continue
        ttl_ms.append(x * 1000)
        onset_ms.append(k[0] / rate * 1000)

    ttl_ms, onset_ms = np.array(ttl_ms), np.array(onset_ms)
    if not len(onset_ms):
        sys.exit("no usable trial: check that CH2 was on the stimulus")
    np.savez_compressed(args.out, ttl_ms=ttl_ms, onset_ms=onset_ms,
                        level=args.level, rate=rate)
    print(f"  {len(edges)} TTL edges, {len(onset_ms)} usable "
          f"({off_end} off the end, {low} below the amplitude floor, {no_cross} never crossed)")
    print(f"  onset at {args.level:.0%}: mean {onset_ms.mean():.3f}  sd {onset_ms.std(ddof=1):.3f}  "
          f"range {onset_ms.min():.2f}-{onset_ms.max():.2f} ms")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
