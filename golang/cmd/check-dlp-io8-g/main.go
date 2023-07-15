// Tests sending ON/OFF TTL signals every second on the 8 digital lines DLP-IO8-G, or reading the lines.
// Time-stamp: <2023-08-11 christophe@pallier.org>
package main

import (
	"flag"
	"fmt"
	"go.bug.st/serial"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var start int64 // will store unix.nanotime at start

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

func openDLPIO8(device string, baudrate int) serial.Port {
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

func writeDLPIO8(port serial.Port, cmd string) {
	_, err := port.Write([]byte(cmd))
	if err != nil {
		log.Fatal(err)
	}
}

func elapsedTime() int64 {
	return (time.Now().UnixNano() - start) / 1000000
}

// Sending ON/OFF TTL signals every second on the 8 digital lines.
func sendTest(port serial.Port) {
	for {
		writeDLPIO8(port, "QWERTYUI")
		fmt.Printf("OFF %dms\n", elapsedTime())

		time.Sleep(1 * time.Second)

		writeDLPIO8(port, "12345678")
		fmt.Printf("ON %dms\n", elapsedTime())

		time.Sleep(1 * time.Second)
	}
}

func receiveTest(port serial.Port) {
	cmds := "ASDFGHJK"
	cmds = "AS"

	buff := make([]byte, 1024)

	start = time.Now().UnixNano()
	for {

		for i, chr := range cmds {

			fmt.Println("Writing " + string(chr))
			writeDLPIO8(port, string(chr))
			
			n, err := port.Read(buff)
			if err != nil {
				log.Fatal(err)
				break
			}
			
			fmt.Printf("time %v: line %d=%v  (%d bytes returned)\n", elapsedTime(), i + 1, string(buff[:n]), n)
			
		}
	}
}

func main() {

	var readMode bool
	var listPorts bool
	var baudRate int
	var portName string

	flag.BoolVar(&listPorts, "list_ports", false, "List serial port devices")
	flag.IntVar(&baudRate, "baud_rate", 115200, "Baud rate (transmission speed in bits/s)")
	flag.StringVar(&portName, "port_name", "/dev/ttyUSB0", "Serial Port Name (e.g /dev/ttyUSB0)")
	flag.BoolVar(&readMode, "read_mode", true, "Mode: True=Read, False=Write")

	flag.Parse()

	if listPorts {
		listSerialPorts()
		return
	}

	port := openDLPIO8(portName, baudRate)
	defer port.Close()

	// Gracefully handles receiving Ctrl-C
	c := make(chan os.Signal)
	signal.Notify(c, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-c
		if err := port.Close(); err != nil {
			log.Fatal(err)
		}

		fmt.Println("\nSIGTERM received: serial port closed")
		os.Exit(1)
	}()

	start = time.Now().UnixNano()

	if readMode {
		fmt.Println("Receving on " + portName)
		receiveTest(port)
	} else {
		fmt.Println("Sending on " + portName)
		sendTest(port)
	}

}
