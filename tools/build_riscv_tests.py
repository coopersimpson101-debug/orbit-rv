#!/usr/bin/env python3
"""Build the official riscv-tests ISA suites with zig cc (no GNU toolchain).

    git clone https://github.com/riscv-software-src/riscv-tests
    git -C riscv-tests checkout 1b3f8bd665d79e5985753a558dae4e3718e2edc8
    git -C riscv-tests submodule update --init env
    python tools/build_riscv_tests.py --src riscv-tests --out build/riscv-tests

Suites: rv64ui, rv64um, rv64ua (-p and -v), rv64mi, rv64si (-p only, as upstream).
Target: rv64ima_zicsr_zifencei, lp64, medany, no compressed instructions.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import re
import subprocess
import sys
from pathlib import Path

PINNED_COMMIT = "1b3f8bd665d79e5985753a558dae4e3718e2edc8"
SUITES = ["rv64ui", "rv64um", "rv64ua", "rv64mi", "rv64si"]
ARCH = ["-target", "riscv64-freestanding-none", "-mcpu=generic_rv64+m+a+zicsr+zifencei",
        "-mabi=lp64", "-mcmodel=medany"]
COMMON = ["-static", "-nostdlib", "-fno-pic", "-fvisibility=hidden", "-Wno-unused-command-line-argument"]


def tests_in(makefrag: Path, suite: str) -> tuple[list[str], list[str]]:
    """(p tests, v tests) listed in a suite's Makefrag."""
    text = makefrag.read_text().replace("\\\n", " ")
    sc = re.search(rf"^{suite}_sc_tests\s*=(.*)$", text, re.M)
    names = sc.group(1).split() if sc else []
    has_v = re.search(rf"^{suite}_v_tests\s*=", text, re.M) is not None
    return names, (names if has_v else [])


def zig_cc() -> list[str]:
    return [sys.executable, "-m", "ziglang", "cc"]


SHIM = Path(__file__).resolve().parent / "rt-shim"


def preprocess(src: Path, asm: Path, inc: list[str], dst: Path) -> None:
    """Run cpp on a test, then drop ``.weak X`` when the test also says ``.global X``.

    The env declares ``mtvec_handler``/``stvec_handler`` weak and some tests
    later make them global; GNU as accepts that, LLVM's assembler rejects it
    ("changed binding to STB_GLOBAL"). Removing the redundant weak directive
    does not change the meaning of the program."""
    r = subprocess.run(zig_cc() + ARCH + inc + ["-E", "-P", "-x", "assembler-with-cpp", str(asm)],
                       capture_output=True, text=True, check=True)
    text = r.stdout
    for sym in set(re.findall(r"\.globa?l\s+(\w+)", text)):
        text = re.sub(rf"\.weak\s+{sym}\s*;", "", text)
    dst.write_text(text, newline="\n")


def build_one(src: Path, out: Path, suite: str, name: str, env: str) -> tuple[str, bool, str]:
    exe = out / f"{suite}-{env}-{name}"
    inc = ["-I", str(src / "env" / env), "-I", str(src / "isa" / "macros" / "scalar")]
    asm = out / "pp" / f"{exe.name}.s"
    asm.parent.mkdir(exist_ok=True)
    try:
        preprocess(src, src / "isa" / suite / f"{name}.S", inc, asm)
    except subprocess.CalledProcessError as e:
        return exe.name, False, e.stderr
    ld = src / "env" / env / "link.ld"
    if ld.stat().st_size < 64 and not ld.read_text().lstrip().startswith(("OUTPUT", "/*", "SECTIONS")):
        # a git symlink checked out as a plain file (Windows without symlink support)
        ld = (ld.parent / ld.read_text().strip()).resolve()
    cmd = zig_cc() + ARCH + COMMON + inc + ["-isystem", str(SHIM), "-T", str(ld)]
    if env == "v":
        entropy = "0x" + hashlib.md5(exe.name.encode()).hexdigest()[:7]
        vflags = [f"-DENTROPY={entropy}", "-std=gnu99", "-O2"]
        objs = []
        for f in ("entry.S", "vm.c", "string.c"):
            o = out / "pp" / f"{exe.name}.{f}.o"
            arch = ARCH
            if f == "vm.c":
                # vm.c embeds one "fssr x0" word (never executed) to recognise FP tests;
                # the assembler only accepts it with F enabled. Codegen stays lp64/integer.
                arch = [a.replace("+m+a+", "+m+a+f+") for a in ARCH]
            r = subprocess.run(zig_cc() + arch + COMMON + inc + ["-isystem", str(SHIM)] + vflags
                               + ["-c", str(src / "env" / "v" / f), "-o", str(o)],
                               capture_output=True, text=True)
            if r.returncode:
                return exe.name, False, r.stderr
            objs.append(str(o))
        cmd += objs
    cmd += [str(asm), "-o", str(exe)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return exe.name, r.returncode == 0, r.stderr.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="riscv-tests checkout")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--suites", nargs="*", default=SUITES)
    ap.add_argument("-j", type=int, default=8)
    a = ap.parse_args(argv)
    head = subprocess.run(["git", "-C", str(a.src), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    if head and head != PINNED_COMMIT:
        print(f"warning: riscv-tests is at {head}, results in the README are for {PINNED_COMMIT}")
    a.out.mkdir(parents=True, exist_ok=True)
    jobs = []
    for suite in a.suites:
        p, v = tests_in(a.src / "isa" / suite / "Makefrag", suite)
        jobs += [(suite, n, "p") for n in p] + [(suite, n, "v") for n in v]
    fails = 0
    with cf.ThreadPoolExecutor(a.j) as ex:
        for name, ok, err in ex.map(lambda j: build_one(a.src, a.out, *j), jobs):
            if not ok:
                fails += 1
                print(f"BUILD FAIL {name}\n{err}")
    print(f"built {len(jobs) - fails}/{len(jobs)} tests into {a.out}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
