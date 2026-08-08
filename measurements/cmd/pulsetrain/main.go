// Command pulsetrain emits pulses of random width at random intervals on one
// DLP-IO8-G channel, and records what it asked for and what the host clock saw.
//
// It measures nothing electrical itself. An Analog Discovery 3, captured
// free-running by ad3-capture.py in another terminal, observes the line; the
// two are paired afterwards by analyse-pulsetrain.py, which regresses the
// realised width on the requested one.
//
//	# terminal 1, started first and outliving the emitter
//	./ad3-capture.py --seconds 150 --out capture.npz
//
//	# terminal 2
//	chrt -f 50 ./pulsetrain -trials 1000 -condition rt -out train.csv
//
//	./analyse-pulsetrain.py train.csv capture.npz
//
// # Why the emitter is a separate process
//
// The Python pulse-stream block fires each trial from inside the loop draining
// the instrument, so the busy-wait that times the pulse also stalls the drain.
// The device buffers 16384 samples, which is 16 ms at 1 MS/s, so a 50 ms pulse
// would overrun the buffer and lose samples exactly where the falling edge is —
// which is why that block refuses fast sample rates. Emitting from a separate
// process removes the coupling: the capture drains continuously at 1 MS/s and
// the pulse widths can be anything.
//
// # Why the widths and the intervals are both random
//
// A sweep that holds the interval fixed and steps the width confounds width
// with time: any drift over the run, thermal or scheduling, appears as a slope
// against width. Sampling both uniformly decorrelates them, so the regression
// estimates a slope attributable to width rather than to when the trial
// happened. It also makes the sequence of widths a signature, so a misalignment
// between emitter and capture destroys the fit rather than biasing it quietly.
//
// # Two widths are recorded, and they answer different questions
//
// target_width_ms is what was asked for. host_width_ms is the interval the host
// clock saw between its own two writes. The instrument sees a third number. The
// host figure separates "this process was descheduled between the two writes"
// from "the device did something else", which a single measured width cannot.
package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"log"
	"math/rand/v2"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"github.com/chrplr/dlpio8"
	"golang.org/x/sys/unix"
)

func main() {
	log.SetFlags(0)

	port := flag.String("port", "", "serial port (default: find the device by USB id)")
	out := flag.String("out", "pulsetrain.csv", "CSV to write")
	channel := flag.Int("channel", 1, "channel to pulse")
	trials := flag.Int("trials", 1000, "number of pulses")
	widthRange := flag.String("width-range", "5,50", "pulse width range in ms, uniform")
	isiRange := flag.String("isi-range", "10,100", "inter-pulse interval range in ms, uniform (gap between pulses, not onset to onset)")
	seed := flag.Uint64("seed", 0, "PRNG seed; 0 picks one and prints it")
	condition := flag.String("condition", "idle", "label for this run: idle, load or rt")
	settle := flag.Duration("settle", 2*time.Second, "wait before the first pulse, so the capture is already running")
	flag.Parse()

	if err := run(*port, *out, *channel, *trials, *widthRange, *isiRange,
		*seed, *condition, *settle); err != nil {
		log.Fatal(err)
	}
}

func run(port, out string, channel, trials int, widthRange, isiRange string,
	seed uint64, condition string, settle time.Duration) error {

	// The thread chrt raised must be the thread doing the timing. Without this
	// the runtime may migrate the goroutine to a thread that was never raised.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	wLo, wHi, err := parseRange(widthRange, "-width-range")
	if err != nil {
		return err
	}
	iLo, iHi, err := parseRange(isiRange, "-isi-range")
	if err != nil {
		return err
	}
	if seed == 0 {
		seed = rand.Uint64()
	}
	rng := rand.New(rand.NewPCG(seed, 0x9E3779B97F4A7C15))

	policy, prio := scheduling()
	if condition == "rt" && policy != "SCHED_FIFO" && policy != "SCHED_RR" {
		return fmt.Errorf("-condition rt but this process is %s. Run it under "+
			"chrt, or a CSV labelled rt would be read as evidence that real-time "+
			"scheduling does not help", policy)
	}

	d, err := dlpio8.New(port)
	if err != nil {
		return err
	}
	defer d.Close()
	defer d.Low(channel)

	fmt.Printf("  device         %s\n", d.Path())
	if lt, err := d.LatencyTimer(); err == nil {
		// Recorded because it is the one setting that moves a DLP result by an
		// order of magnitude. It gates reads, not writes, so it should not
		// affect a pulse — saying so is only defensible if it was written down.
		fmt.Printf("  latency timer  %d ms (gates reads; this run only writes)\n", lt)
	}
	fmt.Printf("  scheduling     %s priority %d\n", policy, prio)
	fmt.Printf("  seed           %d\n", seed)
	fmt.Printf("  widths         uniform [%g, %g] ms\n", wLo, wHi)
	fmt.Printf("  intervals      uniform [%g, %g] ms\n", iLo, iHi)

	plan := make([]trial, trials)
	var total float64
	for i := range plan {
		plan[i] = trial{
			width: wLo + rng.Float64()*(wHi-wLo),
			isi:   iLo + rng.Float64()*(iHi-iLo),
		}
		total += plan[i].width + plan[i].isi
	}
	fmt.Printf("  trials         %d, about %.1f s of emission\n\n", trials, total/1000)
	fmt.Printf("  start the capture for at least %.0f s, then this begins in %v\n",
		total/1000+float64(settle)/float64(time.Second)+5, settle)

	if err := d.Low(channel); err != nil {
		return err
	}
	time.Sleep(settle)

	f, err := os.Create(out)
	if err != nil {
		return err
	}
	defer f.Close()
	w := csv.NewWriter(f)
	defer w.Flush()
	if err := w.Write([]string{
		"condition", "policy", "priority", "seed", "trial",
		"target_width_ms", "target_isi_ms", "host_width_ms", "host_onset_s",
	}); err != nil {
		return err
	}

	start := time.Now()
	for i := range plan {
		width := time.Duration(plan[i].width * float64(time.Millisecond))

		onset := time.Now()
		if err := d.High(channel); err != nil {
			return err
		}
		// Busy-wait rather than sleep: the module has no pulse timer, so the
		// width is exactly the interval between these two writes, and sleeping
		// would add the scheduler's granularity to every trial.
		//
		// The deadline is anchored before the High write, not after it, and
		// that is deliberate. Both edges are delayed from their write by the
		// same host-to-wire latency, so anchoring here makes the electrical
		// width equal the target; anchoring after the write would make it the
		// target plus however long that write took to return. The consequence
		// is that host_width_ms below reads slightly high, by the Low write's
		// own return, while the width the instrument sees does not. Measured
		// against an AD3, n=1000, idle: host_width sits +10.8 µs on target as a
		// regression intercept (median +6.3 µs, p95 +21.9 µs), and the measured
		// width shows no such offset.
		deadline := onset.Add(width)
		for time.Now().Before(deadline) {
		}
		if err := d.Low(channel); err != nil {
			return err
		}
		hostWidth := float64(time.Since(onset)) / float64(time.Millisecond)

		if err := w.Write([]string{
			condition, policy, strconv.Itoa(prio), strconv.FormatUint(seed, 10),
			strconv.Itoa(i),
			strconv.FormatFloat(plan[i].width, 'f', 4, 64),
			strconv.FormatFloat(plan[i].isi, 'f', 4, 64),
			strconv.FormatFloat(hostWidth, 'f', 4, 64),
			strconv.FormatFloat(onset.Sub(start).Seconds(), 'f', 6, 64),
		}); err != nil {
			return err
		}

		// Sleep the gap; spin only the last spinTail of it. The width is the
		// measured quantity and is spun in full, but spinning the gap too would
		// hold the CPU for the whole run — and at SCHED_FIFO that trips the
		// kernel's real-time throttle. sched_rt_runtime_us defaults to 950000
		// of a 1000000 period, so a task at 100% duty can be stopped for 50 ms
		// about once a second, landing in whatever pulse is in progress.
		//
		// Measured, before this was fixed: 23 of 1000 trials hit, biggest width
		// error 49.63 ms against a 50 ms throttle window, the hits falling on
		// one-second boundaries. Real-time priority came out worse than normal
		// priority, which is the exact opposite of the reason for using it.
		//
		// The throttle needs the host to be busy as well. A bare pinned
		// SCHED_FIFO 50 spinner took 0 stalls in 20 s on an idle machine and 24
		// in 25 s under load; an idle runqueue borrows unused real-time
		// bandwidth from other CPUs and never reaches the limit. Both runs
		// above were loaded.
		end := time.Now().Add(time.Duration(plan[i].isi * float64(time.Millisecond)))
		if rest := time.Until(end) - spinTail; rest > 0 {
			time.Sleep(rest)
		}
		for time.Now().Before(end) {
		}
		if (i+1)%100 == 0 {
			fmt.Printf("\r  %d/%d", i+1, trials)
		}
	}
	w.Flush()
	if err := w.Error(); err != nil {
		return err
	}

	fmt.Printf("\r  %d pulses in %.1f s\n", trials, time.Since(start).Seconds())
	fmt.Printf("  wrote %s\n", filepath.Clean(out))
	fmt.Printf("\n  stop the capture, then:\n    ./analyse-pulsetrain.py %s <capture.npz>\n", out)
	return nil
}

// spinTail is how much of each inter-pulse gap is spun rather than slept, to
// absorb the sleep's own overshoot without running at a duty cycle the
// real-time throttle would notice.
const spinTail = 200 * time.Microsecond

type trial struct {
	width float64 // ms
	isi   float64 // ms
}

func parseRange(s, flagName string) (lo, hi float64, err error) {
	parts := strings.Split(s, ",")
	if len(parts) != 2 {
		return 0, 0, fmt.Errorf("%s must be \"lo,hi\" in ms, got %q", flagName, s)
	}
	if lo, err = strconv.ParseFloat(strings.TrimSpace(parts[0]), 64); err != nil {
		return 0, 0, fmt.Errorf("%s: %q is not a number", flagName, parts[0])
	}
	if hi, err = strconv.ParseFloat(strings.TrimSpace(parts[1]), 64); err != nil {
		return 0, 0, fmt.Errorf("%s: %q is not a number", flagName, parts[1])
	}
	if lo <= 0 || hi < lo {
		return 0, 0, fmt.Errorf("%s: need 0 < lo <= hi, got %g,%g", flagName, lo, hi)
	}
	return lo, hi, nil
}

// scheduling returns this process's scheduling policy and priority.
//
// Recorded per run because it changes the result by milliseconds and leaves no
// trace in the data otherwise. A CSV labelled "rt" that was actually collected
// at normal priority — because chrt silently failed, or the rtprio grant was
// not live — is worse than no data, since it would be read as evidence that
// real-time scheduling does not help.
func scheduling() (string, int) {
	p, _, errno := unix.Syscall(unix.SYS_SCHED_GETSCHEDULER, 0, 0, 0)
	if errno != 0 {
		return "unknown", 0
	}
	// SCHED_NORMAL is what x/sys/unix calls policy 0; the scheduler manual and
	// the Python harness both call it SCHED_OTHER. Use the name the existing
	// CSVs already carry, so the two harnesses' rows compare directly.
	names := map[int]string{
		unix.SCHED_NORMAL: "SCHED_OTHER",
		unix.SCHED_FIFO:   "SCHED_FIFO",
		unix.SCHED_RR:     "SCHED_RR",
		unix.SCHED_BATCH:  "SCHED_BATCH",
		unix.SCHED_IDLE:   "SCHED_IDLE",
	}
	name, ok := names[int(p)]
	if !ok {
		name = fmt.Sprintf("policy%d", p)
	}

	var param struct{ priority int32 }
	if _, _, errno := unix.Syscall(unix.SYS_SCHED_GETPARAM, 0,
		uintptr(unsafe.Pointer(&param)), 0); errno != 0 {
		return name, 0
	}
	return name, int(param.priority)
}
