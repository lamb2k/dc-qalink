# client.mk: the control loop's client library for a program the loader runs (dc-qalink 0.2). Include it from a KOS
# Makefile with OUT set (objects go to $(OUT)/cl), then link $(CL_CLIENT_OBJS) and $(CL_CLIENT_LDFLAGS); compile code
# that includes cl_client.h with $(CL_CLIENT_CFLAGS). Brings: the hand-off area, every way back to the loader, the
# watchdog, and the in-game link on the broadband adapter (lib/dcql_net.c, adapter only: no modem or PPP code).
CL_QL := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))..)
CL_OUT ?= $(OUT)/cl
CL_CLIENT_CFLAGS := -I$(CL_QL)/include -I$(CL_QL)/lib -I$(CL_QL)/loader
CL_CLIENT_OBJS := $(CL_OUT)/client.o $(CL_OUT)/cl_util.o $(CL_OUT)/dcql_return.o $(CL_OUT)/dcql_net_bba.o
CL_CLIENT_LDFLAGS := -Wl,--wrap=arch_abort
CL_HDRS := $(CL_QL)/include/dcql_loop.h $(CL_QL)/loader/cl_core.h $(CL_QL)/loader/cl_client.h $(CL_QL)/lib/dcql_net.h
$(CL_OUT)/client.o: $(CL_QL)/loader/client.c $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc -std=gnu11 -Os -Wall $(CL_CLIENT_CFLAGS) -DCL_WRAP_ABORT -c $< -o $@
$(CL_OUT)/cl_util.o: $(CL_QL)/loader/cl_util.c $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc -std=gnu11 -Os -Wall $(CL_CLIENT_CFLAGS) -c $< -o $@
$(CL_OUT)/dcql_return.o: $(CL_QL)/lib/dcql_return.c $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc -std=gnu11 -Os -Wall $(CL_CLIENT_CFLAGS) -c $< -o $@
$(CL_OUT)/dcql_net_bba.o: $(CL_QL)/lib/dcql_net.c $(CL_HDRS)
	@mkdir -p $(dir $@)
	kos-cc -std=gnu11 -Os -Wall $(CL_CLIENT_CFLAGS) -DDCQL_NET_HAVE_MODEM=0 -DDCQL_NET_HAVE_BBA=1 -c $< -o $@
