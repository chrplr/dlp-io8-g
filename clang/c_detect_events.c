#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <getopt.h>
#include <signal.h>
#include <time.h>
#include <fcntl.h>
#include <termios.h>
#include <dirent.h>

static int global_fd = -1;
static struct timespec start_time;

long elapsed_ms() {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (now.tv_sec - start_time.tv_sec) * 1000 + (now.tv_nsec - start_time.tv_nsec) / 1000000;
}

void handle_sigterm(int sig) {
    (void)sig;
    if (global_fd >= 0) {
        close(global_fd);
        printf("\nSIGTERM received: serial port closed\n");
    }
    exit(0);
}

static speed_t get_baudrate(int baudrate) {
    switch (baudrate) {
        case 9600: return B9600;
        case 19200: return B19200;
        case 38400: return B38400;
        case 57600: return B57600;
        case 115200: return B115200;
        default: return B9600;
    }
}

int open_dlp_io8_g(const char* device, int baudrate) {
    int fd = open(device, O_RDWR | O_NOCTTY | O_SYNC);
    if (fd < 0) {
        perror("open");
        return -1;
    }

    struct termios tty;
    memset(&tty, 0, sizeof tty);
    if (tcgetattr(fd, &tty) != 0) {
        perror("tcgetattr");
        close(fd);
        return -1;
    }

    cfsetospeed(&tty, get_baudrate(baudrate));
    cfsetispeed(&tty, get_baudrate(baudrate));

    // 7E1 configuration
    tty.c_cflag &= ~CSIZE;
    tty.c_cflag |= CS7;
    tty.c_cflag |= PARENB;
    tty.c_cflag &= ~PARODD;
    tty.c_cflag &= ~CSTOPB;

    tty.c_iflag &= ~IGNBRK;
    tty.c_lflag = 0;
    tty.c_oflag = 0;
    tty.c_cc[VMIN]  = 0;
    tty.c_cc[VTIME] = 5;

    tty.c_iflag &= ~(IXON | IXOFF | IXANY);
    tty.c_cflag |= (CLOCAL | CREAD);
    tty.c_cflag &= ~CRTSCTS;

    if (tcsetattr(fd, TCSANOW, &tty) != 0) {
        perror("tcsetattr");
        close(fd);
        return -1;
    }

    return fd;
}

int main(int argc, char** argv) {
    int listPorts = 0;
    int baudRate = 115200;
    char* portName = "/dev/ttyUSB0";

    static struct option long_options[] = {
        {"list_ports", no_argument, 0, 'l'},
        {"baud_rate", required_argument, 0, 'b'},
        {"port_name", required_argument, 0, 'p'},
        {0, 0, 0, 0}
    };

    int c_opt;
    while ((c_opt = getopt_long(argc, argv, "lb:p:", long_options, NULL)) != -1) {
        switch (c_opt) {
            case 'l': listPorts = 1; break;
            case 'b': baudRate = atoi(optarg); break;
            case 'p': portName = optarg; break;
        }
    }

    if (listPorts) {
        DIR *d;
        struct dirent *dir;
        d = opendir("/dev");
        if (d) {
            while ((dir = readdir(d)) != NULL) {
                if (strncmp(dir->d_name, "ttyUSB", 6) == 0 || strncmp(dir->d_name, "ttyACM", 6) == 0) {
                    printf("Found port: /dev/%s\n", dir->d_name);
                }
            }
            closedir(d);
        }
        return 0;
    }

    global_fd = open_dlp_io8_g(portName, baudRate);
    if (global_fd < 0) return 1;

    signal(SIGINT, handle_sigterm);
    signal(SIGTERM, handle_sigterm);

    unsigned char ping = 0x27;
    if (write(global_fd, &ping, 1) != 1) perror("write ping");
    unsigned char rb[8];
    read(global_fd, rb, 8);

    unsigned char ascii_mode = 0x60;
    if (write(global_fd, &ascii_mode, 1) != 1) perror("write ascii_mode");

    clock_gettime(CLOCK_MONOTONIC, &start_time);

    unsigned char previous_state = 0;
    unsigned char current_state = 0;
    unsigned char read_line1 = 0x41; // 'A'

    while (1) {
        if (write(global_fd, &read_line1, 1) != 1) break;

        unsigned char buff[8];
        int n = read(global_fd, buff, 8);
        if (n > 0) {
            current_state = buff[0];
            if (current_state != previous_state) {
                printf("time %ld: val=%.*s (%d bytes returned)\n", elapsed_ms(), n, buff, n);
                previous_state = current_state;
            }
        }
    }

    close(global_fd);
    return 0;
}
