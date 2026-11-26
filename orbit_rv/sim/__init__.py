"""Fast RTL simulation: Amaranth -> RTLIL -> Yosys CXXRTL -> C++ (zig c++).

The SoC RTL is converted once per source change into a native simulator
(``build/sim/tb-<hash>.exe``); ``run_rtl`` runs a program image on it and
returns the stop reason, exit code, performance counters, UART output and
(optionally) the commit log, which ``lockstep`` checks against the golden ISS.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import memmap as mm
from ..elf import load_elf

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(os.environ.get("ORBIT_RV_BUILD", ROOT / "build")) / "sim"
RTL_DIR = Path(__file__).resolve().parents[1] / "rtl"
TB = Path(__file__).resolve().parent / "tb.cc"


def _src_hash(ram_size: int, extra: str = "") -> str:
    h = hashlib.sha256()
    for p in sorted(RTL_DIR.glob("*.py")) + [TB, Path(mm.__file__)]:
        h.update(p.read_bytes())
    h.update(f"{ram_size}{extra}".encode())
    return h.hexdigest()[:12]


def yosys(args: list[str]) -> None:
    from yowasp_yosys import run_yosys
    rc = run_yosys(args)
    if rc:
        raise RuntimeError(f"yosys failed ({rc})")


def cxxrtl_include() -> str:
    import yowasp_yosys
    return str(Path(yowasp_yosys.__file__).parent / "share" / "include" / "backends" / "cxxrtl" / "runtime")


def build_sim(ram_size: int = mm.RAM_SIZE_SIM, verbose: bool = False, **soc_kw) -> Path:
    """Build (or reuse) the CXXRTL simulator binary for this RTL."""
    from amaranth.back import rtlil

    from ..rtl.soc import SoC
    key = _src_hash(ram_size, json.dumps(soc_kw, sort_keys=True))
    exe = BUILD / f"tb-{key}{'.exe' if os.name == 'nt' else ''}"
    if exe.exists():
        return exe
    BUILD.mkdir(parents=True, exist_ok=True)
    work = BUILD / f"work-{key}"
    work.mkdir(exist_ok=True)
    soc = SoC(ram_size=ram_size, external_ram=True, **soc_kw)
    il = work / "soc.il"
    il.write_text(rtlil.convert(soc, name="top", ports=soc.ports()), newline="\n")
    cwd = os.getcwd()
    os.chdir(work)
    try:
        yosys(["-q", "-p", "read_rtlil soc.il; hierarchy -top top; proc; flatten; opt_clean; "
                           "write_cxxrtl soc.cc"])
    finally:
        os.chdir(cwd)
    tb = work / "tb.cc"
    tb.write_bytes(TB.read_bytes())
    cmd = [sys.executable, "-m", "ziglang", "c++", "-std=c++17", "-O2", "-fbracket-depth=4096",
           f"-I{cxxrtl_include()}", f"-I{work}", str(tb), "-o", str(exe)]
    if verbose:
        print(" ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("C++ build of the CXXRTL simulator failed:\n" + r.stderr[-4000:])
    return exe


def run_rtl(img, ram_size: int = mm.RAM_SIZE_SIM, max_cycles: int = 10_000_000, log: bool = False,
            script: str | None = None, prompt: str = "orbit> ", tohost: int | None = None,
            keep: Path | None = None, **soc_kw) -> dict:
    """Run a loaded ELF ``Image`` on the RTL. Returns a dict with counters and outputs."""
    exe = build_sim(ram_size, **soc_kw)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "img.bin").write_bytes(img.flat(mm.RAM_BASE))
        args = [str(exe), "--image", str(td / "img.bin"), "--ram-size", str(ram_size),
                "--max-cycles", str(max_cycles), "--uart", str(td / "uart.txt"),
                "--perf", str(td / "perf.json"), "--prompt", prompt]
        if tohost is None:
            tohost = img.tohost
        if tohost:
            args += ["--tohost", str(tohost)]
        args += ["--entry", str(img.entry)]
        if log:
            args += ["--log", str(td / "commits.log")]
        if script is not None:
            (td / "script.txt").write_bytes(script.encode())
            args += ["--script", str(td / "script.txt")]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"simulator failed: {r.stderr}")
        res = json.loads((td / "perf.json").read_text())
        res["uart"] = (td / "uart.txt").read_bytes().decode("latin-1")
        if log:
            res["log"] = (td / "commits.log").read_text().splitlines()
        if keep:
            keep.mkdir(parents=True, exist_ok=True)
            for f in td.iterdir():
                (keep / f.name).write_bytes(f.read_bytes())
    return res


def run_rtl_elf(path, max_cycles: int = 4_000_000, lockstep: bool = False, **kw) -> dict:
    img = load_elf(path)
    res = run_rtl(img, max_cycles=max_cycles, log=lockstep, **kw)
    res["pass"] = res["stop"] in ("tohost", "finisher") and res["exit_code"] == 0
    if lockstep:
        from .lockstep import check
        ok, msg, n = check(img, res.pop("log"), ram_size=kw.get("ram_size", mm.RAM_SIZE_SIM))
        res["lockstep"] = ok
        res["lockstep_msg"] = msg
        res["lockstep_commits"] = n
        res["pass"] = res["pass"] and ok
    else:
        res.pop("log", None)
    res.pop("uart", None) if not kw.get("script") else None
    return res
