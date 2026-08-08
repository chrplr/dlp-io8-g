#!/usr/bin/env python3
"""Plot a TTL trigger against individual photodiode trials, aligned on the TTL.

Reads the .npz that ad3-capture.py writes with --save-raw, for a two-channel
capture where CH1 carries the trigger and CH2 an optical sensor:

    ./ad3-capture.py --channels 1,2 --seconds 12 --rate 5e5 \
        --range 5,50 --offset 2.5,25 --attenuation 1,10 \
        --save-raw --out capture.npz
    ./plot-onset.py capture.npz "my panel" onset.png

Every trial is cut at its own TTL rising edge, so t=0 is the trigger and the
spread between the curves is real trigger-to-light jitter -- not an artifact of
averaging. Nothing here is averaged: a pointwise mean of onset-jittered traces
has a rise several times longer than any individual trial, which would misread
as a slow display.

Edges come from the capture's rise_ch1 if it has one and are detected here
otherwise, so a raw-only .npz saved by some other harness still plots. The
crossings are always found at the full sample rate; only the drawing is
decimated, because six hundred trials at 1 MS/s is fifty million points and
matplotlib will not thank you for them.

The rug beneath the traces marks each trial's own 10% crossing, so the jitter is
visible as a distribution and not only as a fan of curves.
"""
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ad3 import logic_levels  # noqa: E402

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8985"
TTL_C, TRIAL_C = "#2a78d6", "#9c9b97"

if len(sys.argv) < 4:
    sys.exit(__doc__)
z = np.load(sys.argv[1]); title = sys.argv[2]; out = sys.argv[3]
rate = float(z["rate"])
try:
    ttl = z["offset_v_ch1"] + z["range_v_ch1"] * z["samples_ch1"].astype(np.float64) / 65536
    pd_ = z["offset_v_ch2"] + z["range_v_ch2"] * z["samples_ch2"].astype(np.float64) / 65536
except KeyError:
    sys.exit("this capture has no raw samples: re-run ad3-capture.py with "
             "--channels 1,2 --save-raw")
if "rise_ch1" in z.files:
    edges = z["rise_ch1"]
else:
    # No precomputed edges: find them on CH1 at the midpoint of its own
    # 1st..99th percentile band, the same rule ad3-capture.py uses by default.
    # logic_levels, not a percentile: a 1%-duty TTL puts the 99th percentile
    # on the boundary between the two levels. See its docstring.
    lo, hi = logic_levels(ttl)
    above = ttl >= lo + 0.5 * (hi - lo)
    edges = np.flatnonzero(above[1:] & ~above[:-1]) / rate

PRE, POST = int(0.008 * rate), int(0.075 * rate)
trials, ttls, t10s = [], [], []
for x in edges:
    i0 = int(x * rate)
    if i0 - PRE < 0 or i0 + POST >= len(pd_):
        continue
    base = np.median(pd_[i0 - PRE:i0 - int(0.001 * rate)])
    seg = pd_[i0 - PRE:i0 + POST]
    peak = np.percentile(seg[-int(0.005 * rate):], 90)
    a = peak - base
    if a < 4.0:
        continue
    k = np.flatnonzero(seg[PRE:] >= base + .1 * a)
    if not k.size:
        continue
    trials.append(seg); ttls.append(ttl[i0 - PRE:i0 + POST]); t10s.append(k[0] / rate * 1000)
trials = np.array(trials); t10s = np.array(t10s); n = len(trials)
t = (np.arange(-PRE, POST) / rate) * 1000
spread = t10s.max() - t10s.min()

fig, ax = plt.subplots(figsize=(11, 5.6), facecolor=SURFACE)
ax.set_facecolor(SURFACE)
ax.grid(True, color="#e9e8e5", lw=0.8); ax.set_axisbelow(True)
for s in ("top", "right"): ax.spines[s].set_visible(False)
for s in ("left", "bottom"): ax.spines[s].set_color("#d6d5d1")
ax.tick_params(colors=INK2, labelsize=9)

# One LineCollection of decimated traces rather than n Line2Ds of every
# sample. Peak-preserving is not needed: these are slow optical ramps, not
# something with structure between samples.
step = max(1, len(t) // 2000)
alpha = 0.9 if n <= 40 else max(0.06, 6.0 / n)
ax.add_collection(LineCollection(
    [np.column_stack((t[::step], tr[::step])) for tr in trials],
    colors=TRIAL_C, linewidths=0.9, alpha=alpha, zorder=1))
ax.plot(t[::step], np.median(np.array(ttls), axis=0)[::step], color=TTL_C, lw=2,
        zorder=3, label="DLP-IO8 TTL trigger")
ax.plot([], [], color=TRIAL_C, lw=1.4, label=f"photodiode, {n} individual trials")
ax.axvline(0, color=MUTED, lw=1, ls=(0, (4, 3)), zorder=0)

# A rug of each trial's 10% crossing, directly under the traces: the jitter as
# a distribution rather than only as a fan of curves.
y0 = -0.55
ax.add_collection(LineCollection(
    [[(v, y0), (v, y0 + 0.30)] for v in t10s], colors=TRIAL_C,
    linewidths=1.1, alpha=1.0 if n <= 40 else 0.25, zorder=2))
ax.annotate("", xy=(t10s.min(), y0 - 0.22), xytext=(t10s.max(), y0 - 0.22),
            arrowprops=dict(arrowstyle="<->", color=INK, lw=1.3))
ax.text((t10s.min() + t10s.max()) / 2, y0 - 0.95,
        f"onset (10%) spans {spread:.1f} ms", ha="center", color=INK,
        fontsize=10.5, fontweight="bold")
ax.text(t10s.max() + 1.5, y0 + 0.05,
        f"{t10s.min():.1f} – {t10s.max():.1f} ms", color=INK2, fontsize=9, va="center")

ax.set_ylim(-1.9, 8.9)
ax.set_xlim(t[0], t[-1])
ax.set_yticks([0, 2, 4, 6, 8])
ax.set_xlabel("milliseconds after the TTL rising edge", color=INK2, fontsize=10)
ax.set_ylabel("volts", color=INK2, fontsize=10)
ax.set_title(title, color=INK, fontsize=13, loc="left", pad=12)
leg = ax.legend(frameon=False, loc="center right", fontsize=9.5, bbox_to_anchor=(1.0, 0.62))
for tx in leg.get_texts(): tx.set_color(INK2)
fig.text(0.008, 0.005, sys.argv[4] if len(sys.argv) > 4 else "", color=MUTED, fontsize=8.5)
fig.savefig(out, dpi=150, facecolor=SURFACE, bbox_inches="tight")
print(f"  n={n}  onset {t10s.min():.1f}–{t10s.max():.1f} ms  spread {spread:.1f} ms  sd {t10s.std():.2f} ms")
