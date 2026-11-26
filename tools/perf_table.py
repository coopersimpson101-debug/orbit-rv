#!/usr/bin/env python3
"""Measure IPC, cache hit rates and prediction accuracy in RTL simulation.

    python tools/perf_table.py --bench build/bench --out docs/results/perf.json

Each program runs on the CXXRTL model of the SoC (16 MiB RAM model with a
1-cycle-acknowledge bus, 4 KiB I$ + 4 KiB D$) and is checked commit by commit
against the golden ISS. Counters cover the whole run (startup included):

  IPC            = retired instructions / cycles
  I$ hit rate    = 1 - line refills / instructions fetched into decode
  D$ hit rate    = 1 - line refills / committed cacheable loads+stores+AMOs
  ctrl accuracy  = 1 - fetch redirects from execute / resolved branches+jumps
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit_rv.sim import run_rtl_elf  # noqa: E402

ORDER = ["dhrystone", "orbitmark", "median", "multiply", "qsort", "rsort", "towers", "vvadd",
         "memcpy", "irqtest"]


def derive(r: dict) -> dict:
    return {
        "cycles": r["cycles"], "instret": r["instret"],
        "ipc": round(r["instret"] / r["cycles"], 3),
        "icache_hit": round(1 - r["ic_miss"] / max(r["ic_access"], 1), 4),
        "dcache_hit": round(1 - r["dc_miss"] / max(r["dc_access"], 1), 4),
        "ctrl_accuracy": round(1 - r["mispredicts"] / max(r["branches"], 1), 4),
        "branches": r["branches"], "mispredicts": r["mispredicts"],
        "load_use_stalls": r["stall_load_use"], "mem_stall_cycles": r["stall_mem"],
        "lockstep": r.get("lockstep_msg"),
    }


def markdown(rows: dict) -> str:
    s = ("| program | instructions | cycles | IPC | I$ hit | D$ hit | branch/jump prediction |\n"
         "|---|---:|---:|---:|---:|---:|---:|\n")
    for k, v in rows.items():
        s += (f"| {k} | {v['instret']:,} | {v['cycles']:,} | {v['ipc']:.2f} | {100 * v['icache_hit']:.1f}% | "
              f"{100 * v['dcache_hit']:.1f}% | {100 * v['ctrl_accuracy']:.1f}% |\n")
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("docs/results/perf.json"))
    ap.add_argument("--no-lockstep", action="store_true")
    a = ap.parse_args(argv)
    rows = {}
    for name in ORDER:
        elf = a.bench / f"{name}.riscv"
        if not elf.exists():
            continue
        t = time.time()
        r = run_rtl_elf(elf, max_cycles=20_000_000, lockstep=not a.no_lockstep)
        if not r["pass"]:
            print(f"{name}: FAILED {r}")
            return 1
        rows[name] = derive(r)
        print(f"{name}: IPC {rows[name]['ipc']} ({time.time() - t:.1f} s)", flush=True)
    meta = {"machine": f"{platform.system()} {platform.machine()}, {platform.processor()}",
            "date": time.strftime("%Y-%m-%d"), "kind": "RTL simulation (CXXRTL), not hardware"}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"meta": meta, "programs": rows}, indent=1))
    md = markdown(rows)
    a.out.with_suffix(".md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
