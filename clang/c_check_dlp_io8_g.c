#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <getopt.h>
#include <signal.h>
#include <time.h>
#include <dirent.h>
#include "dlp.h"

static dlp_io8g_t* global_dlp = NULL;
static struct timespec start_time;

long elapsed_ms() {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (now.tv_sec - start_time.tv_sec) * 1000 + (now.tv_nsec - start_time.tv_nsec) / 1000000;
}

void handle_sigterm(int sig) {
    (void)sig;
    if (global_dlp) {
        dlp_close(global_dlp);
        printf("\nSIGTERM received: serial port closed\n");
    }
    exit(0);
}

void list_serial_ports() {
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
}

void send_test(dlp_io8g_t* dlp) {
    while (1) {
        dlp_unset(dlp, "12345678");
        printf("OFF %ldms\n", elapsed_ms());
        sleep(5);

        dlp_set(dlp, "12345678");
        printf("ON %ldms\n", elapsed_ms());
        sleep(5);
    }
}

void receive_test(dlp_io8g_t* dlp) {
    unsigned char states[8];
    while (1) {
        size_t n = dlp_read(dlp, states);
        if (n == 8) {
            for (int i = 0; i < 8; i++) {
                printf("time %ld: line %d=%d\n", elapsed_ms(), i + 1, (int)states[i]);
            }
        }
        usleep(500000); // 0.5s
    }
}

int main(int argc, char** argv) {
    int listPorts = 0;
    int baudRate = 115200;
    char* portName = "/dev/ttyUSB0";
    int readMode = 1;

    static struct option long_options[] = {
        {"list_ports", no_argument, 0, 'l'},
        {"baud_rate", required_argument, 0, 'b'},
        {"port_name", required_argument, 0, 'p'},
        {"read_mode", required_argument, 0, 'r'},
        {0, 0, 0, 0}
    };

    int c_opt;
    while ((c_opt = getopt_long(argc, argv, "lb:p:r:", long_options, NULL)) != -1) {
        switch (c_opt) {
            case 'l': listPorts = 1; break;
            case 'b': baudRate = atoi(optarg); break;
            case 'p': portName = optarg; break;
            case 'r': readMode = (strcmp(optarg, "true") == 0 || strcmp(optarg, "1") == 0); break;
        }
    }

    if (listPorts) {
        list_serial_ports();
        return 0;
    }

    signal(SIGINT, handle_sigterm);
    signal(SIGTERM, handle_sigterm);

    global_dlp = dlp_new(portName, baudRate);
    if (!global_dlp) {
        return 1;
    }

    clock_gettime(CLOCK_MONOTONIC, &start_time);
    sleep(1);

    if (readMode) {
        printf("Receiving on %s\n", portName);
        receive_test(global_dlp);
    } else {
        printf("Sending on %s\n", portName);
        send_test(global_dlp);
    }

    dlp_close(global_dlp);
    return 0;
}
