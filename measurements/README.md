# Timing measurements — DLP-IO8-G

Raw data and reproduction instructions. Every figure here is measured on
hardware; anything not measured says so.

Device: DLP-IO8-G, FT232RL, 115200 8N1, `usb-DLP_Design_DLP-IO8_12345678`.
Host: `is158520`, Linux, `ftdi_sio` VCP driver. Session 2026-08-07.

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
cause. `ad3.py` preallocates its output for that reason, and the streaming blocks
default to 250 kS/s. Every block aborts rather than writing a file if the SDK
reports any lost or corrupted samples: a capture with holes has them wherever the
host was busiest, which is exactly where the interesting trials are.

---

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

Measuring it properly needs what the [main README](../README.md#measuring-it-properly)
describes: an event the host can produce at a time it knows exactly, on the same
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

## A note on the file schemas

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
