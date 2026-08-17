#!/usr/bin/env python3
"""Free-running capture from an Analog Discovery 3.

Records one or two channels continuously and saves the edge times, not the
samples. Nothing is fired from inside this loop: the thing being measured runs
in another process, so nothing here can be held up by a busy-wait timing a
pulse. That is the whole reason this exists separately from dlp_timing.py's
pulse-stream block.

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

# Two channels, two different signals

--channels 1,2 captures both inputs in one acquisition on one timebase, which is
what an interval between them needs: the device's clock offset cancels and never
has to be reconciled with the host's. The two rarely want the same settings, so
--range, --offset and --attenuation each accept either one value or a
comma-separated value per channel. A 0-5 V logic line and a 9 V-powered
photodiode behind a 10x probe do not share a window.

Note that with a 10x probe the range is measured at the probe, so the two
reachable ranges become 50 V and 500 V rather than 5 V and 50 V. ad3.AD3 refuses
a range the device cannot honour rather than silently substituting one.

Thresholds default to the midpoint of each channel's own 1st..99th percentile,
so they scale themselves to whatever the signal turns out to be instead of
depending on a constant that suits 5 V logic and nothing else.

Usage, with the capture started first and outliving the emitter:

    ./ad3-capture.py --seconds 150 --out capture.npz
    chrt -f 50 ./pulsetrain -trials 1000 -condition rt -out train.csv
    ./analyse-pulsetrain.py train.csv capture.npz

    # two channels: a TTL on CH1 and a photodiode behind a 10x probe on CH2
    ./ad3-capture.py --channels 1,2 --seconds 30 \\
        --range 5,50 --offset 2.5,25 --attenuation 1,10 --out both.npz
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ad3 import AD3, falling_edges, rising_edges  # noqa: E402


def per_channel(spec, channels, name, cast=float):
    """One value for every channel, or a comma-separated value per channel."""
    parts = [p.strip() for p in str(spec).split(",") if p.strip()]
    if len(parts) == 1:
        return {ch: cast(parts[0]) for ch in channels}
    if len(parts) != len(channels):
        raise SystemExit(f"--{name}: give one value or {len(channels)}, got {len(parts)}")
    return {ch: cast(p) for ch, p in zip(channels, parts)}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=float, required=True,
                   help="capture duration; must span the whole emission")
    p.add_argument("--rate", type=float, default=1e6,
                   help="sample rate in S/s (default 1e6, ~1 us edge resolution)")
    p.add_argument("--channels", default="1",
                   help="AD3 analog inputs to record, e.g. 1 or 1,2 (default 1)")
    p.add_argument("--range", default="5",
                   help="input range in V, one value or one per channel (default 5)")
    p.add_argument("--offset", default="2.5",
                   help="window centre in V, one value or one per channel (default 2.5)")
    p.add_argument("--attenuation", default="1",
                   help="probe attenuation, one value or one per channel (default 1)")
    p.add_argument("--threshold", default=None,
                   help="absolute threshold in V; default is each channel's own 50%% level")
    p.add_argument("--out", default="capture.npz", help="output .npz")
    p.add_argument("--save-raw", action="store_true",
                   help="also store the samples, which is ~180 MB per 90 s at 1 MS/s")
    args = p.parse_args()

    chans = [int(c) - 1 for c in args.channels.split(",") if c.strip()]
    if not chans or any(c not in (0, 1) for c in chans):
        sys.exit("--channels: the AD3 has analog inputs 1 and 2")
    rng = per_channel(args.range, chans, "range")
    off = per_channel(args.offset, chans, "offset")
    att = per_channel(args.attenuation, chans, "attenuation")

    print(f"  AD3            {args.rate/1e3:.0f} kS/s, {args.seconds:.0f} s")
    for ch in chans:
        print(f"  channel {ch+1}      range {rng[ch]:g} V, offset {off[ch]:g} V, "
              f"attenuation x{att[ch]:g}")
    print("  recording, and not firing anything: start the emitter now\n")

    with AD3(channels=tuple(chans), rate=args.rate,
             range_v=rng, offset_v=off, attenuation=att) as d:
        data, stats = d.record(args.seconds)
        applied_r, applied_o = dict(d.range_v), dict(d.offset_v)

    print(f"  {stats['samples']} samples in {stats['seconds']:.2f} s")

    # Loss is fatal, not a warning. Record mode drops samples when the host
    # stops draining, and a capture with holes has them wherever the host was
    # busiest -- which is exactly where the interesting trials are.
    if stats["lost"] or stats["corrupted"]:
        sys.exit(f"  {stats['lost']} lost, {stats['corrupted']} corrupted: the "
                 f"capture has holes in it. Lower --rate and re-run.")

    out = {"rate": args.rate, "channels": np.array([c + 1 for c in chans]),
           "seconds": stats["seconds"], "samples": stats["samples"]}
    total_rise = 0
    for ch in chans:
        kw = dict(range_v=applied_r[ch], offset_v=applied_o[ch])
        if args.threshold is None:
            kw["relative"] = 0.5
        else:
            kw["volts"] = float(args.threshold)
        # A flat channel must not cost the capture. rising_edges refuses to
        # threshold noise, which is the right call for an analysis and the wrong
        # one here: this runs BEFORE the file is written, so on 2026-08-17 an
        # unplugged second channel discarded a 330 s recording — 5.5 minutes of
        # a stimulus that cannot be re-run on demand — for want of edges nobody
        # needed yet. The refusal is now a warning and the samples are saved.
        try:
            rise = rising_edges(data[ch], args.rate, **kw)
            fall = falling_edges(data[ch], args.rate, **kw)
        except ValueError as e:
            print(f"  CH{ch+1}: no usable edges — {e}")
            rise = fall = np.empty(0)
        total_rise += len(rise)
        v = applied_o[ch] + applied_r[ch] * data[ch].astype(np.float64) / 65536
        lo, hi = np.percentile(v, (1, 99))
        thr = lo + 0.5 * (hi - lo) if args.threshold is None else float(args.threshold)
        print(f"  CH{ch+1}: {lo:+.4f} .. {hi:+.4f} V (1st..99th pct), threshold "
              f"{thr:.4f} V -> {len(rise)} rising, {len(fall)} falling")
        out[f"rise_ch{ch+1}"] = rise
        out[f"fall_ch{ch+1}"] = fall
        # The applied settings travel with the data. A capture saved without
        # them cannot be turned back into volts, and they are not necessarily
        # the ones that were requested.
        out[f"range_v_ch{ch+1}"] = applied_r[ch]
        out[f"offset_v_ch{ch+1}"] = applied_o[ch]
        out[f"attenuation_ch{ch+1}"] = att[ch]
        out[f"threshold_v_ch{ch+1}"] = thr
        if args.save_raw:
            out[f"samples_ch{ch+1}"] = data[ch]

    # Single-channel captures keep the original key names, so analysis written
    # against them - analyse-pulsetrain.py - still loads without change.
    if len(chans) == 1:
        out["rise"] = out[f"rise_ch{chans[0]+1}"]
        out["fall"] = out[f"fall_ch{chans[0]+1}"]
        out["channel"] = chans[0] + 1
        out["threshold"] = out[f"threshold_v_ch{chans[0]+1}"]

    # Written before any complaint about content: the recording is the
    # expensive, unrepeatable part and the edges are derived from it.
    np.savez_compressed(args.out, **out)
    print(f"\n  wrote {args.out}")

    if total_rise == 0:
        sys.exit("  no rising edge on any channel: check the wiring, the probe "
                 "attenuation, and that the emitter ran inside the capture window. "
                 "The samples were saved regardless.")


if __name__ == "__main__":
    main()
