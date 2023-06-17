// Test sending ON/OFF TTL signals every second on the 8 digital lines DLP-IO8-G 
// Time-stamp: <2023-06-17 christophe@pallier.org>
package main

import (
	"fmt"
	"log"
	"go.bug.st/serial"
	"time"
	"flag"
    "os"
    "os/signal"
    "syscall"
)

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

func main() {

	var nloops int
	var listPorts bool
	var baudRate int
	var portName string

	flag.BoolVar(&listPorts, "list_ports", false, "List serial port devices")
	flag.IntVar(&baudRate, "baud_rate", 115200, "Baud rate (transmission speed in bits/s)")
	flag.StringVar(&portName, "port_name", "/dev/ttyUSB0", "Serial Port Name (e.g /dev/ttyUSB0)")
	flag.IntVar(&nloops, "nloops", 30, "number of iterations")

	flag.Parse()

	if listPorts {
		listSerialPorts()
		return 
	}

	port := openDLPIO8(portName, baudRate)

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


	for i := 0; i < nloops; i++ {
		writeDLPIO8(port, "QWERTYUI")
		fmt.Println("OFF")
		
		time.Sleep(1 * time.Second)

		writeDLPIO8(port, "12345678")
		fmt.Println("ON")

		time.Sleep(1 * time.Second)
	}

	port.Close()
}
