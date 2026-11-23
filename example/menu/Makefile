# example/menu: dc-qalink's in-game menu example (source a KOS environ.sh first).
#   make OUT=<dir>                   menu.elf (menu + the link on the adapter, dcql.h) and menu-cl.elf (menu + the
#                                    control loop's client library: link and live data), each with a .map
#   make OUT=<dir> host              menu-host: the menu on this PC (pictures and checks; example/menu/host.c)
# Options: EX_SCRIPT=1 (the pad script, stills and limits over the serial line; the lane's Flycast runs),
#          EX_PC=a.b.c.d (the PC the link sends to; default: learn it from the PC's first datagram),
#          EX_PORT / EX_LPORT (UDP ports, default 52040 / 52040), EX_IP (the console's static address on the adapter;
#          default DHCP).
OUT ?= build
EX_SCRIPT ?= 0
EX_PC ?= peer
EX_PORT ?= 52040
EX_LPORT ?= 52040
EX_IP ?=
TOP := $(abspath ../..)

# the default goal first: dcql.mk and client.mk below bring rules of their own
all: $(OUT)/menu.elf $(OUT)/menu-cl.elf

DCQL_DIR := $(TOP)/lib
DCQL_ENABLE := 1
DCQL_NET := bba
DCQL_MENU := 1
DCQL_OBJDIR := $(OUT)/dcql
include $(DCQL_DIR)/dcql.mk
include $(TOP)/loader/client.mk

EX_IPDEF := $(if $(EX_IP),-DEX_IP='"$(EX_IP)"',-DEX_IP=0)
EX_DEFS := -DEX_SCRIPT=$(EX_SCRIPT) -DEX_PC='"$(EX_PC)"' -DEX_PORT=$(EX_PORT) -DEX_LPORT=$(EX_LPORT) $(EX_IPDEF)
HDRS := demo.h $(DCQL_DIR)/dcql_menu.h $(DCQL_DIR)/font/spleen8x16.h

$(OUT)/main.o: main.c $(HDRS)
	@mkdir -p $(OUT)
	kos-cc $(KOS_CFLAGS) -Os $(DCQL_CFLAGS) $(EX_DEFS) -DEX_LINK=1 -I$(DCQL_DIR) -c $< -o $@
$(OUT)/main-cl.o: main.c $(HDRS)
	@mkdir -p $(OUT)
	kos-cc $(KOS_CFLAGS) -Os $(EX_DEFS) -DEX_LINK=0 -DEX_CL=1 $(CL_CLIENT_CFLAGS) -c $< -o $@
$(OUT)/demo.o: demo.c $(HDRS)
	@mkdir -p $(OUT)
	kos-cc $(KOS_CFLAGS) -Os -I$(DCQL_DIR) -c $< -o $@
$(OUT)/menu.elf: $(OUT)/main.o $(OUT)/demo.o $(DCQL_OBJS)
	kos-cc -o $@ $^ $(DCQL_LDFLAGS) -Wl,-Map=$(OUT)/menu.map
$(OUT)/menu-cl.elf: $(OUT)/main-cl.o $(OUT)/demo.o $(DCQL_MENU_OBJS) $(CL_CLIENT_OBJS)
	kos-cc -o $@ $^ $(CL_CLIENT_LDFLAGS) -Wl,-Map=$(OUT)/menu-cl.map

host: $(OUT)/menu-host
$(OUT)/menu-host: host.c demo.c $(DCQL_DIR)/dcql_menu.c $(HDRS)
	@mkdir -p $(OUT)
	gcc -std=gnu11 -O2 -Wall -Wextra -I$(DCQL_DIR) host.c demo.c $(DCQL_DIR)/dcql_menu.c -o $@

clean:
	-rm -rf $(OUT)

.PHONY: all host clean
