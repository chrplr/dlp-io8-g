// Command dlpio8 drives and reads a DLP-IO8-G USB TTL module from the shell.
//
// It replaces three earlier commands, each of which opened the serial port
// itself and did one fixed thing:
//
//	check1                        ->  dlpio8 read
//	check-dlp-io8-g -read_mode=false  ->  dlpio8 blink -period 5s
//	check-dlp-io8-g -read_mode        ->  dlpio8 read -channels 1,2 -interval 0
//	detect-events-on-serial-port      ->  dlpio8 read -channels 1 -changes
//
// Everything here goes through the dlp package, so the port is found by USB id
// rather than assumed to be /dev/ttyUSB0, the line settings are in one place,
// and a short reply is an error instead of a plausible-looking wrong answer.
package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"dlp"

	"go.bug.st/serial"
)

const usage = `dlpio8 drives and reads a DLP-IO8-G USB TTL module.

usage: dlpio8 <command> [flags]

commands:
  read    poll digital inputs and print their state
  write   set channels high or low, once
  pulse   drive channels high for a fixed width, then low
  blink   alternate channels between high and low
  ports   list serial ports and locate the DLP-IO8

Run "dlpio8 <command> -h" for the flags of one command.
`

func main() {
	log.SetFlags(0)
	log.SetPrefix("dlpio8: ")

	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}

	// Ctrl-C cancels the loops rather than killing the process, so the deferred
	// Close runs and the channels a command was driving are left in a known
	// state instead of wherever the last write happened to leave them.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var err error
	switch cmd := os.Args[1]; cmd {
	case "read":
		err = readCmd(ctx, os.Args[2:])
	case "write":
		err = writeCmd(os.Args[2:])
	case "pulse":
		err = pulseCmd(ctx, os.Args[2:])
	case "blink":
		err = blinkCmd(ctx, os.Args[2:])
	case "ports":
		err = portsCmd(os.Args[2:])
	case "-h", "--help", "help":
		fmt.Print(usage)
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n%s", cmd, usage)
		os.Exit(2)
	}

	// A cancelled context is how the loops report Ctrl-C, not a failure.
	if err != nil && !errors.Is(err, context.Canceled) {
		log.Fatal(err)
	}
}

// deviceFlags are the flags every command that opens the device accepts.
type deviceFlags struct {
	port    string
	timeout time.Duration
	latency int
	ascii   bool
}

func addDeviceFlags(fs *flag.FlagSet) *deviceFlags {
	f := &deviceFlags{}
	fs.StringVar(&f.port, "port", "", "serial port (default: find the device by USB id)")
	fs.DurationVar(&f.timeout, "timeout", time.Second, "read timeout")
	fs.IntVar(&f.latency, "latency-timer", 0, "set the FTDI latency timer in ms before starting (needs root)")
	fs.BoolVar(&f.ascii, "ascii", false, "ask the device for ASCII replies instead of binary")
	return f
}

// open connects to the device and reports what it found on stderr, leaving
// stdout for data.
func (f *deviceFlags) open() (*dlp.Device, error) {
	opts := []dlp.Option{dlp.WithTimeout(f.timeout)}
	if f.ascii {
		opts = append(opts, dlp.WithASCII())
	}
	d, err := dlp.New(f.port, opts...)
	if err != nil {
		return nil, err
	}
	fmt.Fprintf(os.Stderr, "connected to %s\n", d.Path())

	if f.latency > 0 {
		if err := d.SetLatencyTimer(f.latency); err != nil {
			d.Close()
			return nil, err
		}
	}
	// Report it either way: it sets the floor on how fast a polling loop can
	// run, and at the ftdi_sio default of 16 ms that floor is 63 Hz.
	if lt, err := d.LatencyTimer(); err == nil {
		note := ""
		if lt == 16 {
			note = "  (the ftdi_sio default; every read waits up to this long)"
		}
		fmt.Fprintf(os.Stderr, "latency timer %d ms%s\n", lt, note)
	}
	return d, nil
}

func readCmd(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("read", flag.ExitOnError)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), "usage: dlpio8 read [flags]\n\n"+
			"Polls digital inputs and writes one line per sample to stdout:\n"+
			"seconds since start, then one 0/1 per channel.\n\n"+
			"Reading a channel switches it to input mode, so it stops driving.\n\n")
		fs.PrintDefaults()
	}
	dev := addDeviceFlags(fs)
	chans := fs.String("channels", "all", `channels to read: "all", "1,3,5" or "1-4"`)
	interval := fs.Duration("interval", time.Second, "time between samples (0 polls as fast as the latency timer allows)")
	changes := fs.Bool("changes", false, "print only samples that differ from the previous one")
	count := fs.Int("count", 0, "stop after this many samples (0 = forever)")
	fs.Parse(args)

	channels, err := parseChannels(*chans)
	if err != nil {
		return err
	}

	d, err := dev.open()
	if err != nil {
		return err
	}
	defer d.Close()

	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()
	fmt.Fprintf(out, "# time_s")
	for _, ch := range channels {
		fmt.Fprintf(out, " ch%d", ch)
	}
	fmt.Fprintln(out)

	var previous []byte
	start := time.Now()
	for n := 0; *count == 0 || n < *count; n++ {
		states, err := d.Read(channels...)
		if err != nil {
			return err
		}
		if !*changes || !sameStates(previous, states) {
			fmt.Fprintf(out, "%9.3f", time.Since(start).Seconds())
			for _, s := range states {
				fmt.Fprintf(out, " %d", s)
			}
			fmt.Fprintln(out)
			// Flush per line while polling slowly, so a redirected run and a
			// watched one show the same thing. Flat out, per-line flushing
			// costs more than the buffering saves.
			if *interval > 0 {
				out.Flush()
			}
			previous = append(previous[:0], states...)
		}
		if *interval > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(*interval):
			}
		} else if err := ctx.Err(); err != nil {
			return err
		}
	}
	return nil
}

func sameStates(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func writeCmd(args []string) error {
	fs := flag.NewFlagSet("write", flag.ExitOnError)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), "usage: dlpio8 write [flags]\n\n"+
			"Sets channels once and exits. -mask writes all 8 channels; note that\n"+
			"it is not atomic (about 610 us from the first line to the eighth), so\n"+
			"a system sampling at 1 kHz can record a code that was never sent.\n\n")
		fs.PrintDefaults()
	}
	dev := addDeviceFlags(fs)
	high := fs.String("high", "", `channels to drive to 5 V, e.g. "1,3" or "1-4"`)
	low := fs.String("low", "", "channels to drive to 0 V")
	mask := fs.String("mask", "", "set all 8 channels from a bitmask, bit 0 = channel 1 (accepts 0x.., 0b.. or decimal)")
	fs.Parse(args)

	if *mask != "" && (*high != "" || *low != "") {
		return errors.New("-mask sets all 8 channels; do not combine it with -high or -low")
	}
	if *mask == "" && *high == "" && *low == "" {
		return errors.New("nothing to do: pass -high, -low or -mask")
	}

	var highChans, lowChans []int
	var maskValue uint64
	var err error
	if *high != "" {
		if highChans, err = parseChannels(*high); err != nil {
			return err
		}
	}
	if *low != "" {
		if lowChans, err = parseChannels(*low); err != nil {
			return err
		}
	}
	if *mask != "" {
		if maskValue, err = strconv.ParseUint(*mask, 0, 8); err != nil {
			return fmt.Errorf("bad -mask %q: %w", *mask, err)
		}
	}

	d, err := dev.open()
	if err != nil {
		return err
	}
	defer d.Close()

	if *mask != "" {
		return d.WriteMask(byte(maskValue))
	}
	if len(highChans) > 0 {
		if err := d.High(highChans...); err != nil {
			return err
		}
	}
	if len(lowChans) > 0 {
		return d.Low(lowChans...)
	}
	return nil
}

func pulseCmd(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("pulse", flag.ExitOnError)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), "usage: dlpio8 pulse [flags]\n\n"+
			"The width is timed by the host, because the module has no pulse timer:\n"+
			"measured within ~20 us of the request on an idle host, but with a median\n"+
			"error of +1.85 ms and 4.75 ms of spread under CPU load.\n\n")
		fs.PrintDefaults()
	}
	dev := addDeviceFlags(fs)
	chans := fs.String("channels", "1", `channels to pulse, e.g. "1" or "1,3"`)
	width := fs.Duration("width", 10*time.Millisecond, "how long to hold the channels high")
	count := fs.Int("count", 1, "number of pulses (0 = forever)")
	period := fs.Duration("period", time.Second, "onset-to-onset interval when -count is not 1")
	fs.Parse(args)

	channels, err := parseChannels(*chans)
	if err != nil {
		return err
	}
	if *width <= 0 {
		return errors.New("-width must be positive")
	}

	d, err := dev.open()
	if err != nil {
		return err
	}
	defer d.Close()
	// Leave the lines low on the way out, whether that is the last pulse or a
	// Ctrl-C in the middle of one.
	defer d.Low(channels...)

	start := time.Now()
	for n := 0; *count == 0 || n < *count; n++ {
		onset := time.Now()
		if err := pulse(d, channels, *width); err != nil {
			return err
		}
		fmt.Printf("%9.3f  pulse %d, %v\n", onset.Sub(start).Seconds(), n+1, *width)

		if *count == 1 {
			break
		}
		remaining := *period - time.Since(onset)
		if remaining <= 0 {
			continue
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(remaining):
		}
	}
	return nil
}

// pulse drives channels high for width, then low.
//
// One channel is one command byte and has no skew. Several are several bytes,
// 86.2 us apart at 115200, so a multi-channel pulse has skewed edges by
// construction; dlp.Pulse handles the single-channel case, which is the one
// worth using when timing matters.
func pulse(d *dlp.Device, channels []int, width time.Duration) error {
	if len(channels) == 1 {
		return d.Pulse(channels[0], width)
	}
	if err := d.High(channels...); err != nil {
		return err
	}
	deadline := time.Now().Add(width)
	for time.Now().Before(deadline) {
	}
	return d.Low(channels...)
}

func blinkCmd(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("blink", flag.ExitOnError)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), "usage: dlpio8 blink [flags]\n\n"+
			"Alternates channels between 5 V and 0 V, for checking wiring with a\n"+
			"scope or a logic probe.\n\n")
		fs.PrintDefaults()
	}
	dev := addDeviceFlags(fs)
	chans := fs.String("channels", "all", `channels to drive: "all", "1,3,5" or "1-4"`)
	period := fs.Duration("period", 5*time.Second, "time in each state")
	count := fs.Int("count", 0, "stop after this many full cycles (0 = forever)")
	fs.Parse(args)

	channels, err := parseChannels(*chans)
	if err != nil {
		return err
	}

	d, err := dev.open()
	if err != nil {
		return err
	}
	defer d.Close()
	defer d.Low(channels...)

	start := time.Now()
	for n := 0; *count == 0 || n < *count; n++ {
		for _, on := range [2]bool{true, false} {
			var err error
			label := "OFF"
			if on {
				err, label = d.High(channels...), "ON"
			} else {
				err = d.Low(channels...)
			}
			if err != nil {
				return err
			}
			fmt.Printf("%9.3f  %s\n", time.Since(start).Seconds(), label)

			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(*period):
			}
		}
	}
	return nil
}

func portsCmd(args []string) error {
	fs := flag.NewFlagSet("ports", flag.ExitOnError)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), "usage: dlpio8 ports\n\n"+
			"Lists the serial ports on this machine and the one the other commands\n"+
			"would pick by default.\n\n")
		fs.PrintDefaults()
	}
	fs.Parse(args)

	ports, err := serial.GetPortsList()
	if err != nil {
		return err
	}
	if len(ports) == 0 {
		fmt.Println("no serial ports found")
	}
	for _, p := range ports {
		fmt.Printf("port: %s\n", p)
	}

	// Which /dev/ttyUSB* a device gets depends on plug order, so report the
	// USB-id path the other commands resolve rather than the kernel name.
	found, err := dlp.FindPort()
	if err != nil {
		fmt.Printf("\nDLP-IO8: %v\n", err)
		return nil
	}
	fmt.Printf("\nDLP-IO8: %s\n", found)
	return nil
}

// parseChannels accepts "all", a comma-separated list, ranges, or a mix:
// "1,3,5", "1-4", "1-2,7". Duplicates are dropped and the order given is kept,
// because Device.Read returns states in the order asked.
func parseChannels(s string) ([]int, error) {
	if s == "" || s == "all" {
		return []int{1, 2, 3, 4, 5, 6, 7, 8}, nil
	}
	var out []int
	seen := make(map[int]bool, 8)
	add := func(ch int) error {
		if ch < 1 || ch > 8 {
			return fmt.Errorf("channel %d out of range (1-8)", ch)
		}
		if !seen[ch] {
			seen[ch] = true
			out = append(out, ch)
		}
		return nil
	}

	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		lo, hi, isRange := strings.Cut(part, "-")
		first, err := strconv.Atoi(strings.TrimSpace(lo))
		if err != nil {
			return nil, fmt.Errorf("bad channel %q: not a number", part)
		}
		last := first
		if isRange {
			if last, err = strconv.Atoi(strings.TrimSpace(hi)); err != nil {
				return nil, fmt.Errorf("bad channel range %q: not a number", part)
			}
		}
		step := 1
		if last < first {
			step = -1
		}
		for ch := first; ; ch += step {
			if err := add(ch); err != nil {
				return nil, err
			}
			if ch == last {
				break
			}
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no channels in %q", s)
	}
	return out, nil
}
