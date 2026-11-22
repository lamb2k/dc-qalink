/* cl_arm.h: arming the control loop's return stub (dc-qalink 0.2, cl_arm.c). Link cl_arm.o and stub_blob.o. */
#ifndef CL_ARM_H
#define CL_ARM_H
#include "cl_core.h"
int cl_arm_find(void);                 /* LOADER.BIN on the mounted disc; 0 = found and checked, <0 = no way back */
void cl_arm_stub(cl_handoff_t *a);     /* the stub and the way back into the hand-off area (disarmed if none) */
const uint8_t *cl_arm_sha(void);
uint32_t cl_arm_lba(void), cl_arm_len(void), cl_arm_stub_bytes(void);
#endif
