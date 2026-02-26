#include <stdio.h>
#include <unistd.h>
#include "dlp.h"

int main() {
    const char* address = "/dev/ttyUSB0";
    int speed = 115200;

    dlp_io8g_t* d = dlp_new(address, speed);
    if (!d) {
        fprintf(stderr, "Failed to open device\n");
        return 1;
    }

    unsigned char states[8];
    while (1) {
        size_t n = dlp_read(d, states);
        if (n == 8) {
            printf("[");
            for (int i = 0; i < 8; i++) {
                printf("%d%s", (int)states[i], i < 7 ? " " : "");
            }
            printf("]\n");
        }
        sleep(1);
    }

    dlp_close(d);
    return 0;
}
