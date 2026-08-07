// Package dlp is a client for the DLP-IO8-G USB TTL input/output module.
//
//	d, err := dlp.New("")          // "" finds the device by USB id
//	if err != nil { log.Fatal(err) }
//	defer d.Close()
//
//	d.High(1)                      // channel 1 -> 5 V
//	d.Low(1)                       // channel 1 -> 0 V
//	states, err := d.ReadAll()     // [0 0 0 0 0 0 0 0]
//
// Three properties of the hardware shape this API, and none are obvious from
// the datasheet's command table. They are measured, not inferred; the figures
// come from a scope and a Black Box ToolKit, with raw data and method in the
// measurements/ directory of this repository.
//
// # There is no atomic multi-channel write
//
// Every command is a single ASCII byte affecting one channel, so setting an
// 8-bit code means sending eight bytes which the module acts on as they arrive.
// At 115200 8N1 that is 86.2 µs per byte and about 610 µs from the first line
// to the eighth, during which the port shows values that were never intended.
// Against a system sampling at 1 kHz that is roughly 61% of a sample period, so
// a code change is sampled mid-transition about three times in five. See
// [Device.WriteMask].
//
// # There is no device clock
//
// The module never timestamps anything, so every timestamp available to you is
// the host's and includes the USB round trip.
//
// # There is no pulse timer
//
// A pulse is two writes from the host, so its width absorbs host scheduling in
// full. On an idle host a single-channel pulse lands within ~20 µs of the
// request; under CPU load the median error reaches +1.85 ms with 4.75 ms of
// spread, and real-time scheduling brings it back to ~0.1 ms. See [Device.Pulse].
//
// And one property of the driver rather than the device, which dominates every
// read: the FTDI latency timer, 16 ms by default. See [Device.SetLatencyTimer].
package dlp

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"go.bug.st/serial"
)

// Command bytes, indexed 0-7 for channels 1-8.
const (
	highCmds      = "12345678"
	lowCmds       = "QWERTYUI"
	digitalInCmds = "ASDFGHJK"
	analogInCmds  = "ZXCVBNM,"

	pingCmd   = "'"
	asciiCmd  = "`"
	binaryCmd = "\\"
)

// DefaultBaudRate is the only rate the module accepts.
const DefaultBaudRate = 115200

// byIDGlob is where a Linux system exposes serial devices under a stable name.
const byIDGlob = "/dev/serial/by-id/*DLP*"

// Device is an open connection to a DLP-IO8-G.
//
// Not safe for concurrent use: every operation writes, and some write then
// read, so two goroutines sharing one Device will interleave into each other's
// replies.
type Device struct {
	port   serial.Port
	path   string
	binary bool
}

// Option configures a Device at construction.
type Option func(*config)

type config struct {
	baudRate int
	timeout  time.Duration
	binary   bool
	ping     bool
}

// WithBaudRate overrides the baud rate. The module only accepts 115200; this
// exists for testing against something that is not one.
func WithBaudRate(b int) Option { return func(c *config) { c.baudRate = b } }

// WithTimeout sets the read timeout (default 1 s).
func WithTimeout(d time.Duration) Option { return func(c *config) { c.timeout = d } }

// WithASCII selects ASCII replies (three bytes per channel: the digit, then LF
// and CR in that order) instead of the binary default of one byte per channel.
func WithASCII() Option { return func(c *config) { c.binary = false } }

// WithoutPing skips the identity check at open.
func WithoutPing() Option { return func(c *config) { c.ping = false } }

// FindPort returns the path of the single attached DLP-IO8, resolved through
// its USB id.
//
// Prefer this to a hardcoded /dev/ttyUSB0. Any FTDI-based instrument competes
// for those names and which one wins depends on the order things were plugged
// in: on the bench this module was written against, the DLP and a Black Box
// ToolKit swapped between ttyUSB0 and ttyUSB1 between sessions. Sending DLP
// command bytes to the wrong instrument is the mild failure; reading the wrong
// one and believing the numbers is worse.
func FindPort() (string, error) {
	matches, err := filepath.Glob(byIDGlob)
	if err != nil {
		return "", err
	}
	switch len(matches) {
	case 0:
		return "", fmt.Errorf("dlp: no device matching %s; is it plugged in? "+
			"(check with: ls -l /dev/serial/by-id/) — on non-Linux systems this "+
			"path does not exist, so pass the port explicitly", byIDGlob)
	case 1:
		return matches[0], nil
	default:
		return "", fmt.Errorf("dlp: %d devices match %s:\n  %s\npass the port "+
			"explicitly to say which one you mean",
			len(matches), byIDGlob, strings.Join(matches, "\n  "))
	}
}

// New opens the device. An empty path calls [FindPort].
func New(path string, opts ...Option) (*Device, error) {
	cfg := config{baudRate: DefaultBaudRate, timeout: time.Second, binary: true, ping: true}
	for _, o := range opts {
		o(&cfg)
	}

	if path == "" {
		var err error
		if path, err = FindPort(); err != nil {
			return nil, err
		}
	}

	port, err := serial.Open(path, &serial.Mode{
		BaudRate: cfg.baudRate,
		Parity:   serial.NoParity,
		DataBits: 8,
		StopBits: serial.OneStopBit,
	})
	if err != nil {
		return nil, fmt.Errorf("dlp: opening %s at %d bps: %w", path, cfg.baudRate, err)
	}
	// Without this a read blocks indefinitely when a reply never comes, which
	// turns a missing device into a hung experiment rather than an error.
	if err := port.SetReadTimeout(cfg.timeout); err != nil {
		port.Close()
		return nil, fmt.Errorf("dlp: setting read timeout on %s: %w", path, err)
	}

	d := &Device{port: port, path: path}
	if cfg.ping {
		if err := d.Ping(); err != nil {
			port.Close()
			return nil, err
		}
	}
	if err := d.SetBinary(cfg.binary); err != nil {
		port.Close()
		return nil, err
	}
	return d, nil
}

// Path returns the device path this connection was opened on.
func (d *Device) Path() string { return d.path }

// Close closes the port.
func (d *Device) Close() error {
	if d.port == nil {
		return nil
	}
	err := d.port.Close()
	d.port = nil
	return err
}

// Ping checks that the device answers, returning nil if it does.
func (d *Device) Ping() error {
	if err := d.port.ResetInputBuffer(); err != nil {
		return fmt.Errorf("dlp: clearing the input buffer: %w", err)
	}
	if _, err := d.port.Write([]byte(pingCmd)); err != nil {
		return fmt.Errorf("dlp: sending ping: %w", err)
	}
	buf, err := d.readExact(1)
	if err != nil {
		return fmt.Errorf("dlp: ping got no reply from %s: %w", d.path, err)
	}
	if buf[0] != 'Q' {
		return fmt.Errorf("dlp: ping to %s returned %q, expected \"Q\"; either "+
			"this is not a DLP-IO8 or a previous read left unconsumed bytes in "+
			"the stream", d.path, buf[0])
	}
	return nil
}

// SetBinary chooses the reply format for reads: one raw byte per channel when
// true, three ASCII bytes when false. Binary is the default because a fixed one
// byte per command makes a read length predictable.
func (d *Device) SetBinary(binary bool) error {
	cmd := binaryCmd
	if !binary {
		cmd = asciiCmd
	}
	if _, err := d.port.Write([]byte(cmd)); err != nil {
		return fmt.Errorf("dlp: selecting reply mode: %w", err)
	}
	time.Sleep(50 * time.Millisecond)
	if err := d.port.ResetInputBuffer(); err != nil {
		return fmt.Errorf("dlp: clearing the input buffer: %w", err)
	}
	d.binary = binary
	return nil
}

// readExact reads exactly n bytes, or returns an error.
//
// A single Read is not enough. The module answers a multi-channel read with one
// byte per channel and nothing guarantees the USB stack delivers them in one
// packet, so returning whatever the first Read produced hands the caller a
// short slice with no indication that anything is missing — which is worse than
// an error, because the values that did arrive look valid.
func (d *Device) readExact(n int) ([]byte, error) {
	buf := make([]byte, n)
	got := 0
	for got < n {
		m, err := d.port.Read(buf[got:])
		got += m
		if err != nil {
			return buf[:got], fmt.Errorf("dlp: read: %w", err)
		}
		if m == 0 {
			// go.bug.st/serial reports a read timeout as (0, nil).
			break
		}
	}
	if got != n {
		return buf[:got], fmt.Errorf("dlp: short read: wanted %d bytes, got %d. "+
			"At the FTDI default latency timer of 16 ms a reply can take that "+
			"long to arrive; raise the timeout with WithTimeout, or lower the "+
			"latency timer with SetLatencyTimer", n, got)
	}
	return buf, nil
}

func cmdBytes(channels []int, table string) ([]byte, error) {
	out := make([]byte, 0, len(channels))
	for _, ch := range channels {
		if ch < 1 || ch > 8 {
			return nil, fmt.Errorf("dlp: channel %d out of range (1-8)", ch)
		}
		out = append(out, table[ch-1])
	}
	return out, nil
}

// High drives the given channels (1-8) to 5 V.
func (d *Device) High(channels ...int) error { return d.write(channels, highCmds) }

// Low drives the given channels (1-8) to 0 V.
func (d *Device) Low(channels ...int) error { return d.write(channels, lowCmds) }

func (d *Device) write(channels []int, table string) error {
	cmds, err := cmdBytes(channels, table)
	if err != nil {
		return err
	}
	// Deliberately no ResetOutputBuffer here. It discards queued output rather
	// than waiting for it, so at best it does nothing — a blocking write does
	// not return until the driver has the data, which was verified up to 1000
	// bytes — and at worst, on a port opened non-blocking, it would drop a
	// command that had already been issued.
	if _, err := d.port.Write(cmds); err != nil {
		return fmt.Errorf("dlp: writing to %s: %w", d.path, err)
	}
	return nil
}

// WriteMask sets all 8 channels from a bitmask, bit 0 being channel 1.
//
// This is NOT atomic and cannot be: it emits one byte per channel, all eight of
// them, which the module acts on as they arrive. The port takes about 610 µs to
// settle and shows partly-updated values throughout, so a system sampling at
// 1 kHz can and will record a code that was never sent.
//
// There is no workaround at the device level. If a trigger code has to be
// unambiguous, either have the receiving system latch on a separate strobe
// raised once the code has settled, or use hardware that writes a whole port in
// one instruction. Where the paradigm allows it, one channel per event type —
// pulsed, so each onset is a single command byte — has no skew at all and still
// distinguishes eight events.
func (d *Device) WriteMask(mask byte) error {
	cmds := make([]byte, 0, 8)
	for ch := 1; ch <= 8; ch++ {
		if mask&(1<<uint(ch-1)) != 0 {
			cmds = append(cmds, highCmds[ch-1])
		} else {
			cmds = append(cmds, lowCmds[ch-1])
		}
	}
	if _, err := d.port.Write(cmds); err != nil {
		return fmt.Errorf("dlp: writing to %s: %w", d.path, err)
	}
	return nil
}

// Pulse drives a channel high for d, then low.
//
// The width is timed by the host, because the module has no pulse timer: it is
// the interval between two writes and inherits whatever the operating system
// does to this process in between. A busy-wait is used rather than time.Sleep
// for that reason, but it only removes the sleep granularity, not preemption —
// under CPU load the realised width has no useful upper bound. Measured, n=50
// per width: within ~20 µs of the request on an idle host, median error up to
// +1.85 ms with 4.75 ms of spread under load, and back to ~0.1 ms of spread
// when the process runs at real-time priority.
func (d *Device) Pulse(channel int, width time.Duration) error {
	if err := d.High(channel); err != nil {
		return err
	}
	deadline := time.Now().Add(width)
	for time.Now().Before(deadline) {
	}
	return d.Low(channel)
}

// Read returns the state (0 or 1) of the given channels, in the order asked.
//
// Reading switches a channel to input mode, per the datasheet: "the mode of
// each I/O is automatically changed with each command sent". So reading a
// channel you were driving stops it driving.
func (d *Device) Read(channels ...int) ([]byte, error) {
	cmds, err := cmdBytes(channels, digitalInCmds)
	if err != nil {
		return nil, err
	}
	if err := d.port.ResetInputBuffer(); err != nil {
		return nil, fmt.Errorf("dlp: clearing the input buffer: %w", err)
	}
	if _, err := d.port.Write(cmds); err != nil {
		return nil, fmt.Errorf("dlp: writing to %s: %w", d.path, err)
	}

	width := 1
	if !d.binary {
		width = 3
	}
	raw, err := d.readExact(width * len(cmds))
	if err != nil {
		return nil, err
	}

	states := make([]byte, len(cmds))
	for i := range cmds {
		if d.binary {
			states[i] = raw[i] & 1
		} else {
			states[i] = raw[i*3] - '0'
		}
	}
	return states, nil
}

// ReadAll returns the state of all 8 channels, channel 1 first.
func (d *Device) ReadAll() ([]byte, error) {
	return d.Read(1, 2, 3, 4, 5, 6, 7, 8)
}

// sysfsDir is the Linux sysfs directory for this port, or "" if there is none.
func (d *Device) sysfsDir() string {
	resolved, err := filepath.EvalSymlinks(d.path)
	if err != nil {
		return ""
	}
	dir := "/sys/bus/usb-serial/devices/" + filepath.Base(resolved)
	if fi, err := os.Stat(dir); err != nil || !fi.IsDir() {
		return ""
	}
	return dir
}

// LatencyTimer returns the FTDI latency timer for this port, in milliseconds.
//
// This is the single most important number for anything that reads from the
// device. The FTDI chip batches data going back to the host and holds a
// partly-filled buffer for this long before sending it, so a poll the module
// answers instantly still takes up to this long to reach your program.
//
// Measured on this hardware, round trip for an 8-channel read against the
// setting: 16 ms gives 15.98 ms and a 63 Hz poll rate, 4 ms gives 3.99 ms and
// 251 Hz, 1 ms gives 1.01 ms and 995 Hz. The relationship is exactly
// "round trip = latency timer", so the module's own processing is negligible
// and the whole cost is driver batching. Reading one channel costs the same as
// reading eight: the timer, not the data, is what you wait for.
//
// A polling loop gets the worst case rather than the average, because waiting
// for each reply synchronises the loop to the timer.
//
// It has no effect on the write path, which USB frame scheduling governs.
// Linux only; returns an error elsewhere.
func (d *Device) LatencyTimer() (int, error) {
	dir := d.sysfsDir()
	if dir == "" {
		return 0, fmt.Errorf("dlp: no usb-serial sysfs entry for %s "+
			"(the latency timer is a Linux ftdi_sio setting)", d.path)
	}
	b, err := os.ReadFile(filepath.Join(dir, "latency_timer"))
	if err != nil {
		return 0, fmt.Errorf("dlp: reading the latency timer: %w", err)
	}
	return strconv.Atoi(strings.TrimSpace(string(b)))
}

// SetLatencyTimer sets the FTDI latency timer, in milliseconds (1-255).
//
// Needs root, and reverts when the device is replugged unless a udev rule makes
// it stick:
//
//	SUBSYSTEM=="usb-serial", DRIVERS=="ftdi_sio", ATTR{latency_timer}="1"
//
// See [Device.LatencyTimer] for what it costs to leave at the default.
func (d *Device) SetLatencyTimer(ms int) error {
	if ms < 1 || ms > 255 {
		return fmt.Errorf("dlp: latency timer %d out of range (1-255 ms)", ms)
	}
	dir := d.sysfsDir()
	if dir == "" {
		return fmt.Errorf("dlp: no usb-serial sysfs entry for %s "+
			"(the latency timer is a Linux ftdi_sio setting)", d.path)
	}
	path := filepath.Join(dir, "latency_timer")
	if err := os.WriteFile(path, []byte(strconv.Itoa(ms)), 0o644); err != nil {
		return fmt.Errorf("dlp: writing %s: %w\nthis needs root: "+
			"echo %d | sudo tee %s", path, err, ms, path)
	}
	return nil
}
