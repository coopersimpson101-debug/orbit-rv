"""OBus: the SoC's simple request/acknowledge bus (Wishbone-classic-like).

A master raises ``cyc`` with ``adr`` (byte address, 8-byte aligned), ``we``,
``sel`` (byte enables) and ``dat_w`` and holds them until the slave pulses
``ack`` for one cycle (with ``dat_r`` for reads). Slaves act on the first
cycle of a request (``cyc & ~ack``), so side effects happen exactly once.
A line refill is four back-to-back single-beat requests; the arbiter keeps
the grant while the master holds ``cyc``.
"""
from __future__ import annotations

from amaranth import Elaboratable, Module, Mux, Signal


class Bus:
    def __init__(self, name: str):
        self.cyc = Signal(name=f"{name}_cyc")
        self.we = Signal(name=f"{name}_we")
        self.adr = Signal(32, name=f"{name}_adr")
        self.dat_w = Signal(64, name=f"{name}_dat_w")
        self.sel = Signal(8, name=f"{name}_sel")
        self.ack = Signal(name=f"{name}_ack")
        self.dat_r = Signal(64, name=f"{name}_dat_r")

    def connect_master_to(self, m: Module, slave: "Bus") -> None:
        m.d.comb += [slave.cyc.eq(self.cyc), slave.we.eq(self.we), slave.adr.eq(self.adr),
                     slave.dat_w.eq(self.dat_w), slave.sel.eq(self.sel),
                     self.ack.eq(slave.ack), self.dat_r.eq(slave.dat_r)]


class Arbiter(Elaboratable):
    """Two masters (0 = data side, priority; 1 = instruction side) onto one bus."""

    def __init__(self):
        self.m0 = Bus("arb_m0")
        self.m1 = Bus("arb_m1")
        self.out = Bus("arb_out")

    def elaborate(self, platform):
        m = Module()
        owner = Signal(2)  # 0 = none, 1 = m0, 2 = m1
        cur = Signal(2)
        m.d.comb += cur.eq(owner)
        with m.If(owner == 0):
            m.d.comb += cur.eq(Mux(self.m0.cyc, 1, Mux(self.m1.cyc, 2, 0)))
        with m.Elif((owner == 1) & ~self.m0.cyc):
            m.d.comb += cur.eq(Mux(self.m1.cyc, 2, 0))
        with m.Elif((owner == 2) & ~self.m1.cyc):
            m.d.comb += cur.eq(Mux(self.m0.cyc, 1, 0))
        m.d.sync += owner.eq(cur)
        for i, mst in ((1, self.m0), (2, self.m1)):
            with m.If(cur == i):
                m.d.comb += [self.out.cyc.eq(mst.cyc), self.out.we.eq(mst.we),
                             self.out.adr.eq(mst.adr), self.out.dat_w.eq(mst.dat_w),
                             self.out.sel.eq(mst.sel), mst.ack.eq(self.out.ack)]
            m.d.comb += mst.dat_r.eq(self.out.dat_r)
        return m


class Decoder(Elaboratable):
    """Route one master to slaves by address range; unmapped addresses ack with 0."""

    def __init__(self, regions):
        self.bus = Bus("dec")
        self.regions = regions  # list of (base, size, Bus)

    def elaborate(self, platform):
        m = Module()
        conds = []
        for base, size, slave in self.regions:
            hit = Signal(name=f"dec_hit_{base:x}")
            m.d.comb += hit.eq((self.bus.adr >= base) & (self.bus.adr < base + size))
            conds.append(hit)
            m.d.comb += [slave.cyc.eq(self.bus.cyc & hit), slave.we.eq(self.bus.we),
                         slave.adr.eq(self.bus.adr - base), slave.dat_w.eq(self.bus.dat_w),
                         slave.sel.eq(self.bus.sel)]
            with m.If(hit):
                m.d.comb += [self.bus.ack.eq(slave.ack), self.bus.dat_r.eq(slave.dat_r)]
        any_hit = conds[0]
        for c in conds[1:]:
            any_hit = any_hit | c
        dflt_ack = Signal()
        m.d.sync += dflt_ack.eq(self.bus.cyc & ~any_hit & ~dflt_ack)
        with m.If(~any_hit):
            m.d.comb += self.bus.ack.eq(dflt_ack)
        return m
