#!/usr/bin/env python3
"""SCPI client for a Siglent SDS1104X-E, over LAN or USBTMC, no dependencies.

    from scope import Scope
    with Scope(host="10.11.13.220") as s:      # LAN, preferred
        print(s.idn)

    with Scope(path="/dev/usbtmc4") as s:      # USBTMC fallback
        print(s.idn)

**Prefer LAN.** Both work, but they fail very differently. On USBTMC an
unsupported query can leave the bulk endpoint stalled, after which every read
returns nothing, the driver's own CLEAR and ABORT ioctls return EPIPE, and the
only recovery is unplugging the cable -- so one bad command costs a trip to the
bench and invalidates whatever was running. Over TCP the same mistake costs a
socket timeout and a reconnect. For an unattended run of a few hundred
acquisitions that difference decides whether the data survives.

The scope ships with a static address on 10.11.13.0/24 (this one is
10.11.13.220) and does NOT fall back to link-local, so on a direct cable the
host has to join that subnet:

    sudo ip addr add 10.11.13.1/24 dev <iface>

If the address is unknown, capture the scope's own ARP announcements while it
boots -- it probes for its address on the way up:

    sudo tcpdump -i <iface> -n -e arp
"""

import glob
import os
import re
import socket
import time

__all__ = ["Scope", "ScopeError", "ScopeNotFound"]

_NUM = re.compile(rb"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")


class ScopeError(RuntimeError):
    """Base class for errors raised by this module."""


class ScopeNotFound(ScopeError):
    """No instrument could be identified, or more than one was."""


class _LanTransport:
    """One SCPI connection over a raw TCP socket (port 5025)."""

    def __init__(self, host, port=5025, timeout=5.0):
        self.name = f"{host}:{port}"
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)

    def write(self, data):
        self._sock.sendall(data)

    def read(self, size):
        try:
            return self._sock.recv(size)
        except socket.timeout:
            return b""

    def close(self):
        self._sock.close()


class _UsbtmcTransport:
    """One SCPI connection over the kernel usbtmc character device."""

    #: Deliberately modest. Asking the driver for a megabyte to collect a
    #: twenty-byte reply issues an oversized bulk-IN request, which is a good
    #: way to stall this instrument's endpoint -- and a stalled endpoint cannot
    #: be recovered in software.
    MAX_READ = 1 << 20

    def __init__(self, path=None, timeout=5.0):
        self.name = path or self.find_device()
        try:
            self._fd = os.open(self.name, os.O_RDWR)
        except PermissionError:
            raise PermissionError(
                f"cannot open {self.name} as this user. Either\n"
                f"    sudo chmod a+rw {self.name}\n"
                "or, so it survives a replug (the node renumbers each time):\n"
                '    SUBSYSTEM=="usbmisc", KERNEL=="usbtmc*", '
                'GROUP="plugdev", MODE="0660"') from None

    @staticmethod
    def find_device(pattern="/dev/usbtmc*"):
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise ScopeNotFound(
                f"no device matching {pattern}. Is the scope on USB and the "
                "usbtmc module loaded? Check: lsusb | grep -i siglent")
        if len(matches) > 1:
            raise ScopeNotFound(f"{len(matches)} USBTMC devices: "
                                + ", ".join(matches) + "; pass path= explicitly")
        return matches[0]

    def write(self, data):
        os.write(self._fd, data)

    def read(self, size):
        return os.read(self._fd, min(size, self.MAX_READ))

    def close(self):
        os.close(self._fd)


class Scope:
    """An open connection to a Siglent SDS1000X-E series oscilloscope."""

    DEFAULT_HOST = "10.11.13.220"

    def __init__(self, host=None, port=5025, path=None, timeout=5.0,
                 headers=False):
        if path:
            self._t = _UsbtmcTransport(path, timeout)
        else:
            self._t = _LanTransport(host or self.DEFAULT_HOST, port, timeout)
        self.name = self._t.name
        self.idn = self.query("*IDN?")
        if not self.idn:
            raise ScopeError(f"{self.name}: no response to *IDN?")
        # Replies normally echo the command ("TDIV 5.00E-02S"). Turning headers
        # off makes them bare values, which is one less thing to strip -- but
        # every parse here tolerates either, because the setting is remembered
        # by the instrument and someone else may have changed it.
        if headers is False:
            self.write("CHDR OFF")

    # ------------------------------------------------------------ transport

    def write(self, cmd):
        if isinstance(cmd, str):
            cmd = cmd.encode()
        self._t.write(cmd if cmd.endswith(b"\n") else cmd + b"\n")

    def query(self, cmd, size=4096, delay=0.0):
        self.write(cmd)
        if delay:
            time.sleep(delay)
        return self._t.read(size).decode("latin-1").strip()

    def query_raw(self, cmd, size=1 << 22, delay=0.0):
        self.write(cmd)
        if delay:
            time.sleep(delay)
        return self._t.read(size)

    def close(self):
        if getattr(self, "_t", None) is not None:
            self._t.close()
            self._t = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -------------------------------------------------------------- parsing

    @staticmethod
    def number(reply):
        """Extract the numeric value from a reply, or None.

        Returns None for the instrument's "****", which is what a measurement
        reports when it cannot be made -- no signal, no edge, or a waveform that
        does not cross the threshold. That is a real outcome and must not be
        silently turned into a zero.
        """
        if not reply or "*****" in reply or "****" in reply:
            return None
        m = _NUM.search(reply.encode())
        return float(m.group()) if m else None

    def value(self, cmd):
        """Query a command and return its value as a float, or None."""
        return self.number(self.query(cmd))

    # ------------------------------------------------------------- measuring

    def delay(self, a, b, kind="FRR"):
        """Delay between two channels, in seconds, or None if unmeasurable.

        kind is the instrument's delay type: FRR is first-rise to first-rise,
        which is the one that corresponds to inter-line skew. Others include
        FRF, FFR, FFF for the other edge combinations, and PHA for phase.
        """
        return self.value(f"C{a}-C{b}:MEAD? {kind}")

    def param(self, ch, kind):
        """A single-channel parameter measurement, e.g. PKPK, WID, RISE."""
        return self.value(f"C{ch}:PAVA? {kind}")

    # ---------------------------------------------------------------- setup

    def apply(self, cmd, query, want, tol=1e-9, tries=5, delay=0.05):
        """Send a setting and confirm the instrument took it.

        Necessary, not defensive. A burst of setup commands sent back-to-back is
        silently truncated by this instrument -- sixteen channel-setup commands
        in a row left every channel at 500 uV/div while reporting no error, and
        the measurements that followed were of clipped noise and looked
        plausible. Reading each setting back both paces the traffic and turns a
        dropped command into a loud failure instead of a quiet one.

        want=None only paces and confirms a reply came back, for settings whose
        readback is not a simple number.
        """
        for attempt in range(tries):
            self.write(cmd)
            time.sleep(delay)
            got = self.query(query)
            if want is None:
                if got:
                    return got
                continue
            val = self.number(got)
            if val is not None and abs(val - want) <= tol * max(1.0, abs(want)):
                return val
        raise ScopeError(
            f"{cmd!r} did not take after {tries} attempts: {query} reports "
            f"{got!r}, wanted {want}")

    def channel(self, ch, on=True, vdiv=None, offset=None, coupling=None):
        self.apply(f"C{ch}:TRA {'ON' if on else 'OFF'}", f"C{ch}:TRA?", None)
        # Volts/div first: the valid offset range depends on it, so setting the
        # offset while the scale is still wrong can put it out of range.
        if vdiv is not None:
            self.apply(f"C{ch}:VDIV {vdiv}V", f"C{ch}:VDIV?", vdiv, tol=1e-3)
        if coupling is not None:
            self.apply(f"C{ch}:CPL {coupling}", f"C{ch}:CPL?", None)
        if offset is not None:
            self.apply(f"C{ch}:OFST {offset}V", f"C{ch}:OFST?", offset, tol=1e-3)

    #: The timebase is not continuous: it steps through a 1-2-5 sequence. Asking
    #: for anything else makes the instrument silently choose a neighbour, so a
    #: caller that computed a timebase from a signal duration must snap first --
    #: otherwise the verified setter rightly rejects a setting the instrument was
    #: never able to honour.
    TDIV_STEPS = [m * 10 ** e for e in range(-9, 2) for m in (1, 2, 5)]

    @classmethod
    def snap_timebase(cls, seconds_per_div):
        """The smallest valid timebase that is at least the value asked for."""
        for v in cls.TDIV_STEPS:
            if v >= seconds_per_div * (1 - 1e-9):
                return v
        return cls.TDIV_STEPS[-1]

    def timebase(self, seconds_per_div, snap=True):
        want = self.snap_timebase(seconds_per_div) if snap else seconds_per_div
        self.apply(f"TDIV {want}S", "TDIV?", want, tol=1e-3)
        return want

    def trigger_edge(self, ch, level, slope="POS", mode="NORM"):
        self.apply(f"TRSE EDGE,SR,C{ch},HT,OFF", "TRSE?", None)
        self.apply(f"C{ch}:TRLV {level}V", f"C{ch}:TRLV?", level, tol=1e-2)
        self.apply(f"C{ch}:TRSL {slope}", f"C{ch}:TRSL?", None)
        self.apply(f"TRMD {mode}", "TRMD?", None)

    def arm_single(self):
        """Arm one acquisition."""
        self.write("TRMD SINGLE")

    def wait_ready(self, timeout=5.0, poll=0.005):
        """Block until the acquisition has completed.

        INR bit 0 is set when a new signal has been acquired; reading INR clears
        it, so this must not be called twice per acquisition expecting the same
        answer.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            inr = self.value("INR?")
            if inr is not None and int(inr) & 1:
                return True
            time.sleep(poll)
        return False


    def wait_armed(self, timeout=5.0, poll=0.02):
        """Block until the instrument will actually accept a trigger.

        Arming is not instantaneous. With the trigger centred the instrument
        must first fill its pre-trigger buffer -- seven divisions' worth -- and
        a trigger arriving before that is ignored outright. SAST distinguishes
        the two states: "Arm" means still filling, "Ready" means waiting.

        Guessing a fixed delay instead is how an entire condition disappears:
        firing ~80 ms after arming captured almost every pulse at 2 ms/div,
        where the pre-trigger is 14 ms, and not one at 20 ms/div, where it is
        140 ms. The failure scales with the timebase, so it looks like long
        pulses being special rather than like an arming race.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.query("SAST?").lower()
            if "ready" in st or "trig" in st or "stop" in st:
                return True
            time.sleep(poll)
        return False

    def wait_stopped(self, timeout=5.0, poll=0.02):
        """Block until a single-shot acquisition has completed.

        Uses SAST (Arm / Ready / Trig'd / Stop) rather than the INR status bit.
        INR is latched and cleared by reading, so a stray query anywhere in the
        loop consumes the very event being waited for, and the failure is
        intermittent and looks like a flaky trigger.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "stop" in self.query("SAST?").lower():
                return True
            time.sleep(poll)
        return False


if __name__ == "__main__":
    import sys
    kw = {"path": sys.argv[1]} if len(sys.argv) > 1 and sys.argv[1].startswith("/dev") \
        else {"host": sys.argv[1]} if len(sys.argv) > 1 else {}
    with Scope(**kw) as s:
        print(f"connection  {s.name}")
        print(f"*IDN?       {s.idn}")
        print(f"timebase    {s.value('TDIV?')} s/div")
        print(f"sample rate {s.value('SARA?'):.3g} Sa/s")
        print(f"memory      {s.query('MSIZ?')}")
