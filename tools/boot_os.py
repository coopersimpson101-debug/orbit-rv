#!/usr/bin/env python3
"""Boot orbit-os on the orbit-rv RTL (CXXRTL) and capture the UART.

    python tools/boot_os.py --kernel ../orbit-os/build/rv64/kernel.elf \\
        --uart docs/results/orbit-os-uart.txt --json docs/results/orbit-os-boot.json

The SoC model has 256 KiB of RAM (as on the FPGA) and a 25 MHz clock, so
mtime and the UART divisor behave as on the board. A scripted shell session
is typed into the UART RX line; each ``spawn`` waits for the demo's final
line. The whole run is then checked commit by commit against the golden ISS
(``--no-lockstep`` to skip).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit_rv.elf import load_elf  # noqa: E402
from orbit_rv.sim import run_rtl  # noqa: E402
from orbit_rv.sim.lockstep import check  # noqa: E402

SESSION = [
    ("help", None),
    ("ps", None),
    ("spawn spin", "[spin-B] done"),
    ("spawn user", "exited with code 42"),
    ("spawn ufault", "killing user task"),
    ("uptime", None),
    ("poweroff", None),
]
CLK_HZ = 25_000_000


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kernel", type=Path, required=True)
    ap.add_argument("--uart", type=Path, default=Path("build/orbit-os-uart.txt"))
    ap.add_argument("--json", type=Path, default=Path("build/orbit-os-boot.json"))
    ap.add_argument("--max-cycles", type=int, default=200_000_000)
    ap.add_argument("--no-lockstep", action="store_true")
    a = ap.parse_args(argv)
    img = load_elf(a.kernel)
    script = "".join(f"{c}\t{w}\n" if w else f"{c}\n" for c, w in SESSION)
    t = time.time()
    r = run_rtl(img, ram_size=256 * 1024, max_cycles=a.max_cycles, log=not a.no_lockstep, script=script)
    wall = time.time() - t
    uart = r.pop("uart")
    log = r.pop("log", None)
    a.uart.parent.mkdir(parents=True, exist_ok=True)
    a.uart.write_text(uart.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    res = {k: v for k, v in r.items()}
    res["ipc"] = round(r["instret"] / r["cycles"], 3)
    res["simulated_seconds_at_25MHz"] = round(r["cycles"] / CLK_HZ, 3)
    res["wall_seconds"] = round(wall, 1)
    if log is not None:
        t = time.time()
        ok, msg, n = check(img, log, ram_size=256 * 1024)
        res.update(lockstep=ok, lockstep_msg=msg, lockstep_seconds=round(time.time() - t, 1))
    a.json.write_text(json.dumps(res, indent=1))
    print(uart)
    print(json.dumps(res, indent=1))
    ok = r["stop"] == "finisher" and r["exit_code"] == 0 and res.get("lockstep", True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
