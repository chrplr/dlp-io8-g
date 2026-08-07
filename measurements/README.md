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
./dlp_timing.py <block> --help
```

The loopback blocks need one jumper, ch1 to ch8; both channels are on the same
board so the ground is already common. Remove that jumper before probing ch8
with the scope, or ch1 and ch8 are shorted and their skew reads zero for a
wiring reason.

The scope blocks were run with a Siglent SDS1104X-E over LAN. It ships on
10.11.13.0/24 and does not fall back to link-local, so a direct cable needs the
host on that subnet (`sudo ip addr add 10.11.13.1/24 dev <iface>`). Probes at
1x, not 10x: this is a 0-5 V logic signal.

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

`clang/dlp.c` calls `tcflush(fd, TCOFLUSH)` before every write and
`golang/dlp.go` calls `ResetOutputBuffer()`. Both discard queued output rather
than draining it, which looked like it should be able to drop a trigger byte.

**It does not fire here.** Writing 8, 64, 200 and 1000 command bytes and
discarding immediately afterwards, every byte still arrived — 1000/1000 in the
largest case. The reason is visible in the timing: that 1000-byte write itself
took 55.7 ms to return, because a blocking write does not come back until the
driver has taken the data. By the time the flush runs there is nothing left in
the kernel queue to discard.

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

**Under CPU load (`stress-ng --cpu 0`):**

| requested | on the wire | median error | spread | host's own busy-wait |
|---|---|---|---|---|
| 5 ms | 6.330 ms | **+1.33 ms** | 4.75 ms | 6.408 ms |
| 10 ms | 11.840 ms | **+1.85 ms** | 3.01 ms | 11.929 ms |
| 20 ms | 19.980 ms | −20 µs | 1.80 ms | 20.056 ms |
| 50 ms | 49.990 ms | −10 µs | 2.58 ms | 50.077 ms |

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
`chrt`), not anything about this hardware.

The same conclusion arrived independently from the NeuroSpin MEG TTL box, whose
host round-trip tail went from 6 ms idle to 25 ms under the same load. Two
unrelated devices, the same host, the same answer.

## Not measured

- **Pass 1's raw per-trial rows.** Lost. The skew block wrote to a fixed
  `skew.csv`, so a five-trial pass 2 validation overwrote the completed n=99
  pass 1 dataset. The summary statistics above survive in the run log, and pass 2
  independently reproduces the quantisation, but the per-trial values behind pass
  1's cluster counts no longer exist. The filename now carries the probe map
  (`skew-ch2-3-4.csv`, `skew-ch6-7-8.csv`) so it cannot recur.
- **Real-time priority.** The loaded pulse figures were taken at normal
  scheduling. Whether `chrt` recovers the idle numbers is the claim that the
  whole real-time-priority setup rests on, and nothing here has tested it. Run
  `pulse --condition rt` under `chrt -f 50` once the rtprio grant is live.
- **Absolute host-to-edge latency.** Not measurable with this device and a scope
  alone: nothing shares a clock with the instrument, so there is no way to
  anchor "when the host asked" to "when the edge happened". It needs a reference
  device of known latency. The constant part can be calibrated out by an
  experiment in any case; the variable part is what the `pulse` block measures.
- **Head-to-head against the NeuroSpin MEG TTL box**, whose latency is measured,
  which would convert the relative numbers here into absolute ones.
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
