#! /usr/bin/env python3
# Time-stamp: <2022-04-29 08:31:31 christophe@pallier.org>

""" Generate a square wave on pin1 of DLP-IO8-G """

from time import sleep, perf_counter
from serial import Serial

dlp = Serial(port='/dev/ttyUSB0', baudrate=115200, timeout=0.1)  # open serial port
# byte codes to control line 1:
ON1 = b'1'
OFF1 = b'Q'
READ2 = b'S'

# number of periods
NPERIODS = 10

# Timing of the square wave
TIME_HIGH = 1.000   # 1s pulse
TIME_LOW = 4.000    # send every 5000ms
PERIOD = TIME_HIGH + TIME_LOW

onset_times = [(i * PERIOD) for i in range(NPERIODS)]

actual_onsets = []
line2events = []
i = 0
dlp.write(READ2)
state2 = dlp.read(3)
t0 = perf_counter()


while i < NPERIODS:
    # busy wait until the start of the next period
    while perf_counter() - t0 < onset_times[i]:
        dlp.write(READ2)
        state2a = dlp.read(3)
        if len(state2a) > 0 and state2a != state2:
            line2events.append(perf_counter() - t0)
            state2 = state2a
            print(state2)
        pass

    actual_onsets.append(perf_counter() - t0)
    dlp.write(ON1)

    # busy wait for 'TIME_HIGH' seconds. 
    t1 = perf_counter()
    while perf_counter() - t1 < (TIME_HIGH):
        dlp.write(READ2)
        state2a = dlp.read(3)
        if len(state2a) > 0 and state2a != state2:
            line2events.append(perf_counter() - t0)
            state2 = state2a
            print(state2)
        pass

    dlp.write(OFF1)
    i = i + 1
    print(f"\r{i:4d}", end='')

sleep(TIME_LOW)
print(f'\r{NPERIODS} periods of {PERIOD} seconds')
print('Total time-elapsed: ' + str(perf_counter() - t0))
print("Actual onsets:")
for t in actual_onsets:
    print(t, end=" ")
print(end="\n")

print("Change events on line 2:")
for t in line2events:
    print(t, end=" ")
print(end="\n")



dlp.close()         # close the port
