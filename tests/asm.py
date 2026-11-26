"""Tiny RV64 encoder for unit tests (only what the tests need)."""


def r(op, rd, f3, rs1, rs2, f7):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def i(op, rd, f3, rs1, imm):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def s(op, f3, rs1, rs2, imm):
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((imm & 31) << 7) | op


def addi(rd, rs1, imm): return i(0x13, rd, 0, rs1, imm)
def slli(rd, rs1, sh): return i(0x13, rd, 1, rs1, sh)
def srai(rd, rs1, sh): return i(0x13, rd, 5, rs1, 0x400 | sh)
def add(rd, a, b): return r(0x33, rd, 0, a, b, 0)
def sub(rd, a, b): return r(0x33, rd, 0, a, b, 0x20)
def mul(rd, a, b): return r(0x33, rd, 0, a, b, 1)
def mulh(rd, a, b): return r(0x33, rd, 1, a, b, 1)
def div(rd, a, b): return r(0x33, rd, 4, a, b, 1)
def rem(rd, a, b): return r(0x33, rd, 6, a, b, 1)
def divw(rd, a, b): return r(0x3B, rd, 4, a, b, 1)
def ld(rd, rs1, imm): return i(0x03, rd, 3, rs1, imm)
def lw(rd, rs1, imm): return i(0x03, rd, 2, rs1, imm)
def lbu(rd, rs1, imm): return i(0x03, rd, 4, rs1, imm)
def sd(rs2, rs1, imm): return s(0x23, 3, rs1, rs2, imm)
def sw(rs2, rs1, imm): return s(0x23, 2, rs1, rs2, imm)
def lui(rd, imm20): return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x37
def csrrw(rd, csr, rs1): return i(0x73, rd, 1, rs1, csr)
def csrrs(rd, csr, rs1): return i(0x73, rd, 2, rs1, csr)
def csrrc(rd, csr, rs1): return i(0x73, rd, 3, rs1, csr)
def amo(f5, rd, rs1, rs2, d=True): return r(0x2F, rd, 3 if d else 2, rs1, rs2, f5 << 2)


ECALL = 0x00000073
EBREAK = 0x00100073
MRET = 0x30200073
SRET = 0x10200073
WFI = 0x10500073
J_SELF = 0x0000006F  # jal x0, 0
