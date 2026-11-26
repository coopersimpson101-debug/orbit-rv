/* Linker script for benchmarks: the layout of riscv-tests benchmarks/common/test.ld
 * without its PHDRS block (ld.lld does not accept SHF_* names in FLAGS()). */
OUTPUT_ARCH( "riscv" )
ENTRY(_start)

SECTIONS
{
  . = 0x80000000;
  .text.init : { *(.text.init) }
  . = ALIGN(0x1000);
  .tohost : { *(.tohost) }
  . = ALIGN(0x1000);
  .text : { *(.text) *(.text.*) }
  .rodata : { *(.rodata) *(.rodata.*) }
  .data : { *(.data) *(.data.*) }
  .sdata : {
    __global_pointer$ = . + 0x800;
    *(.srodata.cst16) *(.srodata.cst8) *(.srodata.cst4) *(.srodata.cst2) *(.srodata*)
    *(.sdata .sdata.* .gnu.linkonce.s.*)
  }
  .sbss : { *(.sbss .sbss.* .gnu.linkonce.sb.*) *(.scommon) }
  .bss : { *(.bss) *(.bss.*) }
  .tdata : { _tdata_begin = .; *(.tdata) _tdata_end = .; }
  .tbss : { *(.tbss) _tbss_end = .; }
  _end = .;
}
