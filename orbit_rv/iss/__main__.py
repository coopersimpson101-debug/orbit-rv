"""Command line for the golden ISS.

    python -m orbit_rv.iss prog.elf                      # run until tohost / limit
    python -m orbit_rv.iss prog.elf --log commits.log    # Spike-style commit log
    python -m orbit_rv.iss kernel.elf --ram 256K --script "help\\nps\\n"   # scripted UART input
"""
from __future__ import annotations

import argparse
import sys
import time

from . import load_hart, trace_lines
from ..elf import load_elf


def parse_size(s: str) -> int:
    s = s.upper()
    mul = 1
    if s.endswith("K"):
        mul, s = 1024, s[:-1]
    elif s.endswith("M"):
        mul, s = 1 << 20, s[:-1]
    return int(s, 0) * mul


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orbit_rv.iss", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("elf")
    ap.add_argument("--max", type=int, default=20_000_000, help="instruction limit")
    ap.add_argument("--log", help="write a commit log here")
    ap.add_argument("--ram", default="16M", help="RAM size (default 16M)")
    ap.add_argument("--script", help="UART input, fed one line at a time when the hart idles "
                                     "or prints the prompt (use \\n for newlines)")
    ap.add_argument("--prompt", default="orbit> ", help="prompt that triggers the next line")
    ap.add_argument("--insns-per-tick", type=int, default=5,
                    help="instructions per 10 MHz mtime tick (5 = a 50 MHz, IPC 1 hart)")
    ap.add_argument("--quiet", action="store_true", help="do not echo the UART")
    a = ap.parse_args(argv)

    out = bytearray()

    def on_tx(b: int) -> None:
        out.append(b)
        if not a.quiet:
            sys.stdout.write(chr(b))
            sys.stdout.flush()

    h = load_hart(load_elf(a.elf), parse_size(a.ram), insns_per_tick=a.insns_per_tick,
                  on_tx=on_tx)
    if a.log:
        h.trace = []
    lines = []
    if a.script:
        lines = [ln + "\n" for ln in a.script.encode().decode("unicode_escape").split("\n")]
        if lines and lines[-1] == "\n":
            lines.pop()
    state = {"seen": 0}

    def feed(hh) -> bool:
        if not lines:
            return False
        hh.uart.feed(lines.pop(0).encode())
        return True

    def prompt_watch(hh) -> bool:
        idx = out.rfind(a.prompt.encode())
        if idx >= 0 and idx + len(a.prompt) == len(out) and len(out) > state["seen"] \
                and not hh.uart.rx:
            state["seen"] = len(out)
            if not feed(hh):
                return True  # script finished and the shell is waiting again
        return False

    h.on_idle = feed
    t0 = time.perf_counter()
    n = 0
    while not h.halted and n < a.max:
        n += h.run(min(20_000, a.max - n), until=prompt_watch if a.script is not None else None)
        if a.script is not None and not lines and out.endswith(a.prompt.encode()) \
                and len(out) == state["seen"]:
            break
        if h.wfi and not h.mip() & h.mie and not lines:
            break
    dt = time.perf_counter() - t0
    if a.log:
        with open(a.log, "w", newline="\n") as f:
            for line in trace_lines(h.trace):
                f.write(line + "\n")
    sys.stdout.flush()
    status = "halted" if h.halted else "stopped"
    code = h.exit_code if h.halted else None
    print(f"\n[iss] {status} after {h.instret} instructions ({dt:.2f} s, "
          f"{h.instret / max(dt, 1e-9) / 1e3:.0f} kIPS), tohost exit code {code}",
          file=sys.stderr)
    if h.halted:
        return 0 if code == 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
