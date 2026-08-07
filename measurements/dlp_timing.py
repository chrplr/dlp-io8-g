#!/usr/bin/env python3
"""Timing measurements for the DLP-IO8-G.

    ./dlp_timing.py poll                 # no wiring needed
    ./dlp_timing.py loopback             # needs ch1 -> ch8 jumper
    ./dlp_timing.py discard              # needs ch1 -> ch8 jumper
    ./dlp_timing.py shortest             # needs ch1 -> ch8 jumper
    ./dlp_timing.py <block> --help

Every block writes raw per-trial rows to --out and summarises nothing that
cannot be recomputed from them.

What each block can and cannot establish, given that this device has no clock of
its own: nothing here measures an absolute host-to-edge latency, because there
is no second timestamp to compare against. These blocks measure round trips and
comparisons, which stay entirely within the host clock. Absolute numbers need an
oscilloscope or a Black Box ToolKit; see README.md in this directory.
"""

import argparse
import csv
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dlpio8 import DLPIO8, CHANNELS  # noqa: E402


# ------------------------------------------------------------------ helpers

def quantile(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    i = int(q * len(sorted_vals) + 0.5) - 1
    return sorted_vals[min(max(i, 0), len(sorted_vals) - 1)]


def describe(vals, unit="ms"):
    if not vals:
        return "no data"
    s = sorted(vals)
    return (f"n={len(s)}  min {s[0]:.3f}  p50 {quantile(s, .5):.3f}  "
            f"p95 {quantile(s, .95):.3f}  p99 {quantile(s, .99):.3f}  "
            f"max {s[-1]:.3f}  mean {st.mean(s):.3f} {unit}")


class Recorder:
    """One CSV of raw per-trial rows."""

    def __init__(self, outdir, name, header):
        os.makedirs(outdir, exist_ok=True)
        self.path = os.path.join(outdir, f"{name}.csv")
        self._f = open(self.path, "w", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(header)

    def row(self, *vals):
        self._w.writerow([f"{v:.4f}" if isinstance(v, float) else v for v in vals])

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def require_loopback(dlp, out_ch, in_ch):
    """Verify the jumper is really there before spending a run on it."""
    dlp.low(out_ch)
    time.sleep(0.05)
    lo = dlp.read(in_ch)[0]
    dlp.high(out_ch)
    time.sleep(0.05)
    hi = dlp.read(in_ch)[0]
    dlp.low(out_ch)
    time.sleep(0.05)
    if not (lo == 0 and hi == 1):
        sys.exit(
            f"no loopback detected: driving ch{out_ch} low then high read "
            f"{lo} then {hi} on ch{in_ch}, expected 0 then 1.\n"
            f"Wire ch{out_ch} to ch{in_ch} (and note that reading a channel "
            "switches it to input mode, so ch%d must not also be driven)."
            % in_ch)


def report_latency_timer(dlp, want=None):
    """Report the latency timer, setting it first if asked.

    Recorded in every filename and every row, because it changes the input path
    by more than an order of magnitude and is invisible in the data otherwise --
    two runs of the same block on the same hardware are not comparable without
    it.
    """
    if want is not None:
        dlp.latency_timer = want
        got = dlp.latency_timer
        if got != want:
            sys.exit(f"asked for latency_timer={want}, device reports {got}")
    lt = dlp.latency_timer
    print(f"  port           {dlp.port_path}")
    print(f"  latency_timer  {lt} ms", end="")
    if lt == 16:
        print("   <- the ftdi_sio default. Every reply waits up to this long;")
        print("                    unsynchronised polls average half of it, and a poll")
        print("                    loop that waits for each reply pins at the full 16.")
    else:
        print()
    return lt


# ------------------------------------------------------------------- blocks

def block_poll(args):
    """Round-trip cost of asking the device something.

    This is the whole input path: a command out, the module's reply back, and
    the FTDI receive batching in between. It needs no wiring, because it is not
    measuring a signal -- it is measuring the cost of the conversation.
    """
    with DLPIO8(port=args.port) as dlp:
        lt = report_latency_timer(dlp, args.latency_timer)
        ops = (("ping", dlp.ping),
               ("read1", lambda: dlp.read(1)),
               ("read8", dlp.read_all))
        with Recorder(args.out, f"poll-lt{lt}", ["op", "latency_timer_ms",
                                                 "trial", "roundtrip_ms"]) as rec:
            for name, fn in ops:
                ts = []
                for i in range(args.trials):
                    t = time.perf_counter()
                    fn()
                    dt = (time.perf_counter() - t) * 1000
                    ts.append(dt)
                    rec.row(name, lt, i, dt)
                print(f"  {name:8s} {describe(ts)}")
                if name == "read8":
                    print(f"  {'':8s} => sustained poll rate "
                          f"{1000 / quantile(sorted(ts), .5):.0f} Hz")
            print(f"\n  wrote {rec.path}")


def block_loopback(args):
    """Host write -> host learns of the resulting edge, through the device.

    Both timestamps are the host's, so no clock has to be reconciled with any
    other. It bounds the write path plus the read path from above, and it is the
    honest thing to quote for a device that cannot timestamp anything itself.
    """
    with DLPIO8(port=args.port) as dlp:
        lt = report_latency_timer(dlp, args.latency_timer)
        require_loopback(dlp, args.out_ch, args.in_ch)
        print(f"  loopback ch{args.out_ch} -> ch{args.in_ch} confirmed\n")

        with Recorder(args.out, f"loopback-lt{lt}",
                      ["latency_timer_ms", "trial", "roundtrip_ms", "polls"]) as rec:
            ts = []
            for i in range(args.trials):
                dlp.low(args.out_ch)
                time.sleep(0.01)
                dlp.read(args.in_ch)          # settle, and clear the stream
                t0 = time.perf_counter()
                dlp.high(args.out_ch)
                polls = 0
                while True:
                    polls += 1
                    if dlp.read(args.in_ch)[0] == 1:
                        break
                    if time.perf_counter() - t0 > 1.0:
                        polls = -1
                        break
                dt = (time.perf_counter() - t0) * 1000
                ts.append(dt)
                rec.row(lt, i, dt, polls)
            dlp.low(args.out_ch)
            print(f"  write -> detect  {describe(ts)}")
            print(f"\n  wrote {rec.path}")


def block_discard(args):
    """Does resetting the output buffer before a write lose queued bytes?

    The C client in this repository calls tcflush(fd, TCOFLUSH) before every
    write, and the Go client calls ResetOutputBuffer(). Both DISCARD whatever is
    still queued rather than waiting for it. If a previous command has not yet
    gone out, it is dropped -- and a dropped 'Q' leaves a line stuck high, which
    an experiment sees as a trigger that never ended.

    Two arms on the same physical setup: pyserial's plain write (what dlpio8.py
    does) against the same sequence with an explicit discard first (what the C
    and Go clients do). After each burst the line is read back. If it is high
    when the last command was a low, a byte was lost.

    Read the null result carefully. This device is level-based, so only the LAST
    surviving command decides the line state: a byte dropped mid-burst leaves no
    trace a readback can find, and counting the edges that were actually
    produced needs a scope or a BBTK. What this block can detect is a drop in
    the final command, which is the case that strands a line high -- and to make
    even that reachable, the discard arm ends with one more discard, standing in
    for the next trigger's flush. Without it the arm cannot fail by
    construction, which is not the same as the bug not existing.
    """
    with DLPIO8(port=args.port) as dlp:
        lt = report_latency_timer(dlp, args.latency_timer)
        require_loopback(dlp, args.out_ch, args.in_ch)
        print(f"  loopback ch{args.out_ch} -> ch{args.in_ch} confirmed\n")

        hi = DLPIO8.HIGH_CMDS[args.out_ch - 1:args.out_ch]
        lo = DLPIO8.LOW_CMDS[args.out_ch - 1:args.out_ch]

        with Recorder(args.out, f"discard-lt{lt}",
                      ["arm", "gap_ms", "burst", "trial", "final_state",
                       "stuck"]) as rec:
            for arm in ("plain", "discard"):
                print(f"  {arm} arm:")
                for gap_ms in args.gaps:
                    stuck = 0
                    for trial in range(args.trials):
                        for _ in range(args.burst):
                            if arm == "discard":
                                dlp.serial.reset_output_buffer()
                            dlp.serial.write(hi)
                            if gap_ms:
                                t = time.perf_counter() + gap_ms / 1000
                                while time.perf_counter() < t:
                                    pass
                            if arm == "discard":
                                dlp.serial.reset_output_buffer()
                            dlp.serial.write(lo)
                            if gap_ms:
                                t = time.perf_counter() + gap_ms / 1000
                                while time.perf_counter() < t:
                                    pass
                        if arm == "discard":
                            # The byte at risk is the one written BEFORE a
                            # discard, so a burst whose last write is never
                            # followed by one cannot fail -- the single byte the
                            # readback inspects would be the only safe one. This
                            # trailing discard is what a real client does when
                            # the next trigger arrives: dlp_set() flushes, and
                            # whatever the previous call queued is gone.
                            dlp.serial.reset_output_buffer()
                        dlp.drain()
                        time.sleep(0.05)
                        state = dlp.read(args.in_ch)[0]
                        rec.row(arm, gap_ms, args.burst, trial, state, state == 1)
                        stuck += state == 1
                    print(f"    gap {gap_ms:5.2f} ms: {stuck:3d}/{args.trials} "
                          f"bursts left the line stuck high")
            dlp.low(args.out_ch)
            print(f"\n  wrote {rec.path}")


def block_shortest(args):
    """The shortest pulse the host can still see on an input.

    NOT IMPLEMENTED, on purpose. The obvious version of this block is wrong, and
    it fails in a way that produces a confident, plausible, entirely fictional
    result -- so it is better to have nothing here than to have that.

    The device cannot report an edge, only a level when asked, so a pulse
    shorter than the gap between polls is invisible. To show where that boundary
    falls you must generate a pulse of a known short width and poll for it. But
    a single-threaded program sharing one serial port cannot do both: the
    polling read blocks for a whole latency-timer period, and it blocks BETWEEN
    the write that raises the line and the write that lowers it. The pulse is
    therefore stretched to the length of one poll, and is then detected every
    time.

    Measured while establishing this: asking for a 1 ms pulse with a 16 ms
    latency timer put the line high for 16.04 ms, and the block reported 50/50
    detection at every width down to 1 ms. Every one of those pulses was really
    ~16 ms long.

    The measurement needs a pulse source independent of the poller:

      - the NeuroSpin MEG TTL box, whose firmware times a pulse itself, wired
        D30 -> DLP ch8. Its own width error is measured and small, so a pulse
        it reports as 5 ms really is;
      - or a second DLP on another port, which is what this repository's
        deleted golang/README.md described (-read_mode on one port, writing on
        the other);
      - or a signal generator, or the BBTK in its response-echo mode.

    Whichever is used, the generator must not share this process's serial port.
    """
    sys.exit(block_shortest.__doc__.strip() + "\n\nNothing was measured.")


def block_skew(args):
    """Inter-line skew: what a multi-channel write actually does to the port.

    The device has no atomic multi-channel write. Setting N channels means
    sending N single-byte commands which the module acts on as they arrive, so
    the port cannot change all at once and there is an interval during which it
    shows a value that was never intended. This measures that interval.

    The prediction, if the module simply acts on each byte as the UART delivers
    it, is 10 bit-times per byte at 115200 -- 86.8 us -- and nothing else. Any
    excess is per-byte processing inside the module, and it scales with how many
    channels a trigger code spans.

    The reverse-order arm is the control. Sending 87654321 instead of 12345678
    must reverse the sign of every skew; if it does not, the module is not
    acting on bytes in arrival order and the serialisation model is wrong,
    whatever the numbers look like.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scope import Scope

    # Each pattern names the probed channel that rises FIRST. That channel is
    # both the trigger source and the reference every delay is measured from,
    # so the delays stay positive and the two arms are directly comparable.
    # Triggering on ch1 for the reverse pattern measures nothing: ch1 rises
    # last there, and the delays to channels that already rose are negative.
    # --probe-map says which DLP channel each scope channel CH2..CH4 is on.
    # The byte separation is then derived from the pattern itself rather than
    # assumed: in pass 1 (ch2,ch3,ch4) the probes are 1, 2 and 3 bytes from the
    # reference, in pass 2 (ch6,ch7,ch8) they are 5, 6 and 7. Hard-coding "3"
    # for the farthest probe silently divides pass 2's span by the wrong number.
    probe_dlp = [int(x) for x in args.probe_map.split(",")]
    if len(probe_dlp) != 3:
        raise SystemExit(f"--probe-map needs 3 DLP channels, got {args.probe_map!r}")

    patterns = {"forward": (b"12345678", 1), "reverse": (b"87654321", 8)}
    if args.pattern != "both":
        patterns = {args.pattern: patterns[args.pattern]}

    with DLPIO8(port=args.port) as dlp, Scope(host=args.scope) as s:
        print(f"  scope {s.idn}")
        for ch in (1, 2, 3, 4):
            s.channel(ch, on=True, vdiv=1, offset=-2, coupling="D1M")
        s.timebase(args.tdiv)
        s.apply("TRDL 0S", "TRDL?", None)
        print(f"  {s.value('TDIV?') * 1e6:.0f} us/div, "
              f"{s.value('SARA?'):.3g} Sa/s\n")

        # The probe map goes in the filename. Every other block encodes its
        # condition there (poll-lt16, pulse-load); this one did not, and a pass 2
        # validation run silently overwrote a completed pass 1 dataset that had
        # taken seven minutes of instrument time to collect.
        stem = "skew-ch" + "-".join(str(c) for c in probe_dlp)
        with Recorder(args.out, stem,
                      ["pattern", "ref_dlp_ch", "trial", "scope_ch",
                       "byte_separation", "delay_us"]) as rec:
            for name, (pattern, ref_dlp) in patterns.items():
                order = [int(c) for c in pattern.decode()]
                # Byte separation of each probed channel from the reference,
                # read off the pattern rather than assumed. In pass 1 the probes
                # are 1, 2 and 3 bytes out; in pass 2 they are 5, 6 and 7.
                sep = {sc: order.index(dc) - order.index(ref_dlp)
                       for sc, dc in zip((2, 3, 4), probe_dlp)}
                if any(v <= 0 for v in sep.values()):
                    print(f"  {name}: DLP ch{ref_dlp} does not precede every "
                          f"probed channel in this pattern; skipping")
                    continue
                others = sorted(sep, key=lambda c: sep[c])
                # The reference is always on scope CH1, so that is the trigger.
                s.trigger_edge(1, level=2.5, slope="POS", mode="SINGLE")
                by_ch = {c: [] for c in others}
                misses = 0
                print(f"  {name} ({pattern.decode()}), reference DLP "
                      f"ch{ref_dlp} on scope CH1:", flush=True)

                for trial in range(args.trials):
                    # Fail fast. A configuration that never triggers costs the
                    # full timeout every trial, and a hundred of those is
                    # several silent minutes that yield nothing.
                    if trial == args.probe and misses == trial:
                        print(f"    aborted: none of the first {trial} trials "
                              f"triggered. Check the probes and the 2.5 V "
                              f"level.", flush=True)
                        break
                    if args.trials > 20 and trial and trial % 20 == 0:
                        print(f"    ... {trial}/{args.trials}", flush=True)

                    dlp.low(*CHANNELS)
                    time.sleep(0.02)
                    # Arm through apply(), not write(): an unverified setup
                    # command is silently dropped by this instrument, and a
                    # dropped arm looks exactly like a signal that never came.
                    s.apply("TRMD SINGLE", "TRMD?", None)
                    if not s.wait_armed(timeout=args.trigger_timeout):
                        misses += 1
                        continue
                    dlp._write(pattern)
                    if not s.wait_stopped(timeout=args.trigger_timeout):
                        misses += 1
                        continue
                    for ch in others:
                        d = s.delay(1, ch, "FRR")
                        if d is not None:
                            by_ch[ch].append(d * 1e6)
                            rec.row(name, ref_dlp, trial, ch, sep[ch], d * 1e6)

                for ch in others:
                    v, n = by_ch[ch], sep[ch]
                    label = f"    +{n} byte{'s' if n > 1 else ' '} (CH{ch}, "\
                            f"DLP ch{probe_dlp[(2, 3, 4).index(ch)]}):"
                    print(f"{label}  {describe(v, 'us') if v else 'no measurement'}")
                if misses:
                    print(f"    {misses}/{args.trials} trials did not trigger")
                farthest = others[-1]
                if by_ch[farthest]:
                    span = st.median(by_ch[farthest])
                    per_byte = span / sep[farthest]
                    print(f"    => {per_byte:.2f} us per byte "
                          f"(115200 8N1 predicts 86.81)")
                    if sep[farthest] == 7:
                        print(f"    => full 8-channel span MEASURED: "
                              f"{span:.1f} us")
                    else:
                        print(f"    => full 8-channel span extrapolated to "
                              f"{per_byte * 7:.1f} us from {sep[farthest]}-byte "
                              f"separations")
            dlp.low(*CHANNELS)
            print(f"\n  wrote {rec.path}")


def block_pulse(args):
    """Single-line pulse width and its jitter, measured by the scope.

    The question this answers is whether one TTL line on this device is good
    enough for a system sampling at 1 kHz. Skew does not arise -- a single
    channel is one command byte -- so what is left is how faithfully a pulse the
    host asks for appears on the wire.

    Absolute latency is deliberately not claimed. Nothing here shares a clock
    with the scope, so there is no way to anchor "when the host asked" to "when
    the edge happened", and any absolute figure would be an assumption dressed
    up as a measurement. Latency's CONSTANT part can be calibrated out by an
    experiment anyway; its variable part cannot, and that is what is measured
    here.

    Two widths are recorded per trial. The host's own busy-wait interval says
    how well the host placed its two writes; the scope's says what actually
    reached the wire. If the first is tight and the second is not, the variance
    is in the USB path rather than in the timing code -- a distinction that
    decides whether there is anything to fix in software.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scope import Scope

    with DLPIO8(port=args.port) as dlp, Scope(host=args.scope) as s:
        print(f"  scope {s.idn}")
        s.channel(1, on=True, vdiv=1, offset=-2, coupling="D1M")
        for ch in (2, 3, 4):
            s.channel(ch, on=False)
        s.apply("TRDL 0S", "TRDL?", None)
        s.trigger_edge(1, level=2.5, slope="POS", mode="SINGLE")

        with Recorder(args.out, f"pulse-{args.condition}",
                      ["condition", "requested_ms", "trial",
                       "host_width_ms", "scope_width_ms"]) as rec:
            for width in args.widths:
                # Put the pulse across about a third of the screen: wide enough
                # to measure precisely, narrow enough that the whole pulse and
                # both edges stay inside the record.
                s.timebase(round(width / 1000 / 4, 9))
                host_w, scope_w, misses = [], [], 0
                for trial in range(args.trials):
                    dlp.low(args.out_ch)
                    time.sleep(0.02)
                    s.apply("TRMD SINGLE", "TRMD?", None)
                    if not s.wait_armed(timeout=args.trigger_timeout):
                        misses += 1
                        continue
                    t0 = time.perf_counter()
                    dlp.high(args.out_ch)
                    end = t0 + width / 1000
                    while time.perf_counter() < end:
                        pass
                    dlp.low(args.out_ch)
                    host_w.append((time.perf_counter() - t0) * 1000)
                    if not s.wait_stopped(timeout=args.trigger_timeout):
                        misses += 1
                        continue
                    w = s.param(1, "PWID")
                    if w is not None:
                        scope_w.append(w * 1000)
                        rec.row(args.condition, width, trial,
                                host_w[-1], w * 1000)
                print(f"\n  requested {width} ms:")
                print(f"    host busy-wait   {describe(host_w, 'ms')}")
                print(f"    on the wire      {describe(scope_w, 'ms')}")
                if scope_w:
                    err = st.median(scope_w) - width
                    print(f"    => median error {err:+.4f} ms, "
                          f"spread {max(scope_w) - min(scope_w):.4f} ms")
                if misses:
                    print(f"    {misses}/{args.trials} trials did not trigger")
            dlp.low(args.out_ch)
            print(f"\n  wrote {rec.path}")


# -------------------------------------------------------------------- main

def main():
    # The shared options live on a parent parser rather than the top-level one,
    # so they are accepted after the block name as well as before it. With them
    # only at the top level, `dlp_timing.py poll --out dir` is a usage error,
    # which is exactly the order anyone types.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", default=None,
                        help="device path (default: find by USB id)")
    common.add_argument("--out", default=".", help="directory for the CSV output")
    common.add_argument("--out-ch", type=int, default=1, help="output channel (1-8)")
    common.add_argument("--in-ch", type=int, default=8, help="input channel (1-8)")
    common.add_argument("--latency-timer", type=int, default=None,
                        help="set the FTDI latency timer (ms) before running; "
                             "needs write access to the sysfs attribute")

    p = argparse.ArgumentParser(description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="block", required=True)

    s = sub.add_parser("poll", parents=[common],
                       help="round-trip cost of a query (no wiring)")
    s.add_argument("--trials", type=int, default=300)
    s.set_defaults(fn=block_poll)

    s = sub.add_parser("loopback", parents=[common],
                       help="write -> detect round trip (needs jumper)")
    s.add_argument("--trials", type=int, default=200)
    s.set_defaults(fn=block_loopback)

    s = sub.add_parser("skew", parents=[common],
                       help="inter-line skew (needs a scope + probes)")
    s.add_argument("--trials", type=int, default=100)
    s.add_argument("--scope", default=None, help="scope IP (default 10.11.13.220)")
    s.add_argument("--tdiv", type=float, default=200e-6, help="scope s/div")
    s.add_argument("--pattern", default="both",
                   choices=["both", "forward", "reverse"])
    s.add_argument("--trigger-timeout", type=float, default=1.5,
                   help="seconds to wait for each acquisition")
    s.add_argument("--probe", type=int, default=3,
                   help="abort an arm if none of the first N trials trigger")
    s.add_argument("--probe-map", default="2,3,4",
                   help="DLP channels on scope CH2,CH3,CH4 (pass 1: 2,3,4; "
                        "pass 2: 6,7,8)")
    s.set_defaults(fn=block_skew)

    s = sub.add_parser("pulse", parents=[common],
                       help="single-line pulse width and jitter (needs scope)")
    s.add_argument("--trials", type=int, default=50)
    s.add_argument("--widths", type=float, nargs="+", default=[5, 10, 20, 50])
    s.add_argument("--scope", default=None, help="scope IP (default 10.11.13.220)")
    s.add_argument("--condition", default="idle",
                   help="label for the host condition, recorded in the CSV")
    s.add_argument("--trigger-timeout", type=float, default=1.5)
    s.set_defaults(fn=block_pulse)

    s = sub.add_parser("discard", parents=[common],
                       help="are queued bytes lost? (needs jumper)")
    s.add_argument("--trials", type=int, default=50)
    s.add_argument("--burst", type=int, default=20)
    s.add_argument("--gaps", type=float, nargs="+",
                   default=[10, 5, 2, 1, 0.5, 0])
    s.set_defaults(fn=block_discard)

    s = sub.add_parser("shortest", parents=[common],
                       help="shortest detectable input (needs jumper)")
    s.add_argument("--trials", type=int, default=50)
    s.add_argument("--widths", type=float, nargs="+",
                   default=[100, 50, 20, 10, 5, 2, 1])
    s.set_defaults(fn=block_shortest)

    args = p.parse_args()
    print(f"\nblock {args.block}")
    args.fn(args)
    print()


if __name__ == "__main__":
    main()
