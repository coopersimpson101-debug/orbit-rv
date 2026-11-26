"""Full-SoC RTL simulation (CXXRTL) checked commit by commit against the ISS.

Needs yowasp-yosys + ziglang (to build the simulator, ~5 min the first time;
cached in build/sim) and prebuilt programs:

    ORBIT_RV_RISCV_TESTS=build/riscv-tests  (tools/build_riscv_tests.py)
    ORBIT_RV_BENCH=build/bench              (tools/build_benchmarks.py)
    ORBIT_OS_KERNEL=.../orbit-os/build/rv64/kernel.elf   (optional OS boot)

Tests whose inputs are missing are skipped with the reason.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("yowasp_yosys")
pytest.importorskip("ziglang")

from orbit_rv.elf import load_elf  # noqa: E402
from orbit_rv.sim import run_rtl, run_rtl_elf  # noqa: E402
from orbit_rv.sim.lockstep import check  # noqa: E402

RT = os.environ.get("ORBIT_RV_RISCV_TESTS")
BENCH = os.environ.get("ORBIT_RV_BENCH")
KERNEL = os.environ.get("ORBIT_OS_KERNEL")

SAMPLE = ["rv64ui-p-add", "rv64ui-p-ld", "rv64ui-p-fence_i", "rv64um-p-divw", "rv64ua-p-lrsc",
          "rv64mi-p-csr", "rv64mi-p-illegal", "rv64si-p-dirty", "rv64si-p-wfi", "rv64ui-v-sd",
          "rv64ua-v-amoadd_d"]


@pytest.mark.skipif(not RT, reason="ORBIT_RV_RISCV_TESTS not set (build with tools/build_riscv_tests.py)")
@pytest.mark.parametrize("name", SAMPLE)
def test_riscv_test_lockstep(name):
    path = Path(RT) / name
    if not path.exists():
        pytest.skip(f"{name} not built")
    r = run_rtl_elf(path, lockstep=True)
    assert r["stop"] == "tohost" and r["exit_code"] == 0, r
    assert r["lockstep"], r["lockstep_msg"]


@pytest.mark.skipif(not BENCH, reason="ORBIT_RV_BENCH not set (build with tools/build_benchmarks.py)")
@pytest.mark.parametrize("name", ["orbitmark", "irqtest", "towers"])
def test_benchmark_lockstep(name):
    path = Path(BENCH) / f"{name}.riscv"
    if not path.exists():
        pytest.skip(f"{name} not built")
    r = run_rtl_elf(path, max_cycles=5_000_000, lockstep=True)
    assert r["pass"], r
    assert r["instret"] > 0 and r["cycles"] > r["instret"] / 2


@pytest.mark.skipif(not KERNEL, reason="ORBIT_OS_KERNEL not set (orbit-os: python build.py --board rv64)")
def test_orbit_os_boots_on_rtl():
    img = load_elf(KERNEL)
    r = run_rtl(img, ram_size=256 * 1024, max_cycles=60_000_000, log=True, script="help\nps\npoweroff\n")
    assert "OrbitOS shell" in r["uart"] and "SBI SRST shutdown" in r["uart"], r["uart"][-2000:]
    assert r["stop"] == "finisher" and r["exit_code"] == 0
    ok, msg, _ = check(img, r["log"], ram_size=256 * 1024)
    assert ok, msg
