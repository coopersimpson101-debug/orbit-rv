"""Unit tests for the golden ISS: arithmetic, memory, CSRs, traps, Sv39, devices."""
from __future__ import annotations

import random

import pytest

import asm as A
from orbit_rv import memmap as mm
from orbit_rv.iss.hart import (ECALL_M, ECALL_S, ECALL_U, ILLEGAL, LOAD_PAGE, M64, MISALIGNED_LOAD,
                               STORE_PAGE, Hart, sx)

BASE = mm.RAM_BASE


def hart(prog, ram=1 << 20):
    h = Hart(ram_size=ram)
    for n, w in enumerate(prog):
        h.ram[4 * n:4 * n + 4] = w.to_bytes(4, "little")
    return h


def run(prog, n=None, **kw):
    h = hart(prog, **kw)
    for _ in range(n or len(prog)):
        h.step()
    return h


def test_addi_sign_extension_and_x0():
    h = run([A.addi(1, 0, -1), A.addi(0, 0, 5), A.addi(2, 1, 2)])
    assert h.x[1] == M64 and h.x[0] == 0 and h.x[2] == 1


def test_shifts_rv64():
    h = run([A.addi(1, 0, -16), A.srai(2, 1, 2), A.slli(3, 1, 60)])
    assert h.x[2] == (-4) & M64
    assert h.x[3] == 0


@pytest.mark.parametrize("seed", range(4))
def test_muldiv_against_python(seed):
    rnd = random.Random(seed)
    for _ in range(40):
        a, b = rnd.getrandbits(64), rnd.choice([0, 1, M64, rnd.getrandbits(64), rnd.getrandbits(8)])
        h = hart([A.mul(3, 1, 2), A.mulh(4, 1, 2), A.div(5, 1, 2), A.rem(6, 1, 2), A.divw(7, 1, 2)])
        h.x[1], h.x[2] = a, b
        for _ in range(5):
            h.step()
        sa, sb = sx(a, 64), sx(b, 64)
        assert h.x[3] == (a * b) & M64
        assert h.x[4] == ((sa * sb) >> 64) & M64
        if b == 0:
            assert h.x[5] == M64 and h.x[6] == a
        elif not (sa == -(1 << 63) and sb == -1):
            q = abs(sa) // abs(sb) * (1 if (sa < 0) == (sb < 0) else -1)
            assert h.x[5] == q & M64
            assert h.x[6] == (sa - q * sb) & M64


def test_load_store_and_misaligned_trap():
    h = hart([A.addi(2, 0, 0x7F), A.sd(2, 1, 8), A.ld(3, 1, 8), A.lw(4, 1, 9)])
    h.x[1] = BASE + 0x1000
    h.mtvec = BASE + 0x400
    for _ in range(4):
        h.step()
    assert h.x[3] == 0x7F
    assert h.mcause == MISALIGNED_LOAD and h.mtval == BASE + 0x1000 + 9 and h.pc == BASE + 0x400


def test_ecall_causes_and_delegation():
    h = run([A.ECALL])
    assert h.mcause == ECALL_M and h.priv == 3
    h = hart([A.ECALL])
    h.priv = 1
    h.medeleg = 1 << ECALL_S
    h.stvec = BASE + 0x200
    h.step()
    assert h.priv == 1 and h.scause == ECALL_S and h.pc == BASE + 0x200 and h.sepc == BASE
    h = hart([A.ECALL])
    h.priv = 0
    h.step()
    assert h.mcause == ECALL_U and h.priv == 3


def test_csr_privilege_and_readonly():
    h = hart([A.csrrw(1, 0xF11, 0)])  # write to read-only mvendorid
    h.step()
    assert h.mcause == ILLEGAL and h.mtval == A.csrrw(1, 0xF11, 0)
    h = hart([A.csrrs(1, 0x300, 0)])
    h.priv = 1
    h.step()
    assert h.mcause == ILLEGAL  # mstatus from S-mode


def test_mip_rmw_uses_software_seip():
    # csrs mip, STIP while the PLIC drives SEIP must not latch SEIP into the software bit
    h = hart([A.addi(5, 0, 0x20), A.csrrs(0, 0x344, 5), A.J_SELF])
    h.plic.prio[10] = 1
    h.plic.enable[1] = 1 << 10
    h.uart.ier = 1
    h.uart.feed(b"x")
    h.step()
    h.step()
    h.uart.rx.clear()
    h.plic.pending = 0
    assert h.mip() == 0x20


def test_mret_to_user_and_back():
    h = hart([A.MRET, A.ECALL])
    h.mepc = BASE + 4
    h.mstatus &= ~(3 << 11)  # MPP = U
    h.step()
    assert h.priv == 0 and h.pc == BASE + 4
    h.step()
    assert h.priv == 3 and h.mcause == ECALL_U and (h.mstatus >> 11) & 3 == 0


def _map_sv39(h, va, pa, flags):
    """Three-level mapping with tables at RAM + 0x10000/0x11000/0x12000."""
    root, l1, l0 = BASE + 0x10000, BASE + 0x11000, BASE + 0x12000

    def wr(addr, val):
        o = addr - BASE
        h.ram[o:o + 8] = val.to_bytes(8, "little")
    wr(root + ((va >> 30) & 511) * 8, ((l1 >> 12) << 10) | 1)
    wr(l1 + ((va >> 21) & 511) * 8, ((l0 >> 12) << 10) | 1)
    wr(l0 + ((va >> 12) & 511) * 8, ((pa >> 12) << 10) | flags)
    h.satp = (8 << 60) | (root >> 12)


def test_sv39_translation_and_svade_faults():
    h = hart([A.J_SELF])
    h.priv = 1
    _map_sv39(h, 0x4000_0000, BASE + 0x20000, 0b11000111)  # V R W A D
    h.phys_write(BASE + 0x20010, 8, 0x1234)
    assert h.load(0x4000_0010, 8) == 0x1234
    h2 = hart([A.J_SELF])
    h2.priv = 1
    _map_sv39(h2, 0x4000_0000, BASE + 0x20000, 0b01000111)  # D clear
    assert h2.load(0x4000_0010, 8) == 0
    with pytest.raises(Exception) as e:
        h2.store(0x4000_0010, 8, 1)
    assert e.value.cause == STORE_PAGE
    h3 = hart([A.J_SELF])
    h3.priv = 1
    _map_sv39(h3, 0x4000_0000, BASE + 0x20000, 0b00000111)  # A clear
    with pytest.raises(Exception) as e:
        h3.load(0x4000_0000, 8)
    assert e.value.cause == LOAD_PAGE


def test_user_page_needs_sum_in_s_mode():
    h = hart([A.J_SELF])
    h.priv = 1
    _map_sv39(h, 0x1000, BASE + 0x20000, 0b11010111)  # V R W U A D
    with pytest.raises(Exception):
        h.load(0x1000, 8)
    h.mstatus |= 1 << 18  # SUM
    h._tlb.clear()
    assert h.load(0x1000, 8) == 0


def test_amo_and_lr_sc():
    h = hart([A.addi(2, 0, 5), A.amo(0x00, 3, 1, 2), A.amo(0x00, 4, 1, 2),
              A.amo(0x02, 5, 1, 0), A.amo(0x03, 6, 1, 2), A.amo(0x03, 7, 1, 2)])
    h.x[1] = BASE + 0x1000
    for _ in range(6):
        h.step()
    assert h.x[3] == 0 and h.x[4] == 5          # amoadd returns the old value
    assert h.x[5] == 10                           # lr.d
    assert h.x[6] == 0 and h.x[7] == 1            # sc succeeds once, then fails
    assert h.phys_read(BASE + 0x1000, 8, 1) == 5   # the successful SC stored x2


def test_timer_interrupt_and_wfi_fast_forward():
    h = hart([A.WFI, A.J_SELF] + [0] * 62 + [A.J_SELF])
    h.mtvec = BASE + 0x100
    h.mie = 1 << 7
    h.mstatus |= 1 << 3
    h.clint.mtimecmp = 1000
    h.run(10)
    assert h.mcause == (1 << 63) | 7 and h.pc == BASE + 0x100 and h.mepc == BASE + 4


def test_uart_and_finisher():
    out = []
    h = Hart(ram_size=4096, on_tx=out.append)
    h.phys_write(mm.UART_BASE, 1, ord("A"))
    assert out == [ord("A")]
    h.uart.feed(b"z")
    assert h.phys_read(mm.UART_BASE + 5, 1, 1) & 1
    assert h.phys_read(mm.UART_BASE, 1, 1) == ord("z")
    h.phys_write(mm.FINISHER_BASE, 4, 0x5555)
    assert h.halted and h.exit_code == 0


def test_plic_claim_complete():
    h = Hart(ram_size=4096)
    h.phys_write(mm.PLIC_BASE + 4 * 10, 4, 3)
    h.phys_write(mm.PLIC_BASE + mm.PLIC_ENABLE + 0x80, 4, 1 << 10)
    h.uart.ier = 1
    h.uart.feed(b"q")
    assert h.mip() & (1 << 9)
    claim = mm.PLIC_BASE + mm.PLIC_CONTEXT + 0x1000 + 4
    assert h.phys_read(claim, 4, 1) == 10
    assert h.phys_read(claim, 4, 1) == 0          # in service
    h.uart.rx.clear()
    h.phys_write(claim, 4, 10)
    assert not h.mip() & (1 << 9)
