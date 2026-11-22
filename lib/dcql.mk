# dcql.mk: include from a KOS Makefile.
#   DCQL_DIR := path/to/dc-qalink/lib
#   include $(DCQL_DIR)/dcql.mk
#   OBJS += $(DCQL_OBJS); compile your files with $(DCQL_CFLAGS) -I$(DCQL_DIR); link with $(DCQL_LDFLAGS).
#   Do NOT also link -lppp.
# DCQL_ENABLE=1 builds the link (QA / test builds). Default 0: DCQL_OBJS is empty and the header's calls are no-ops,
# so a retail build carries nothing.
# DCQL_NET=modem (default, as 0.1: only the modem; libppp compiled from $(KOS_BASE)/addons/libppp with five
#   functions renamed to dcql_net.c's hardened versions), bba (only the broadband / LAN adapter: KOS's net stack, no
#   PPP), or both (the adapter if one is fitted, else the modem; dcql_config_t.net can force either).
# DCQL_LOOP=1 adds the control loop's way back (dcql_return.c), so the RESET command returns a program the loader
#   started to the loader.
comma := ,
DCQL_ENABLE ?= 0
DCQL_NET ?= modem
DCQL_LOOP ?= 0
DCQL_DIR ?= $(dir $(lastword $(MAKEFILE_LIST)))
DCQL_OBJDIR ?= dcql-obj
ifeq ($(DCQL_ENABLE),1)
DCQL_QL := $(abspath $(DCQL_DIR)/..)
DCQL_CFLAGS := -DDCQL_ENABLE=1
DCQL_NET_DEFS_modem := -DDCQL_NET_HAVE_MODEM=1 -DDCQL_NET_HAVE_BBA=0
DCQL_NET_DEFS_bba := -DDCQL_NET_HAVE_MODEM=0 -DDCQL_NET_HAVE_BBA=1
DCQL_NET_DEFS_both := -DDCQL_NET_HAVE_MODEM=1 -DDCQL_NET_HAVE_BBA=1
DCQL_PPP_DIR := $(KOS_BASE)/addons/libppp
DCQL_PPP_OBJS := $(if $(filter bba,$(DCQL_NET)),,$(addprefix $(DCQL_OBJDIR)/,ppp.o lcp.o pap.o ipcp.o ppp_modem.o))
DCQL_LOOP_OBJS := $(if $(filter 1,$(DCQL_LOOP)),$(DCQL_OBJDIR)/dcql_return.o $(DCQL_OBJDIR)/cl_util.o)
DCQL_OBJS := $(DCQL_OBJDIR)/dcql.o $(DCQL_OBJDIR)/dcql_net.o $(DCQL_PPP_OBJS) $(DCQL_LOOP_OBJS)
DCQL_LDFLAGS := $(if $(DCQL_PPP_OBJS),-Wl$(comma)--wrap=createChainBuffer -Wl$(comma)--wrap=destroyChainBuffer -Wl$(comma)--wrap=thd_worker_create_ex)
DCQL_PPP_DEFS := -Dthd_create=dcql_ppp_thd_create -Dthd_pass=dcql_ppp_thd_pass \
                 -Dmodem_write_data=dcql_ppp_modem_write -Dmodem_read_data=dcql_ppp_modem_read \
                 -Dsem_wait_timed=dcql_ppp_sem_wait_timed

$(DCQL_OBJDIR)/dcql.o: $(DCQL_DIR)/dcql.c $(DCQL_DIR)/dcql.h $(DCQL_DIR)/dcql_net.h
	@mkdir -p $(DCQL_OBJDIR)
	kos-cc $(KOS_CFLAGS) -Os $(DCQL_CFLAGS) -I$(DCQL_DIR) -c $< -o $@
$(DCQL_OBJDIR)/dcql_net.o: $(DCQL_DIR)/dcql_net.c $(DCQL_DIR)/dcql_net.h
	@mkdir -p $(DCQL_OBJDIR)
	kos-cc $(KOS_CFLAGS) -Os $(DCQL_NET_DEFS_$(DCQL_NET)) -I$(DCQL_DIR) -c $< -o $@
$(DCQL_OBJDIR)/dcql_return.o: $(DCQL_DIR)/dcql_return.c $(DCQL_QL)/include/dcql_loop.h
	@mkdir -p $(DCQL_OBJDIR)
	kos-cc $(KOS_CFLAGS) -Os -I$(DCQL_QL)/include -I$(DCQL_QL)/loader -c $< -o $@
$(DCQL_OBJDIR)/cl_util.o: $(DCQL_QL)/loader/cl_util.c $(DCQL_QL)/include/dcql_loop.h
	@mkdir -p $(DCQL_OBJDIR)
	kos-cc $(KOS_CFLAGS) -Os -I$(DCQL_QL)/include -I$(DCQL_QL)/loader -c $< -o $@
$(DCQL_OBJDIR)/%.o: $(DCQL_PPP_DIR)/%.c
	@mkdir -p $(DCQL_OBJDIR)
	kos-cc $(KOS_CFLAGS) -Os -I$(DCQL_PPP_DIR) -I$(KOS_BASE)/kernel/net $(DCQL_PPP_DEFS) -c $< -o $@
else
DCQL_CFLAGS :=
DCQL_OBJS :=
DCQL_LDFLAGS :=
endif
