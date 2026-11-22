# netdisc: the network disc's example (dc-qalink 0.2). Needs a KallistiOS environment (environ.sh).
#   make OUT=<dir>        netdisc.elf (pushed by the PC to the loader) and netdisc.map (the sizes)
# A program of your own adds $(OUT)/cl/dcql_disc.o (rule below) to the client library's objects (loader/client.mk).
OUT ?= build
QL := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))../..)
.PHONY: all
all: $(OUT)/netdisc.elf
include $(QL)/loader/client.mk
CFLAGS_N := -std=gnu11 -Os -Wall -Wextra -Wno-unused-parameter $(CL_CLIENT_CFLAGS)
DISC_OBJ := $(CL_OUT)/dcql_disc.o

$(DISC_OBJ): $(QL)/lib/dcql_disc.c $(QL)/lib/dcql_disc.h $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc $(CFLAGS_N) -c $< -o $@
$(OUT)/netdisc.o: $(QL)/example/netdisc/main.c $(QL)/lib/dcql_disc.h $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc $(CFLAGS_N) -c $< -o $@
$(OUT)/netdisc.elf: $(OUT)/netdisc.o $(DISC_OBJ) $(CL_CLIENT_OBJS)
	kos-cc $(CFLAGS_N) $^ -Wl,--wrap=cdrom_read_toc $(CL_CLIENT_LDFLAGS) -Wl,-Map=$(OUT)/netdisc.map -o $@
