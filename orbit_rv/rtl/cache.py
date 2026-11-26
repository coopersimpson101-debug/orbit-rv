"""Instruction and data caches: direct-mapped, 32-byte lines, block-RAM arrays.

Both caches are virtually indexed, physically tagged (VIPT): the index comes
from address bits [11:3] (inside the 4 KiB page offset), so the block RAM
read can start a cycle early (stage "s0", from the previous pipeline stage)
while the TLB translates; the physical tag is compared in "s1".

Every cycle the RAM read address is either the next request (``s0_addr``,
when ``advance``) or the current one (``s1_pa``) again. A request only hits
if the RAM output really belongs to it (``r_ok``: last cycle's read address
equals the s1 address), which makes stalls, replays after refills and
ownership changes (page-table walker) safe without special cases.

The D-cache is write-back/write-allocate with per-line dirty bits; FENCE.I
writes every dirty line back (``flush``). Valid/dirty bits live in flip-flops
so whole-cache invalidation takes one cycle.
"""
from __future__ import annotations

from amaranth import C, Cat, Elaboratable, Module, Mux, Signal
from amaranth.lib import memory

from .bus import Bus

LINE_BYTES = 32
WORDS = LINE_BYTES // 8


class _CacheBase(Elaboratable):
    def __init__(self, size: int, name: str):
        self.size = size
        self.lines = size // LINE_BYTES
        self.ibits = (self.lines - 1).bit_length()       # line index bits
        self.wbits = self.ibits + 2                        # word index bits
        assert size <= 4096, "VIPT without aliasing needs <= 4 KiB per way"
        self.tag_lo = 5 + self.ibits
        self.tbits = 32 - self.tag_lo
        self.bus = Bus(f"{name}_bus")
        # request interface
        self.s0_addr = Signal(12)     # next request's index bits (VA == PA here)
        self.advance = Signal()       # s1 takes the s0 request next cycle
        self.s1_valid = Signal()
        self.s1_pa = Signal(32)
        self.s1_kill = Signal()       # do not start a refill for this request
        self.s1_hit = Signal()
        self.s1_rdata = Signal(64)
        self.idle = Signal()
        # statistics
        self.stat_access = Signal()
        self.stat_miss = Signal()


class ICache(_CacheBase):
    def __init__(self, size: int = 4096):
        super().__init__(size, "ic")
        self.invalidate = Signal()    # request; performed when idle
        self.inv_busy = Signal()

    def elaborate(self, platform):
        m = Module()
        m.submodules.data = data = memory.Memory(shape=64, depth=self.lines * WORDS, init=[])
        m.submodules.tags = tags = memory.Memory(shape=self.tbits, depth=self.lines, init=[])
        dwp = data.write_port()
        twp = tags.write_port()
        drp = data.read_port(transparent_for=(dwp,))
        trp = tags.read_port(transparent_for=(twp,))
        valid = Signal(self.lines)
        inv_pend = Signal()
        m.d.comb += self.inv_busy.eq(inv_pend | self.invalidate)

        s1_word = self.s1_pa[3:3 + self.wbits]
        s1_line = self.s1_pa[5:5 + self.ibits]
        s1_tag = self.s1_pa[self.tag_lo:]
        rd_addr = Signal(self.wbits)
        r_addr = Signal(self.wbits)
        r_ok = Signal()
        m.d.sync += r_addr.eq(rd_addr)
        m.d.comb += r_ok.eq(r_addr == s1_word)

        hit = Signal()
        m.d.comb += hit.eq(self.s1_valid & r_ok & valid.bit_select(s1_line, 1)
                           & (trp.data == s1_tag) & ~inv_pend)
        m.d.comb += [self.s1_hit.eq(hit), self.s1_rdata.eq(drp.data)]

        beat = Signal(2)
        miss_pa = Signal(32)
        with m.If(self.invalidate):
            m.d.sync += inv_pend.eq(1)
        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += [self.idle.eq(1),
                             rd_addr.eq(Mux(self.advance, self.s0_addr[3:3 + self.wbits], s1_word))]
                with m.If(inv_pend):
                    m.d.sync += [valid.eq(0), inv_pend.eq(0)]
                with m.Elif(self.s1_valid & r_ok & ~hit & ~self.s1_kill):
                    m.d.sync += [miss_pa.eq(Cat(C(0, 5), self.s1_pa[5:])), beat.eq(0)]
                    m.d.comb += self.stat_miss.eq(1)
                    m.next = "REFILL"
            with m.State("REFILL"):
                m.d.comb += [rd_addr.eq(s1_word),
                             self.bus.cyc.eq(1), self.bus.sel.eq(0xFF),
                             self.bus.adr.eq(miss_pa | (beat << 3))]
                with m.If(self.bus.ack):
                    m.d.comb += [dwp.en.eq(1), dwp.addr.eq(Cat(beat, miss_pa[5:5 + self.ibits])),
                                 dwp.data.eq(self.bus.dat_r)]
                    m.d.sync += beat.eq(beat + 1)
                    with m.If(beat == WORDS - 1):
                        line = miss_pa[5:5 + self.ibits]
                        m.d.comb += [twp.en.eq(1), twp.addr.eq(line),
                                     twp.data.eq(miss_pa[self.tag_lo:])]
                        m.d.sync += valid.eq(valid | (1 << line))
                        m.next = "IDLE"
        m.d.comb += [drp.addr.eq(rd_addr), trp.addr.eq(rd_addr[2:])]
        m.d.comb += self.stat_access.eq(self.s1_valid & r_ok & self.advance & hit)
        return m


class DCache(_CacheBase):
    def __init__(self, size: int = 4096):
        super().__init__(size, "dc")
        self.s1_write = Signal()      # write s1_wdata/s1_wmask this cycle (only acts on a hit)
        self.s1_wdata = Signal(64)
        self.s1_wmask = Signal(8)
        self.flush = Signal()         # level: write back all dirty lines
        self.flush_done = Signal()

    def elaborate(self, platform):
        m = Module()
        m.submodules.data = data = memory.Memory(shape=64, depth=self.lines * WORDS, init=[])
        m.submodules.tags = tags = memory.Memory(shape=self.tbits, depth=self.lines, init=[])
        dwp = data.write_port(granularity=8)
        twp = tags.write_port()
        drp = data.read_port(transparent_for=(dwp,))
        trp = tags.read_port(transparent_for=(twp,))
        valid = Signal(self.lines)
        dirty = Signal(self.lines)

        s1_word = self.s1_pa[3:3 + self.wbits]
        s1_line = self.s1_pa[5:5 + self.ibits]
        s1_tag = self.s1_pa[self.tag_lo:]
        rd_addr = Signal(self.wbits)
        r_addr = Signal(self.wbits)
        r_lookup = Signal()
        is_lookup = Signal()
        r_ok = Signal()
        m.d.sync += [r_addr.eq(rd_addr), r_lookup.eq(is_lookup)]
        m.d.comb += r_ok.eq(r_lookup & (r_addr == s1_word))

        hit = Signal()
        m.d.comb += hit.eq(self.s1_valid & r_ok & valid.bit_select(s1_line, 1)
                           & (trp.data == s1_tag))
        m.d.comb += [self.s1_hit.eq(hit & self.idle), self.s1_rdata.eq(drp.data)]

        beat = Signal(2)
        miss_pa = Signal(32)
        wb_line = Signal(self.ibits)
        wb_tag = Signal(self.tbits)
        wb_data = Signal(64)
        after_wb = Signal()  # 0: refill afterwards, 1: continue flushing
        fl_line = Signal(self.ibits + 1)

        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += [self.idle.eq(1), is_lookup.eq(1),
                             rd_addr.eq(Mux(self.advance, self.s0_addr[3:3 + self.wbits], s1_word))]
                with m.If(hit & self.s1_write & ~self.s1_kill):
                    m.d.comb += [dwp.en.eq(self.s1_wmask), dwp.addr.eq(s1_word),
                                 dwp.data.eq(self.s1_wdata)]
                    m.d.sync += dirty.eq(dirty | (1 << s1_line))
                with m.If(self.flush):
                    m.d.sync += fl_line.eq(0)
                    m.next = "FLUSH"
                with m.Elif(self.s1_valid & r_ok & ~hit & ~self.s1_kill):
                    m.d.comb += self.stat_miss.eq(1)
                    m.d.sync += [miss_pa.eq(Cat(C(0, 5), self.s1_pa[5:])), beat.eq(0),
                                 wb_line.eq(s1_line), wb_tag.eq(trp.data), after_wb.eq(0)]
                    with m.If(valid.bit_select(s1_line, 1) & dirty.bit_select(s1_line, 1)):
                        m.next = "WB_RD"
                    with m.Else():
                        m.next = "REFILL"
            with m.State("FLUSH"):
                # look at line fl_line: read its tag, write it back if dirty
                m.d.comb += rd_addr.eq(Cat(C(0, 2), fl_line[:self.ibits]))
                with m.If(fl_line[self.ibits]):
                    m.d.comb += self.flush_done.eq(1)
                    with m.If(~self.flush):
                        m.next = "IDLE"
                with m.Elif(valid.bit_select(fl_line[:self.ibits], 1)
                            & dirty.bit_select(fl_line[:self.ibits], 1)):
                    m.d.sync += [wb_line.eq(fl_line[:self.ibits]), beat.eq(0), after_wb.eq(1)]
                    m.next = "FLUSH_TAG"
                with m.Else():
                    m.d.sync += fl_line.eq(fl_line + 1)
            with m.State("FLUSH_TAG"):
                m.d.comb += rd_addr.eq(Cat(C(0, 2), wb_line))
                m.d.sync += wb_tag.eq(trp.data)
                m.next = "WB_RD"
            with m.State("WB_RD"):
                m.d.comb += rd_addr.eq(Cat(beat, wb_line))
                m.next = "WB_LATCH"
            with m.State("WB_LATCH"):
                m.d.comb += rd_addr.eq(Cat(beat, wb_line))
                m.d.sync += wb_data.eq(drp.data)
                m.next = "WB"
            with m.State("WB"):
                m.d.comb += [rd_addr.eq(s1_word), is_lookup.eq(1),
                             self.bus.cyc.eq(1), self.bus.we.eq(1), self.bus.sel.eq(0xFF),
                             self.bus.dat_w.eq(wb_data),
                             self.bus.adr.eq(Cat(C(0, 3), beat, wb_line, wb_tag))]
                with m.If(self.bus.ack):
                    m.d.sync += beat.eq(beat + 1)
                    with m.If(beat == WORDS - 1):
                        m.d.sync += dirty.eq(dirty & ~(1 << wb_line))
                        with m.If(after_wb):
                            m.d.sync += fl_line.eq(fl_line + 1)
                            m.next = "FLUSH"
                        with m.Else():
                            m.next = "REFILL"
                    with m.Else():
                        m.next = "WB_RD"
            with m.State("REFILL"):
                m.d.comb += [rd_addr.eq(s1_word), is_lookup.eq(1),
                             self.bus.cyc.eq(1), self.bus.sel.eq(0xFF),
                             self.bus.adr.eq(miss_pa | (beat << 3))]
                with m.If(self.bus.ack):
                    m.d.comb += [dwp.en.eq(0xFF), dwp.addr.eq(Cat(beat, miss_pa[5:5 + self.ibits])),
                                 dwp.data.eq(self.bus.dat_r)]
                    m.d.sync += beat.eq(beat + 1)
                    with m.If(beat == WORDS - 1):
                        line = miss_pa[5:5 + self.ibits]
                        m.d.comb += [twp.en.eq(1), twp.addr.eq(line),
                                     twp.data.eq(miss_pa[self.tag_lo:])]
                        m.d.sync += [valid.eq(valid | (1 << line)), dirty.eq(dirty & ~(1 << line))]
                        m.next = "IDLE"
        m.d.comb += [drp.addr.eq(rd_addr), trp.addr.eq(rd_addr[2:])]
        m.d.comb += self.stat_access.eq(hit & self.advance)
        return m
