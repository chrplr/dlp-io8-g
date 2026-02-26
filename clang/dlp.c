#include "dlp.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <errno.h>

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

dlp_io8g_t* dlp_new(const char* device, int baudrate) {
    int fd = open(device, O_RDWR | O_NOCTTY | O_SYNC);
    if (fd < 0) {
        perror("Error opening serial port");
        return NULL;
    }

    struct termios tty;
    memset(&tty, 0, sizeof tty);
    if (tcgetattr(fd, &tty) != 0) {
        perror("Error from tcgetattr");
        close(fd);
        return NULL;
    }

    cfsetospeed(&tty, get_baudrate(baudrate));
    cfsetispeed(&tty, get_baudrate(baudrate));

    tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;     // 8-bit chars
    tty.c_iflag &= ~IGNBRK;         // disable break processing
    tty.c_lflag = 0;                // no signaling chars, no echo,
                                    // no canonical processing
    tty.c_oflag = 0;                // no remapping, no delays
    tty.c_cc[VMIN]  = 0;            // read doesn't block
    tty.c_cc[VTIME] = 5;            // 0.5 seconds read timeout

    tty.c_iflag &= ~(IXON | IXOFF | IXANY); // shut off xon/xoff ctrl

    tty.c_cflag |= (CLOCAL | CREAD); // ignore modem controls,
                                    // enable reading
    tty.c_cflag &= ~(PARENB | PARODD);      // shut off parity
    tty.c_cflag &= ~CSTOPB;
    tty.c_cflag &= ~CRTSCTS;

    if (tcsetattr(fd, TCSANOW, &tty) != 0) {
        perror("Error from tcsetattr");
        close(fd);
        return NULL;
    }

    dlp_io8g_t* dlp = (dlp_io8g_t*)malloc(sizeof(dlp_io8g_t));
    if (!dlp) {
        close(fd);
        return NULL;
    }
    dlp->fd = fd;

    // Ping to check the device (sending 0x27 is ')
    unsigned char ping_cmd = 0x27;
    if (write(fd, &ping_cmd, 1) != 1) {
        perror("Error writing ping");
        dlp_close(dlp);
        return NULL;
    }

    unsigned char buff[8];
    int n = read(fd, buff, 1);
    if (n != 1 || buff[0] != 'Q') {
        fprintf(stderr, "Device did not respond to ping correctly\n");
        dlp_close(dlp);
        return NULL;
    }

    // set BINARY mode for return values (sending 0x5C is \)
    unsigned char binary_cmd = 0x5C;
    if (write(fd, &binary_cmd, 1) != 1) {
        perror("Error writing binary command");
        dlp_close(dlp);
        return NULL;
    }

    return dlp;
}

void dlp_close(dlp_io8g_t* dlp) {
    if (dlp) {
        if (dlp->fd >= 0) {
            close(dlp->fd);
        }
        free(dlp);
    }
}

bool dlp_ping(dlp_io8g_t* dlp) {
    unsigned char ping_cmd = 0x27;
    if (write(dlp->fd, &ping_cmd, 1) != 1) return false;

    unsigned char buff[1];
    int n = read(dlp->fd, buff, 1);
    return (n == 1 && buff[0] == 'Q');
}

size_t dlp_read(dlp_io8g_t* dlp, unsigned char* states) {
    const char* cmds = "ASDFGHJK";
    
    // Clear buffers
    tcflush(dlp->fd, TCIOFLUSH);

    if (write(dlp->fd, cmds, 8) != 8) return 0;

    int total_read = 0;
    while (total_read < 8) {
        int n = read(dlp->fd, states + total_read, 8 - total_read);
        if (n <= 0) break;
        total_read += n;
    }

    return (size_t)total_read;
}

void dlp_set(dlp_io8g_t* dlp, const char* lines) {
    tcflush(dlp->fd, TCOFLUSH);
    if (write(dlp->fd, lines, strlen(lines)) < 0) {
        perror("write error in dlp_set");
    }
}

void dlp_unset(dlp_io8g_t* dlp, const char* lines) {
    char* cmd = strdup(lines);
    if (!cmd) return;

    for (int i = 0; cmd[i]; i++) {
        switch (cmd[i]) {
            case '1': cmd[i] = 'Q'; break;
            case '2': cmd[i] = 'W'; break;
            case '3': cmd[i] = 'E'; break;
            case '4': cmd[i] = 'R'; break;
            case '5': cmd[i] = 'T'; break;
            case '6': cmd[i] = 'Y'; break;
            case '7': cmd[i] = 'U'; break;
            case '8': cmd[i] = 'I'; break;
        }
    }

    tcflush(dlp->fd, TCOFLUSH);
    if (write(dlp->fd, cmd, strlen(cmd)) < 0) {
        perror("write error in dlp_unset");
    }
    free(cmd);
}
