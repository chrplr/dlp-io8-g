// Tests sending ON/OFF TTL signals every second on the 8 digital lines DLP-IO8-G, or reading the lines.
// Time-stamp: <2023-08-11 christophe@pallier.org>
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.bug.st/serial"
)

var start int64 // will store unix.nanotime at start

func elapsedTime() int64 {
	return (time.Now().UnixNano() - start) / 1000000
}

func listSerialPorts() {
	ports, err := serial.GetPortsList()
	if err != nil {
		log.Fatal(err)
	}
	if len(ports) == 0 {
		log.Fatal("No serial ports found!")
	}
	for _, port := range ports {
		fmt.Printf("Found port: %v\n", port)
	}
}

func openDLP_IO8_G(device string, baudrate int) serial.Port {
	mode := &serial.Mode{
		BaudRate: baudrate,
		Parity:   serial.EvenParity,
		DataBits: 7,
		StopBits: serial.OneStopBit,
	}
	port, err := serial.Open(device, mode)
	if err != nil {
		log.Fatal(err)
	}
	return port
}

func pingDLP_IO8_G(port serial.Port) bool {
	_, err := port.Write([]byte{0x27})
	if err != nil {
		log.Fatal(err)
	}

	var n int
	buff := make([]byte, 8)
	n, err = port.Read(buff)
	if err != nil {
		log.Fatal(err)
	}
	if n != 0 {
		return buff[0] == 'Q' // 'Q' should be returned
	} else {
		return false
	}
}

func setReturnASCIIMode(port serial.Port) {
	_, err := port.Write([]byte{0x60})
	if err != nil {
		log.Fatal(err)
	}
}

func setReturnBinaryMode(port serial.Port) {
	_, err := port.Write([]byte{0x5C})
	if err != nil {
		log.Fatal(err)
	}
}

func main() {

	var listPorts bool
	var baudRate int
	var portName string

	flag.BoolVar(&listPorts, "list_ports", false, "List serial port devices")
	flag.IntVar(&baudRate, "baud_rate", 115200, "Baud rate (transmission speed in bits/s)")
	flag.StringVar(&portName, "port_name", "/dev/ttyUSB0", "Serial Port Name (e.g /dev/ttyUSB0)")

	flag.Parse()

	if listPorts {
		listSerialPorts()
		return
	}

	port := openDLP_IO8_G(portName, baudRate)
	defer port.Close()

	pingDLP_IO8_G(port)

	// Gracefully handles receiving Ctrl-C
	c := make(chan os.Signal)
	signal.Notify(c, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-c
		if err := port.Close(); err != nil {
			log.Fatal(err)
		}

		fmt.Println("\nSIGTERM received: ", portName, "closed")
		os.Exit(1)
	}()

	setASCIIMode := []byte{0x60}
	//setBinaryMode := []byte{0x5C}
	_, err := port.Write(setASCIIMode)
	if err != nil {
		log.Fatal(err)
	}

	buff := make([]byte, 8)
	var previous_state byte = 0
	var current_state byte = 0
	readLine1 := []byte{0x41}
	var n int = 0

	start = time.Now().UnixNano()

	for {
		_, err := port.Write(readLine1)
		if err != nil {
			log.Fatal(err)
		}

		n, err = port.Read(buff)
		if err != nil {
			log.Fatal(err)
		}
		if n != 0 {
			fmt.Println(elapsedTime(), ": ", buff[:n])
			current_state = buff[0]
			if current_state != previous_state {
				fmt.Printf("time %v: val=%v  (%d bytes returned)\n", elapsedTime(), buff[:n], n)
			}
		} else {
			fmt.Println("0 bytes returned")
		}
	}

}
