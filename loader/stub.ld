/* stub.ld: the return stub at CL_STUB_ADDR in P2 (uncached). The image must stay below the stack's floor: the
   Makefile checks the binary against CL_STUB_MAX less 1 KiB of stack. */
OUTPUT_FORMAT("elf32-shl")
ENTRY(_stub_entry)
SECTIONS
{
    . = 0xacfe1000;
    .text : { *(.text.entry) *(.text*) *(.rodata*) *(.data*) *(.bss*) *(COMMON) }
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
