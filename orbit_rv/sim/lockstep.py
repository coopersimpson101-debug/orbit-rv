"""Commit-by-commit comparison of an RTL commit log against the golden ISS.

The ISS runs in lockstep mode: it never takes interrupts on its own; when the
RTL log shows an interrupt, the ISS takes the same one at the same point.
Values the ISS cannot know (MMIO loads, cycle/time/mip reads; tagged ``nd`` by
the RTL) are copied from the RTL after checking the instruction itself.
Everything else (PC, encoding, privilege, register writes, exception causes,
EPC and TVAL) must match exactly.
"""
from __future__ import annotations

import re

from .. import memmap as mm
from ..iss import load_hart

_C = re.compile(r"core\s+0:\s+(\d)\s+0x([0-9a-f]+)\s+\(0x([0-9a-f]+)\)(?:\s+x(\d+)\s+0x([0-9a-f]+))?(?:\s+st\s+0x([0-9a-f]+)\s+0x([0-9a-f]+))?(\s+nd)?")
_T = re.compile(r"core\s+0:\s+(exception|interrupt)\s+(\d+)\s+epc\s+0x([0-9a-f]+)\s+tval\s+0x([0-9a-f]+)")


def check(img, lines, ram_size: int = mm.RAM_SIZE_SIM) -> tuple[bool, str, int]:
    h = load_hart(img, ram_size)
    h.lockstep = True
    h.trace = []
    n = 0
    for ln, line in enumerate(lines):
        mc = _C.match(line)
        if mc:
            priv, pc, insn = int(mc.group(1)), int(mc.group(2), 16), int(mc.group(3), 16)
            rd = int(mc.group(4)) if mc.group(4) else 0
            val = int(mc.group(5), 16) if mc.group(5) else 0
            st = (int(mc.group(6), 16), int(mc.group(7), 16)) if mc.group(6) else None
            nd = bool(mc.group(8))
            if h.pc != pc or h.priv != priv:
                return False, f"line {ln}: RTL commits {priv}:{pc:#x}, ISS is at {h.priv}:{h.pc:#x}\n  {line}", n
            h.trace.clear()
            h.step()
            rec = h.trace[-1] if h.trace else None
            if rec is None or rec[0] == "trap":
                return False, f"line {ln}: RTL commits {pc:#x} but the ISS traps: {rec}\n  {line}", n
            _, ipc, iinsn, ird, ival, _mmio, ist = rec
            if iinsn != insn:
                return False, f"line {ln}: encoding RTL {insn:#010x} ISS {iinsn:#010x} at {pc:#x}", n
            if nd and rd:
                h.x[rd] = val
                ival = val
            if ird != rd or (rd and ival != val):
                return False, (f"line {ln}: at {pc:#x} ({insn:#010x}) RTL writes x{rd}={val:#x}, "
                               f"ISS writes x{ird}={ival:#x}"), n
            if st is not None or ist is not None:
                if st is None or ist is None or st[0] != ist[0] \
                        or (st[1] & ((1 << (8 * ist[1])) - 1)) != ist[2]:
                    return False, (f"line {ln}: at {pc:#x} ({insn:#010x}) RTL stores {st}, "
                                   f"ISS stores {ist and (hex(ist[0]), ist[1], hex(ist[2]))}"), n
            n += 1
            continue
        mt = _T.match(line)
        if mt:
            kind, cause, epc, tval = mt.group(1), int(mt.group(2)), int(mt.group(3), 16), int(mt.group(4), 16)
            if h.pc != epc:
                return False, f"line {ln}: RTL {kind} at {epc:#x}, ISS is at {h.pc:#x}", n
            if kind == "interrupt":
                h.take_trap(cause, 0, True)
            else:
                h.trace.clear()
                h.step()
                rec = h.trace[-1] if h.trace else None
                if rec is None or rec[0] != "trap" or rec[1] != cause or rec[3] != tval:
                    return False, (f"line {ln}: RTL exception cause {cause} tval {tval:#x} at {epc:#x}; "
                                   f"ISS: {rec}"), n
            n += 1
    return True, f"{n} commits/traps match", n
