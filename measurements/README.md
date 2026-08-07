# Timing measurements — DLP-IO8-G

Raw data and reproduction instructions. Every figure here is measured on
hardware; anything not measured says so.

Device: DLP-IO8-G, FT232RL, 115200 8N1, `usb-DLP_Design_DLP-IO8_12345678`.
Host: `is158520`, Linux, `ftdi_sio` VCP driver. Session 2026-08-07.

```bash
./dlp_timing.py poll     --out <dir> --latency-timer 1     # no wiring
./dlp_timing.py loopback --out <dir> --latency-timer 1     # needs ch1 -> ch8
./dlp_timing.py discard  --out <dir>                       # needs ch1 -> ch8
./dlp_timing.py <block> --help
```

Wiring for the loopback blocks is one jumper, ch1 to ch8. Both channels are on
the same board so the ground is already common.

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

## Not measured

- **Inter-line skew, and what a trigger code costs.** The single most important
  open question. There is no atomic multi-channel write on this device: setting
  8 channels means 8 bytes acted on as they arrive, so a code change takes
  ≥ 8 × 86.8 µs ≈ 0.7 ms during which the port shows a wrong value. The
  repository's `scope_4lines_A.jpg` is cited for "less than 1 ms", but it was
  taken at 1.00 ms/div — a whole screen is 10 ms, and the effect is ~87 µs. That
  capture can bound the skew; it cannot measure it. Needs a scope at ~100 µs/div.
- **Absolute host→edge latency and its jitter.** Needs an external instrument.
- **Pulse width fidelity.** The module has no pulse timer, so a width is the
  interval between two host writes and inherits host scheduling in full. Needs
  an instrument to see what actually reached the wire.
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
