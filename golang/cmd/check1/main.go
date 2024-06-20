package main

import (
	"dlp"
	"fmt"
	"time"
)

var (
	address = "/dev/ttyUSB0"
	speed   = 115200
)

func main() {
	d, err := dlp.NewDLPio8g(address, speed)
	if err != nil {
		panic(err)
	}
	defer d.Close()

	for {
		fmt.Println(d.Read())
		time.Sleep(1000 * time.Millisecond)
	}
}
