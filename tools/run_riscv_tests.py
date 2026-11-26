#!/usr/bin/env python3
"""Run built riscv-tests on the golden ISS and/or the RTL (CXXRTL) and tabulate.

    python tools/run_riscv_tests.py --dir build/riscv-tests --sim iss
    python tools/run_riscv_tests.py --dir build/riscv-tests --sim rtl --lockstep --json results.json

A test passes when it writes ``tohost = 1`` (exit code 0). With ``--lockstep``
every RTL commit is also checked against the ISS, instruction by instruction.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import functools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Expected failures, by design (see README): misaligned accesses trap instead of
# being handled in hardware, and there are no PMP entries.
KNOWN_FAIL = ["rv64ui-p-ma_data", "rv64ui-v-ma_data", "rv64mi-p-pmpaddr"]
SUITES = ["rv64ui-p", "rv64ui-v", "rv64um-p", "rv64um-v", "rv64ua-p", "rv64ua-v", "rv64mi-p", "rv64si-p"]


def run_iss(path: str) -> dict:
    from orbit_rv.iss import run_elf
    t = time.perf_counter()
    h = run_elf(path, max_insns=3_000_000)
    return {"pass": bool(h.halted and h.exit_code == 0), "insns": h.instret,
            "code": h.exit_code, "secs": round(time.perf_counter() - t, 3)}


def run_rtl(path: str, lockstep: bool) -> dict:
    from orbit_rv.sim import run_rtl_elf
    t = time.perf_counter()
    r = run_rtl_elf(path, max_cycles=4_000_000, lockstep=lockstep)
    r["secs"] = round(time.perf_counter() - t, 3)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--sim", choices=["iss", "rtl"], default="iss")
    ap.add_argument("--lockstep", action="store_true", help="RTL: check every commit against the ISS")
    ap.add_argument("--filter", default="", help="substring filter on test names")
    ap.add_argument("--json", type=Path, help="write per-test results here")
    ap.add_argument("-j", type=int, default=6)
    ap.add_argument("--allow-fail", nargs="*", default=KNOWN_FAIL,
                    help="tests expected to fail (exit status stays 0 if only these fail)")
    a = ap.parse_args(argv)

    tests = sorted(p for p in a.dir.iterdir()
                   if p.is_file() and p.name.startswith("rv64") and "." not in p.name
                   and a.filter in p.name)
    fn = run_iss if a.sim == "iss" else functools.partial(run_rtl, lockstep=a.lockstep)
    results = {}
    with cf.ProcessPoolExecutor(a.j) as ex:
        futs = {ex.submit(fn, str(p)): p.name for p in tests}
        for f in cf.as_completed(futs):
            name = futs[f]
            try:
                results[name] = f.result()
            except Exception as e:  # noqa: BLE001 - report and keep going
                results[name] = {"pass": False, "error": repr(e)}
    print(f"{'suite':<10} {'pass':>5} {'total':>6}  failing")
    tot_p = tot = 0
    for suite in SUITES:
        names = sorted(n for n in results if n.startswith(suite + "-"))
        if not names:
            continue
        ok = [n for n in names if results[n]["pass"]]
        bad = [n[len(suite) + 1:] for n in names if not results[n]["pass"]]
        tot_p += len(ok)
        tot += len(names)
        print(f"{suite:<10} {len(ok):>5} {len(names):>6}  {' '.join(bad)}")
    print(f"{'total':<10} {tot_p:>5} {tot:>6}")
    if a.json:
        a.json.write_text(json.dumps(results, indent=1, sort_keys=True))
    failing = {n for n, r in results.items() if not r["pass"]}
    unexpected = sorted(failing - set(a.allow_fail))
    if unexpected:
        print(f"unexpected failures: {' '.join(unexpected)}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
