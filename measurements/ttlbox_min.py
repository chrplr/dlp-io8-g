#!/usr/bin/env python3
"""A deliberately minimal client for the NeuroSpin MEG TTL box.

This exists only so the head-to-head measurement can drive both devices from
one host loop. The real client is Go -- github.com/neurospin/neurospin-meg-ttl-box
-- and it is the one to use for anything else: it does capability detection,
timestamped input events, clock alignment and error handling, none of which is
reimplemented here.

What is here is the little that a head-to-head needs: open the port, confirm the
firmware speaks protocol v1, and set the output port. The protocol is a binary
opcode followed by its argument, and the two used are:

    1        get_info        -> 'M','T','B', version u8, caps u8
    17       set_port_mask   [u8 mask]   assigns all 8 lines in one AVR write

Opening the port asserts DTR, which resets the board, hence the wait.
"""

import glob
import time

import serial

OP_GET_INFO = 1
OP_SET_TRIGGER_DURATION = 10
OP_SEND_TRIGGER_MASK = 11
OP_SET_PORT_MASK = 17

CAP_ATOMIC_PORT = 0x01
CAP_TIMESTAMPS = 0x02


class TTLBoxMin:
    BY_ID_GLOB = "/dev/serial/by-id/*Arduino*"

    def __init__(self, port=None, baudrate=115200, timeout=1.0, reset_delay=2.0):
        self.port_path = port or self.find_port()
        self.serial = serial.Serial(self.port_path, baudrate, timeout=timeout)
        time.sleep(reset_delay)
        self.version, self.caps = self.get_info()

    @staticmethod
    def find_port(pattern=BY_ID_GLOB):
        matches = sorted(glob.glob(pattern))
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one device matching {pattern}, found {matches}")
        return matches[0]

    def get_info(self):
        self.serial.reset_input_buffer()
        self.serial.write(bytes([OP_GET_INFO]))
        r = self.serial.read(5)
        if len(r) != 5 or r[:3] != b"MTB":
            raise RuntimeError(
                f"get_info returned {r!r}; expected b'MTB' + version + caps. "
                "Firmware older than protocol v1 ignores the opcode and stays "
                "silent, so a short read means an old board rather than a bad one.")
        return r[3], r[4]

    def set_port(self, mask):
        """Assign all 8 output lines at once (opcode 17).

        One AVR port write, so unlike the DLP-IO8 there is no interval during
        which a partly-updated code is visible. That difference is the point of
        the comparison this client exists for.
        """
        return self.serial.write(bytes([OP_SET_PORT_MASK, mask & 0xFF]))

    def set_trigger_duration(self, ms):
        """Set the pulse width the firmware will use (opcode 10, u16 LE ms)."""
        if not 0 <= ms <= 65535:
            raise ValueError(f"duration {ms} ms out of range (0-65535)")
        return self.serial.write(
            bytes([OP_SET_TRIGGER_DURATION, ms & 0xFF, (ms >> 8) & 0xFF]))

    def send_trigger(self, mask):
        """Pulse the masked lines for the duration set above (opcode 11).

        The width is timed by the FIRMWARE, from millis(), and torn down in the
        device's own loop. That is the whole point of using this rather than two
        set_port calls: the host issues one command and is not involved in when
        the line drops, so the width cannot absorb host scheduling.

        It costs a known bias in exchange: millis() truncates, so the realised
        width is uniform on [w-1, w] and averages about 0.5 ms short of the
        request. That is documented and reproducible, unlike jitter.
        """
        return self.serial.write(bytes([OP_SEND_TRIGGER_MASK, mask & 0xFF]))

    def close(self):
        try:
            self.set_port(0)
        finally:
            self.serial.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


if __name__ == "__main__":
    with TTLBoxMin() as b:
        print(f"  {b.port_path}")
        print(f"  firmware v{b.version}, caps 0x{b.caps:02X} "
              f"(atomic port: {bool(b.caps & CAP_ATOMIC_PORT)}, "
              f"timestamps: {bool(b.caps & CAP_TIMESTAMPS)})")
