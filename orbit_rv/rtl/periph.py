"""SoC peripherals on OBus: CLINT, PLIC, 16550-subset UART, finisher, block RAM.

Register semantics match the ISS models in ``orbit_rv/iss/devices.py``.
"""
from __future__ import annotations

from amaranth import Array, C, Cat, Elaboratable, Module, Mux, Signal
from amaranth.lib import memory

from .. import memmap as mm
from .bus import Bus


def _bytes_merge(old, new, sel, width=64):
    """Byte-enable merge of ``new`` into ``old``."""
    parts = []
    for i in range(width // 8):
        parts.append(Mux(sel[i], new[8 * i:8 * i + 8], old[8 * i:8 * i + 8]))
    return Cat(*parts)


class Clint(Elaboratable):
    """msip, mtimecmp, mtime. mtime advances by ``num/den`` per clock cycle
    (e.g. 2/5 turns a 25 MHz clock into the 10 MHz timebase)."""

    def __init__(self, num: int = 2, den: int = 5):
        self.bus = Bus("clint")
        self.mtip = Signal()
        self.msip = Signal()
        self.mtime = Signal(64)
        self.num, self.den = num, den

    def elaborate(self, platform):
        m = Module()
        b = self.bus
        mtimecmp = Signal(64, init=(1 << 64) - 1)
        acc = Signal(range(self.den + self.num + 1))
        new_acc = Signal.like(acc)
        m.d.comb += new_acc.eq(acc + self.num)
        with m.If(new_acc >= self.den):
            m.d.sync += [acc.eq(new_acc - self.den), self.mtime.eq(self.mtime + 1)]
        with m.Else():
            m.d.sync += acc.eq(new_acc)
        m.d.comb += self.mtip.eq(self.mtime >= mtimecmp)
        req = b.cyc & ~b.ack
        m.d.sync += b.ack.eq(req)
        off = b.adr[:16]
        with m.If(req):
            with m.If(off == mm.CLINT_MSIP):
                m.d.sync += b.dat_r.eq(self.msip)
                with m.If(b.we & b.sel[0]):
                    m.d.sync += self.msip.eq(b.dat_w[0])
            with m.Elif(off == mm.CLINT_MTIMECMP):
                m.d.sync += b.dat_r.eq(mtimecmp)
                with m.If(b.we):
                    m.d.sync += mtimecmp.eq(_bytes_merge(mtimecmp, b.dat_w, b.sel))
            with m.Elif(off == mm.CLINT_MTIME):
                m.d.sync += b.dat_r.eq(self.mtime)
                with m.If(b.we):
                    m.d.sync += self.mtime.eq(_bytes_merge(self.mtime, b.dat_w, b.sel))
            with m.Else():
                m.d.sync += b.dat_r.eq(0)
        return m


class Plic(Elaboratable):
    """PLIC subset: 31 level-triggered sources, 3-bit priorities, two
    contexts (0 = M, 1 = S), threshold, claim/complete."""

    def __init__(self, nsrc: int = mm.PLIC_NSRC):
        self.bus = Bus("plic")
        self.nsrc = nsrc
        self.src = Signal(nsrc)
        self.irq = Signal(2)

    def elaborate(self, platform):
        m = Module()
        b = self.bus
        n = self.nsrc
        prio = Array(Signal(3, name=f"prio{i}") for i in range(n))
        pending = Signal(n)
        inservice = Signal(n)
        enable = Array(Signal(n, name=f"en{c}") for c in range(2))
        thresh = Array(Signal(3, name=f"th{c}") for c in range(2))
        best = []
        for c in range(2):
            bid = Signal(5, name=f"best{c}")
            bp = Signal(3, name=f"bestp{c}")
            # priority encoder: highest priority wins, lowest id breaks ties
            cand_id, cand_p = C(0, 5), C(0, 3)
            for i in range(n - 1, 0, -1):
                ok = pending[i] & enable[c][i] & (prio[i] > thresh[c]) & (prio[i] >= cand_p)
                nid, np_ = Signal(5, name=f"cid{c}_{i}"), Signal(3, name=f"cp{c}_{i}")
                m.d.comb += [nid.eq(Mux(ok, i, cand_id)), np_.eq(Mux(ok, prio[i], cand_p))]
                cand_id, cand_p = nid, np_
            m.d.comb += [bid.eq(cand_id), bp.eq(cand_p)]
            best.append(bid)
        m.d.comb += self.irq.eq(Cat(best[0] != 0, best[1] != 0))

        req = b.cyc & ~b.ack
        m.d.sync += b.ack.eq(req)
        # 32-bit registers; adr is 8-byte aligned, sel picks the lane
        hi = b.sel[4]
        off = Cat(C(0, 2), hi, b.adr[3:22])
        wdat = Mux(hi, b.dat_w[32:], b.dat_w[:32])
        rdat = Signal(32)
        set_pend = Signal(n)
        clr_pend = Signal(n)
        set_ins = Signal(n)
        clr_ins = Signal(n)
        m.d.comb += set_pend.eq(self.src & ~inservice)
        with m.If(req):
            with m.If(off < 0x1000):
                idx = off[2:7]
                m.d.comb += rdat.eq(prio[idx])
                with m.If(b.we & (idx != 0)):
                    m.d.sync += prio[idx].eq(wdat[:3])
            with m.Elif(off == mm.PLIC_PENDING):
                m.d.comb += rdat.eq(pending)
            for c in range(2):
                with m.Elif(off == mm.PLIC_ENABLE + 0x80 * c):
                    m.d.comb += rdat.eq(enable[c])
                    with m.If(b.we):
                        m.d.sync += enable[c].eq(wdat & ~1)
                with m.Elif(off == mm.PLIC_CONTEXT + 0x1000 * c):
                    m.d.comb += rdat.eq(thresh[c])
                    with m.If(b.we):
                        m.d.sync += thresh[c].eq(wdat[:3])
                with m.Elif(off == mm.PLIC_CONTEXT + 0x1000 * c + 4):
                    with m.If(b.we):
                        with m.If((wdat != 0) & (wdat < n)):
                            m.d.comb += clr_ins.eq(1 << wdat[:5])
                    with m.Else():
                        m.d.comb += [rdat.eq(best[c]), clr_pend.eq(1 << best[c]),
                                     set_ins.eq(1 << best[c])]
        m.d.sync += [b.dat_r.eq(Cat(rdat, rdat)),
                     pending.eq(((pending | set_pend) & ~clr_pend) & ~1),
                     inservice.eq(((inservice | set_ins) & ~clr_ins) & ~1)]
        return m


class _Fifo(Elaboratable):
    def __init__(self, depth=16):
        self.depth = depth
        self.w_en = Signal()
        self.w_data = Signal(8)
        self.r_en = Signal()
        self.r_data = Signal(8)
        self.level = Signal(range(depth + 1))
        self.clear = Signal()

    def elaborate(self, platform):
        m = Module()
        buf = Array(Signal(8, name=f"fifo{i}") for i in range(self.depth))
        rp = Signal(range(self.depth))
        wp = Signal(range(self.depth))
        full = self.level == self.depth
        empty = self.level == 0
        do_w = self.w_en & ~full
        do_r = self.r_en & ~empty
        m.d.comb += self.r_data.eq(buf[rp])
        with m.If(do_w):
            m.d.sync += [buf[wp].eq(self.w_data), wp.eq(wp + 1)]
        with m.If(do_r):
            m.d.sync += rp.eq(rp + 1)
        m.d.sync += self.level.eq(self.level + do_w - do_r)
        with m.If(self.clear):
            m.d.sync += [rp.eq(0), wp.eq(0), self.level.eq(0)]
        return m


class Uart(Elaboratable):
    """16550 subset with 16-byte FIFOs, 16x oversampled RX, 8N1 only."""

    def __init__(self):
        self.bus = Bus("uart")
        self.tx = Signal(init=1)
        self.rx = Signal(init=1)
        self.irq = Signal()
        self.divisor = Signal(16)

    def elaborate(self, platform):
        m = Module()
        b = self.bus
        m.submodules.txf = txf = _Fifo()
        m.submodules.rxf = rxf = _Fifo()
        ier = Signal(4)
        lcr = Signal(8)
        mcr = Signal(5)
        scr = Signal(8)
        fcr = Signal(8)
        dll = Signal(8, init=1)
        dlm = Signal(8)
        m.d.comb += self.divisor.eq(Cat(dll, dlm))
        dlab = lcr[7]

        # baud x16 tick
        cnt = Signal(16)
        tick = Signal()
        with m.If(cnt + 1 >= self.divisor):
            m.d.sync += cnt.eq(0)
            m.d.comb += tick.eq(1)
        with m.Else():
            m.d.sync += cnt.eq(cnt + 1)

        # transmitter
        tx_busy = Signal()
        tx_shift = Signal(10)
        tx_bits = Signal(4)
        tx_sub = Signal(4)
        with m.If(~tx_busy):
            with m.If(txf.level != 0):
                m.d.comb += txf.r_en.eq(1)
                m.d.sync += [tx_shift.eq(Cat(C(0, 1), txf.r_data, C(1, 1))), tx_busy.eq(1),
                             tx_bits.eq(0), tx_sub.eq(0)]
        with m.Elif(tick):
            m.d.sync += tx_sub.eq(tx_sub + 1)
            with m.If(tx_sub == 15):
                m.d.sync += [tx_shift.eq(Cat(tx_shift[1:], C(1, 1))), tx_bits.eq(tx_bits + 1)]
                with m.If(tx_bits == 9):
                    m.d.sync += tx_busy.eq(0)
        m.d.comb += self.tx.eq(Mux(tx_busy, tx_shift[0], 1))

        # receiver
        rx_s1 = Signal(init=1)
        rx_s = Signal(init=1)
        m.d.sync += [rx_s1.eq(self.rx), rx_s.eq(rx_s1)]
        rx_busy = Signal()
        rx_sub = Signal(4)
        rx_bits = Signal(4)
        rx_sh = Signal(8)
        with m.If(~rx_busy):
            with m.If(~rx_s):
                m.d.sync += [rx_busy.eq(1), rx_sub.eq(0), rx_bits.eq(0)]
        with m.Elif(tick):
            m.d.sync += rx_sub.eq(rx_sub + 1)
            with m.If(rx_sub == 7):
                with m.If(rx_bits == 0):
                    with m.If(rx_s):  # false start bit
                        m.d.sync += rx_busy.eq(0)
                with m.Elif(rx_bits == 9):
                    m.d.sync += rx_busy.eq(0)
                    with m.If(rx_s):
                        m.d.comb += [rxf.w_en.eq(1), rxf.w_data.eq(rx_sh)]
                with m.Else():
                    m.d.sync += rx_sh.eq(Cat(rx_sh[1:], rx_s))
                with m.If(rx_bits != 9):
                    m.d.sync += rx_bits.eq(rx_bits + 1)

        dr = rxf.level != 0
        thre = txf.level == 0
        temt = thre & ~tx_busy
        m.d.comb += self.irq.eq((ier[0] & dr) | (ier[1] & thre))
        iid = Mux(ier[0] & dr, 0x04, Mux(ier[1] & thre, 0x02, 0x01))

        req = b.cyc & ~b.ack
        m.d.sync += b.ack.eq(req)
        # byte registers: the byte lane is the low address bits, chosen by sel
        lane = Signal(3)
        for i in range(8):
            with m.If(b.sel[i]):
                m.d.comb += lane.eq(i)
        wbyte = b.dat_w.word_select(lane, 8)
        rbyte = Signal(8)
        with m.If(req):
            with m.Switch(lane):
                with m.Case(0):
                    with m.If(dlab):
                        m.d.comb += rbyte.eq(dll)
                        with m.If(b.we):
                            m.d.sync += dll.eq(wbyte)
                    with m.Else():
                        m.d.comb += rbyte.eq(rxf.r_data)
                        with m.If(b.we):
                            m.d.comb += [txf.w_en.eq(1), txf.w_data.eq(wbyte)]
                        with m.Else():
                            m.d.comb += rxf.r_en.eq(1)
                with m.Case(1):
                    with m.If(dlab):
                        m.d.comb += rbyte.eq(dlm)
                        with m.If(b.we):
                            m.d.sync += dlm.eq(wbyte)
                    with m.Else():
                        m.d.comb += rbyte.eq(ier)
                        with m.If(b.we):
                            m.d.sync += ier.eq(wbyte[:4])
                with m.Case(2):
                    m.d.comb += rbyte.eq(iid | Mux(fcr[0], 0xC0, 0))
                    with m.If(b.we):
                        m.d.sync += fcr.eq(wbyte & 0xC9)
                        m.d.comb += [rxf.clear.eq(wbyte[1]), txf.clear.eq(wbyte[2])]
                with m.Case(3):
                    m.d.comb += rbyte.eq(lcr)
                    with m.If(b.we):
                        m.d.sync += lcr.eq(wbyte)
                with m.Case(4):
                    m.d.comb += rbyte.eq(mcr)
                    with m.If(b.we):
                        m.d.sync += mcr.eq(wbyte[:5])
                with m.Case(5):
                    m.d.comb += rbyte.eq(Cat(dr, C(0, 4), thre, temt, C(0, 1)))
                with m.Case(7):
                    m.d.comb += rbyte.eq(scr)
                    with m.If(b.we):
                        m.d.sync += scr.eq(wbyte)
        m.d.sync += b.dat_r.eq(Cat(*([rbyte] * 8)))
        return m


class Finisher(Elaboratable):
    """QEMU-virt-style test finisher: a 32-bit write latches ``done``/``code``."""

    def __init__(self):
        self.bus = Bus("fin")
        self.done = Signal()
        self.value = Signal(32)

    def elaborate(self, platform):
        m = Module()
        b = self.bus
        req = b.cyc & ~b.ack
        m.d.sync += [b.ack.eq(req), b.dat_r.eq(0)]
        with m.If(req & b.we & b.sel[0] & (b.adr[3:12] == 0)):
            m.d.sync += [self.done.eq(1), self.value.eq(b.dat_w[:32])]
        return m


class BlockRam(Elaboratable):
    """Main memory in FPGA block RAM (64-bit words, byte enables)."""

    def __init__(self, size: int, init=()):
        self.bus = Bus("ram")
        self.size = size
        self.init = list(init)

    def elaborate(self, platform):
        m = Module()
        b = self.bus
        depth = self.size // 8
        m.submodules.mem = mem = memory.Memory(shape=64, depth=depth, init=self.init)
        rp = mem.read_port()
        wp = mem.write_port(granularity=8)
        idx = b.adr[3:3 + (depth - 1).bit_length()]
        req = b.cyc & ~b.ack
        m.d.comb += [rp.addr.eq(idx), wp.addr.eq(idx), wp.data.eq(b.dat_w),
                     wp.en.eq(Mux(req & b.we, b.sel, 0)), b.dat_r.eq(rp.data)]
        m.d.sync += b.ack.eq(req)
        return m
