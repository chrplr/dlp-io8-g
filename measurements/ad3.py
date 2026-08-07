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


class AD3:
    """An open connection to an Analog Discovery 3."""

    def __init__(self, channels=(0, 1), rate=1e6):
        self.h = c_int()
        _check(_dwf.FDwfDeviceOpen(c_int(-1), byref(self.h)) != 0 and self.h.value != 0,
               "opening the device")
        self.channels = tuple(channels)
        self.rate = rate
        for ch in self.channels:
            _dwf.FDwfAnalogInChannelEnableSet(self.h, c_int(ch), c_int(1))
            _dwf.FDwfAnalogInChannelRangeSet(self.h, c_int(ch), c_double(RANGE_V))
            _dwf.FDwfAnalogInChannelOffsetSet(self.h, c_int(ch), c_double(OFFSET_V))
        _dwf.FDwfAnalogInAcquisitionModeSet(self.h, c_int(ACQ_RECORD))
        _dwf.FDwfAnalogInFrequencySet(self.h, c_double(rate))

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
        return data, {"samples": pos, "lost": lost_total, "corrupted": corrupt_total,
                      "seconds": time.perf_counter() - t0}


def raw_threshold(volts=2.5):
    """The int16 count corresponding to a voltage, for the configured range."""
    return (volts - OFFSET_V) / RANGE_V * 65536


def rising_edges(samples, rate, volts=2.5):
    """Times (seconds from capture start) of every rising threshold crossing.

    Sub-sample resolved by linear interpolation between the two straddling
    samples, so the resolution is not the sample interval: at 1 MS/s the
    crossings land well inside a microsecond, which matters when the effect
    being measured is tens of microseconds.
    """
    thr = raw_threshold(volts)
    above = samples > thr
    idx = np.flatnonzero(~above[:-1] & above[1:])
    if idx.size == 0:
        return np.empty(0)
    a = samples[idx].astype(np.float64)
    b = samples[idx + 1].astype(np.float64)
    frac = np.where(b != a, (thr - a) / (b - a), 0.0)
    return (idx + frac) / rate


def falling_edges(samples, rate, volts=2.5):
    """Times of every falling threshold crossing, interpolated as above."""
    thr = raw_threshold(volts)
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
            v = OFFSET_V + RANGE_V * s.astype(np.float64) / 65536
            print(f"  CH{ch + 1}: {v.min():+.3f} .. {v.max():+.3f} V, "
                  f"{len(rising_edges(s, d.rate))} rising edges")
