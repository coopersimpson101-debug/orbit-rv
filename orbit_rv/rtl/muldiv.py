"""Iterative M-extension unit.

Multiply: 64x16 partial products, 4 cycles (+1 to start), unsigned magnitude
with sign correction, so MUL/MULH/MULHSU/MULHU share one datapath.
Divide: restoring radix-2, 64 cycles (+2), with the RISC-V special cases
(divide by zero, signed overflow) handled up front.
"""
from __future__ import annotations

from amaranth import Cat, Elaboratable, Module, Mux, Signal


class MulDiv(Elaboratable):
    def __init__(self):
        self.start = Signal()
        self.op = Signal(3)      # funct3: MUL MULH MULHSU MULHU DIV DIVU REM REMU
        self.word = Signal()     # *W variants
        self.a = Signal(64)
        self.b = Signal(64)
        self.busy = Signal()
        self.done = Signal()     # result valid (held until next start)
        self.result = Signal(64)

    def elaborate(self, platform):
        m = Module()
        op, word = Signal(3), Signal()
        # For W ops, signed ops use sign-extended 32-bit operands, unsigned zero-extended.
        a_w_s = self.a[:32].as_signed()
        b_w_s = self.b[:32].as_signed()
        a_w_u = self.a[:32]
        b_w_u = self.b[:32]
        div_signed_in = ~self.op[0]  # DIV/REM signed, DIVU/REMU unsigned
        a_ext = Signal(64)
        b_ext = Signal(64)
        with m.If(self.word):
            with m.If(self.op[2] & ~div_signed_in):
                m.d.comb += [a_ext.eq(a_w_u), b_ext.eq(b_w_u)]
            with m.Else():
                m.d.comb += [a_ext.eq(a_w_s), b_ext.eq(b_w_s)]
        with m.Else():
            m.d.comb += [a_ext.eq(self.a), b_ext.eq(self.b)]
        # signedness of each operand
        a_signed = Mux(self.op[2], div_signed_in, (self.op == 1) | (self.op == 2) | self.word)
        b_signed = Mux(self.op[2], div_signed_in, (self.op == 1) | self.word)
        a_neg = a_signed & a_ext[63]
        b_neg = b_signed & b_ext[63]
        a_mag = Mux(a_neg, -a_ext, a_ext)[:64]
        b_mag = Mux(b_neg, -b_ext, b_ext)[:64]

        neg_res = Signal()
        mc = Signal(64)       # multiplicand / divisor magnitude
        acc = Signal(128)     # product accumulator / {remainder, quotient}
        cnt = Signal(7)
        special = Signal()
        spec_val = Signal(64)
        rem_neg = Signal()

        res_raw = Signal(64)
        prod = Mux(neg_res, -acc, acc)[:128]
        quo = Mux(neg_res, -acc[:64], acc[:64])[:64]
        rem = Mux(rem_neg, -acc[64:], acc[64:])[:64]
        with m.Switch(op):
            with m.Case(0):
                m.d.comb += res_raw.eq(prod[:64])
            with m.Case(1, 2, 3):
                m.d.comb += res_raw.eq(prod[64:])
            with m.Case(4, 5):
                m.d.comb += res_raw.eq(quo)
            with m.Case(6, 7):
                m.d.comb += res_raw.eq(rem)
        final = Mux(special, spec_val, res_raw)
        m.d.comb += self.result.eq(Mux(word, final[:32].as_signed(), final))

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.sync += [op.eq(self.op), word.eq(self.word), self.done.eq(0),
                                 special.eq(0), mc.eq(b_mag), cnt.eq(0)]
                    with m.If(~self.op[2]):
                        m.d.sync += [acc.eq(a_mag), neg_res.eq(a_neg ^ b_neg)]
                        m.next = "MUL"
                    with m.Else():
                        m.d.sync += [acc.eq(a_mag), neg_res.eq(a_neg ^ b_neg), rem_neg.eq(a_neg)]
                        bw = Mux(self.word, b_ext[:32] == 0, b_ext == 0)
                        most_neg = Mux(self.word, a_ext == 0xFFFF_FFFF_8000_0000,
                                       a_ext == 0x8000_0000_0000_0000)
                        ovf = div_signed_in & most_neg & (b_ext == ((1 << 64) - 1))
                        with m.If(bw):
                            # x/0 = all ones, x%0 = x
                            m.d.sync += [special.eq(1),
                                         spec_val.eq(Mux(self.op[1], a_ext, (1 << 64) - 1)),
                                         self.done.eq(1)]
                        with m.Elif(ovf):
                            m.d.sync += [special.eq(1), spec_val.eq(Mux(self.op[1], 0, a_ext)),
                                         self.done.eq(1)]
                        with m.Else():
                            m.next = "DIV"
            with m.State("MUL"):
                m.d.comb += self.busy.eq(1)
                # acc = {hi, lo}; lo holds the remaining multiplier bits (shifted out 16 at a time)
                chunk = acc[:16]
                partial = mc * chunk  # 80 bits
                hi = acc[64:] + partial
                m.d.sync += [acc.eq(Cat(acc[16:64], hi[:80])), cnt.eq(cnt + 1)]
                with m.If(cnt == 3):
                    m.d.sync += self.done.eq(1)
                    m.next = "IDLE"
            with m.State("DIV"):
                m.d.comb += self.busy.eq(1)
                # restoring division: acc = {rem, quotient-in-progress}
                shifted = Cat(acc[63], acc[64:128])  # (rem << 1) | next dividend bit
                trial = Signal(66)
                m.d.comb += trial.eq(Cat(acc[63], acc[64:128]) - mc)
                with m.If(~trial[65]):
                    m.d.sync += acc.eq(Cat(1, acc[:63], trial[:64]))
                with m.Else():
                    m.d.sync += acc.eq(Cat(0, acc[:63], shifted[:64]))
                m.d.sync += cnt.eq(cnt + 1)
                with m.If(cnt == 63):
                    m.d.sync += self.done.eq(1)
                    m.next = "IDLE"
        return m
