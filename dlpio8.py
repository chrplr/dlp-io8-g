#!/usr/bin/env python3
"""A client for the DLP-IO8-G USB TTL input/output module.

    from dlpio8 import DLPIO8

    with DLPIO8() as dlp:          # finds the device by USB id
        dlp.high(1)                # channel 1 -> 5 V
        dlp.low(1)                 # channel 1 -> 0 V
        print(dlp.read_all())      # [0, 0, 0, 0, 0, 0, 0, 0]

Three properties of this device shape everything below, and none of them are
obvious from the datasheet's command table.

**There is no atomic multi-channel write.** Every command is a single ASCII byte
affecting one channel, so setting an 8-bit trigger code means sending 8 bytes
which the module acts on as they arrive. At 115200 8N1 that is 8 x 86.8 us of
serialisation, during which the port shows a partly-updated value. A recording
device sampling at 1 kHz can latch that intermediate. See `write_mask`.

**There is no device clock.** The module never timestamps anything and cannot be
asked what time it is, so every timestamp available to you is host-side and
includes the USB round trip.

**There is no device-side pulse timer.** A pulse is two writes from the host, so
its width absorbs host scheduling jitter in full. See `pulse`.

And one property of the *driver* rather than the device, which dominates every
read: the FTDI latency timer, 16 ms by default. See `latency_timer`.
"""

import glob
import os
import time

import serial

__all__ = ["DLPIO8", "DLPError", "PortNotFound", "PingFailed", "CHANNELS"]

CHANNELS = range(1, 9)


class DLPError(RuntimeError):
    """Base class for errors raised by this module."""


class PortNotFound(DLPError):
    """No DLP-IO8 could be identified, or more than one was."""


class PingFailed(DLPError):
    """The device on the port did not answer the ping with 'Q'."""


class DLPIO8:
    """An open connection to a DLP-IO8-G.

    Not safe for concurrent use: every operation is a write, and some are a
    write followed by a read, so two threads sharing one instance will
    interleave into each other's replies.
    """

    #: Command byte per channel, indexed 0-7 for channels 1-8.
    HIGH_CMDS = b"12345678"
    LOW_CMDS = b"QWERTYUI"
    DIGITAL_IN_CMDS = b"ASDFGHJK"
    ANALOG_IN_CMDS = b"ZXCVBNM,"

    PING = b"'"
    ASCII_MODE = b"`"
    BINARY_MODE = b"\\"

    #: Where to look for the device. The USB serial number is in the name, so
    #: this also distinguishes two DLPs from each other.
    BY_ID_GLOB = "/dev/serial/by-id/*DLP*"

    BAUDRATE = 115200

    def __init__(self, port=None, baudrate=BAUDRATE, timeout=1.0,
                 binary=True, ping=True):
        """Open the device.

        port     device path, or None to find it by USB id
        binary   read replies as one raw byte per channel rather than ASCII
        ping     verify the device answers before returning
        """
        self.port_path = port or self.find_port()
        self.serial = serial.Serial(self.port_path, baudrate, timeout=timeout)
        self._binary = None
        if ping:
            self.ping()
        self.set_binary(binary)

    # ---------------------------------------------------------------- setup

    @staticmethod
    def find_port(glob_pattern=BY_ID_GLOB):
        """Resolve the device path from its USB id.

        Never guess a bare /dev/ttyUSBn. On a bench with a DLP, a Black Box
        ToolKit and a scope adapter, all three are FTDI-class devices competing
        for the same names, and which one is ttyUSB0 depends on the order they
        were plugged in. Sending DLP command bytes to the wrong one is the least
        bad outcome; setting the wrong device's latency timer while believing
        you set the DLP's is worse, because the run still produces numbers.
        """
        matches = sorted(glob.glob(glob_pattern))
        if not matches:
            raise PortNotFound(
                f"no device matching {glob_pattern}. Is it plugged in? "
                "Check with: ls -l /dev/serial/by-id/")
        if len(matches) > 1:
            raise PortNotFound(
                f"{len(matches)} devices match {glob_pattern}:\n  "
                + "\n  ".join(matches)
                + "\nPass port= explicitly to say which one you mean.")
        return matches[0]

    def ping(self):
        """Check the device answers. Raises PingFailed if not."""
        self.serial.reset_input_buffer()
        self.serial.write(self.PING)
        reply = self.serial.read(1)
        if reply != b"Q":
            raise PingFailed(
                f"{self.port_path}: ping returned {reply!r}, expected b'Q'. "
                "Either this is not a DLP-IO8, or a previous read left "
                "unconsumed bytes in the stream.")
        return True

    def set_binary(self, binary=True):
        """Choose the reply format for reads.

        Binary returns one raw byte per channel; ASCII returns three, the digit
        followed by LF and CR (in that order). Binary is the default here
        because a fixed one byte per command makes a read length predictable,
        which ASCII's three-byte replies only accidentally are.
        """
        self.serial.write(self.BINARY_MODE if binary else self.ASCII_MODE)
        self.serial.flush()
        time.sleep(0.05)
        self.serial.reset_input_buffer()
        self._binary = binary

    def close(self):
        if self.serial and self.serial.is_open:
            self.serial.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- output

    @staticmethod
    def _cmd_bytes(channels, table):
        out = bytearray()
        for ch in channels:
            if ch not in CHANNELS:
                raise ValueError(f"channel {ch} out of range (1-8)")
            out.append(table[ch - 1])
        return bytes(out)

    def high(self, *channels):
        """Drive the given channels to 5 V."""
        return self._write(self._cmd_bytes(channels, self.HIGH_CMDS))

    def low(self, *channels):
        """Drive the given channels to 0 V."""
        return self._write(self._cmd_bytes(channels, self.LOW_CMDS))

    def write_mask(self, mask):
        """Set all 8 channels to match an 8-bit mask, bit 0 = channel 1.

        **This is not atomic and cannot be.** It emits one byte per channel, all
        8 of them, which the module acts on as they arrive: the port takes about
        0.7 ms to settle and shows wrong intermediate values throughout. If the
        thing reading these lines samples faster than that -- an MEG or EEG
        amplifier at 1 kHz certainly does -- it can and will record a code that
        was never intended.

        There is no workaround at the device level. If a trigger code has to be
        unambiguous, either hold the receiving system off until the port has
        settled, or use hardware that writes a whole port in one instruction.
        """
        if not 0 <= mask <= 0xFF:
            raise ValueError(f"mask {mask} out of range (0-255)")
        cmds = bytearray()
        for ch in CHANNELS:
            table = self.HIGH_CMDS if mask & (1 << (ch - 1)) else self.LOW_CMDS
            cmds.append(table[ch - 1])
        return self._write(bytes(cmds))

    def pulse(self, channels, seconds):
        """Drive channels high, wait, drive them low.

        The width is timed by the host, because the module has no pulse timer of
        its own: it is the interval between two writes, and it inherits whatever
        the operating system does to this process in between. A busy-wait is
        used rather than time.sleep for that reason, but it only removes the
        sleep granularity, not preemption -- under load the realised width has
        no upper bound.
        """
        if not isinstance(channels, (list, tuple)):
            channels = [channels]
        self.high(*channels)
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            pass
        self.low(*channels)

    def _write(self, data):
        """Write and return the host time immediately afterwards.

        Deliberately no flush of the output buffer beforehand. The C and Go
        clients in this repository call tcflush(TCOFLUSH) / ResetOutputBuffer(),
        which DISCARDS anything still queued rather than waiting for it -- so a
        trigger issued while a previous one is still going out can silently lose
        bytes, leaving a line stuck high or a pulse never emitted. pyserial's
        write() simply appends, which is the behaviour you want.

        The returned timestamp is when the kernel accepted the bytes, not when
        they reached the wire, so it bounds the true edge time from below.
        """
        self.serial.write(data)
        return time.perf_counter()

    def drain(self):
        """Block until everything written has actually gone out (tcdrain).

        Note this is pyserial's flush(), which drains. reset_output_buffer() is
        the one that discards; do not reach for it to "make sure the port is
        clean" before a trigger.
        """
        self.serial.flush()

    # ----------------------------------------------------------------- input

    def read(self, *channels):
        """Read the given channels as digital inputs; returns a list of 0/1.

        Reading switches a channel to input mode, per the datasheet: "the mode
        of each I/O is automatically changed with each command sent". So reading
        a channel you were driving stops it driving.
        """
        cmds = self._cmd_bytes(channels, self.DIGITAL_IN_CMDS)
        self.serial.reset_input_buffer()
        self.serial.write(cmds)
        width = 1 if self._binary else 3
        raw = self.serial.read(width * len(cmds))
        if len(raw) != width * len(cmds):
            raise DLPError(
                f"short read: wanted {width * len(cmds)} bytes, got {len(raw)}. "
                "At the default FTDI latency timer of 16 ms a read can take "
                "that long to come back; raise the serial timeout, or lower the "
                "latency timer (see DLPIO8.latency_timer).")
        if self._binary:
            return [b & 1 for b in raw]
        return [int(raw[i * 3:i * 3 + 1]) for i in range(len(cmds))]

    def read_all(self):
        """Read all 8 channels; returns a list of 0/1 indexed channel 1 first."""
        return self.read(*CHANNELS)

    # -------------------------------------------------------- driver tuning

    @property
    def sysfs_dir(self):
        """The /sys directory for this port, or None if it is not a usb-serial."""
        name = os.path.basename(os.path.realpath(self.port_path))
        path = f"/sys/bus/usb-serial/devices/{name}"
        return path if os.path.isdir(path) else None

    @property
    def latency_timer(self):
        """The FTDI latency timer for this port, in ms, or None if unavailable.

        This is the single most important number for anything that reads from
        the device. The FTDI chip batches data going back to the host, and holds
        a partly-filled buffer for this long before sending it. The driver
        default is 16 ms, which means a poll answered instantly by the module
        still takes up to 16 ms to reach your program, averaging 8 ms if your
        polls are unsynchronised and pinning at the full 16 if they are.

        Setting it to 1 is the usual fix and costs nothing that matters here.
        Writing it needs root, and it resets when the device is replugged unless
        a udev rule makes it stick:

            SUBSYSTEM=="usb-serial", DRIVERS=="ftdi_sio", ATTR{latency_timer}="1"

        It has no effect on the write path, which is governed by USB frame
        scheduling instead.
        """
        d = self.sysfs_dir
        if not d:
            return None
        try:
            with open(os.path.join(d, "latency_timer")) as f:
                return int(f.read().strip())
        except OSError:
            return None

    @latency_timer.setter
    def latency_timer(self, ms):
        if not 1 <= ms <= 255:
            raise ValueError(f"latency timer {ms} out of range (1-255 ms)")
        d = self.sysfs_dir
        if not d:
            raise DLPError(f"{self.port_path} is not a usb-serial device")
        path = os.path.join(d, "latency_timer")
        try:
            with open(path, "w") as f:
                f.write(str(ms))
        except PermissionError:
            raise PermissionError(
                f"cannot write {path} as this user. Run:\n"
                f"    echo {ms} | sudo tee {path}\n"
                "or install a udev rule so it survives a replug:\n"
                '    SUBSYSTEM=="usb-serial", DRIVERS=="ftdi_sio", '
                'ATTR{latency_timer}="1"') from None


if __name__ == "__main__":
    with DLPIO8() as dlp:
        print(f"port           {dlp.port_path}")
        print(f"latency_timer  {dlp.latency_timer} ms"
              + ("   <- the driver default; every read pays up to this"
                 if dlp.latency_timer == 16 else ""))
        print(f"inputs         {dlp.read_all()}")
