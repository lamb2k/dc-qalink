# dc-qalink console asset viewer (0.2, docs/ASSETS.md). Needs a KallistiOS environment (source environ.sh).
#   make OUT=<dir>        <dir>/viewer.elf and <dir>/viewer.map
# The bridge pushes viewer.elf through the loader (pc/bridge/assets.py, --viewer).
OUT ?= build
QL := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))../..)
V := $(QL)/example/viewer
.PHONY: all
all: $(OUT)/viewer.elf
include $(QL)/loader/client.mk
CFLAGS_V := -std=gnu11 -O2 -Wall -Wextra -Wno-unused-parameter $(CL_CLIENT_CFLAGS) -I$(V)


$(OUT)/viewer.o: $(V)/viewer.c $(V)/dqa.h $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc $(CFLAGS_V) -c $< -o $@
$(OUT)/viewer.elf: $(OUT)/viewer.o $(CL_CLIENT_OBJS)
	kos-cc $(CFLAGS_V) $^ -Wl,--wrap=cdrom_read_toc $(CL_CLIENT_LDFLAGS) -Wl,-Map=$(OUT)/viewer.map -o $@
	@sh-elf-size $@
