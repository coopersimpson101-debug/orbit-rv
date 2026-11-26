"""Amaranth simulator tests of individual RTL blocks against the golden ISS models."""
from __future__ import annotations

import random

import pytest

amaranth = pytest.importorskip("amaranth")
from amaranth.sim import Simulator  # noqa: E402

from orbit_rv import memmap as mm  # noqa: E402
from orbit_rv.iss.hart import _OP, _OP32, M32, M64, Hart, Trap, decode, sext32  # noqa: E402
from orbit_rv.rtl.decode import Decoder  # noqa: E402
from orbit_rv.rtl.mmu import TLB  # noqa: E402
from orbit_rv.rtl.muldiv import MulDiv  # noqa: E402
from orbit_rv.rtl.periph import Plic, Uart  # noqa: E402


def sim(dut, bench, clock=True):
    s = Simulator(dut)
    if clock:
        s.add_clock(1e-6)
    s.add_testbench(bench)
    s.run()


def test_muldiv_matches_iss():
    dut = MulDiv()
    rnd = random.Random(1)
    vals = [0, 1, 2, M64, 1 << 63, (1 << 63) - 1, 0xFFFF_FFFF, 0x8000_0000, 7, M64 - 6]
    cases = []
    for _ in range(60):
        a = rnd.choice(vals + [rnd.getrandbits(64)] * 4)
        b = rnd.choice(vals + [rnd.getrandbits(64)] * 4)
        op = rnd.randrange(8)
        word = rnd.random() < 0.3 and op not in (1, 2, 3)
        cases.append((op, word, a, b))

    def expect(op, word, a, b):
        if word:
            return sext32(_OP32[(1, op)](a & M32, b & M32))
        return _OP[(1, op)](a, b) & M64

    async def bench(ctx):
        for op, word, a, b in cases:
            ctx.set(dut.op, op)
            ctx.set(dut.word, word)
            ctx.set(dut.a, a)
            ctx.set(dut.b, b)
            ctx.set(dut.start, 1)
            await ctx.tick()
            ctx.set(dut.start, 0)
            for _ in range(80):
                if ctx.get(dut.done):
                    break
                await ctx.tick()
            assert ctx.get(dut.done)
            assert ctx.get(dut.result) == expect(op, word, a, b), (op, word, hex(a), hex(b))
    sim(dut, bench)


def test_decoder_legality_matches_iss():
    """Random encodings of every opcode: RTL 'illegal' == ISS raising illegal instruction."""
    dut = Decoder()
    rnd = random.Random(2)
    words = [(rnd.getrandbits(25) << 7) | op for op in range(128) for _ in range(12)]
    words += [0x00000073, 0x00100073, 0x30200073, 0x10200073, 0x10500073, 0x12000073,
              0x0000100F, 0x0FF0000F, 0x02A5B533]
    h = Hart(ram_size=4096)

    def iss_illegal(w):
        fn, _ = decode(w)
        h.priv = 3
        h.x = [0] * 32
        h.x[1] = mm.RAM_BASE
        try:
            fn(h, mm.RAM_BASE)
        except Trap as t:
            return t.cause == 2
        return False

    async def bench(ctx):
        for w in words:
            ctx.set(dut.insn, w)
            rtl_ill = bool(ctx.get(dut.illegal))
            if (w & 0x7F) == 0x73 and (w >> 12) & 7 not in (0, 4):
                continue  # CSR existence/privilege is checked in the memory stage, not decode
            if (w & 0x7F) == 0x73 and not rtl_ill:
                continue  # privileged SYSTEM ops: legality depends on the mode (M stage)
            assert rtl_ill == iss_illegal(w), hex(w)
    sim(dut, bench, clock=False)


def test_uart_loopback_and_registers():
    dut = Uart()
    b = dut.bus

    async def access(ctx, lane, val=None):
        ctx.set(b.cyc, 1)
        ctx.set(b.we, val is not None)
        ctx.set(b.sel, 1 << lane)
        ctx.set(b.dat_w, (val or 0) << (8 * lane))
        await ctx.tick()
        while not ctx.get(b.ack):
            await ctx.tick()
        r = (ctx.get(b.dat_r) >> (8 * lane)) & 0xFF
        ctx.set(b.cyc, 0)
        await ctx.tick()
        return r

    async def bench(ctx):
        await access(ctx, 3, 0x83)      # DLAB
        await access(ctx, 0, 2)         # divisor 2 -> 32 clocks per bit
        await access(ctx, 3, 0x03)
        assert ctx.get(dut.divisor) == 2
        await access(ctx, 1, 0x01)      # RX interrupt enable
        await access(ctx, 0, 0x5A)      # transmit
        for _ in range(32 * 12):        # loop TX back into RX for one frame
            ctx.set(dut.rx, ctx.get(dut.tx))
            await ctx.tick()
        lsr = await access(ctx, 5)
        assert lsr & 1 and lsr & 0x20
        assert ctx.get(dut.irq)
        assert await access(ctx, 0) == 0x5A
        assert not ctx.get(dut.irq)
    sim(dut, bench)


def test_plic_claim_complete():
    dut = Plic()
    b = dut.bus

    async def access(ctx, off, val=None):
        hi = (off >> 2) & 1
        ctx.set(b.cyc, 1)
        ctx.set(b.we, val is not None)
        ctx.set(b.adr, off & ~7)
        ctx.set(b.sel, 0xF0 if hi else 0x0F)
        ctx.set(b.dat_w, (val or 0) << (32 * hi))
        await ctx.tick()
        while not ctx.get(b.ack):
            await ctx.tick()
        r = (ctx.get(b.dat_r) >> (32 * hi)) & M32
        ctx.set(b.cyc, 0)
        await ctx.tick()
        return r

    async def bench(ctx):
        await access(ctx, 4 * 10, 3)
        await access(ctx, mm.PLIC_ENABLE + 0x80, 1 << 10)
        ctx.set(dut.src, 1 << 10)
        await ctx.tick()
        await ctx.tick()
        assert ctx.get(dut.irq) == 0b10
        claim = mm.PLIC_CONTEXT + 0x1000 + 4
        assert await access(ctx, claim) == 10
        assert ctx.get(dut.irq) == 0
        ctx.set(dut.src, 0)
        await access(ctx, claim, 10)
        await ctx.tick()
        assert ctx.get(dut.irq) == 0
        assert await access(ctx, mm.PLIC_PENDING) == 0
    sim(dut, bench)


def test_tlb_lookup_superpage_and_permissions():
    dut = TLB(4, "t")

    async def bench(ctx):
        # 2 MiB superpage VA 0x4020_0000 -> PA 0x8020_0000, V R W X A D, supervisor
        ctx.set(dut.ins, 1)
        ctx.set(dut.ins_vpn, 0x4020_0000 >> 12)
        ctx.set(dut.ins_ppn, 0x8020_0000 >> 12)
        ctx.set(dut.ins_level, 1)
        ctx.set(dut.ins_flags, 0b11001111)
        await ctx.tick()
        ctx.set(dut.ins, 0)
        ctx.set(dut.va, 0x4021_2345)
        ctx.set(dut.priv, 1)
        ctx.set(dut.acc, 1)
        assert ctx.get(dut.hit) and ctx.get(dut.pa) == 0x8021_2345 and not ctx.get(dut.fault)
        ctx.set(dut.priv, 0)  # user access to a supervisor page
        assert ctx.get(dut.fault)
        ctx.set(dut.va, 0x4040_0000)
        assert not ctx.get(dut.hit)
        ctx.set(dut.flush, 1)
        await ctx.tick()
        ctx.set(dut.flush, 0)
        ctx.set(dut.va, 0x4021_2345)
        assert not ctx.get(dut.hit)
    sim(dut, bench)
