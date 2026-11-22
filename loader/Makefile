# dc-qalink control loop (0.2): the loader, its return stub, the test program and the host test build.
# Needs a KallistiOS environment (source environ.sh: KOS_BASE, kos-cc, KOS_CC).
#   make OUT=<dir>                 loader.elf, LOADER.BIN, testprog.elf, testprog.bin, stub.bin
#   make OUT=<dir> host            cl_host (the protocol core on the PC, for tests/test_loop.py)
# Options: BUILD=<text> (on the loader's screen and in HELLO), TITLE="<text>" (the screen's first line),
#   EXTRA_OBJS=<objects> linked into the loader and the test program (a user's patched KOS files, say),
#   LOADER_NET=both|bba|modem (default both: the adapter if fitted, else the modem).
OUT ?= build
BUILD ?= dev
TITLE ?= DC-QALINK LOADER
LOADER_NET ?= both
EXTRA_OBJS ?=
QL := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))..)
L := $(QL)/loader
INC := -I$(QL)/include -I$(QL)/lib -I$(L)
NET_DEFS_both := -DDCQL_NET_HAVE_MODEM=1 -DDCQL_NET_HAVE_BBA=1
NET_DEFS_bba := -DDCQL_NET_HAVE_MODEM=0 -DDCQL_NET_HAVE_BBA=1
NET_DEFS_modem := -DDCQL_NET_HAVE_MODEM=1 -DDCQL_NET_HAVE_BBA=0
CFLAGS_L := -std=gnu11 -Os -Wall -Wextra -Wno-unused-parameter $(INC) -DLOADER_BUILD=\"$(BUILD)\" -DLOADER_TITLE='"$(TITLE)"'
# KOS libppp from source with dc-qalink's five redirections (lib/dcql_net.c)
PPP_DIR := $(KOS_BASE)/addons/libppp
PPP_SRC := ppp.c lcp.c pap.c ipcp.c ppp_modem.c
PPP_OBJS := $(addprefix $(OUT)/ppp/,$(PPP_SRC:.c=.o))
PPP_DEFS := -Dthd_create=dcql_ppp_thd_create -Dthd_pass=dcql_ppp_thd_pass \
            -Dmodem_write_data=dcql_ppp_modem_write -Dmodem_read_data=dcql_ppp_modem_read \
            -Dsem_wait_timed=dcql_ppp_sem_wait_timed
LOADER_PPP := $(if $(filter bba,$(LOADER_NET)),,$(PPP_OBJS))
LOADER_OBJS := $(addprefix $(OUT)/,loader.o cl_core.o cl_util.o cl_arm.o dcql_net.o stub_blob.o) $(LOADER_PPP) $(EXTRA_OBJS)
include $(L)/client.mk
TEST_OBJS := $(OUT)/testprog.o $(CL_CLIENT_OBJS) $(EXTRA_OBJS)
WRAPS := -Wl,--wrap=createChainBuffer -Wl,--wrap=destroyChainBuffer -Wl,--wrap=thd_worker_create_ex \
         -Wl,--wrap=cdrom_read_toc
STUB_CFLAGS := -ml -m4-single-only -Os -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -nostdlib \
               -fno-pic -Wall -Wextra -I$(QL)/include

.PHONY: all host
all: $(OUT)/loader.elf $(OUT)/LOADER.BIN $(OUT)/testprog.elf $(OUT)/testprog.bin $(OUT)/stub.bin

$(OUT)/ppp/%.o: $(PPP_DIR)/%.c
	@mkdir -p $(dir $@)
	kos-cc $(KOS_CFLAGS) -Os -I$(PPP_DIR) -I$(KOS_BASE)/kernel/net $(PPP_DEFS) -c $< -o $@
HDRS := $(QL)/include/dcql_loop.h $(L)/cl_core.h $(QL)/lib/dcql_net.h $(L)/cl_client.h $(L)/cl_arm.h
$(OUT)/%.o: $(L)/%.c $(HDRS)
	@mkdir -p $(dir $@)
	kos-cc $(CFLAGS_L) -c $< -o $@
$(OUT)/dcql_net.o: $(QL)/lib/dcql_net.c $(HDRS)
	@mkdir -p $(dir $@)
	kos-cc $(CFLAGS_L) $(NET_DEFS_$(LOADER_NET)) -c $< -o $@

# the return stub: a freestanding program linked at CL_STUB_ADDR (P2); the loader carries its bytes
$(OUT)/stub.elf: $(L)/stub.c $(L)/stub_entry.S $(L)/stub.ld $(QL)/include/dcql_loop.h
	@mkdir -p $(dir $@)
	$(KOS_CC) $(STUB_CFLAGS) -T $(L)/stub.ld -o $@ $(L)/stub_entry.S $(L)/stub.c -lgcc
$(OUT)/stub.bin: $(OUT)/stub.elf
	sh-elf-objcopy -O binary $< $@
	@n=$$(stat -c %s $@); echo "stub: $$n bytes (at most 7168: 8 KiB less 1 KiB of stack)"; test $$n -le 7168
$(OUT)/stub_blob.o: $(L)/stub_blob.S $(OUT)/stub.bin
	kos-cc -DSTUB_BIN='"$(OUT)/stub.bin"' -c $< -o $@

$(OUT)/loader.elf: $(LOADER_OBJS)
	kos-cc $(CFLAGS_L) $^ $(WRAPS) -o $@
$(OUT)/LOADER.BIN: $(OUT)/loader.elf
	sh-elf-objcopy -R .stack -O binary $< $@
$(OUT)/testprog.elf: $(TEST_OBJS)
	kos-cc $(CFLAGS_L) $^ -Wl,--wrap=cdrom_read_toc $(CL_CLIENT_LDFLAGS) -o $@
$(OUT)/testprog.bin: $(OUT)/testprog.elf
	sh-elf-objcopy -R .stack -O binary $< $@

host: $(OUT)/cl_host
$(OUT)/cl_host: $(L)/host.c $(L)/cl_core.c $(L)/cl_util.c $(L)/cl_core.h $(QL)/include/dcql_loop.h
	@mkdir -p $(OUT)
	gcc -std=c99 -D_DEFAULT_SOURCE -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer \
	  -I$(QL)/include -I$(L) -o $@ $(L)/host.c $(L)/cl_core.c $(L)/cl_util.c
