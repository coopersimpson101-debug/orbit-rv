#!/usr/bin/env python3
"""Emit the SoC as plain Verilog-2005 (usable from SystemVerilog flows).

    python tools/export_verilog.py --out build/orbit_rv_soc.v [--fpga-ram]

Amaranth -> RTLIL -> Yosys ``write_verilog``. By default the RAM bus is a
top-level port (as in simulation); ``--fpga-ram`` includes the 256 KiB
block-RAM main memory instead.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amaranth.back import rtlil  # noqa: E402

from orbit_rv import memmap as mm  # noqa: E402
from orbit_rv.rtl.soc import SoC  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("build/orbit_rv_soc.v"))
    ap.add_argument("--fpga-ram", action="store_true")
    a = ap.parse_args(argv)
    soc = SoC(ram_size=mm.RAM_SIZE_FPGA if a.fpga_ram else mm.RAM_SIZE_SIM, external_ram=not a.fpga_ram)
    from yowasp_yosys import run_yosys
    a.out.parent.mkdir(parents=True, exist_ok=True)
    out = a.out.resolve()
    with tempfile.TemporaryDirectory(dir=out.parent) as td:
        Path(td, "soc.il").write_text(rtlil.convert(soc, name="orbit_rv_soc", ports=soc.ports()),
                                      newline="\n")
        cwd = os.getcwd()
        os.chdir(td)
        try:
            rc = run_yosys(["-q", "-p", "read_rtlil soc.il; proc; flatten; opt_clean; "
                                        "write_verilog -noattr soc.v"])
        finally:
            os.chdir(cwd)
        if rc:
            return rc
        out.write_bytes(Path(td, "soc.v").read_bytes())
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
