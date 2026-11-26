# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-26

### Added
- 5-stage in-order RV64IMA_Zicsr_Zifencei core in Amaranth HDL: forwarding,
  load-use interlock, 32-entry BTB with 2-bit counters, iterative mul/div.
- M/S/U privilege modes, precise traps, delegation, CLINT timer/software and
  PLIC external interrupts, WFI.
- Sv39 MMU: 4-entry ITLB, 8-entry DTLB, hardware page-table walker through the
  D-cache, superpages, Svade A/D policy.
- 4 KiB direct-mapped I-cache and write-back D-cache (32-byte lines), FENCE.I.
- SoC: OBus interconnect, CLINT, PLIC, 16550-subset UART, finisher, block RAM.
- Golden Python ISS and a commit-by-commit lockstep checker.
- CXXRTL-based fast RTL simulator built with Yosys and zig c++.
- riscv-tests and benchmark builds with zig cc; IPC / cache / predictor table.
- ECP5 (ULX3S 85F) synthesis and place-and-route with the yowasp tools.
- CI: unit and RTL tests, Spike commit-log comparison, FPGA synthesis.
