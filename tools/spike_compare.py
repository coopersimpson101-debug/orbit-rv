#!/usr/bin/env python3
"""Compare Spike commit logs against the golden ISS (and optionally the RTL).

    python tools/spike_compare.py --spike /opt/spike/bin/spike \\
        --tests build/riscv-tests --bench build/bench --rtl --out spike-report.md

For every program: run ``spike --isa=rv64ima_zicsr_zifencei_zicntr_zihpm --log-commits``,
the ISS with a commit trace, and (with ``--rtl``) the CXXRTL simulator;
compare PC, encoding, privilege and integer-register writes commit by commit
from the ELF entry point on. Register values of CSR reads that are
implementation-defined (misa, m*id, cycle/time, PMP, trigger CSRs, mip) are
not compared, nor RTL values tagged ``nd`` (MMIO, timers); the instruction
itself still is. A spurious SC failure on one side (allowed by the ISA; Spike
drops reservations at its hart-interleave boundary) is resynchronised. The
tests orbit-rv fails by design are skipped. Exit status is non-zero if any
program in the must-match set (everything except rv64mi-p-breakpoint, which
depends on debug triggers, and irqtest, which depends on interrupt timing)
differs or times out.

This runs in CI (ubuntu) where Spike is built from source; it cannot run on
the Windows development machine.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit_rv import memmap as mm  # noqa: E402
from orbit_rv.commitlog import Commit, parse_lines  # noqa: E402
from orbit_rv.elf import load_elf  # noqa: E402
from orbit_rv.iss import load_hart, trace_lines  # noqa: E402

IMPL_DEFINED_CSRS = ({0x301, 0xF11, 0xF12, 0xF13, 0xF15, 0xB00, 0xC00, 0xC01, 0x344, 0x144, 0x30A,
                      0x10A, 0x320, 0x7A0, 0x7A1, 0x7A2, 0x7A3, 0x7A5}
                     | set(range(0x3A0, 0x3F0)) | set(range(0xB03, 0xB20)) | set(range(0xC03, 0xC20))
                     | set(range(0x323, 0x340)))
# Tests orbit-rv fails by design (tools/run_riscv_tests.py KNOWN_FAIL): not run on Spike.
# ma_data needs hardware misaligned support; on Spike it can also run for a very long time.
EXPECTED_FAIL = {"rv64ui-p-ma_data", "rv64ui-v-ma_data", "rv64mi-p-pmpaddr"}
# Everything else must match, except programs whose results depend on implementation-defined
# features: debug triggers (breakpoint) and interrupt timing (irqtest).
MUST_MATCH = re.compile(r"^(rv64(?!mi-p-breakpoint)|bench-(?!irqtest))")
SPIKE_TIMEOUT = 300


def masked(c: Commit) -> Commit:
    op, f3, csr = c.insn & 0x7F, (c.insn >> 12) & 7, c.insn >> 20
    if op == 0x73 and f3 and csr in IMPL_DEFINED_CSRS:
        return Commit(c.priv, c.pc, c.insn, {})
    return c


def is_sc(c: Commit) -> bool:
    return (c.insn & 0x7F) == 0x2F and (c.insn >> 27) == 0x03


def _sc_resync(seq: list[Commit], k: int, limit: int = 256) -> int | None:
    """Index of the next *successful* SC at the same PC after a failed SC at seq[k]."""
    for m in range(k + 1, min(len(seq), k + limit)):
        if seq[m].pc == seq[k].pc and is_sc(seq[m]) and not any(seq[m].writes.values()):
            return m
    return None


def compare(ref: list[Commit], dut: list[Commit], nd: set[int] | None = None) -> tuple[bool, int, str]:
    """Commit-by-commit compare. One architecturally allowed difference is tolerated:
    an SC may fail spuriously (Spike drops the reservation at its hart-interleave
    boundary every few thousand instructions). If one side's SC fails where the other's
    succeeds, the failing side's retry loop is skipped up to its successful SC at the
    same PC, and the comparison continues from there."""
    i = j = n = 0
    resyncs = 0
    while i < len(ref) and j < len(dut):
        a, b = masked(ref[i]), masked(dut[j])
        if nd and j in nd:
            a, b = Commit(a.priv, a.pc, a.insn, {}), Commit(b.priv, b.pc, b.insn, {})
        if (a.pc, a.insn, a.priv, a.writes) != (b.pc, b.insn, b.priv, b.writes):
            if is_sc(a) and is_sc(b) and a.pc == b.pc and a.writes.keys() == b.writes.keys():
                fa, fb = any(a.writes.values()), any(b.writes.values())
                if fa and not fb and (k := _sc_resync(ref, i)) is not None:
                    i, resyncs = k, resyncs + 1
                    continue
                if fb and not fa and (k := _sc_resync(dut, j)) is not None:
                    j, resyncs = k, resyncs + 1
                    continue
            return False, n, f"commit {n}: spike {ref[i]} | other {dut[j]}"
        i, j, n = i + 1, j + 1, n + 1
    # Spike keeps spinning in the tohost loop until its HTIF polls (every few
    # thousand steps), so its log may be longer; the other simulators stop at once.
    if len(dut) - j > 64 or len(ref) - i > 20000:
        return False, n, f"length spike={len(ref)} other={len(dut)}"
    extra = f", {resyncs} spurious SC failure(s) skipped" if resyncs else ""
    return True, n, f"{n} commits{extra}"


# orbit-rv implements cycle/time/instret (Zicntr) and read-as-zero hpmcounters (Zihpm); current
# Spike only allows those CSR reads when the extensions are named. Fallbacks are for other Spikes.
ISA_STRINGS = ["rv64ima_zicsr_zifencei_zicntr_zihpm", "rv64ima_zicsr_zifencei", "rv64ima"]
_isa: list[str] = []


def run_spike(spike: str, elf: Path, td: Path) -> list[Commit] | None:
    """Spike's commit log, or None if Spike timed out."""
    log = td / (elf.name + ".spike.log")
    for isa in (_isa or ISA_STRINGS):
        if log.exists():
            log.unlink()
        try:
            r = subprocess.run([spike, f"--isa={isa}", f"-m{mm.RAM_BASE:#x}:0x1000000",
                                "--log-commits", f"--log={log}", str(elf)],
                               capture_output=True, text=True, timeout=SPIKE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
        if log.exists() and log.stat().st_size:
            if not _isa:
                _isa.append(isa)
                print(f"spike accepts --isa={isa}", flush=True)
            break
        if _isa:
            break
        print(f"spike rejected --isa={isa}: {r.stderr.strip()[:200]}", flush=True)
    if not log.exists():
        return []
    return parse_lines(log.read_text(errors="replace").splitlines(), start_pc=load_elf(elf).entry)


def run_iss(elf: Path) -> list[Commit]:
    img = load_elf(elf)
    h = load_hart(img)
    h.trace = []
    h.run(5_000_000)
    return parse_lines(list(trace_lines(h.trace)), start_pc=img.entry)


def run_rtl(elf: Path) -> tuple[list[Commit], set[int]]:
    from orbit_rv.sim import run_rtl as rr
    img = load_elf(elf)
    res = rr(img, max_cycles=8_000_000, log=True)
    lines = [ln for ln in res["log"] if re.match(r"core\s+0:\s+\d\s", ln)]
    nd = {i for i, ln in enumerate(lines) if ln.endswith(" nd")}
    return parse_lines(lines, start_pc=img.entry), nd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spike", required=True)
    ap.add_argument("--tests", type=Path, help="riscv-tests build dir")
    ap.add_argument("--bench", type=Path, help="benchmark build dir (*.riscv)")
    ap.add_argument("--rtl", action="store_true", help="also compare the RTL simulator")
    ap.add_argument("--out", type=Path, default=Path("spike-report.md"))
    ap.add_argument("--json", type=Path, default=Path("spike-report.json"))
    a = ap.parse_args(argv)
    progs: list[tuple[str, Path]] = []
    if a.tests:
        progs += [(p.name, p) for p in sorted(a.tests.iterdir())
                  if p.is_file() and p.name.startswith("rv64") and "." not in p.name]
    if a.bench:
        progs += [("bench-" + p.stem, p) for p in sorted(a.bench.glob("*.riscv"))]
    rows, results, bad = [], {}, []
    with tempfile.TemporaryDirectory() as td:
        for name, elf in progs:
            if name in EXPECTED_FAIL:
                rows.append(f"| {name} | no | skipped | " + ("skipped |" if a.rtl else "")
                            + " expected failure on orbit-rv (see README)")
                print(rows[-1], flush=True)
                continue
            spike = run_spike(a.spike, elf, Path(td))
            if spike is None:
                results[name] = {"iss": False, "iss_commits": 0,
                                 "iss_msg": f"spike timed out after {SPIKE_TIMEOUT} s"}
                if MUST_MATCH.match(name):
                    bad.append(name)
                rows.append(f"| {name} | {'yes' if MUST_MATCH.match(name) else 'no'} | TIMEOUT | "
                            + ("- |" if a.rtl else "") + f" spike timed out after {SPIKE_TIMEOUT} s")
                print(rows[-1], flush=True)
                continue
            ok_i, n_i, msg_i = compare(spike, run_iss(elf))
            r = {"iss": ok_i, "iss_commits": n_i, "iss_msg": msg_i}
            if a.rtl and name.startswith("rv64"):
                # Benchmarks read mcycle and compute with it, so their RTL values legitimately
                # differ from Spike's after the first counter read; the RTL is checked against
                # the ISS (with those reads replayed) by tools/run_riscv_tests.py / perf_table.py.
                rtl, nd = run_rtl(elf)
                ok_r, n_r, msg_r = compare(spike, rtl, nd)
                r.update(rtl=ok_r, rtl_commits=n_r, rtl_msg=msg_r)
            results[name] = r
            must = bool(MUST_MATCH.match(name))
            if must and not (r["iss"] and r.get("rtl", True)):
                bad.append(name)
            rows.append(f"| {name} | {'yes' if must else 'no'} | {'match' if ok_i else 'MISMATCH'} {n_i} | "
                        + ((f"{'match' if r['rtl'] else 'MISMATCH'} {r['rtl_commits']} |" if "rtl" in r
                            else "n/a (cycle-dependent) |") if a.rtl else "")
                        + (f" {r['iss_msg'] if not ok_i else r['rtl_msg']}" if not (ok_i and r.get("rtl", True))
                           else "".join(f" {m.split(', ', 1)[1]}" for m in (msg_i, r.get("rtl_msg", ""))
                                        if "spurious" in m)[:200]))
            print(rows[-1], flush=True)
    head = "| program | must match | ISS vs Spike | " + ("RTL vs Spike |" if a.rtl else "") + " note\n"
    head += "|---|---|---|" + ("---|" if a.rtl else "") + "---\n"
    total = len(results)
    ok = sum(1 for r in results.values() if r["iss"] and r.get("rtl", True))
    a.out.write_text(encoding="utf-8", data=f"## Spike lockstep\n\n{ok}/{total} programs match.\n\n" + head + "\n".join(rows) + "\n")
    a.json.write_text(json.dumps(results, indent=1))
    print(f"{ok}/{total} programs match; must-match failures: {bad or 'none'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
