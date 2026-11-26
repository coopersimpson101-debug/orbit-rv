"""The orbit-rv SoC memory map, shared by the RTL, the golden ISS and the docs.

The device addresses deliberately match a subset of QEMU's ``virt`` riscv64
machine (and Spike's CLINT), so one kernel image runs on the RTL, on the ISS
and under ``qemu-system-riscv64 -M virt -bios none``.
"""

RESET_PC = 0x8000_0000

# SiFive-test-style "finisher" (as on QEMU virt): write 0x5555 = power off (pass),
# 0x3333 | code << 16 = fail with code, 0x7777 = reset (the simulators stop).
FINISHER_BASE = 0x0010_0000
FINISHER_SIZE = 0x0000_1000
FINISHER_PASS, FINISHER_FAIL, FINISHER_RESET = 0x5555, 0x3333, 0x7777

CLINT_BASE = 0x0200_0000
CLINT_SIZE = 0x0001_0000
CLINT_MSIP = 0x0000
CLINT_MTIMECMP = 0x4000
CLINT_MTIME = 0xBFF8

PLIC_BASE = 0x0C00_0000
PLIC_SIZE = 0x0040_0000
PLIC_NSRC = 32            # sources 1..31 (0 is reserved)
PLIC_NCTX = 2             # 0 = hart0 M-mode, 1 = hart0 S-mode
PLIC_PRIORITY = 0x00_0000
PLIC_PENDING = 0x00_1000
PLIC_ENABLE = 0x00_2000   # + 0x80 * ctx
PLIC_CONTEXT = 0x20_0000  # + 0x1000 * ctx: +0 threshold, +4 claim/complete

UART_BASE = 0x1000_0000
UART_SIZE = 0x0000_1000
UART_IRQ = 10             # PLIC source

RAM_BASE = 0x8000_0000
RAM_SIZE_FPGA = 256 * 1024      # block RAM on the ECP5 build
RAM_SIZE_SIM = 16 * 1024 * 1024  # default for simulation

# Physical addresses are 32 bits wide in this SoC (PPN bits above are a fault).
PA_BITS = 32

TIMEBASE_HZ = 10_000_000  # mtime frequency (same as QEMU virt)

REGIONS = [
    ("FINISHER", FINISHER_BASE, FINISHER_SIZE, "write 0x5555 = power off, 0x3333|code<<16 = fail"),
    ("CLINT", CLINT_BASE, CLINT_SIZE, "msip, mtimecmp, mtime (10 MHz)"),
    ("PLIC", PLIC_BASE, PLIC_SIZE, f"{PLIC_NSRC - 1} sources, 2 contexts (M, S)"),
    ("UART", UART_BASE, UART_SIZE, "16550 subset, byte registers, PLIC source 10"),
    ("RAM", RAM_BASE, RAM_SIZE_SIM, "cached; 256 KiB block RAM on the FPGA"),
]


def is_ram(pa: int, ram_size: int) -> bool:
    return RAM_BASE <= pa < RAM_BASE + ram_size


def is_mmio(pa: int) -> bool:
    return (FINISHER_BASE <= pa < FINISHER_BASE + FINISHER_SIZE
            or CLINT_BASE <= pa < CLINT_BASE + CLINT_SIZE
            or PLIC_BASE <= pa < PLIC_BASE + PLIC_SIZE
            or UART_BASE <= pa < UART_BASE + UART_SIZE)
