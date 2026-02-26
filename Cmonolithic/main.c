#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <time.h>

#define PORTNAME "/dev/ttyUSB0"
#define BAUDRATE B115200

#define NPERIODS 100
#define TIME_HIGH 0.010
#define TIME_LOW 0.090
#define PERIOD (TIME_HIGH + TIME_LOW)

// Helper function to act like Python's time.perf_counter()
double perf_counter() {
    struct timespec ts;
    // CLOCK_MONOTONIC is unaffected by system clock jumps, ideal for intervals
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

// Helper to configure the serial port
int setup_serial(const char* portname) {
    int fd = open(portname, O_RDWR | O_NOCTTY | O_SYNC);
    if (fd < 0) {
        perror("Error opening serial port");
        return -1;
    }

    struct termios tty;
    if (tcgetattr(fd, &tty) != 0) {
        perror("Error from tcgetattr");
        return -1;
    }

    cfsetospeed(&tty, BAUDRATE);
    cfsetispeed(&tty, BAUDRATE);

    // 8-bit characters, no parity, 1 stop bit
    tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8; 
    tty.c_iflag &= ~IGNBRK; 
    tty.c_lflag = 0; 
    tty.c_oflag = 0; 
    tty.c_cc[VMIN]  = 0; 
    tty.c_cc[VTIME] = 5; 

    tty.c_iflag &= ~(IXON | IXOFF | IXANY); 
    tty.c_cflag |= (CLOCAL | CREAD); 
    tty.c_cflag &= ~(PARENB | PARODD); 
    tty.c_cflag &= ~CSTOPB;
    tty.c_cflag &= ~CRTSCTS;

    if (tcsetattr(fd, TCSANOW, &tty) != 0) {
        perror("Error from tcsetattr");
        return -1;
    }
    return fd;
}

int main() {
    int fd = setup_serial(PORTNAME);
    if (fd < 0) {
        return 1;
    }

    const char ON1[] = "8";
    const char OFF1[] = "I";

    double onset_times[NPERIODS];
    double actual_onsets[NPERIODS];

    // Pre-calculate onset times
    for (int i = 0; i < NPERIODS; i++) {
        onset_times[i] = i * PERIOD;
    }

    double t0 = perf_counter();

    for (int i = 0; i < NPERIODS; i++) {
        // Busy wait until the start of the next period
        while (perf_counter() - t0 < onset_times[i]) {
            // pass
        }

        actual_onsets[i] = perf_counter() - t0;
        write(fd, ON1, 1);

        // Busy wait for 'TIME_HIGH' seconds
        double t1 = perf_counter();
        while (perf_counter() - t1 < TIME_HIGH) {
            // pass
        }

        write(fd, OFF1, 1);
        
        // Print progress. fflush is required in C to print without a newline
        printf("\r%4d", i + 1);
        fflush(stdout); 
    }

    // Sleep for TIME_LOW microseconds at the end
    usleep((useconds_t)(TIME_LOW * 1000000.0));

    double total_time = perf_counter() - t0;
    
    printf("\r%d periods of %f seconds\n", NPERIODS, PERIOD);
    printf("Total time-elapsed: %f\n", total_time);
    
    printf("Actual onsets:\n");
    for (int i = 0; i < NPERIODS; i++) {
        printf("%f %f %f\n", onset_times[i], actual_onsets[i], actual_onsets[i] - onset_times[i]);
    }
    printf("\n");

    close(fd);
    return 0;
}
