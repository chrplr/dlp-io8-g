#!/usr/bin/env python3
"""Free-running capture from an Analog Discovery 3, for the pulsetrain tool.

Records one channel continuously and saves the edge times, not the samples. The
companion emitter (cmd/pulsetrain) runs in another process, so nothing this loop
does can be held up by a busy-wait timing a pulse -- which is the whole reason
this exists separately from dlp_timing.py's pulse-stream block.

# Why that separation raises the usable sample rate

pulse-stream fires each trial from inside the drain loop, via ad3.record's
on_tick. The device buffers 16384 samples, so the slack is 16384/rate seconds:
16 ms at 1 MS/s. A 50 ms busy-wait inside that loop overruns the buffer and
loses samples exactly where the falling edge is, which is why that block refuses
rates that fast for long pulses. Here the loop only drains, so 1 MS/s is safe
for any pulse width and the edge resolution is about a microsecond.

# Why edges and not samples

At 1 MS/s a 90 s capture is 180 MB of int16 per channel, and the analysis only
ever wants the crossings. Edges are a few thousand floats. Pass --save-raw to
keep the samples too, when a waveform needs looking at rather than measuring.

Usage, with the capture started first and outliving the emitter:

    ./ad3-capture.py --seconds 150 --out capture.npz
    chrt -f 50 ./pulsetrain -trials 1000 -condition rt -out train.csv
    ./analyse-pulsetrain.py train.csv capture.npz
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ad3 import AD3, falling_edges, rising_edges  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=float, required=True,
                   help="capture duration; must span the whole emission")
    p.add_argument("--rate", type=float, default=1e6,
                   help="sample rate in S/s (default 1e6, ~1 us edge resolution)")
    p.add_argument("--channel", type=int, default=1, choices=(1, 2),
                   help="AD3 analog input the DLP line is wired to (default 1)")
    p.add_argument("--out", default="capture.npz", help="output .npz")
    p.add_argument("--threshold", type=float, default=2.5,
                   help="logic threshold in volts (default 2.5)")
    p.add_argument("--save-raw", action="store_true",
                   help="also store the samples, which is ~180 MB per 90 s at 1 MS/s")
    args = p.parse_args()

    ch = args.channel - 1
    print(f"  AD3            {args.rate/1e3:.0f} kS/s, channel {args.channel}, "
          f"{args.seconds:.0f} s")
    print(f"  threshold      {args.threshold} V")
    print("  recording, and not firing anything: start the emitter now\n")

    with AD3(channels=(ch,), rate=args.rate) as d:
        data, stats = d.record(args.seconds)

    samples = data[ch]
    print(f"  {stats['samples']} samples in {stats['seconds']:.2f} s")

    # Loss is fatal, not a warning. Record mode drops samples when the host
    # stops draining, and a capture with holes has them wherever the host was
    # busiest -- which is exactly where the interesting trials are.
    if stats["lost"] or stats["corrupted"]:
        sys.exit(f"  {stats['lost']} lost, {stats['corrupted']} corrupted: the "
                 f"capture has holes in it. Lower --rate and re-run.")

    rise = rising_edges(samples, args.rate, args.threshold)
    fall = falling_edges(samples, args.rate, args.threshold)
    print(f"  {len(rise)} rising, {len(fall)} falling edges")

    if len(rise) == 0:
        sys.exit("  no edges at all: check the wiring and the threshold, and "
                 "that the emitter ran inside the capture window.")

    out = {"rise": rise, "fall": fall, "rate": args.rate,
           "channel": args.channel, "threshold": args.threshold,
           "seconds": stats["seconds"], "samples": stats["samples"]}
    if args.save_raw:
        out["samples_raw"] = samples
    np.savez_compressed(args.out, **out)
    print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
