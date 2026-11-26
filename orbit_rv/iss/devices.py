"""Device models for the golden ISS: CLINT, PLIC, 16550 UART subset.

They follow the same register semantics as the RTL peripherals in
``orbit_rv/rtl/periph.py`` (see docs/memory-map.md).
"""
from __future__ import annotations

from collections import deque

from .. import memmap as mm


class Clint:
    """msip / mtimecmp / mtime. ``mtime`` is derived from the hart's
    instruction count (``ticks_per_insn``) so ISS runs are deterministic."""

    def __init__(self, insns_per_tick: int = 5):
        self.msip = 0
        self.mtimecmp = (1 << 64) - 1
        self.mtime_base = 0
        self.insns_per_tick = insns_per_tick
        self.instret_ref = 0       # set by the hart before each access
        self.instret_base = 0

    def mtime(self) -> int:
        return (self.mtime_base + (self.instret_ref - self.instret_base) // self.insns_per_tick) & (
            (1 << 64) - 1)

    def set_mtime(self, v: int) -> None:
        self.mtime_base = v
        self.instret_base = self.instret_ref

    def instret_for_mtime(self, t: int) -> int:
        """Smallest instret at which mtime >= t."""
        if t <= self.mtime_base:
            return self.instret_base
        return self.instret_base + (t - self.mtime_base) * self.insns_per_tick

    def read(self, off: int, size: int) -> int:
        if off < 0x4000:
            return self.msip if off == 0 else 0
        if mm.CLINT_MTIMECMP <= off < mm.CLINT_MTIMECMP + 8:
            return (self.mtimecmp >> ((off - mm.CLINT_MTIMECMP) * 8)) & ((1 << (size * 8)) - 1)
        if mm.CLINT_MTIME <= off < mm.CLINT_MTIME + 8:
            return (self.mtime() >> ((off - mm.CLINT_MTIME) * 8)) & ((1 << (size * 8)) - 1)
        return 0

    @staticmethod
    def _merge(old: int, off: int, size: int, val: int) -> int:
        sh = off * 8
        m = ((1 << (size * 8)) - 1) << sh
        return (old & ~m) | ((val << sh) & m)

    def write(self, off: int, size: int, val: int) -> None:
        if off == 0:
            self.msip = val & 1
        elif mm.CLINT_MTIMECMP <= off < mm.CLINT_MTIMECMP + 8:
            self.mtimecmp = self._merge(self.mtimecmp, off - mm.CLINT_MTIMECMP, size, val)
        elif mm.CLINT_MTIME <= off < mm.CLINT_MTIME + 8:
            self.set_mtime(self._merge(self.mtime(), off - mm.CLINT_MTIME, size, val))


class Plic:
    def __init__(self):
        n = mm.PLIC_NSRC
        self.prio = [0] * n
        self.pending = 0
        self.inservice = 0
        self.enable = [0] * mm.PLIC_NCTX
        self.threshold = [0] * mm.PLIC_NCTX
        self.levels = 0  # source levels, bit i = source i

    def update(self, levels: int) -> None:
        self.levels = levels & ~1
        self.pending |= self.levels & ~self.inservice

    def _best(self, ctx: int) -> int:
        best, bp = 0, self.threshold[ctx]
        cand = self.pending & self.enable[ctx]
        i = 1
        while cand >> i:
            if (cand >> i) & 1 and self.prio[i] > bp:
                best, bp = i, self.prio[i]
            i += 1
        return best

    def irq(self, ctx: int) -> bool:
        return self._best(ctx) != 0

    def read(self, off: int, size: int) -> int:
        if off < 0x1000:
            i = off >> 2
            return self.prio[i] if i < mm.PLIC_NSRC else 0
        if off == mm.PLIC_PENDING:
            return self.pending & 0xFFFF_FFFF
        if mm.PLIC_ENABLE <= off < mm.PLIC_ENABLE + 0x80 * mm.PLIC_NCTX:
            ctx, r = divmod(off - mm.PLIC_ENABLE, 0x80)
            return self.enable[ctx] if r == 0 else 0
        if mm.PLIC_CONTEXT <= off < mm.PLIC_CONTEXT + 0x1000 * mm.PLIC_NCTX:
            ctx, r = divmod(off - mm.PLIC_CONTEXT, 0x1000)
            if r == 0:
                return self.threshold[ctx]
            if r == 4:
                s = self._best(ctx)
                if s:
                    self.pending &= ~(1 << s)
                    self.inservice |= 1 << s
                return s
        return 0

    def write(self, off: int, size: int, val: int) -> None:
        val &= 0xFFFF_FFFF
        if off < 0x1000:
            i = off >> 2
            if 0 < i < mm.PLIC_NSRC:
                self.prio[i] = val & 7
        elif mm.PLIC_ENABLE <= off < mm.PLIC_ENABLE + 0x80 * mm.PLIC_NCTX:
            ctx, r = divmod(off - mm.PLIC_ENABLE, 0x80)
            if r == 0:
                self.enable[ctx] = val & ~1
        elif mm.PLIC_CONTEXT <= off < mm.PLIC_CONTEXT + 0x1000 * mm.PLIC_NCTX:
            ctx, r = divmod(off - mm.PLIC_CONTEXT, 0x1000)
            if r == 0:
                self.threshold[ctx] = val & 7
            elif r == 4 and 0 < val < mm.PLIC_NSRC:
                self.inservice &= ~(1 << val)
                self.pending |= self.levels & (1 << val)


class Uart:
    """16550 subset: RBR/THR, IER, IIR/FCR, LCR (DLAB), MCR, LSR, MSR, SCR, DLL/DLM.
    Transmission is instantaneous in the ISS."""

    def __init__(self, on_tx=None):
        self.rx: deque[int] = deque()
        self.tx = bytearray()
        self.ier = 0
        self.lcr = 0
        self.mcr = 0
        self.scr = 0
        self.dll = 0
        self.dlm = 0
        self.fcr = 0
        self.on_tx = on_tx

    def feed(self, data: bytes) -> None:
        self.rx.extend(data)

    def irq(self) -> bool:
        return bool((self.ier & 1) and self.rx) or bool(self.ier & 2)

    def read(self, off: int, size: int) -> int:
        dlab = self.lcr & 0x80
        if off == 0:
            if dlab:
                return self.dll
            return self.rx.popleft() if self.rx else 0
        if off == 1:
            return self.dlm if dlab else self.ier
        if off == 2:
            if (self.ier & 1) and self.rx:
                iid = 0x04
            elif self.ier & 2:
                iid = 0x02
            else:
                iid = 0x01
            return iid | (0xC0 if self.fcr & 1 else 0)
        if off == 3:
            return self.lcr
        if off == 4:
            return self.mcr
        if off == 5:
            return 0x60 | (1 if self.rx else 0)
        if off == 6:
            return 0
        if off == 7:
            return self.scr
        return 0

    def write(self, off: int, size: int, val: int) -> None:
        val &= 0xFF
        dlab = self.lcr & 0x80
        if off == 0:
            if dlab:
                self.dll = val
            else:
                self.tx.append(val)
                if self.on_tx:
                    self.on_tx(val)
        elif off == 1:
            if dlab:
                self.dlm = val
            else:
                self.ier = val & 0x0F
        elif off == 2:
            self.fcr = val & 0xC9
            if val & 2:
                self.rx.clear()
        elif off == 3:
            self.lcr = val
        elif off == 4:
            self.mcr = val & 0x1F
        elif off == 7:
            self.scr = val
