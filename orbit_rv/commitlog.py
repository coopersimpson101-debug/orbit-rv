"""Commit logs: one format for the ISS, the RTL and Spike.

Lines follow Spike's ``--log-commits`` layout so a single parser handles all
three sources::

    core   0: 3 0x0000000080000004 (0x00000297) x5  0x0000000080000004

The ISS and RTL additionally emit ``core   0: exception <cause> epc .. tval ..``
and ``core   0: interrupt <cause> epc ..`` lines, which the comparison skips
(Spike's own trap lines have a different spelling and are skipped too).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_COMMIT = re.compile(r"core\s+\d+:\s+(\d)\s+0x([0-9a-fA-F]+)\s+\(0x([0-9a-fA-F]+)\)(.*)")
_XREG = re.compile(r"\bx(\d+)\s+0x([0-9a-fA-F]+)")


@dataclass
class Commit:
    priv: int
    pc: int
    insn: int
    writes: dict = field(default_factory=dict)  # xN -> value (x0 dropped)

    def __str__(self) -> str:
        s = f"{self.priv} {self.pc:#018x} ({self.insn:#010x})"
        for r, v in sorted(self.writes.items()):
            s += f" x{r:<2d} {v:#018x}"
        return s


def format_commit(priv: int, pc: int, insn: int, rd: int, val: int) -> str:
    s = f"core   0: {priv} 0x{pc:016x} (0x{insn:08x})"
    if rd:
        s += f" x{rd:<2d} 0x{val:016x}"
    return s


def format_trap(kind: str, cause: int, epc: int, tval: int = 0) -> str:
    return f"core   0: {kind} {cause} epc 0x{epc:016x} tval 0x{tval:016x}"


def parse_lines(lines, start_pc: int | None = None) -> list[Commit]:
    """Parse commit lines. Everything before the first commit at ``start_pc``
    (Spike's boot ROM) is dropped."""
    out: list[Commit] = []
    started = start_pc is None
    for line in lines:
        m = _COMMIT.search(line)
        if not m:
            continue
        pc = int(m.group(2), 16)
        if not started:
            if pc != start_pc:
                continue
            started = True
        writes = {}
        for r, v in _XREG.findall(m.group(4)):
            if int(r):
                writes[int(r)] = int(v, 16)
        out.append(Commit(int(m.group(1)), pc, int(m.group(3), 16), writes))
    return out


def parse_file(path, start_pc: int | None = None) -> list[Commit]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_lines(f, start_pc)


@dataclass
class CompareResult:
    ok: bool
    compared: int
    message: str


def compare(ref: list[Commit], dut: list[Commit], ref_name="ref", dut_name="dut",
            allow_tail: int = 0) -> CompareResult:
    """Instruction-by-instruction comparison of PC, encoding, privilege and
    integer-register writes. ``allow_tail``: tolerate up to this many extra
    trailing commits in either log (the two simulators stop at slightly
    different points after the tohost write)."""
    n = min(len(ref), len(dut))
    for i in range(n):
        a, b = ref[i], dut[i]
        if (a.pc, a.insn, a.priv, a.writes) != (b.pc, b.insn, b.priv, b.writes):
            ctx = "\n".join(f"    {ref_name}[{j}] {ref[j]}" for j in range(max(0, i - 3), i))
            return CompareResult(False, i, (
                f"mismatch at commit {i}:\n{ctx}\n  {ref_name}: {a}\n  {dut_name}: {b}"))
    if abs(len(ref) - len(dut)) > allow_tail:
        return CompareResult(False, n, f"length differs: {ref_name}={len(ref)} {dut_name}={len(dut)}")
    return CompareResult(True, n, f"{n} commits match")
