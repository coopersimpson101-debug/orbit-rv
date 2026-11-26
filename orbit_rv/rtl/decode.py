"""RV64IMA_Zicsr_Zifencei instruction decoder (combinational)."""
from __future__ import annotations

from amaranth import C, Cat, Elaboratable, Module, Signal

# ALU operations
ADD, SUB, SLL, SLT, SLTU, XOR, SRL, SRA, OR, AND = range(10)
# operand A / B select
A_RS1, A_PC, A_ZERO = 0, 1, 2
B_RS2, B_IMM = 0, 1
# system operations (handled in the memory stage)
SYS_NONE, SYS_ECALL, SYS_EBREAK, SYS_MRET, SYS_SRET, SYS_WFI, SYS_SFENCE, SYS_FENCEI = range(8)
# memory operations
MEM_NONE, MEM_LOAD, MEM_STORE, MEM_AMO, MEM_LR, MEM_SC = range(6)


class Decoder(Elaboratable):
    def __init__(self):
        self.insn = Signal(32)
        self.illegal = Signal()
        self.rd = Signal(5)
        self.rs1 = Signal(5)
        self.rs2 = Signal(5)
        self.rd_we = Signal()
        self.rs1_used = Signal()
        self.rs2_used = Signal()
        self.imm = Signal(64)
        self.alu_op = Signal(4)
        self.alu_w = Signal()
        self.a_sel = Signal(2)
        self.b_sel = Signal()
        self.branch = Signal()
        self.jal = Signal()
        self.jalr = Signal()
        self.mem = Signal(3)
        self.mem_size = Signal(2)
        self.mem_unsigned = Signal()
        self.amo_op = Signal(5)
        self.muldiv = Signal()
        self.csr = Signal()
        self.sys = Signal(3)
        self.late = Signal()         # result only known in the memory stage

    def elaborate(self, platform):
        m = Module()
        i = self.insn
        op = i[0:7]
        f3 = i[12:15]
        f7 = i[25:32]
        m.d.comb += [self.rd.eq(i[7:12]), self.rs1.eq(i[15:20]), self.rs2.eq(i[20:25])]
        imm_i = Cat(i[20:32], i[31].replicate(52))
        imm_s = Cat(i[7:12], i[25:32], i[31].replicate(52))
        imm_b = Cat(C(0, 1), i[8:12], i[25:31], i[7], i[31].replicate(52))
        imm_u = Cat(C(0, 12), i[12:32], i[31].replicate(32))
        imm_j = Cat(C(0, 1), i[21:31], i[20], i[12:20], i[31].replicate(44))
        ill = Signal()
        m.d.comb += self.illegal.eq(ill | (i[0:2] != 3))

        def r_type():
            m.d.comb += [self.rd_we.eq(1), self.rs1_used.eq(1), self.rs2_used.eq(1),
                         self.b_sel.eq(B_RS2)]

        with m.Switch(op):
            with m.Case(0x37):  # LUI
                m.d.comb += [self.rd_we.eq(1), self.a_sel.eq(A_ZERO), self.b_sel.eq(B_IMM),
                             self.imm.eq(imm_u), self.alu_op.eq(ADD)]
            with m.Case(0x17):  # AUIPC
                m.d.comb += [self.rd_we.eq(1), self.a_sel.eq(A_PC), self.b_sel.eq(B_IMM),
                             self.imm.eq(imm_u), self.alu_op.eq(ADD)]
            with m.Case(0x6F):  # JAL
                m.d.comb += [self.rd_we.eq(1), self.jal.eq(1), self.imm.eq(imm_j)]
            with m.Case(0x67):  # JALR
                m.d.comb += [self.rd_we.eq(1), self.jalr.eq(1), self.rs1_used.eq(1),
                             self.imm.eq(imm_i), ill.eq(f3 != 0)]
            with m.Case(0x63):  # branches
                m.d.comb += [self.branch.eq(1), self.rs1_used.eq(1), self.rs2_used.eq(1),
                             self.imm.eq(imm_b), ill.eq((f3 == 2) | (f3 == 3))]
            with m.Case(0x03):  # loads
                m.d.comb += [self.rd_we.eq(1), self.rs1_used.eq(1), self.imm.eq(imm_i),
                             self.mem.eq(MEM_LOAD), self.mem_size.eq(f3[:2]),
                             self.mem_unsigned.eq(f3[2]), self.late.eq(1), ill.eq(f3 == 7)]
            with m.Case(0x23):  # stores
                m.d.comb += [self.rs1_used.eq(1), self.rs2_used.eq(1), self.imm.eq(imm_s),
                             self.mem.eq(MEM_STORE), self.mem_size.eq(f3[:2]), ill.eq(f3[2])]
            with m.Case(0x13):  # OP-IMM
                m.d.comb += [self.rd_we.eq(1), self.rs1_used.eq(1), self.b_sel.eq(B_IMM),
                             self.imm.eq(imm_i)]
                with m.Switch(f3):
                    with m.Case(0):
                        m.d.comb += self.alu_op.eq(ADD)
                    with m.Case(2):
                        m.d.comb += self.alu_op.eq(SLT)
                    with m.Case(3):
                        m.d.comb += self.alu_op.eq(SLTU)
                    with m.Case(4):
                        m.d.comb += self.alu_op.eq(XOR)
                    with m.Case(6):
                        m.d.comb += self.alu_op.eq(OR)
                    with m.Case(7):
                        m.d.comb += self.alu_op.eq(AND)
                    with m.Case(1):
                        m.d.comb += [self.alu_op.eq(SLL), ill.eq(i[26:32] != 0)]
                    with m.Case(5):
                        m.d.comb += [self.alu_op.eq(SRL | i[30]),  # SRL=6, SRA=7
                                     ill.eq((i[26:32] != 0) & (i[26:32] != 0x10))]
            with m.Case(0x1B):  # OP-IMM-32
                m.d.comb += [self.rd_we.eq(1), self.rs1_used.eq(1), self.b_sel.eq(B_IMM),
                             self.imm.eq(imm_i), self.alu_w.eq(1)]
                with m.Switch(f3):
                    with m.Case(0):
                        m.d.comb += self.alu_op.eq(ADD)
                    with m.Case(1):
                        m.d.comb += [self.alu_op.eq(SLL), ill.eq(f7 != 0)]
                    with m.Case(5):
                        m.d.comb += [self.alu_op.eq(SRL | i[30]),
                                     ill.eq((f7 != 0) & (f7 != 0x20))]
                    with m.Default():
                        m.d.comb += ill.eq(1)
            with m.Case(0x33, 0x3B):  # OP, OP-32
                r_type()
                w = op == 0x3B
                m.d.comb += self.alu_w.eq(w)
                with m.If(f7 == 1):
                    m.d.comb += [self.muldiv.eq(1), ill.eq(w & (f3 != 0) & (f3 < 4))]
                with m.Elif((f7 == 0) | (f7 == 0x20)):
                    alt = f7 == 0x20
                    with m.Switch(f3):
                        with m.Case(0):
                            m.d.comb += self.alu_op.eq(ADD)
                            with m.If(alt):
                                m.d.comb += self.alu_op.eq(SUB)
                        with m.Case(1):
                            m.d.comb += [self.alu_op.eq(SLL), ill.eq(alt)]
                        with m.Case(2):
                            m.d.comb += [self.alu_op.eq(SLT), ill.eq(alt | w)]
                        with m.Case(3):
                            m.d.comb += [self.alu_op.eq(SLTU), ill.eq(alt | w)]
                        with m.Case(4):
                            m.d.comb += [self.alu_op.eq(XOR), ill.eq(alt | w)]
                        with m.Case(5):
                            m.d.comb += self.alu_op.eq(SRL | alt)
                        with m.Case(6):
                            m.d.comb += [self.alu_op.eq(OR), ill.eq(alt | w)]
                        with m.Case(7):
                            m.d.comb += [self.alu_op.eq(AND), ill.eq(alt | w)]
                with m.Else():
                    m.d.comb += ill.eq(1)
            with m.Case(0x0F):  # FENCE / FENCE.I
                with m.If(f3 == 1):
                    m.d.comb += self.sys.eq(SYS_FENCEI)
                with m.Elif(f3 != 0):
                    m.d.comb += ill.eq(1)
            with m.Case(0x2F):  # AMO
                f5 = i[27:32]
                m.d.comb += [self.rd_we.eq(1), self.rs1_used.eq(1), self.rs2_used.eq(1),
                             self.mem_size.eq(f3[:2]), self.amo_op.eq(f5), self.late.eq(1),
                             self.imm.eq(0)]
                with m.If((f3 != 2) & (f3 != 3)):
                    m.d.comb += ill.eq(1)
                with m.If(f5 == 2):
                    m.d.comb += [self.mem.eq(MEM_LR), self.rs2_used.eq(0), ill.eq(i[20:25] != 0)]
                with m.Elif(f5 == 3):
                    m.d.comb += self.mem.eq(MEM_SC)
                with m.Else():
                    m.d.comb += self.mem.eq(MEM_AMO)
                    with m.Switch(f5):
                        with m.Case(0x00, 0x01, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C):
                            pass
                        with m.Default():
                            m.d.comb += ill.eq(1)
            with m.Case(0x73):  # SYSTEM
                with m.If(f3 == 0):
                    with m.If(i == 0x00000073):
                        m.d.comb += self.sys.eq(SYS_ECALL)
                    with m.Elif(i == 0x00100073):
                        m.d.comb += self.sys.eq(SYS_EBREAK)
                    with m.Elif(i == 0x30200073):
                        m.d.comb += self.sys.eq(SYS_MRET)
                    with m.Elif(i == 0x10200073):
                        m.d.comb += self.sys.eq(SYS_SRET)
                    with m.Elif(i == 0x10500073):
                        m.d.comb += self.sys.eq(SYS_WFI)
                    with m.Elif((f7 == 0x09) & (i[7:12] == 0)):
                        m.d.comb += [self.sys.eq(SYS_SFENCE), self.rs1_used.eq(1), self.rs2_used.eq(1)]
                    with m.Else():
                        m.d.comb += ill.eq(1)
                with m.Elif(f3 == 4):
                    m.d.comb += ill.eq(1)
                with m.Else():
                    m.d.comb += [self.csr.eq(1), self.rd_we.eq(1), self.late.eq(1),
                                 self.rs1_used.eq(~f3[2])]
            with m.Default():
                m.d.comb += ill.eq(1)
        return m
