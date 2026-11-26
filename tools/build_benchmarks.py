#!/usr/bin/env python3
"""Build benchmark programs with zig cc for rv64ima (no C, no FP).

    python tools/build_benchmarks.py --src riscv-tests --out build/bench

Builds the single-threaded integer programs from riscv-tests/benchmarks
(dhrystone, median, multiply, qsort, rsort, towers, vvadd, memcpy; spmv
needs floating point)
plus this repo's programs/ (orbitmark, irqtest), all with the riscv-tests
``common/crt.S`` and ``programs/bench.ld`` (test.ld minus PHDRS). Upstream ``common/syscalls.c`` is replaced by
``programs/bench_rt.c`` (same interface): the upstream file needs GCC nested
functions and an HTIF host for printf. Here printf goes to the 16550 UART at
0x1000_0000 and exit still goes through ``tohost``.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCH = ["-target", "riscv64-freestanding-none", "-mcpu=generic_rv64+m+a+zicsr+zifencei",
        "-mabi=lp64", "-mcmodel=medany"]
OPTS = ["-DPREALLOCATE=1", "-static", "-std=gnu99", "-O2", "-fno-common", "-fno-builtin-printf",
        "-fno-pic", "-nostdlib", "-Wno-implicit-int", "-Wno-implicit-function-declaration",
        "-Wno-unused-command-line-argument", "-Wno-int-conversion", "-Wno-incompatible-pointer-types",
        "-Wno-format", "-Wno-deprecated-non-prototype", "-Wno-return-type",
        "-Wno-incompatible-library-redeclaration"]
UPSTREAM = ["dhrystone", "median", "multiply", "qsort", "rsort", "towers", "vvadd", "memcpy"]
LOCAL = ["orbitmark", "irqtest"]


def build(name: str, srcs: list[Path], src: Path, out: Path) -> bool:
    common = src / "benchmarks" / "common"
    shim = ROOT / "tools" / "rt-shim"
    inc = ["-I", str(shim), "-I", str(src / "env"), "-I", str(common)]
    for d in sorted({s.parent for s in srcs}):
        inc += ["-I", str(d)]
    cmd = ([sys.executable, "-m", "ziglang", "cc"] + ARCH + OPTS + inc
           + ["-T", str(ROOT / "programs" / "bench.ld"), str(common / "crt.S"), str(ROOT / "programs" / "bench_rt.c")]
           + [str(s) for s in srcs] + ["-o", str(out / f"{name}.riscv")])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(f"BUILD FAIL {name}\n{r.stderr[-3000:]}")
    return r.returncode == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="riscv-tests checkout")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    jobs = [(n, sorted((a.src / "benchmarks" / n).glob("*.c"))) for n in UPSTREAM]
    jobs += [(n, [ROOT / "programs" / f"{n}.c"]) for n in LOCAL]
    ok = sum(build(name, srcs, a.src, a.out) for name, srcs in jobs)
    print(f"built {ok}/{len(jobs)} benchmarks into {a.out}")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
