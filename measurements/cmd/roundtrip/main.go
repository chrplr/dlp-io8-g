// Command roundtrip measures the round trip from a host write to the host
// learning of the resulting edge, through the device.
//
// Wire a jumper from the output channel to the input channel (default ch1 to
// ch2; both are on the same board, so the ground is already common). The tool
// drives the output low, clears the stream, takes a timestamp, drives it high,
// and polls the input until it reads 1.
//
// # What this measures, and what it does not
//
// Both timestamps are the host's, so no clock has to be reconciled with any
// other. It bounds the write path plus the read path from above, together, and
// it cannot separate them: the module has no clock, so there is no second
// timestamp to difference against. Do not quote this figure as a write latency.
// See "Why no absolute latency is quoted here" in measurements/README.md.
//
// # Expect to measure the latency timer
//
// The FTDI chip holds a partly-filled buffer for the latency timer before
// sending it to the host, so a poll the module answers instantly still takes
// that long to come back. The Python harness measured round trip = latency
// timer exactly, n=300 per setting: 15.979 ms at 16 ms, 1.005 ms at 1 ms. That
// is why this sweeps the timer by default rather than quoting a single number,
// and why every row records the setting it was collected under.
//
// Sweeping writes to sysfs, which needs root:
//
//	sudo ./roundtrip -out 2026-08-08-dlp
//
// This is a Go port of the `loopback` block of dlp_timing.py and writes the
// same CSV columns, under a different file name so the two can be compared
// without either overwriting the other.
package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/chrplr/dlpio8"
)

func main() {
	log.SetFlags(0)

	port := flag.String("port", "", "serial port (default: find the device by USB id)")
	out := flag.String("out", ".", "directory to write CSVs into")
	prefix := flag.String("prefix", "roundtrip-go", "CSV file name prefix")
	trials := flag.Int("trials", 300, "trials per latency-timer setting")
	outCh := flag.Int("out-ch", 1, "channel to drive")
	inCh := flag.Int("in-ch", 2, "channel to read (jumpered to -out-ch)")
	sweep := flag.String("sweep", "1,2,4,8,16", `latency timer values in ms to sweep, or "" for whatever it is now`)
	timeout := flag.Duration("trial-timeout", time.Second, "give up on a trial after this long")
	flag.Parse()

	if err := run(*port, *out, *prefix, *trials, *outCh, *inCh, *sweep, *timeout); err != nil {
		log.Fatal(err)
	}
}

func run(port, out, prefix string, trials, outCh, inCh int, sweep string, timeout time.Duration) error {
	if outCh == inCh {
		return fmt.Errorf("-out-ch and -in-ch must differ (both are %d)", outCh)
	}

	settings, err := parseSweep(sweep)
	if err != nil {
		return err
	}

	d, err := dlpio8.New(port)
	if err != nil {
		return err
	}
	defer d.Close()
	fmt.Printf("  device         %s\n", d.Path())

	if err := requireLoopback(d, outCh, inCh); err != nil {
		return err
	}
	fmt.Printf("  loopback       ch%d -> ch%d confirmed\n", outCh, inCh)

	if len(settings) == 0 {
		lt, err := d.LatencyTimer()
		if err != nil {
			return fmt.Errorf("cannot read the latency timer, so a run would not "+
				"record the one condition that dominates the result: %w", err)
		}
		settings = []int{lt}
		fmt.Printf("  latency timer  %d ms (not sweeping)\n\n", lt)
	} else {
		// Probe writability before measuring anything. Otherwise a sweep run
		// without root spends a full set of trials on whichever setting the
		// machine already happens to be in, writes that CSV, and only then
		// fails — leaving one real file among the ones it never collected.
		if err := probeSettable(d); err != nil {
			return err
		}
		fmt.Printf("  sweeping       %v ms\n\n", settings)
	}

	for _, want := range settings {
		if err := setAndConfirm(d, want, len(settings) > 1); err != nil {
			return err
		}
		lt, err := d.LatencyTimer()
		if err != nil {
			return err
		}

		path := filepath.Join(out, fmt.Sprintf("%s-lt%d.csv", prefix, lt))
		rows, err := measure(d, outCh, inCh, trials, timeout, lt, path)
		if err != nil {
			return err
		}
		fmt.Printf("  latency timer %2d ms: %s\n", lt, describe(rows))
		fmt.Printf("    wrote %s\n", path)
	}
	return d.Low(outCh)
}

// measure runs the trials for one latency-timer setting and writes the CSV.
func measure(d *dlpio8.Device, outCh, inCh, trials int, timeout time.Duration,
	lt int, path string) ([]float64, error) {

	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	w := csv.NewWriter(f)
	defer w.Flush()
	if err := w.Write([]string{"latency_timer_ms", "trial", "roundtrip_ms", "polls"}); err != nil {
		return nil, err
	}

	ts := make([]float64, 0, trials)
	for i := 0; i < trials; i++ {
		if err := d.Low(outCh); err != nil {
			return nil, err
		}
		time.Sleep(10 * time.Millisecond)
		// Read once before timing, so the settling read and any bytes it left
		// in the stream are not charged to the trial.
		if _, err := d.Read(inCh); err != nil {
			return nil, err
		}

		t0 := time.Now()
		if err := d.High(outCh); err != nil {
			return nil, err
		}
		polls := 0
		for {
			polls++
			states, err := d.Read(inCh)
			if err != nil {
				return nil, err
			}
			if states[0] == 1 {
				break
			}
			if time.Since(t0) > timeout {
				// Recorded as -1 rather than dropped: a run where some trials
				// never saw the edge is a different thing from a slow run, and
				// summarising it away would hide that.
				polls = -1
				break
			}
		}
		dt := float64(time.Since(t0)) / float64(time.Millisecond)
		ts = append(ts, dt)

		if err := w.Write([]string{
			strconv.Itoa(lt), strconv.Itoa(i),
			strconv.FormatFloat(dt, 'f', 4, 64), strconv.Itoa(polls),
		}); err != nil {
			return nil, err
		}
	}
	w.Flush()
	return ts, w.Error()
}

// requireLoopback verifies the jumper before a run is spent on it.
func requireLoopback(d *dlpio8.Device, outCh, inCh int) error {
	read := func() (byte, error) {
		time.Sleep(50 * time.Millisecond)
		s, err := d.Read(inCh)
		if err != nil {
			return 0, err
		}
		return s[0], nil
	}
	if err := d.Low(outCh); err != nil {
		return err
	}
	lo, err := read()
	if err != nil {
		return err
	}
	if err := d.High(outCh); err != nil {
		return err
	}
	hi, err := read()
	if err != nil {
		return err
	}
	if err := d.Low(outCh); err != nil {
		return err
	}
	if lo != 0 || hi != 1 {
		return fmt.Errorf("no loopback detected: driving ch%d low then high read "+
			"%d then %d on ch%d, expected 0 then 1.\nWire ch%d to ch%d, and note "+
			"that reading a channel switches it to input mode, so ch%d must not "+
			"also be driven", outCh, lo, hi, inCh, outCh, inCh, inCh)
	}
	return nil
}

// setAndConfirm sets the latency timer and verifies it took. Sweeping without
// root would otherwise collect every setting at the current value and label the
// rows with settings that were never applied.
func setAndConfirm(d *dlpio8.Device, want int, sweeping bool) error {
	// Writing sysfs needs root, so do not write when there is nothing to
	// change: measuring at the setting the machine is already in should not
	// require sudo.
	if got, err := d.LatencyTimer(); err == nil && got == want {
		return nil
	}
	if err := d.SetLatencyTimer(want); err != nil {
		if sweeping {
			return fmt.Errorf("cannot set the latency timer to %d ms, so a sweep "+
				"would label every run with a setting it was not collected "+
				"under. Re-run under sudo, or pass -sweep \"\" to measure at the "+
				"current setting: %w", want, err)
		}
		return err
	}
	got, err := d.LatencyTimer()
	if err != nil {
		return err
	}
	if got != want {
		return fmt.Errorf("asked for a latency timer of %d ms, sysfs reports %d", want, got)
	}
	return nil
}

// probeSettable checks that the latency timer can be written, by writing back
// the value it already has. Harmless, and it proves root before a run starts.
func probeSettable(d *dlpio8.Device) error {
	lt, err := d.LatencyTimer()
	if err != nil {
		return fmt.Errorf("cannot read the latency timer, so a sweep cannot "+
			"verify its own settings: %w", err)
	}
	if err := d.SetLatencyTimer(lt); err != nil {
		return fmt.Errorf("a sweep has to write the latency timer, and this "+
			"process cannot. Re-run under sudo, or pass -sweep \"\" to measure "+
			"at the current %d ms setting: %w", lt, err)
	}
	return nil
}

func parseSweep(s string) ([]int, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil, nil
	}
	var out []int
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		n, err := strconv.Atoi(part)
		if err != nil {
			return nil, fmt.Errorf("bad -sweep value %q: not a number", part)
		}
		if n < 1 || n > 255 {
			return nil, fmt.Errorf("bad -sweep value %d: the latency timer is 1-255 ms", n)
		}
		out = append(out, n)
	}
	return out, nil
}

// quantile matches the Python harness's definition, so the two agree row for
// row on the same data rather than differing by an interpolation convention.
func quantile(sorted []float64, q float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	i := int(q*float64(len(sorted))+0.5) - 1
	return sorted[min(max(i, 0), len(sorted)-1)]
}

func describe(vals []float64) string {
	if len(vals) == 0 {
		return "no data"
	}
	s := append([]float64(nil), vals...)
	sort.Float64s(s)
	var sum float64
	for _, v := range s {
		sum += v
	}
	return fmt.Sprintf("n=%d  min %.3f  p50 %.3f  p95 %.3f  p99 %.3f  max %.3f  mean %.3f ms",
		len(s), s[0], quantile(s, .5), quantile(s, .95), quantile(s, .99),
		s[len(s)-1], sum/float64(len(s)))
}
