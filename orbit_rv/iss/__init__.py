"""Golden instruction-set simulator for orbit-rv."""
from __future__ import annotations

from ..commitlog import format_commit, format_trap
from ..elf import Image, load_elf
from .hart import Hart, Trap, decode

__all__ = ["Hart", "Trap", "decode", "load_hart", "run_elf", "trace_lines"]


def load_hart(img: Image, ram_size: int | None = None, **kw) -> Hart:
    from .. import memmap as mm
    h = Hart(ram_size=ram_size or mm.RAM_SIZE_SIM, **kw)
    for addr, data in img.segments:
        h.load_image(addr, data)
    h.pc = img.entry
    h.tohost = img.tohost
    return h


def trace_lines(trace):
    for r in trace:
        if r[0] == "trap":
            yield format_trap("exception", r[1], r[2], r[3])
        elif r[0] == "irq":
            yield format_trap("interrupt", r[1], r[2])
        else:
            yield format_commit(r[0], r[1], r[2], r[3], r[4])


def run_elf(path, max_insns=5_000_000, trace=False, ram_size=None, **kw):
    """Run an ELF until it writes ``tohost`` (or ``max_insns``). Returns the hart."""
    h = load_hart(load_elf(path), ram_size, **kw)
    if trace:
        h.trace = []
    h.run(max_insns)
    return h
