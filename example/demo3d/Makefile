# example/demo3d: dc-qalink's sample game (0.3). Needs a KallistiOS environment (source environ.sh).
#   make OUT=<dir>                  <dir>/demo3d.elf (with the link: live data, menu, network disc) and demo3d.map
#   make OUT=<dir> DCQL_ENABLE=0    the game alone: no network code, no menu (it still runs from its own disc)
#   make OUT=<dir> stage            <dir>/stage/: demo3d.elf and DEMO3D/ (FLOOR.BMP, CRAFT.OBJ), the folder the bridge
#                                   serves (--files, --overlay, --asset-root) and QA Link Cloud's "Start with the sample"
#   make art                        writes the art again (make_art.py; the files are kept in the repository)
OUT ?= build
DCQL_ENABLE ?= 1
QL := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))../..)
D := $(QL)/example/demo3d
.PHONY: all stage art clean
all: $(OUT)/demo3d.elf

CFLAGS_D := -std=gnu11 -O2 -Wall -Wextra -Wno-unused-parameter -I$(QL)/lib
ifeq ($(DCQL_ENABLE),1)
include $(QL)/loader/client.mk
DCQL_DIR := $(QL)/lib
DCQL_MENU := 1
DCQL_OBJDIR := $(OUT)/dcql
include $(QL)/lib/dcql.mk
CFLAGS_D += -DDEMO_LINK=1 $(CL_CLIENT_CFLAGS)
LINK_OBJS := $(CL_CLIENT_OBJS) $(DCQL_MENU_OBJS) $(CL_OUT)/dcql_disc.o
LINK_LD := $(CL_CLIENT_LDFLAGS) -Wl,--wrap=cdrom_read_toc
$(CL_OUT)/dcql_disc.o: $(QL)/lib/dcql_disc.c $(QL)/lib/dcql_disc.h $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc -std=gnu11 -Os -Wall -Wextra -Wno-unused-parameter $(CL_CLIENT_CFLAGS) -c $< -o $@
else
CFLAGS_D += -DDEMO_LINK=0
LINK_OBJS :=
LINK_LD :=
endif

$(OUT)/demo3d.o: $(D)/demo3d.c $(QL)/lib/font/spleen8x16.h
	@mkdir -p $(dir $@)
	kos-cc $(CFLAGS_D) -c $< -o $@
$(OUT)/demo3d.elf: $(OUT)/demo3d.o $(LINK_OBJS)
	kos-cc $(CFLAGS_D) $^ $(LINK_LD) -lm -Wl,-Map=$(OUT)/demo3d.map -o $@
	@sh-elf-size $@

stage: $(OUT)/demo3d.elf
	rm -rf $(OUT)/stage && mkdir -p $(OUT)/stage/DEMO3D
	sh-elf-strip -o $(OUT)/stage/demo3d.elf $(OUT)/demo3d.elf
	cp $(D)/disc/DEMO3D/FLOOR.BMP $(D)/disc/DEMO3D/CRAFT.OBJ $(OUT)/stage/DEMO3D/
	mkdir -p $(OUT)/stage/extras && cp $(D)/art/FLOOR_ALT.BMP $(OUT)/stage/extras/
	@echo "staged: $(OUT)/stage (serve it: qalink_bridge.py --files <it> --overlay <it> --asset-root <it>)"

art:
	python3 $(D)/make_art.py

clean:
	-rm -rf $(OUT)
