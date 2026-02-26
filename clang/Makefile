CC=gcc
CFLAGS=-Wall -Wextra -O2
LDFLAGS=

TOOLS = test_dlp c_check_dlp_io8_g c_check1 c_detect_events

all: $(TOOLS)

test_dlp: test_dlp.o dlp.o
	$(CC) $(LDFLAGS) -o $@ $^

c_check_dlp_io8_g: c_check_dlp_io8_g.o dlp.o
	$(CC) $(LDFLAGS) -o $@ $^

c_check1: c_check1.o dlp.o
	$(CC) $(LDFLAGS) -o $@ $^

c_detect_events: c_detect_events.o
	$(CC) $(LDFLAGS) -o $@ $^

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o $(TOOLS)
