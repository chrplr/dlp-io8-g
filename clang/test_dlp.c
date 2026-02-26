#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <signal.h>
#include <string.h>
#include "dlp.h"

static dlp_io8g_t* dlp_ptr = NULL;

void handle_sigint(int sig) {
    (void)sig;
    if (dlp_ptr) {
        printf("\nClosing device...\n");
        dlp_close(dlp_ptr);
    }
    exit(0);
}

int main(int argc, char** argv) {
    const char* device = "/dev/ttyUSB0";
    int baudrate = 115200;

    if (argc > 1) device = argv[1];
    if (argc > 2) baudrate = atoi(argv[2]);

    signal(SIGINT, handle_sigint);

    printf("Connecting to %s at %d bps...\n", device, baudrate);
    dlp_ptr = dlp_new(device, baudrate);
    if (!dlp_ptr) {
        fprintf(stderr, "Failed to connect to %s\n", device);
        return 1;
    }

    printf("Connected! Pinging...\n");
    if (dlp_ping(dlp_ptr)) {
        printf("Ping successful!\n");
    } else {
        printf("Ping failed!\n");
    }

    printf("Reading lines (Ctrl+C to stop)...\n");
    unsigned char states[8];
    while (1) {
        size_t n = dlp_read(dlp_ptr, states);
        if (n == 8) {
            printf("States: ");
            for (int i = 0; i < 8; i++) {
                printf("%d ", (int)states[i]);
            }
            printf("\n");
        } else {
            printf("Read failed (got %zu bytes)\n", n);
        }
        sleep(1);
    }

    dlp_close(dlp_ptr);
    return 0;
}
