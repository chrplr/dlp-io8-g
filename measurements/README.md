# Timing measurements — DLP-IO8-G

Raw data and reproduction instructions. Every figure here is measured on
hardware; anything not measured says so.

Device: DLP-IO8-G, FT232RL, 115200 8N1, `usb-DLP_Design_DLP-IO8_12345678`.
Host: `is158520`, Linux, `ftdi_sio` VCP driver, 22 cores. Sessions
2026-08-07 (Python, `2026-08-07-dlp/`) and 2026-08-08 (Go, on the published
`dlpio8` module, `2026-08-08-dlp-go/`).

```bash
./dlp_timing.py poll     --out <dir> --latency-timer 1   # no wiring
./dlp_timing.py loopback --out <dir> --latency-timer 1   # ch1 -> ch8 jumper
./dlp_timing.py discard  --out <dir>                     # ch1 -> ch8 jumper
./dlp_timing.py skew     --out <dir> --probe-map 2,3,4   # scope on 4 channels
./dlp_timing.py pulse    --out <dir> --condition idle    # scope on ch1
./dlp_timing.py pulse-stream --device ttlbox --trials 1000 --out <dir>  # AD3
./dlp_timing.py h2h-stream   --trials 2000 --out <dir>                  # AD3
./dlp_timing.py <block> --help
```

Two of these have Go counterparts, built on the published
[dlpio8](https://github.com/chrplr/dlpio8) module rather than on the Python
client here. They exist to exercise that module on the same rig, and because the
second measures something the Python blocks do not:

```bash
cd measurements
go build ./cmd/roundtrip ./cmd/pulsetrain        # binaries named for their packages
sudo ./roundtrip -trials 300 -out <dir>          # ch1 -> ch2 jumper, sweeps the timer
ad3-capture --seconds 150 --out cap.npz &        # started first, outlives the emitter
chrt -f 50 ./pulsetrain -trials 1000 -condition rt -out train.csv
./analyse-pulsetrain.py train.csv cap.npz --out paired.csv
```

The loopback blocks need one jumper, ch1 to ch8; both channels are on the same
board so the ground is already common. Remove that jumper before probing ch8
with the scope, or ch1 and ch8 are shorted and their skew reads zero for a
wiring reason.

## Two instruments

The `skew`, `pulse` and `headtohead` blocks were run with a **Siglent
SDS1104X-E** over LAN. It ships on 10.11.13.0/24 and does not fall back to
link-local, so a direct cable needs the host on that subnet (`sudo ip addr add
10.11.13.1/24 dev <iface>`). Probes at 1x, not 10x: this is a 0-5 V logic signal.

The `*-stream` blocks were run with a **Digilent Analog Discovery 3** through the
WaveForms SDK. The difference is not resolution but sample size: a scope driven
over SCPI costs about a second and a half per armed acquisition, which caps a
sitting at n≈100, while the AD3 records continuously to host memory and a single
82-second capture holds n=2000. Use the **analog** inputs for 5 V logic — the 16
digital channels are 3.3 V.

Two things bound how fast a streaming capture can safely run, and only the first
is obvious. The device sends `rate x channels x 2` bytes per second and the host
must absorb all of it; but the draining loop also does its own work per
iteration, and if that work allocates, the interpreter is doing memory
management inside the window where the on-board buffer is filling. Captures at
1 MS/s lost a buffer even at real-time priority, where scheduling cannot be the
cause. ad3-capture preallocates its output for that reason, and the streaming blocks
default to 250 kS/s.

That ceiling is a property of firing trials from inside the drain loop, not of
the rate. A capture that only drains ran **120 s at 1 MS/s with zero lost and
zero corrupted samples** (2026-08-08, 120 M samples, one channel). Under
`stress-ng` on every core it did lose samples at that rate, and refused to write
the file; reserving one core for the capture and dropping to 250 kS/s held it to
zero across all three loaded runs. Every block aborts rather than writing a file if the SDK
reports any lost or corrupted samples: a capture with holes has them wherever the
host was busiest, which is exactly where the interesting trials are.

---

## Dependency: ad3-capture

The AD3 instrument code used to live here — `ad3.py` and `ad3-capture.py` — and
now lives in [ad3-capture](https://github.com/chrplr/ad3-capture), with the
commit history. It was never DLP-specific: a WaveForms wrapper and a two-channel
capture CLI, with a second user in
[goxpyriment](https://github.com/chrplr/goxpyriment)'s display and
audio-visual timing. A reader looking for the AD3 toolchain would not think to
look in a repo named for a USB TTL box, and on 2026-08-16 neither did its author.

Everything here that touches the AD3 needs it installed:

```bash
pip install -e ../ad3-capture      # or from wherever it is checked out
```

`dlp_timing.py`'s `pulse-stream` and `h2h-stream` blocks, `extract-onsets.py`,
`extract-av-sync.py` and `plot-onset.py` all `import ad3`; the capture command is
now `ad3-capture` on PATH rather than `./ad3-capture.py` in this directory.

### Still here, and arguably shouldn't be

- `extract-av-sync.py` — audio-visual lag from a photodiode and an audio channel
  recorded on one AD3 acquisition, for goxpyriment's `Timing-Tests -test av`.
  The audio channel cannot use `rising_edges` (a 440 Hz tone crosses any
  threshold 880 times a second), so it computes a running-RMS envelope and takes
  the crossing there. It also counts silence inside a tone, which is how an
  audio buffer that underruns shows itself.

  Validated on a synthetic capture rather than against hardware truth: a planted
  26.500 ms lag came back as 25.590 ms — the two 10 % level choices plus the
  envelope window, a constant — with SD 0.074 ms and 5 of 5 planted dropouts
  found. Treat the absolute lag as uncalibrated and the scatter and slope as
  measurements.

  It belongs with goxpyriment rather than here; it stays for now because moving
  it is a separate decision from extracting the instrument code.

## The headline: the FTDI latency timer

The driver default is **16 ms**, and nothing in this repository has ever changed
it. It governs the receive path: the FTDI chip holds a partly-filled buffer for
this long before sending it to the host, so a poll the module answers instantly
still takes that long to come back.

Round trip for a full 8-channel read, n=300 per setting:

| `latency_timer` | median round trip | sustained poll rate |
|---|---|---|
| **16 (default)** | **15.979 ms** | **63 Hz** |
| 8 | 7.986 ms | 125 Hz |
| 4 | 3.988 ms | 251 Hz |
| 2 | 1.996 ms | 501 Hz |
| 1 | 1.005 ms | 995 Hz |

The relationship is exactly `round trip = latency_timer`, which says the
module's own processing is negligible and the entire cost is driver batching.
Reading one channel costs the same as reading all eight — the timer, not the
data, is what you wait for.

**A poll loop is the worst case, not the average.** A loop that waits for each
reply synchronises itself to the timer and pays the full 16 ms every iteration;
an isolated read lands at a random phase and averages half. Both were observed:
the ping loop pinned at 15.97 ms, while isolated reads came in around 9–11 ms.

Set it, and make it survive a replug:

```bash
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB1/latency_timer
# persistent:
# SUBSYSTEM=="usb-serial", DRIVERS=="ftdi_sio", ATTR{latency_timer}="1"
```

It has no effect on the write path, which USB frame scheduling governs instead.

## Write, through the device, and back

Host writes `1`, then polls until channel 8 reads high. Both timestamps are the
host's, so nothing has to be reconciled across clocks. n=200.

| `latency_timer` | median | max |
|---|---|---|
| 16 | 15.987 ms | 16.503 ms |
| 1 | 0.996 ms | 1.133 ms |

This bounds the write path plus the read path together. It cannot separate them,
and neither can anything else here: **the module has no clock**, so there is no
second timestamp to difference against. Splitting this figure, or getting an
absolute host-to-edge latency at all, needs an oscilloscope or a BBTK.

### The same, swept, in Go

`cmd/roundtrip` is a Go port of this block over a ch1 → ch2 jumper, writing
`roundtrip-go-lt*.csv` with the same columns. Unlike the Python block it sweeps
the timer in one run, which needs root; it probes sysfs for writability before
measuring anything, so a run without `sudo` fails immediately rather than
collecting whichever setting the machine was already in and labelling the rest
with settings it never applied. n=300 per setting, 2026-08-08:

| `latency_timer` | min | median | p95 | max | median − setting | poll rate |
|---|---|---|---|---|---|---|
| 1 | 0.851 | 0.994 | 1.048 | 1.102 | −6.1 µs | 1006 Hz |
| 2 | 1.835 | 1.996 | 2.044 | 2.116 | −4.0 µs | 501 Hz |
| 4 | 3.798 | 3.996 | 4.044 | 4.190 | −4.0 µs | 250 Hz |
| 8 | 7.840 | 7.997 | 8.059 | 8.179 | −3.3 µs | 125 Hz |
| 16 | 15.367 | 15.992 | 16.082 | 16.418 | −8.5 µs | 62.5 Hz |

Regressing all 1500 trials on the setting rather than reading the column by eye:

    round trip = 1.000053 (± 0.000225) × latency_timer − 7.5 (± 1.9) µs
    residual SD 47.5 µs, R² 0.99992

**The slope is 1 to within 2 parts in 10⁴.** Everything that is not the latency
timer — the write, the module's own processing, both USB traversals — comes to
−7.5 µs, which is to say nothing resolvable at this scale. The earlier statement
that "round trip = latency_timer" was read off five medians; this is the same
claim with an interval on it.

**Every one of the 1500 trials completed in a single poll.** The first read
issued after the write already returned 1, at every setting including 1 ms. So
the write, the edge, and the module noticing it all fit inside one latency-timer
period even at the shortest — an upper bound of about 1 ms on the whole
non-polling path, and still not a way to split it, for the reason the section
above gives.

Against the Python at the two settings it also measured: 0.994 vs 0.996 ms at
lt=1, and 15.992 vs 15.987 ms at lt=16. That agreement is worth exactly what it
is and no more. The two implementations run the same method against the same
device over the same USB path, so it corroborates the Go client and the port,
not the physics — it is not two independent routes to one quantity. What it does
rule out is a systematic error in either client library: a spurious flush, a
mis-set line discipline, an extra round trip per read. Either one having such a
fault would separate them.

## Discarding queued output: hypothesis not supported

`clang/dlp.c` calls `tcflush(fd, TCOFLUSH)` before every write, and earlier
versions of the Go client called `ResetOutputBuffer()`. Both discard queued
output rather than draining it, which looked like it should be able to drop a
trigger byte.

**It does not fire here.** Writing 8, 64, 200 and 1000 command bytes and
discarding immediately afterwards, every byte still arrived — 1000/1000 in the
largest case. The reason is visible in the timing: that 1000-byte write itself
took 55.7 ms to return, because a blocking write does not come back until the
driver has taken the data. By the time the flush runs there is nothing left in
the kernel queue to discard.

On the strength of this result the Go client
([github.com/chrplr/dlpio8](https://github.com/chrplr/dlpio8)) no longer flushes
the output buffer at all; the comment in its `write` method points back here.

So the flush is **pointless rather than harmful**, in this configuration. It
would become harmful if the port were ever opened non-blocking. Removing it is
still worth doing, as clarity and as insurance, but it is not a bug fix and the
repository's existing data is not suspect because of it.

Two limits on that conclusion, both real: it was tested through pyserial, not
through the C and Go paths themselves; and a level-based device only ever shows
you its *last surviving* command, so a byte dropped mid-burst leaves no trace a
readback can find. Counting the edges actually produced needs an instrument.

## Inter-line skew, and what a trigger code costs

Measured with a Siglent SDS1104X-E at 200 µs/div, 250 MSa/s, over LAN. DLP ch1-4
on scope CH1-4, ch1 as the trigger and the reference for every delay. n=99 per
arm, single-shot acquisitions.

| separation | forward `12345678` | reverse `87654321` |
|---|---|---|
| 1 byte | 83.15 µs | 83.25 µs |
| 2 bytes | 175.58 µs | 175.50 µs |
| 3 bytes | 258.71 µs | 258.83 µs |

A second pass moved the probes to ch6, ch7 and ch8, keeping ch1 as reference, to
measure the full span rather than extrapolate it. n=99, forward pattern only.

| separation | measured |
|---|---|
| +5 bytes (ch6) | 434.24 µs |
| +6 bytes (ch7) | 517.67 µs |
| **+7 bytes (ch8)** | **609.47 µs** |

**A full 8-channel code takes 609 µs to settle — measured, not extrapolated.**
Per byte 87.07 µs. Pass 1's extrapolation from three separations gave 604 µs, so
it was good to 1 %: the spacing stays linear all the way to the eighth byte, with
no FIFO or buffer effect appearing at higher byte counts.

The reverse arm is the control, and it passes: sending the bytes in the opposite
order reverses the edge order and reproduces the same magnitudes to within
0.2 µs. The module acts on bytes strictly in arrival order, and the skew is
serialisation. Nothing about it is channel-specific.

### The spacing is quantised, not jittery

Every inter-byte gap is either **83.0 µs or 92.7 µs and never anything in
between** — 0 of 99 values fell in the 86-90 µs band, in either arm. The two
clusters are 9.70 µs apart. The module services arriving bytes on an internal
grid rather than acting the instant one lands.

Pass 2 reproduces this independently, at a different part of the byte stream:
the 6→7 and 7→8 gaps cluster at 82.94 and 92.68 µs, again with **0 of 99** values
in between, against pass 1's 82.97 and 92.67 for the 1→2 gap. Same grid, same
spacing, four bytes further along.

The gaps are anti-correlated, so the error does not accumulate. Across all seven
gaps the ch1→ch8 span spreads only 14.25 µs (597.79 to 612.04), where seven
independent 9.7 µs quantisations would give far more. The underlying byte rate is
fixed and only the phase against the grid moves, so settling is highly
reproducible: 609 µs, give or take about 7.

One small offset worth recording: the measured byte period is 86.24 µs where
115200 8N1 predicts 86.81, so the module's effective bit rate runs about 0.7 %
fast. Both arms and all three separations agree on it, so it is real rather than
measurement error.

### What this means at 1 kHz

609 µs is comfortably "less than 1 ms" — but that is the wrong test for a system
sampling every millisecond. The right question is whether a sample can land
inside the transition, and 609 µs is about 61 % of a sample period: roughly
three times in five, a code change is sampled mid-update and the acquisition
records a value that was never intended.

So **do not send binary codes across several lines** unless the acquisition
reads the code some milliseconds after the onset edge rather than latching it at
the edge, or a separate strobe line is raised last once the code has settled.

The recommended pattern is **one line per event type, pulsed**: each onset is a
single command byte, so there is no skew at all and up to 8 event types remain
distinguishable. Skew also scales with how many lines actually change, so a
client that sent only the changed bytes would cut it proportionally — `write_mask`
in `dlpio8.py` currently always sends all eight.

## Single-line pulse timing, idle and under load

A single channel is one command byte, so there is no skew to worry about. What
is left is how faithfully a pulse the host asks for appears on the wire. Scope
measures the realised positive width; the host records its own busy-wait
interval alongside, which is what separates a transport problem from a
scheduling one. n=50 per width.

**Idle:**

| requested | on the wire | median error | spread |
|---|---|---|---|
| 5 ms | 4.990 ms | −10 µs | 50 µs |
| 10 ms | 9.990 ms | −10 µs | 110 µs |
| 20 ms | 19.990 ms | −10 µs | 60 µs |
| 50 ms | 49.980 ms | −20 µs | 120 µs |

**Under CPU load (`stress-ng --cpu 0`), normal priority:**

| requested | on the wire | median error | spread | host's own busy-wait |
|---|---|---|---|---|
| 5 ms | 6.330 ms | **+1.33 ms** | 4.75 ms | 6.408 ms |
| 10 ms | 11.840 ms | **+1.85 ms** | 3.01 ms | 11.929 ms |
| 20 ms | 19.980 ms | −20 µs | 1.80 ms | 20.056 ms |
| 50 ms | 49.990 ms | −10 µs | 2.58 ms | 50.077 ms |

**Under the same CPU load, at real-time priority (`chrt -f 50`):**

| requested | on the wire | median error | spread |
|---|---|---|---|
| 5 ms | 4.950 ms | −50 µs | 120 µs |
| 10 ms | 9.940 ms | −60 µs | **70 µs** |
| 20 ms | 19.950 ms | −45 µs | 110 µs |
| 50 ms | 49.960 ms | −35 µs | 110 µs |

Real-time priority recovers idle-quality timing entirely: a **25 to 40 fold
reduction in spread** under identical load. Every row records the policy and
priority it was collected under (`SCHED_FIFO`, 50) rather than a trusted label —
the block refuses to write a file marked `rt` if the process is not actually
real-time, since a mislabelled file would read as evidence that real-time
scheduling does not help.

### The DLP is not what degrades

Compare the last two columns. At 5 ms the host's own busy-wait measured
6.408 ms and the wire showed 6.330 — the wire tracked the host to within 80 µs.
Same at 10 ms: 11.929 against 11.840.

**The USB path added essentially nothing. The host's timing loop was
preempted**, the second write went out late, and the module faithfully
reproduced the host's mistake. Without the host column this reads as "the DLP
degrades under load", which is wrong and sends you looking for a fix in the
wrong place.

So for single-line triggering at 1 kHz the device is not the limiting factor by
two orders of magnitude — the stimulus PC's scheduling is. The fix is real-time
priority for the experiment process (a grant in `/etc/security/limits.d/` plus
`chrt`), not anything about this hardware, and the third table above shows it
working: under load that otherwise cost milliseconds, `chrt -f 50` brings the
spread back to 70-120 µs.

The same conclusion arrived independently from the NeuroSpin MEG TTL box, whose
host round-trip tail went from 6 ms idle to 25 ms under the same load. Two
unrelated devices, the same host, the same answer.

## Head to head with the NeuroSpin MEG TTL box

Both devices driven from one host loop, both edges captured in one scope
acquisition. Wiring: TTL box D30 on CH1, DLP ch1 on CH2, common ground.

### Write latency: the difference is not resolved, and here is why

Writing to A then B and measuring the edge gap includes the host's own gap
between the two writes, which is unknown. Running both orders looks like it
removes it:

    delta(A then B) = w_A + lat_B - lat_A
    delta(B then A) = w_B + lat_A - lat_B

where `w` is how long the first `write()` call takes to return, since that is
when the second one starts. Half the difference is the latency difference plus
`(w_A - w_B)/2` — so the method works only if the two write calls cost the same,
or if each cost is a fixed property of its device and can be measured and
subtracted.

**Neither holds.** Timing both calls per trial inside a streaming capture
(n=1631 pairs, 250 kS/s, idle host):

| write call | issued first | issued second |
|---|---|---|
| DLP (`serial.write`, 1 byte, `ftdi_sio`) | 40.84 µs | **14.13 µs** |
| MEG TTL box (`serial.write`, 2 bytes, `cdc_acm`) | 72.34 µs | 68.26 µs |

The same one-byte DLP write costs 41 µs going first and 14 µs going second. The
box is position-independent; the DLP is not, so the residual does not cancel and
is not a constant that can be subtracted. It is also larger than the quantity
being estimated.

The two arms then disagree about a number whose two estimates must sum to zero.
Treating the edge as a fixed offset from the start of the write call gives
−5.5 µs from one arm and −45.8 µs from the other; anchoring instead to the call's
*return* narrows the inconsistency to 10.9 µs but does not close it. Underneath
both sits USB microframe scheduling at 125 µs granularity, which is coarser than
the effect.

So the honest statement is: **the DLP-to-TTL-box write latency difference is of
order tens of microseconds, and neither its sign nor its magnitude is
established by this method.** Both arms do agree it is small against the box's
own ~1.5 ms absolute latency, which is the part that matters — for putting a
single trigger on a wire the two are interchangeable, and the choice between them
must be made on the pulse-width and skew behaviour below, which differ by
hundreds of times more.

Measuring it properly needs what the
[main README](../README.md#why-no-absolute-latency-is-quoted-here) describes: an event the host can produce at a time it knows exactly, on the same
instrument. Two devices behind two independent USB stacks cannot be separated by
a better estimator.

### Pulse width: the real difference, and it is not speed

Same measurement on both devices — the realised width on the wire. The DLP's
width is the interval between two host writes; the TTL box's is timed by its
firmware from a single command. **n=1000 per width**, streamed from the AD3 at
100 kS/s.

**Realised width, spread in ms:**

| device / condition | 1 ms | 2 ms | 5 ms | 10 ms | 20 ms | 50 ms |
|---|---|---|---|---|---|---|
| TTL box, idle | 1.03 | 2.01 | 2.05 | 2.05 | 2.04 | 2.04 |
| DLP, idle | 0.15 | 0.13 | 0.14 | 0.30 | 0.13 | 0.13 |
| DLP, load + `chrt -f 50` | 0.13 | 0.14 | 0.13 | 0.13 | 0.14 | 0.15 |

The DLP's median error is **+24 to +30 µs idle** and **+33 to +47 µs** under full
CPU load at real-time priority — the load costs it about 15 µs and nothing else.

The row that is missing is the DLP under load at *normal* priority, and it is
missing for a reason worth recording: under `stress-ng --cpu 0` at
`SCHED_OTHER`, the host cannot drain the instrument either. That capture lost
131,064 samples and corrupted 19 million more, against zero for the same run
under `chrt -f 50`. It was measured on the scope instead, n=50, and it is the
whole argument for real-time priority:

| DLP, under CPU load, normal priority | 5 ms | 10 ms | 20 ms | 50 ms |
|---|---|---|---|---|
| spread | **4.75** | **3.01** | **1.80** | **2.58** |

Neither device is simply better:

* **Configured correctly the DLP is about 15× more precise** — 0.13 ms against
  2.04 ms — because the TTL box pays a timebase cost the DLP does not.
* **Configured carelessly the DLP is about 35× worse**, and the TTL box does not
  notice.

So the TTL box offers roughly 2 ms of width uncertainty that cannot be improved,
and the DLP offers 0.13 ms *if* real-time priority is set up and 4.75 ms if it is
not. One is a property of the device; the other is a property of the system
administration. Which is preferable depends on whether the machine's
configuration is under your control and will stay that way.

**One disagreement between the instruments, unresolved.** On the DLP's idle
median error the scope (n=50) gives −10 to −20 µs and the AD3 (n=1000) gives +24
to +30 µs: the same quantity, one instrument each, about 45 µs and a sign apart.
Both are far inside the 0.13 ms spread, so no conclusion above depends on it, but
neither figure should be quoted as *the* bias until the discrepancy is
understood.

### Why the TTL box's width spans 2 ms and not 1

`millis()` truncates, so the obvious model of a firmware-timed pulse says the
realised width is uniform on [w−1, w]: a flat histogram exactly 1 ms wide. The
measured spread is twice that, and the reason is that **`millis()` does not tick
at 1 ms.**

Timer0 on a 16 MHz AVR overflows every 1024 µs, and `wiring.c` carries a
fractional accumulator (`FRACT_INC` 3, `FRACT_MAX` 125) that adds a catch-up
millisecond every ~41.7 overflows, keeping the clock right on average at the
price of advancing by 2 about one time in 42. So the number of overflows needed
to reach `millis() + w` depends on the accumulator's phase when the pulse
started: for some phases *n* suffice, for the rest it takes *n+1*. Each case
gives a uniform band 1024 µs wide, and the realised width is a mixture of two
of them — **2.048 ms across**.

`analyse-pulse-stream.py` simulates this. It has **no free parameters**:
everything in it is fixed by `wiring.c` and the 16 MHz clock, so the comparison
is a test rather than a fit. Measured against modelled, n=1000 per width:

| requested | measured spread | model | trials in the early band (measured / model) |
|---|---|---|---|
| 1 ms | 1.025 | 1.024 | — |
| 2 ms | 2.009 | 2.048 | 2.0% / 2.4% |
| 5 ms | 2.050 | 2.048 | 8.6% / 9.6% |
| 10 ms | 2.049 | 2.048 | 20.9% / 21.5% |
| 20 ms | 2.041 | 2.048 | 43.7% / 45.6% |
| 50 ms | 2.043 | 2.048 | 14.2% / 15.1% |

The band split is the discriminating test: it swings from 2% to 44% and back to
14% across widths, non-monotonically, and the model predicts each value to about
a percentage point.

A two-sample Kolmogorov-Smirnov test against the simulation passes at four of the
six widths outright and at all six once a single scalar offset is removed
(D = 0.015–0.033 against a 0.043 critical value), so what it rejects is a shift,
not a shape. That offset is itself informative: fitting it across widths gives

    offset = +17.6 us + 905 ppm x width

The constant is the firmware's own gap between raising the line and reading
`millis()`, plus one loop pass to notice the end. The 905 ppm is the Mega's
ceramic resonator running slow against the AD3's timebase — well inside its
±5000 ppm specification, and not something a firmware change can remove.

**For an experimenter the practical statement is: a requested *w* ms pulse from
this box lands in (w−2.05, w+0.04] ms.** The `[w−1, w]` model understates the
spread by a factor of two.

### An output port that failed silently

During this session the TTL box's output port stopped working mid-run, after
roughly 19,000 pulses. It is recorded here because of *how* it failed:

- `get_info` kept answering normally — version 1, caps 0x03.
- A static `set_port(0x01)`, which does not involve the pulse machinery,
  produced nothing. Neither did `set_port(0xFF)`: the whole port was dead.
- A DTR reset into the bootloader did **not** recover it.
- Unplugging and replugging USB did, completely and immediately.

Only removing power cleared it, which is the signature of I/O latch-up rather
than firmware state or permanent damage. The cause is not known; nothing was
connected to the line but a high-impedance instrument input.

The operational point stands regardless of cause: **a health check that only
talks to the box over serial reports a perfectly healthy device while no
triggers whatsoever reach the amplifier.** In a recording session that is a full
data set with no usable event markers, discovered afterwards. A check that
observes the line electrically — the box's own D30 → D22 loopback, where it
fails to see its own edge — is the one that catches this.

## Pulse width against request, by regression

`cmd/pulsetrain` + `ad3-capture` + `analyse-pulsetrain.py` measure something
the `pulse` and `pulse-stream` blocks do not. Those step through a handful of
fixed widths and summarise each; this samples widths uniformly on [5, 50] ms and
inter-pulse intervals uniformly on [10, 100] ms, and fits

    measured_width = intercept + slope * target_width

The fixed-width blocks can say the median error at 10 ms and at 50 ms. They
cannot cleanly separate a proportional loss from a fixed per-pulse overhead,
because with the interval held constant any drift across the run — thermal,
scheduling, anything monotonic — enters as a slope against width. Randomising
both decorrelates width from when the trial happened, so the slope is
attributable to width. It also makes the width sequence a signature: the
analysis aligns emitter to capture by sliding for minimum RMS, and a wrong
pairing fails loudly instead of biasing the fit quietly.

Collected 2026-08-08, n=1000 per condition, AD3 on ch1 at 250 kS/s with zero
lost or corrupted samples in every run. Widths uniform on [5, 50] ms, intervals
uniform on [10, 100] ms, seed 20260808 — the same seed in all three, so the
conditions see an identical pulse sequence.

| condition | slope | intercept | residual SD | max abs error |
|---|---|---|---|---|
| idle | 0.99985 ± 0.00006 | +0.0012 ± 0.0017 ms | **23 µs** | 0.35 ms |
| load (`stress-ng`) | 1.00110 ± 0.00246 | +0.7275 ± 0.0740 ms | **995 µs** | 5.04 ms |
| load + `chrt -f 50` | 1.00004 ± 0.00027 | −0.0036 ± 0.0080 ms | **108 µs** | 1.81 ms |

**The slope is 1 in every condition.** Load costs a fixed +0.73 ms offset and a
heavy tail; it does not scale with the width requested. That is what the
fixed-width table above could not settle: its +1.33 ms at 5 ms and +1.85 ms at
10 ms against −20 µs at 20 ms and −10 µs at 50 ms look like a width-dependent
effect, and at n=50 per width they are not — they are sampling noise on a
heavy-tailed distribution. Split the n=1000 load run into width quartiles and
the p95 error is flat at 2.6–2.8 ms across all four.

The one thing that does vary with width is the *median* error under load, which
falls from +0.33 ms in the shortest quartile to +0.06 ms in the longest. That is
compatible with a slope of 1: OLS fits the mean, and the mean is set by the tail,
which is flat. Characterising the median trend properly needs quantile
regression, and this data has not been used for that. **The mechanism is not
established here** — a candidate is that the width error is the difference
between two preemption delays, one before each write, but nothing in these runs
distinguishes that from the alternatives.

**Real-time priority recovers a factor of 9 in residual SD** (995 → 108 µs) and
returns the intercept to zero, under load that otherwise costs three quarters of
a millisecond on every pulse.

### The device is not what degrades, by regression this time

Fit the measured width on the *host's own* busy-wait interval instead of on the
target, under load:

| under load | residual SD |
|---|---|
| measured ~ target | 995 µs |
| measured ~ host | **188 µs** |

The wire tracks the host's own clock five times more closely than it tracks what
the host meant to do. The host's timing loop is preempted, the second write goes
out late, and the module faithfully reproduces the mistake. This is the same
conclusion the four-row scope table reached by comparing two columns, now with
n=1000 and an interval estimate.

### Busy-waiting the gaps makes `chrt` worse, not better

The first `rt` run came out *worse* than the unprivileged one: alignment RMS
5.66 ms, and a maximum host-side width error of **49.63 ms**. The cause is the
kernel's real-time throttle. `sched_rt_runtime_us` is 950000 of a 1000000
period, so a SCHED_FIFO task at 100% duty can be suspended for 50 ms once a
second — and the emitter was spinning through the inter-pulse gaps as well as
the pulses, so it was at 100% duty for the whole 85 s run. 23 of 1000 trials
were hit, the biggest error one millisecond short of the full throttle window,
and the hits land on one-second boundaries.

**Load is a necessary condition, and that was not obvious.** Isolating the
mechanism afterwards with a bare spinner rather than the emitter: a pinned
`SCHED_FIFO 50` thread spinning continuously took **0 stalls in 20 s on an idle
machine**, and **24 stalls in 25 s under `stress-ng --cpu 20`** — 51.0 ms each,
at 0.999, 2.000, 3.001, 4.002 s, one per second exactly. Unpinned under the same
load it took 8 irregular stalls of 6–41 ms, since it migrates between runqueues
and hits the limit less consistently. On an idle runqueue the kernel borrows
unused real-time bandwidth from other CPUs and the limit is never reached. The
rt run above was loaded, so the original observation stands — but a version of
this warning that omits the load condition would send someone to test on a quiet
machine and conclude there is no problem.

`pulsetrain` now sleeps the gap and spins only its last 200 µs, which drops the
duty cycle to about a third and takes it clear of the throttle. The raw evidence
is kept in `pulsetrain-rt-throttled.csv`.

The general lesson is not about this tool: **`chrt -f` plus a busy-wait that
never yields is a trap.** Raising priority to protect timing, and then holding
the CPU continuously, buys a 50 ms stall every second on a busy host — far worse
than the scheduling jitter it was meant to avoid.

What the throttle does *not* appear to do is spread to other real-time tasks.
An RR 20 thread imitating an audio server's data loop (5 ms period) was run on
the same CPU as a `SCHED_FIFO 50` spinner in conditions where that spinner was
demonstrably being throttled: worst wakeup lateness **1.114 ms**, against
1.013 ms for the same thread with no spinner present. The co-located real-time
thread was not caught. That is a measurement, not an explanation — the
mechanism for why it escapes was not established — but on this host a spinning
real-time experiment did not degrade an audio-priority neighbour.

### An expected asymmetry that is not a fault

`host ~ target` has a small positive intercept in every condition (+10.8 µs
idle) where `measured ~ target` has none. The busy-wait deadline is anchored
*before* the first write, so both edges carry the same host-to-wire latency and
it cancels out of the electrical width, while the host's own figure additionally
includes the second write's return. The mirror image shows up as the −9.6 µs
intercept of `measured ~ host`. This is the arrangement working, not an error.

## Emitting and capturing in separate processes

`pulse-stream` fires each trial from inside the loop draining the instrument,
through `ad3.record`'s `on_tick`. That couples them: the busy-wait timing the
pulse also stalls the drain, and the device's 16384-sample buffer is only
16 ms at 1 MS/s, so a 50 ms pulse overruns it and loses samples exactly where
the falling edge is. Hence that block's refusal to run fast when the widths are
long, and its 250 kS/s default.

`pulsetrain` is a separate process from `ad3-capture`, so the drain loop is
never held by anything. That removes the constraint rather than working around
it: the capture runs at 1 MS/s for ~1 µs edge resolution regardless of pulse
width. The cost is that the two have to be paired afterwards instead of being
counted in lockstep, which is what the alignment step is for.

## A note on the file schemas

`2026-08-08-dlp-go/` holds the Go session. `roundtrip-go-lt{1,2,4,8,16}.csv`
carry the same columns as the Python `loopback-*.csv`, one file per setting. `pulsetrain-<condition>.csv` is the
emitter's own log — what it asked for and what its clock saw — and
`pulsetrain-paired-<condition>.csv` adds the instrument's width and the fit
residual per trial, after alignment. `pulsetrain-edges-<condition>.npz` is the
raw AD3 output: threshold crossing times only, since the samples themselves are
180 MB per run and nothing downstream reads them.

`pulsetrain-rt-throttled.csv` is a failed run kept on purpose — the emitter at
SCHED_FIFO with a 100% duty busy-wait, hitting the kernel real-time throttle. It
has no paired file because the analysis refused to align it, which is the
behaviour being demonstrated.

`pulse-dlp-*.csv` were recorded before the block gained a `--device` column and
so lack it; `pulse-ttlbox-*.csv` and everything later carry it. The DLP files
were renamed rather than re-recorded, since the measurement itself is unchanged
and instrument time is better spent on things not yet measured.

`h2h-stream-{idle,rt}.csv` carry `condition,policy,priority,arm,delta_us` and
**cannot be corrected for the write-call asymmetry** described above, because
they predate the per-trial `first_write_us`/`second_write_us` columns the
correction needs. They are the raw edge separations, which are real; the latency
difference is not recoverable from them. The block now records both write
durations and assigns the arm from the recorded firing order rather than from
which edge rose first — the two agree only while the host gap exceeds the
latency difference, and the trials where it does not are exactly the tail.

## Not measured

- **Pass 1's raw per-trial rows.** Lost. The skew block wrote to a fixed
  `skew.csv`, so a five-trial pass 2 validation overwrote the completed n=99
  pass 1 dataset. The summary statistics above survive in the run log, and pass 2
  independently reproduces the quantisation, but the per-trial values behind pass
  1's cluster counts no longer exist. The filename now carries the probe map
  (`skew-ch2-3-4.csv`, `skew-ch6-7-8.csv`) so it cannot recur.
- **Absolute host-to-edge latency.** Not measurable with this device and a scope
  alone: nothing shares a clock with the instrument, so there is no way to
  anchor "when the host asked" to "when the edge happened". It needs a reference
  device of known latency. The constant part can be calibrated out by an
  experiment in any case; the variable part is what the `pulse` block measures.
- **The write-latency difference between the two devices.** Attempted twice, on
  two instruments, and not resolved either time — the order-reversal estimator
  rests on an assumption the data falsifies. See [Write latency](#write-latency-the-difference-is-not-resolved-and-here-is-why).
  Bounded, not measured: of order tens of microseconds.
- **The DLP's idle pulse-width bias, to better than ~45 µs.** The scope and the
  AD3 disagree on its sign. Both agree it is far smaller than the spread.
- **The DLP under CPU load at normal priority, at large n.** The AD3 cannot be
  driven at all in that condition, so the only data for it is the n=50 scope
  measurement above.
- **The shortest detectable input.** Attempted and withdrawn — see below.

## A block that was withdrawn

`shortest` was written to find the briefest input pulse the host can still
catch. It reported 50/50 detection at every width down to 1 ms, including with
a 16 ms latency timer, which is impossible: you cannot see a 1 ms pulse by
looking every 16 ms.

The cause is that one program on one serial port cannot both generate a short
pulse and poll for it. The polling read blocks for a whole latency-timer period,
and it blocks *between* the write that raises the line and the write that lowers
it — so the pulse is stretched to the length of one poll and is then always
seen. Measured directly: a requested 1 ms pulse held the line high for 16.04 ms.

The block now refuses to run and explains why. Doing it properly needs a pulse
source independent of the poller — the MEG TTL box wired D30 → ch8 is the
obvious one, since its firmware times its own pulses and that timing is
measured. The bogus CSVs were deleted rather than kept.
