! stub_entry.S: entry and exit of the control loop's return stub (dc-qalink 0.2, MIT licence). See stub.c.
! Entered by a jump with interrupts off, in any register bank: SR becomes MD=1 RB=0 BL=1 IMASK=15, the stack the top of
! the stub's area (P2), then stub_main. stub_go(sr, vbr, fpscr) leaves for the loader at 0x8C010000 as KallistiOS's
! exec trampoline does: caches off and invalidated (CCR = 0x808), the BIOS's SR / VBR / FPSCR, its boot stack
! (0x8C00F400) and PR = 0xA0000000 (a loader that returns reboots).
	.section .text.entry,"ax"
	.global _stub_entry
	.align 2
_stub_entry:
	mov.l	.sr_init, r0
	ldc	r0, sr
	mov.l	.stack_top, r15
	mov.l	.main, r0
	jsr	@r0
	nop
1:	bra	1b
	nop
	.align 2
.sr_init:	.long 0x500000f0
.stack_top:	.long 0xacfe3000
.main:		.long _stub_main

	.text
	.global _stub_go
	.align 2
_stub_go:
	mov.l	.ccr, r0
	mov.l	.ccrval, r1
	mov.l	r1, @r0
	nop
	nop
	nop
	nop
	nop
	nop
	nop
	nop
	ldc	r5, vbr
	lds	r6, fpscr
	mov.l	.boot_stack, r15
	mov.l	.boot_pr, r1
	lds	r1, pr
	! the target in r8: r0-r7 are banked, and the BIOS's SR has RB = 1, so after the ldc an r1 would be bank 1's (left
	! by the last interrupt or by the program's own call into the stub: the stub then started itself again, lane ql-core)
	mov.l	.dst, r8
	ldc	r4, sr
	jmp	@r8
	nop
	.align 2
.ccr:		.long 0xff00001c
.ccrval:	.long 0x00000808
.boot_stack:	.long 0x8c00f400
.boot_pr:	.long 0xa0000000
.dst:		.long 0x8c010000
