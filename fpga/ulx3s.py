#!/usr/bin/env python3
"""Synthesis + place-and-route of the orbit-rv SoC for the ULX3S (ECP5 LFE5U-85F).

    python fpga/ulx3s.py --image path/to/kernel.bin            # synth + PnR + bitstream
    python fpga/ulx3s.py --image kernel.bin --synth-only        # stop after Yosys

Toolchain: yowasp-yosys and yowasp-nextpnr-ecp5 (WebAssembly builds from PyPI),
so nothing needs to be installed beyond ``pip install``. The program image is
baked into the 256 KiB block-RAM main memory, the core boots it at
0x8000_0000 from the 25 MHz oscillator, and the UART goes to the on-board FTDI
(115200 8N1). Outputs land in ``build/fpga/``: ``top.json`` (netlist),
``top.config`` (placed and routed), ``orbit_rv.bit`` (bitstream, needs
``ecppack`` from yowasp-nextpnr-ecp5's ``yowasp-ecppack``), plus the
utilisation/timing reports used in the README.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from amaranth import ClockDomain, Elaboratable, Module, Signal  # noqa: E402
from amaranth.back import rtlil  # noqa: E402

from orbit_rv import memmap as mm  # noqa: E402
from orbit_rv.rtl.soc import SoC  # noqa: E402

CLK_HZ = 25_000_000


class Top(Elaboratable):
    def __init__(self, image: bytes):
        words = [int.from_bytes(image[i:i + 8].ljust(8, b"\0"), "little")
                 for i in range(0, len(image), 8)]
        if len(words) * 8 > mm.RAM_SIZE_FPGA:
            raise ValueError("image does not fit in block RAM")
        self.soc = SoC(ram_size=mm.RAM_SIZE_FPGA, external_ram=False, ram_init=words, clk_hz=CLK_HZ)
        self.clk_25mhz = Signal()
        self.ftdi_rxd = Signal()   # FPGA -> PC
        self.ftdi_txd = Signal()   # PC -> FPGA
        self.led = Signal(8)

    def elaborate(self, platform):
        m = Module()
        m.domains.por = por = ClockDomain(reset_less=True)
        m.domains.sync = sync = ClockDomain()
        m.d.comb += [por.clk.eq(self.clk_25mhz), sync.clk.eq(self.clk_25mhz)]
        cnt = Signal(16)
        with m.If(~cnt[15]):
            m.d.por += cnt.eq(cnt + 1)
        m.d.comb += sync.rst.eq(~cnt[15])
        m.submodules.soc = soc = self.soc
        heartbeat = Signal(25)
        m.d.sync += heartbeat.eq(heartbeat + 1)
        m.d.comb += [self.ftdi_rxd.eq(soc.uart_tx), soc.uart_rx.eq(self.ftdi_txd),
                     self.led.eq(soc.fin_done | (heartbeat[-1] << 7) | (~soc.uart_tx << 1))]
        return m

    def ports(self):
        return [self.clk_25mhz, self.ftdi_rxd, self.ftdi_txd, self.led]


def report(log: str, out: Path) -> dict:
    res: dict = {}
    for name in ("TRELLIS_COMB", "TRELLIS_FF", "DP16KD", "MULT18X18D", "TRELLIS_RAMW", "TRELLIS_IO",
                 "CCU2C"):
        mt = re.findall(rf"{name}:\s+(\d+)/\s*(\d+)", log)
        if mt:
            res[name] = {"used": int(mt[-1][0]), "avail": int(mt[-1][1])}
    fm = re.findall(r"Max frequency for clock\s+'([^']+)':\s+([\d.]+) MHz \((PASS|FAIL) at ([\d.]+) MHz\)", log)
    if fm:
        res["fmax_mhz"] = float(fm[-1][1])
        res["target_mhz"] = float(fm[-1][3])
        res["timing"] = fm[-1][2]
    out.write_text(json.dumps(res, indent=1))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", type=Path, help="flat binary loaded at 0x8000_0000 (default: empty RAM)")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "fpga")
    ap.add_argument("--synth-only", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--pnr-only", action="store_true", help="reuse top.json from a previous run")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    image = a.image.read_bytes() if a.image else b""
    # the WebAssembly tools only see the working directory: keep every input there
    (a.out / "ulx3s.lpf").write_bytes((ROOT / "fpga" / "ulx3s.lpf").read_bytes())
    t0 = time.time()
    cwd = os.getcwd()
    os.chdir(a.out)
    try:
        if not a.pnr_only:
            top = Top(image)
            Path("top.il").write_text(rtlil.convert(top, name="top", ports=top.ports()), newline="\n")
            from yowasp_yosys import run_yosys
            rc = run_yosys(["-q", "-l", "yosys.log", "-p",
                            "read_rtlil top.il; synth_ecp5 -top top -abc9 -json top.json; "
                            "tee -o yosys_stat.txt stat"])
            if rc:
                raise SystemExit("yosys failed")
            print(f"[fpga] yosys done in {time.time() - t0:.0f} s")
        if a.synth_only:
            return 0
        from yowasp_nextpnr_ecp5 import run_nextpnr_ecp5
        t1 = time.time()
        rc = run_nextpnr_ecp5(["--85k", "--package", "CABGA381", "--speed", "6",
                               "--json", "top.json", "--lpf", "ulx3s.lpf",
                               "--textcfg", "top.config", "--freq", str(CLK_HZ // 1_000_000),
                               "--seed", str(a.seed), "--log", "nextpnr.log", "--timing-allow-fail"])
        print(f"[fpga] nextpnr done in {time.time() - t1:.0f} s (rc {rc})")
    finally:
        os.chdir(cwd)
    res = report((a.out / "nextpnr.log").read_text(errors="replace"), a.out / "report.json")
    print(json.dumps(res, indent=1))
    try:
        from yowasp_nextpnr_ecp5 import run_ecppack
        os.chdir(a.out)
        run_ecppack(["top.config", "orbit_rv.bit"])
        os.chdir(cwd)
    except ImportError:
        print("[fpga] ecppack not available; pack top.config with `ecppack top.config orbit_rv.bit`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
