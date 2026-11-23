CC = clang

DEBUG ?= 0

OBJC_INCLUDE = -I/usr/lib/gcc/x86_64-pc-linux-gnu/16/include
GNUSTEP_FLAGS = $(shell gnustep-config --objc-flags)
GNUSTEP_LIBS = $(shell gnustep-config --base-libs)

CFLAGS = -O2 -Wall -std=gnu11 -fblocks
OBJCFLAGS = -O2 -Wall -std=gnu11 $(OBJC_INCLUDE) $(GNUSTEP_FLAGS)

ifneq ($(DEBUG),0)
DEFINES += -DDEBUG=$(DEBUG)
endif

all: devicetree-parse devicetree-repack

devicetree-parse: devicetree-parse.o parse.o
	$(CC) -o $@ $^ -lBlocksRuntime

devicetree-repack: repack.o
	$(CC) -o $@ $^ -lBlocksRuntime $(GNUSTEP_LIBS)

devicetree-parse.o: devicetree-parse.c devicetree-parse.h
	$(CC) $(CFLAGS) $(DEFINES) -c -o $@ $<

parse.o: parse.c devicetree-parse.h
	$(CC) $(CFLAGS) $(DEFINES) -D_GNU_SOURCE -c -o $@ $<

repack.o: repack.m
	$(CC) $(OBJCFLAGS) $(DEFINES) -c -o $@ $<

clean:
	rm -f *.o *.d devicetree-parse devicetree-repack

.PHONY: all clean
