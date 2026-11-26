"""Sv39 MMU: fully-associative TLBs and a hardware page-table walker.

The walker reads PTEs through the D-cache (so page tables the kernel just
wrote, still dirty in the cache, are seen correctly). Svade: PTE.A/D are
never written by hardware; A=0 (any access) or D=0 (store) is a page fault.
Permission checks (U/SUM/MXR/R/W/X/D) are done at lookup time against the
current privilege, so mode switches need no TLB flush. Leaf PTEs with A=0
are not cached.
"""
from __future__ import annotations

from amaranth import C, Cat, Elaboratable, Module, Mux, Signal

from .. import memmap as mm

ACC_FETCH, ACC_LOAD, ACC_STORE = 0, 1, 2
FAULT_NONE, FAULT_PAGE, FAULT_ACCESS = 0, 1, 2


class TLB(Elaboratable):
    def __init__(self, entries: int, name: str):
        self.n = entries
        self.name = name
        # lookup (combinational)
        self.va = Signal(64)
        self.acc = Signal(2)
        self.priv = Signal(2)       # effective privilege of the access
        self.sum = Signal()
        self.mxr = Signal()
        self.hit = Signal()
        self.pa = Signal(32)
        self.fault = Signal()       # page fault on a hit (permissions)
        # insert / flush
        self.ins = Signal()
        self.ins_vpn = Signal(27)
        self.ins_ppn = Signal(20)
        self.ins_level = Signal(2)
        self.ins_flags = Signal(8)
        self.flush = Signal()
        self.stat_miss = Signal()

    def elaborate(self, platform):
        m = Module()
        n = self.n
        valid = Signal(n)
        vpn = [Signal(27, name=f"{self.name}_vpn{i}") for i in range(n)]
        ppn = [Signal(20, name=f"{self.name}_ppn{i}") for i in range(n)]
        lvl = [Signal(2, name=f"{self.name}_lvl{i}") for i in range(n)]
        flg = [Signal(8, name=f"{self.name}_flg{i}") for i in range(n)]
        rr = Signal(range(n))

        va_vpn = self.va[12:39]
        sel_ppn = Signal(20)
        sel_lvl = Signal(2)
        sel_flg = Signal(8)
        any_hit = Signal()
        for i in range(n):
            match = Signal(name=f"{self.name}_m{i}")
            m.d.comb += match.eq(valid[i]
                                 & (vpn[i][18:27] == va_vpn[18:27])
                                 & ((lvl[i] == 2) | (vpn[i][9:18] == va_vpn[9:18]))
                                 & ((lvl[i] != 0) | (vpn[i][0:9] == va_vpn[0:9])))
            with m.If(match):
                m.d.comb += [sel_ppn.eq(ppn[i]), sel_lvl.eq(lvl[i]), sel_flg.eq(flg[i])]
            any_hit = any_hit | match
        m.d.comb += self.hit.eq(any_hit)
        # physical address: superpages take the low VPN parts from the VA
        lo = Signal(20)
        m.d.comb += lo.eq(Mux(sel_lvl == 2, Cat(self.va[12:30], sel_ppn[18:20]),
                              Mux(sel_lvl == 1, Cat(self.va[12:21], sel_ppn[9:20]), sel_ppn)))
        m.d.comb += self.pa.eq(Cat(self.va[:12], lo))
        r, w, x, u, d = sel_flg[1], sel_flg[2], sel_flg[3], sel_flg[4], sel_flg[7]
        bad = Signal()
        with m.If((self.priv == 0) & ~u):
            m.d.comb += bad.eq(1)
        with m.If((self.priv == 1) & u & ((self.acc == ACC_FETCH) | ~self.sum)):
            m.d.comb += bad.eq(1)
        with m.Switch(self.acc):
            with m.Case(ACC_FETCH):
                with m.If(~x):
                    m.d.comb += bad.eq(1)
            with m.Case(ACC_LOAD):
                with m.If(~(r | (x & self.mxr))):
                    m.d.comb += bad.eq(1)
            with m.Case(ACC_STORE):
                with m.If(~w | ~d):
                    m.d.comb += bad.eq(1)
        m.d.comb += self.fault.eq(bad)

        with m.If(self.flush):
            m.d.sync += valid.eq(0)
        with m.Elif(self.ins):
            for i in range(n):
                with m.If(rr == i):
                    m.d.sync += [vpn[i].eq(self.ins_vpn), ppn[i].eq(self.ins_ppn),
                                 lvl[i].eq(self.ins_level), flg[i].eq(self.ins_flags),
                                 valid[i].eq(1)]
            m.d.sync += rr.eq(Mux(rr == n - 1, 0, rr + 1))
        return m


class PTW(Elaboratable):
    """Sv39 walker. Requests: data side (priority) and instruction side."""

    def __init__(self, ram_size: int):
        self.ram_size = ram_size
        self.satp_ppn = Signal(44)
        self.dreq = Signal()
        self.dva = Signal(64)
        self.ireq = Signal()
        self.iva = Signal(64)
        self.busy = Signal()          # owns the D-cache port
        # D-cache port (driven while busy)
        self.c_s0 = Signal(12)
        self.c_advance = Signal()
        self.c_valid = Signal()
        self.c_pa = Signal(32)
        self.c_hit = Signal()
        self.c_rdata = Signal(64)
        # result (one-cycle pulse)
        self.done = Signal()
        self.side = Signal()          # 0 = data, 1 = instruction
        self.va = Signal(64)
        self.fault = Signal(2)
        self.leaf_ppn = Signal(20)
        self.leaf_level = Signal(2)
        self.leaf_flags = Signal(8)
        self.stat_walks = Signal()

    def elaborate(self, platform):
        m = Module()
        a = Signal(44)
        level = Signal(2)
        pte_pa = Signal(56)
        vpn_i = Signal(9)
        m.d.comb += vpn_i.eq(Mux(level == 2, self.va[30:39],
                                 Mux(level == 1, self.va[21:30], self.va[12:21])))
        m.d.comb += pte_pa.eq(Cat(C(0, 3), vpn_i, a))
        pte = self.c_rdata
        ppn = pte[10:54]
        v, r, w, x = pte[0], pte[1], pte[2], pte[3]
        in_ram = (pte_pa >= mm.RAM_BASE) & (pte_pa < mm.RAM_BASE + self.ram_size)

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.dreq | self.ireq):
                    m.d.comb += self.stat_walks.eq(1)
                    m.d.sync += [self.side.eq(~self.dreq),
                                 self.va.eq(Mux(self.dreq, self.dva, self.iva)),
                                 a.eq(self.satp_ppn), level.eq(2)]
                    m.next = "ISSUE"
            with m.State("ISSUE"):
                m.d.comb += [self.busy.eq(1), self.c_s0.eq(pte_pa[:12]), self.c_advance.eq(1)]
                with m.If(~in_ram):
                    m.d.sync += self.fault.eq(FAULT_ACCESS)
                    m.next = "DONE"
                with m.Else():
                    m.next = "WAIT"
            with m.State("WAIT"):
                m.d.comb += [self.busy.eq(1), self.c_s0.eq(pte_pa[:12]),
                             self.c_valid.eq(1), self.c_pa.eq(pte_pa[:32])]
                with m.If(self.c_hit):
                    with m.If(~v | (~r & w) | (pte[54:64] != 0)):
                        m.d.sync += self.fault.eq(FAULT_PAGE)
                        m.next = "DONE"
                    with m.Elif(r | x):  # leaf
                        mis = Mux(level == 2, ppn[:18] != 0, Mux(level == 1, ppn[:9] != 0, 0))
                        m.d.sync += [self.leaf_ppn.eq(ppn[:20]), self.leaf_level.eq(level),
                                     self.leaf_flags.eq(pte[:8])]
                        with m.If(mis | ~pte[6]):
                            m.d.sync += self.fault.eq(FAULT_PAGE)
                        with m.Elif(ppn[20:] != 0):
                            m.d.sync += self.fault.eq(FAULT_ACCESS)
                        with m.Else():
                            m.d.sync += self.fault.eq(FAULT_NONE)
                        m.next = "DONE"
                    with m.Else():  # pointer to the next level
                        with m.If((level == 0) | (pte[4] | pte[6] | pte[7])):
                            m.d.sync += self.fault.eq(FAULT_PAGE)
                            m.next = "DONE"
                        with m.Else():
                            m.d.sync += [a.eq(ppn), level.eq(level - 1)]
                            m.next = "ISSUE"
            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
        return m
