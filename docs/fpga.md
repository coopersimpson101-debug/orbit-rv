# FPGA: ULX3S (ECP5 LFE5U-85F)

## Build

Everything runs from pip packages (YoWASP: WebAssembly builds of Yosys,
nextpnr-ecp5 and ecppack, from PyPI):

```sh
pip install -e ".[fpga]"
# orbit-os kernel image (branch feat/riscv64-port of orbit-os):
#   python build.py --board rv64   ->  build/rv64/kernel.bin
python fpga/ulx3s.py --image ../orbit-os/build/rv64/kernel.bin --out build/fpga
```

Outputs in `build/fpga/`: `top.json` (netlist), `top.config` (placed and
routed), `orbit_rv.bit`, `yosys_stat.txt`, `nextpnr.log` and `report.json`
(utilisation, Fmax). The kernel image is baked into the 256 KiB block-RAM
main memory; the core starts at `0x8000_0000` straight from the 25 MHz
on-board oscillator (no PLL), held in reset for 32768 cycles after
configuration.

Pins ([`fpga/ulx3s.lpf`](../fpga/ulx3s.lpf)): `clk_25mhz` G2, UART to the
on-board FT231X (`ftdi_rxd` L4 = FPGA TX, `ftdi_txd` M1 = FPGA RX), LEDs
B2..H3 (LED0 = finisher reached, i.e. `poweroff`; LED1 = UART TX activity;
LED7 = heartbeat, ~0.75 Hz).

## Program and capture the boot log (for the author, needs the board)

```sh
openFPGALoader --board ulx3s build/fpga/orbit_rv.bit          # SRAM, volatile
# or: fujprog build/fpga/orbit_rv.bit
python -m serial.tools.miniterm /dev/ttyUSB0 115200 --raw | tee docs/results/board-uart.txt
```

The UART divisor orbit-os programs for 25 MHz (14) gives 111.6 kBd, 3.1 %
below 115200, which 8N1 receivers tolerate. Press the ULX3S PWR button (or
re-program) to reset; type the shell commands from `tools/boot_os.py`.

| check | expected | result on hardware |
|---|---|---|
| boot banner + `orbit>` prompt | as in `docs/results/orbit-os-uart.txt` | ⏳ |
| `spawn spin` | "preempted: yes" (CLINT timer preemption) | ⏳ |
| `spawn user`, `spawn ufault` | U-mode syscalls, page fault kills the task | ⏳ |
| `poweroff` | LED0 lights (finisher) | ⏳ |
| `uptime` after 60 s wall clock | ~60 s (10 MHz mtime from 25 MHz) | ⏳ |

Timing: for this design nextpnr reported a post-route Fmax of 27.63 MHz
against the 25 MHz constraint (seed 1, see `docs/results/fpga.json`), so the
board clock is inside the timing analysis with about 10 % margin.
