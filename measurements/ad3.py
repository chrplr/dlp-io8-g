#!/usr/bin/env python3
"""Streaming capture from a Digilent Analog Discovery 3, via the WaveForms SDK.

Why this rather than a bench scope: an oscilloscope driven over SCPI costs about
a second and a half per acquisition, which caps a measurement at a hundred or so
trials in a sitting. The AD3 records CONTINUOUSLY to host memory, so one capture
holds thousands of trials at 1 us resolution. The trials are fired from inside
the polling loop and the edges are recovered offline.

Use the ANALOG inputs for 5 V logic. They accept 5 to 50 V ranges. The 16
digital channels run at 3.3 V and are not the place for a 5 V TTL line.

Needs libdwf (the WaveForms SDK) and a udev rule for the Digilent USB device.
"""

import time
from ctypes import CDLL, byref, c_byte, c_double, c_int, c_int16, create_string_buffer

import numpy as np

_dwf = CDLL("libdwf.so")

ACQ_RECORD = 3
STS_DONE = 2

#: Volts. A 0-5 V logic swing sits comfortably inside a 5 V range centred on 2.5.
RANGE_V = 5.0
OFFSET_V = 2.5


class AD3Error(RuntimeError):
    """Raised when the device reports a problem, with its own message attached."""


def _check(ok, what):
    if not ok:
        err = create_string_buffer(512)
        _dwf.FDwfGetLastErrorMsg(err)
        raise AD3Error(f"{what}: {err.value.decode().strip()}")


def _per_channel(value, channels, what):
    """Accept a scalar for every channel, or a dict of per-channel values."""
    if isinstance(value, dict):
        missing = [c for c in channels if c not in value]
        if missing:
            raise ValueError(f"{what}: no value for channel(s) {missing}")
        return {c: float(value[c]) for c in channels}
    return {c: float(value) for c in channels}


class AD3:
    """An open connection to an Analog Discovery 3.

    range_v, offset_v and attenuation are each either one value for every
    channel or a dict keyed by channel index, because the two inputs rarely
    want the same settings: a 0-5 V logic line and a 9 V-powered photodiode
    through a 10x probe do not share a window.

    With attenuation set, range_v and offset_v are measured AT THE PROBE, not
    at the device input. A 10x probe therefore has only two usable ranges, 50 V
    and 500 V, because the input's own 5 V and 50 V are multiplied by ten. Ask
    for 5 V through a 10x probe and the device cannot comply; it applies about
    51 V and this constructor says so rather than letting the conversion be
    wrong by an order of magnitude.

    # The device substitutes ranges, quietly

    An AD3 offers two input ranges, nominally 5 V and 50 V, and silently picks
    the nearest one it can honour. Ask for 10 V and it applies about 58.6 V
    without complaining. Anything that then converts counts to volts using the
    number it *asked* for is wrong by that factor -- five point nine, in that
    example, which is more than enough to make a 5 V logic signal look like
    0.87 V and send you hunting for a wiring fault that is not there.

    So this reads the applied range and offset back from the device, converts
    with those, and refuses to continue if the substitution was large. Pass
    allow_range_substitution=True to accept it deliberately; the applied values
    are then in self.range_v and self.offset_v, and in record()'s stats.

    Requesting an offset the chosen range cannot reach forces a substitution
    too: a 5 V window cannot be centred at 5.75 V, so asking for that gets the
    50 V range even though the range itself looked fine.
    """

    def __init__(self, channels=(0, 1), rate=1e6, range_v=RANGE_V,
                 offset_v=OFFSET_V, attenuation=1.0,
                 allow_range_substitution=False):
        self.h = c_int()
        _check(_dwf.FDwfDeviceOpen(c_int(-1), byref(self.h)) != 0 and self.h.value != 0,
               "opening the device")
        self.channels = tuple(channels)
        self.rate = rate
        want_range = _per_channel(range_v, self.channels, "range_v")
        want_offset = _per_channel(offset_v, self.channels, "offset_v")
        self.attenuation = _per_channel(attenuation, self.channels, "attenuation")

        for ch in self.channels:
            _dwf.FDwfAnalogInChannelEnableSet(self.h, c_int(ch), c_int(1))
            # Attenuation first: it rescales what a range means, so setting it
            # afterwards would reinterpret a range that was already chosen.
            _dwf.FDwfAnalogInChannelAttenuationSet(
                self.h, c_int(ch), c_double(self.attenuation[ch]))
            _dwf.FDwfAnalogInChannelRangeSet(self.h, c_int(ch), c_double(want_range[ch]))
            _dwf.FDwfAnalogInChannelOffsetSet(self.h, c_int(ch), c_double(want_offset[ch]))
        _dwf.FDwfAnalogInAcquisitionModeSet(self.h, c_int(ACQ_RECORD))
        _dwf.FDwfAnalogInFrequencySet(self.h, c_double(rate))

        # Push the settings so the device commits to a range, then read back
        # what it actually chose. Without the configure the getters can still
        # report what was asked for rather than what will be used.
        _dwf.FDwfAnalogInConfigure(self.h, c_int(1), c_int(0))
        self.range_v, self.offset_v = {}, {}
        for ch in self.channels:
            got_r, got_o = c_double(), c_double()
            _dwf.FDwfAnalogInChannelRangeGet(self.h, c_int(ch), byref(got_r))
            _dwf.FDwfAnalogInChannelOffsetGet(self.h, c_int(ch), byref(got_o))
            self.range_v[ch], self.offset_v[ch] = got_r.value, got_o.value

        if not allow_range_substitution:
            for ch in self.channels:
                asked, got = want_range[ch], self.range_v[ch]
                # A few percent is the device's own calibration; 5 V nominal
                # comes back as about 5.12. A factor is a substitution.
                if abs(got - asked) > 0.25 * asked:
                    atten = self.attenuation[ch]
                    hint = (f"It offers 5 V and 50 V at the input and picks the nearest "
                            f"it can honour. An offset the range cannot reach (here "
                            f"{want_offset[ch]:g} V) forces the wider one too.")
                    if atten != 1.0:
                        hint = (f"With attenuation x{atten:g} the range is measured at the "
                                f"PROBE, not at the input, so {asked:g} V would need "
                                f"{asked / atten:g} V at the input. The two input ranges "
                                f"are 5 V and 50 V, which through this probe are "
                                f"{5 * atten:g} V and {50 * atten:g} V -- ask for one of "
                                f"those. An unreachable offset forces the wider one too.")
                    self.close()
                    raise AD3Error(
                        f"channel {ch + 1}: asked for a {asked:g} V range, the device "
                        f"applied {got:.3f} V. {hint} Pass "
                        f"allow_range_substitution=True to accept this one.")

    def to_volts(self, ch, samples):
        """Convert this channel's raw counts to volts, using the APPLIED range."""
        return self.offset_v[ch] + self.range_v[ch] * np.asarray(samples, dtype=np.float64) / 65536

    def close(self):
        if self.h.value:
            _dwf.FDwfDeviceCloseAll()
            self.h.value = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def record(self, seconds, on_tick=None, settle=1.0):
        """Stream both channels for `seconds`, returning raw int16 arrays.

        on_tick, if given, is called once per polling iteration with the elapsed
        time. That is where trials are fired: no thread is involved, so nothing
        can preempt the drain loop, and the device's 16384-sample buffer gives
        16 ms of slack at 1 MS/s against the ~50 us a serial write costs.

        Loss is returned, never swallowed. Record mode drops samples when the
        host stops draining, and a capture with steady-state loss has holes in
        it wherever the interesting edges are most likely to be.

        The one rule that matters: do not sleep between configuring and the
        first read. The device starts sampling on Configure, and 200 ms of not
        reading at 1 MS/s overruns the buffer and loses 16384 samples before
        the first trial is fired.

        Two things bound how fast a capture can safely run, and only the first
        is obvious. The device streams rate x channels x 2 bytes per second, and
        the host has to absorb it; but the loop below also has to do its own
        work per iteration, and if that work allocates, the interpreter is doing
        memory management inside the window where the buffer is filling. The
        output is preallocated here for exactly that reason -- an earlier
        version appended a freshly allocated chunk per iteration and built up
        several hundred megabytes over a long capture, and lost a buffer to it
        even at real-time priority, where scheduling could not be the cause.
        """
        _dwf.FDwfAnalogInRecordLengthSet(self.h, c_double(seconds))

        # Preallocated once, with margin: the device delivers a little more than
        # asked for, and growing an array mid-capture is the thing being avoided.
        cap = int(seconds * self.rate * 1.05) + (1 << 16)
        out = {ch: np.empty(cap, dtype=np.int16) for ch in self.channels}
        buf = (c_int16 * 262144)()
        view = np.frombuffer(buf, dtype=np.int16)

        _check(_dwf.FDwfAnalogInConfigure(self.h, c_int(1), c_int(1)) != 0,
               "starting the recording")

        sts, av, lost, corrupt = c_byte(), c_int(), c_int(), c_int()
        pos = lost_total = corrupt_total = 0
        t0 = time.perf_counter()

        while True:
            _check(_dwf.FDwfAnalogInStatus(self.h, c_int(1), byref(sts)) != 0,
                   "reading status")
            _dwf.FDwfAnalogInStatusRecord(self.h, byref(av), byref(lost), byref(corrupt))
            lost_total += lost.value
            corrupt_total += corrupt.value
            if av.value:
                n = min(av.value, 262144, cap - pos)
                if n > 0:
                    for ch in self.channels:
                        _dwf.FDwfAnalogInStatusData16(self.h, c_int(ch), buf,
                                                      c_int(0), c_int(n))
                        out[ch][pos:pos + n] = view[:n]
                    pos += n
            if on_tick is not None:
                on_tick(time.perf_counter() - t0)
            if sts.value == STS_DONE and av.value == 0:
                break

        data = {ch: out[ch][:pos] for ch in self.channels}
        # The applied range and offset travel with the data: a capture saved
        # without them cannot be converted to volts afterwards, and the numbers
        # are not necessarily the ones that were requested.
        return data, {"samples": pos, "lost": lost_total, "corrupted": corrupt_total,
                      "seconds": time.perf_counter() - t0,
                      "rate": self.rate,
                      "range_v": dict(self.range_v),
                      "offset_v": dict(self.offset_v),
                      "attenuation": dict(self.attenuation)}


def raw_threshold(volts=2.5, range_v=RANGE_V, offset_v=OFFSET_V):
    """The int16 count corresponding to a voltage, for the given range.

    range_v and offset_v must be the values the device APPLIED, which are in
    AD3.range_v / AD3.offset_v and in record()'s stats. They are not always the
    ones that were requested -- see AD3.
    """
    return (volts - offset_v) / range_v * 65536


def _threshold_counts(samples, volts, relative, range_v, offset_v, strict, what):
    """Resolve a threshold to raw counts, and refuse an unreachable one.

    An absolute threshold that the signal never approaches produces no edges and
    no error, which is the worst combination: the analysis downstream sees an
    empty array and reports nothing rather than reporting a problem. That has
    happened three times on this bench -- a 2.5 V default against a photodiode
    peaking at 1.4 V, the same default again after a 10x probe divided the
    signal to 0.83 V, and a range the device silently substituted. So when the
    data plainly swing and the threshold sits outside that swing, this raises.

    A genuinely flat channel is different: a line that never toggled has no
    swing to be outside of, and zero edges is the right answer there.
    """
    lo, hi = np.percentile(samples, (1, 99))
    swing = hi - lo
    to_v = lambda c: offset_v + range_v * c / 65536  # noqa: E731

    if relative is not None:
        # A relative threshold on a channel that is not switching lands in the
        # noise and returns a crossing every few samples -- thousands of
        # "edges" that look like data. Refuse instead: a signal with no swing
        # has no 50% level worth speaking of.
        if strict and swing < 0.02 * 65536:
            raise ValueError(
                f"{what}: relative threshold asked for, but this channel only spans "
                f"{to_v(hi) - to_v(lo):.4f} V ({to_v(lo):.4f} to {to_v(hi):.4f}), which "
                f"is under 2% of the {range_v:.1f} V range and is indistinguishable from "
                f"noise. Placing a threshold in the middle of that would return a "
                f"crossing every few samples. Check the wiring, the probe attenuation, "
                f"and that the signal was present during the capture; pass strict=False "
                f"if you really mean to threshold a flat channel.")
        thr = lo + relative * (hi - lo)
    else:
        thr = raw_threshold(volts, range_v, offset_v)

    if strict and swing > 0.02 * 65536 and not (lo < thr < hi):
        raise ValueError(
            f"{what}: threshold {to_v(thr):.3f} V is outside the signal, which runs "
            f"{to_v(lo):.3f} to {to_v(hi):.3f} V (1st to 99th percentile). Every "
            f"crossing would be missed and the result would be an empty array rather "
            f"than an error. Pass relative=0.5 to place the threshold at the midpoint "
            f"of whatever the signal actually does, give a volts= inside that span, or "
            f"pass strict=False if no edges really is the expected answer.")
    return thr


def rising_edges(samples, rate, volts=2.5, *, relative=None,
                 range_v=RANGE_V, offset_v=OFFSET_V, strict=True):
    """Times (seconds from capture start) of every rising threshold crossing.

    Sub-sample resolved by linear interpolation between the two straddling
    samples, so the resolution is not the sample interval: at 1 MS/s the
    crossings land well inside a microsecond, which matters when the effect
    being measured is tens of microseconds.

    The threshold is 2.5 V by default, which suits a 0-5 V logic line on the
    default range and nothing else. For any other signal pass relative=0.5,
    which puts it at the midpoint of the 1st..99th percentile of the data and so
    needs no prior knowledge of the amplitude -- or pass volts= together with
    the range_v/offset_v the device applied.
    """
    thr = _threshold_counts(samples, volts, relative, range_v, offset_v,
                            strict, "rising_edges")
    above = samples > thr
    idx = np.flatnonzero(~above[:-1] & above[1:])
    if idx.size == 0:
        return np.empty(0)
    a = samples[idx].astype(np.float64)
    b = samples[idx + 1].astype(np.float64)
    frac = np.where(b != a, (thr - a) / (b - a), 0.0)
    return (idx + frac) / rate


def falling_edges(samples, rate, volts=2.5, *, relative=None,
                  range_v=RANGE_V, offset_v=OFFSET_V, strict=True):
    """Times of every falling threshold crossing, interpolated as above.

    Same threshold rules as [rising_edges].
    """
    thr = _threshold_counts(samples, volts, relative, range_v, offset_v,
                            strict, "falling_edges")
    above = samples > thr
    idx = np.flatnonzero(above[:-1] & ~above[1:])
    if idx.size == 0:
        return np.empty(0)
    a = samples[idx].astype(np.float64)
    b = samples[idx + 1].astype(np.float64)
    frac = np.where(b != a, (a - thr) / (a - b), 0.0)
    return (idx + frac) / rate


if __name__ == "__main__":
    with AD3() as d:
        data, stats = d.record(1.0)
        print(f"  {stats['samples']} samples in {stats['seconds']:.2f} s, "
              f"lost {stats['lost']}, corrupted {stats['corrupted']}")
        for ch, s in data.items():
            v = d.to_volts(ch, s)
            n = len(rising_edges(s, d.rate, relative=0.5, strict=False))
            print(f"  CH{ch + 1}: {v.min():+.3f} .. {v.max():+.3f} V "
                  f"(range {d.range_v[ch]:.3f}, offset {d.offset_v[ch]:+.3f}, "
                  f"x{d.attenuation[ch]:g}), {n} rising edges at the 50% level")
