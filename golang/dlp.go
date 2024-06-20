package dlp

import (
	"fmt"
	"log"
	"strings"

	"go.bug.st/serial"
)

type DLPio8g struct {
	port serial.Port
}

func NewDLPio8g(device string, baudrate int) (*DLPio8g, error) {

	mode := &serial.Mode{
		BaudRate: baudrate,
		Parity:   serial.NoParity,
		DataBits: 8,
		StopBits: serial.OneStopBit,
	}

	port, err := serial.Open(device, mode)
	if err != nil {
		return nil, fmt.Errorf("Error while trying to open %s at %d bps: %w\n", device, baudrate, err)
	}

	// Ping to check the device
	buff := make([]byte, 8)
	port.Write([]byte("'"))
	n, err := port.Read(buff)
	if err != nil || n != 1 || buff[0] != 'Q' {
		return nil, err
	}

	// set BINARY mode for return values
	n, err = port.Write([]byte("\\"))
	if n == 0 || err != nil {
		return nil, fmt.Errorf("Problem writing on device %s: %w", device, err)
	}

	dlp := &DLPio8g{port}

	return dlp, nil
}

func (dlp DLPio8g) Close() {
	dlp.port.Close()
}

func (dlp DLPio8g) Ping() (bool, error) {
	buff := make([]byte, 8)
	dlp.port.Write([]byte("'"))
	n, err := dlp.port.Read(buff)
	if err != nil {
		return false, err
	}
	if n != 1 {
		return false, fmt.Errorf("No char returned")
	} else {
		return buff[0] == 'Q', nil
	}
}

// Read() returns the states (0/1) of all 8 lines
func (dlp DLPio8g) Read() []byte {
	cmds := []byte("ASDFGHJK")
	buff := make([]byte, 8)

	dlp.port.ResetOutputBuffer()
	dlp.port.ResetInputBuffer()

	_, err := dlp.port.Write(cmds)
	if err != nil {
		log.Fatal(err)
	}

	n, err := dlp.port.Read(buff)
	if err != nil {
		log.Fatal(err)
	}
	// log.Printf("Sent %c ; %d bytes read; msg = '%v'\n", cmds, n, buff[:n])

	return buff[:n]
}

// Set() sets to 1 the lines specified in the string `lines` (e.g. lines="1234" for the first 4 lines)
func (dlp DLPio8g) Set(lines string) {
	dlp.port.ResetOutputBuffer()
	dlp.port.Write([]byte(lines))
}

// Unset() sets to 0 the lines specified in the string `lines` (e.g. lines="1234" for the first 4 lines)
func (dlp DLPio8g) Unset(lines string) {
	cmd := strings.ReplaceAll(lines, "1", "Q")
	cmd = strings.ReplaceAll(cmd, "2", "W")
	cmd = strings.ReplaceAll(cmd, "3", "E")
	cmd = strings.ReplaceAll(cmd, "4", "R")
	cmd = strings.ReplaceAll(cmd, "5", "T")
	cmd = strings.ReplaceAll(cmd, "6", "Y")
	cmd = strings.ReplaceAll(cmd, "7", "U")
	cmd = strings.ReplaceAll(cmd, "8", "I")

	dlp.port.ResetOutputBuffer()
	_, err := dlp.port.Write([]byte(cmd))
	if err != nil {
		log.Fatal(err)
	}
}
