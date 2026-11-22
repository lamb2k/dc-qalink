/* stub_blob.S: the return stub's bytes (stub.bin) inside the loader, which copies them to the hand-off area. */
	.section .rodata
	.balign 32
	.global _cl_stub_blob
	.global _cl_stub_blob_end
_cl_stub_blob:
	.incbin STUB_BIN
_cl_stub_blob_end:
