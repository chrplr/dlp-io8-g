// check1 prints the state of all 8 DLP-IO8 input channels once a second.
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"dlp"
)

func main() {
	port := flag.String("port", "", "serial port (default: find the device by USB id)")
	interval := flag.Duration("interval", time.Second, "time between reads")
	latency := flag.Int("latency-timer", 0, "set the FTDI latency timer in ms before reading (needs root)")
	flag.Parse()

	d, err := dlp.New(*port)
	if err != nil {
		log.Fatal(err)
	}
	defer d.Close()
	fmt.Fprintf(os.Stderr, "connected to %s\n", d.Path())

	if *latency > 0 {
		if err := d.SetLatencyTimer(*latency); err != nil {
			log.Fatal(err)
		}
	}
	// Report it either way: it sets the floor on how fast this loop can run,
	// and at the ftdi_sio default of 16 ms that floor is 63 Hz.
	if lt, err := d.LatencyTimer(); err == nil {
		note := ""
		if lt == 16 {
			note = "  (the ftdi_sio default; every read waits up to this long)"
		}
		fmt.Fprintf(os.Stderr, "latency timer %d ms%s\n", lt, note)
	}

	for {
		states, err := d.ReadAll()
		if err != nil {
			log.Fatal(err)
		}
		fmt.Println(states)
		time.Sleep(*interval)
	}
}
