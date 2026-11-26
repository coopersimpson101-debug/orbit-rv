# Memory map

Single source of truth: [`orbit_rv/memmap.py`](../orbit_rv/memmap.py), used by
the RTL, the ISS and the tools. Device addresses match QEMU's `virt` riscv64
machine, so the same orbit-os image runs on the RTL, the ISS and
`qemu-system-riscv64 -M virt -bios none`.

| base | size | device | notes |
|---|---|---|---|
| `0x0010_0000` | 4 KiB | finisher | 32-bit write: `0x5555` power off, `0x7777` reset, `0x3333 \| code<<16` fail |
| `0x0200_0000` | 64 KiB | CLINT | `msip` @ +0x0, `mtimecmp` @ +0x4000, `mtime` @ +0xBFF8 (10 MHz) |
| `0x0C00_0000` | 4 MiB | PLIC | priority @ +4·src, pending @ +0x1000, enable @ +0x2000+0x80·ctx, threshold @ +0x20_0000+0x1000·ctx, claim/complete @ +4 |
| `0x1000_0000` | 4 KiB | UART | 16550 subset, byte registers (stride 1), PLIC source 10 |
| `0x8000_0000` | 256 KiB (FPGA) / 16 MiB (sim default) | RAM | cached; reset PC |

PLIC contexts: 0 = hart 0 M-mode (drives `mip.MEIP`), 1 = hart 0 S-mode
(drives `mip.SEIP`). Anything else in the 32-bit physical space raises an
access fault (fetches are only allowed from RAM; AMO/LR/SC only to RAM).

## UART registers (DLAB = LCR bit 7)

| offset | read | write |
|---|---|---|
| 0 | RBR (pops RX FIFO) / DLL | THR (pushes TX FIFO) / DLL |
| 1 | IER / DLM | IER (bit0 RX data, bit1 THR empty) / DLM |
| 2 | IIR (0x04 RX, 0x02 THR empty, 0x01 none; 0xC0 if FIFOs on) | FCR (bit1/2 clear RX/TX) |
| 3 | LCR | LCR (only 8N1 is implemented) |
| 4 | MCR | MCR |
| 5 | LSR (bit0 DR, bit5 THRE, bit6 TEMT) | - |
| 7 | SCR | SCR |

Baud rate = clock / (16 · divisor). With the 25 MHz board clock, orbit-os
programs divisor 14 (111.6 kBd, -3.1 % from 115200, within 8N1 tolerance).
