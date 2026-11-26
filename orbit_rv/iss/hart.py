"""Golden instruction-set simulator: RV64IMA_Zicsr_Zifencei, M/S/U modes, Sv39.

Written from the RISC-V Unprivileged ISA (20191213) and Privileged
Architecture (20211203) specifications. It is instruction-accurate (no timing)
and is the reference the RTL is checked against, instruction by instruction.

Implementation choices that the RTL shares (all permitted by the spec):
  * Svade: the hardware never sets PTE.A/D; it raises a page fault instead.
  * Misaligned loads/stores/AMOs trap (checked before translation).
  * No PMP entries (pmpcfg*/pmpaddr* read as zero), no triggers
    (tselect/tdata* read as zero), ASIDLEN = 0, no hardware performance
    counters beyond cycle/instret (mhpmcounter* read zero).
  * WFI waits for an enabled-or-not pending interrupt (mip & mie != 0).
  * Physical addresses are 32 bits: anything that is not RAM or a device
    raises an access fault.
"""
from __future__ import annotations

from .. import memmap as mm
from .devices import Clint, Plic, Uart

M64 = (1 << 64) - 1
M32 = (1 << 32) - 1
SIGN64 = 1 << 63

# exception causes
MISALIGNED_FETCH, FETCH_ACCESS, ILLEGAL, BREAKPOINT = 0, 1, 2, 3
MISALIGNED_LOAD, LOAD_ACCESS, MISALIGNED_STORE, STORE_ACCESS = 4, 5, 6, 7
ECALL_U, ECALL_S, ECALL_M = 8, 9, 11
FETCH_PAGE, LOAD_PAGE, STORE_PAGE = 12, 13, 15

FETCH, LOAD, STORE = 0, 1, 2
_PAGE_FAULT = {FETCH: FETCH_PAGE, LOAD: LOAD_PAGE, STORE: STORE_PAGE}
_ACCESS_FAULT = {FETCH: FETCH_ACCESS, LOAD: LOAD_ACCESS, STORE: STORE_ACCESS}

# mstatus bits
SIE, MIE, SPIE, MPIE, SPP = 1 << 1, 1 << 3, 1 << 5, 1 << 7, 1 << 8
MPP_SHIFT = 11
MPRV, SUM, MXR, TVM, TW, TSR = 1 << 17, 1 << 18, 1 << 19, 1 << 20, 1 << 21, 1 << 22
MSTATUS_WMASK = SIE | MIE | SPIE | MPIE | SPP | (3 << MPP_SHIFT) | MPRV | SUM | MXR | TVM | TW | TSR
SSTATUS_MASK = SIE | SPIE | SPP | SUM | MXR | (3 << 32)
MSTATUS_FIXED = (2 << 32) | (2 << 34)  # UXL = SXL = 64

MISA = (2 << 62) | (1 << 0) | (1 << 8) | (1 << 12) | (1 << 18) | (1 << 20)  # A I M S U
MEDELEG_MASK = 0xB3FF
MIDELEG_MASK = 0x222
MIE_MASK = 0xAAA
MIP_SW_MASK = 0x222  # SSIP, STIP, SEIP writable from M-mode
IRQ_ORDER = (11, 3, 7, 9, 1, 5)  # MEI MSI MTI SEI SSI STI

CSR_NAMES = {
    0x100: "sstatus", 0x104: "sie", 0x105: "stvec", 0x106: "scounteren", 0x10A: "senvcfg",
    0x140: "sscratch", 0x141: "sepc", 0x142: "scause", 0x143: "stval", 0x144: "sip",
    0x180: "satp", 0x300: "mstatus", 0x301: "misa", 0x302: "medeleg", 0x303: "mideleg",
    0x304: "mie", 0x305: "mtvec", 0x306: "mcounteren", 0x30A: "menvcfg", 0x340: "mscratch",
    0x341: "mepc", 0x342: "mcause", 0x343: "mtval", 0x344: "mip", 0xB00: "mcycle",
    0xB02: "minstret", 0xC00: "cycle", 0xC01: "time", 0xC02: "instret", 0xF11: "mvendorid",
    0xF12: "marchid", 0xF13: "mimpid", 0xF14: "mhartid", 0xF15: "mconfigptr",
    0x7A0: "tselect", 0x7A1: "tdata1", 0x7A2: "tdata2", 0x7A3: "tdata3",
}
MARCHID = 0  # not registered with RISC-V International


class Trap(Exception):
    __slots__ = ("cause", "tval")

    def __init__(self, cause: int, tval: int = 0):
        super().__init__(cause, tval)
        self.cause = cause
        self.tval = tval & M64


def sx(v: int, bits: int) -> int:
    """Sign-extend a ``bits``-wide value to a Python int."""
    m = 1 << (bits - 1)
    v &= (1 << bits) - 1
    return (v ^ m) - m


def s64(v: int) -> int:
    return v - (1 << 64) if v & SIGN64 else v


def sext32(v: int) -> int:
    v &= M32
    return (v | ~M32) & M64 if v & 0x8000_0000 else v


class Hart:
    def __init__(self, ram_size: int = mm.RAM_SIZE_SIM, insns_per_tick: int = 5, on_tx=None):
        self.ram_size = ram_size
        self.ram = bytearray(ram_size)
        self.clint = Clint(insns_per_tick)
        self.plic = Plic()
        self.uart = Uart(on_tx)
        self.x = [0] * 32
        self.pc = mm.RESET_PC
        self.priv = 3
        self.instret = 0
        self.mstatus = MSTATUS_FIXED
        self.medeleg = self.mideleg = self.mie = self.mip_sw = 0
        self.mtvec = self.stvec = self.mepc = self.sepc = 0
        self.mcause = self.scause = self.mtval = self.stval = 0
        self.mscratch = self.sscratch = 0
        self.mcounteren = self.scounteren = 0
        self.satp = 0
        self.cycle_off = 0
        self.on_idle = None
        self.resv = None
        self.wfi = False
        self.halted = False
        self.tohost = None
        self.exit_code = None
        self._dcache: dict = {}
        self._tlb: dict = {}
        # lockstep hooks
        self.mmio_override = None   # value returned by the next MMIO load (replay)
        self.last_mmio = False
        self.last_store = None      # (pa, size, value) of the last store
        self.trace = None           # list of commit records, or None
        self.lockstep = False       # if True, interrupts are only taken when injected
        self.mtime_override = None

    # ------------------------------------------------------------------ memory
    def load_image(self, addr: int, data: bytes) -> None:
        off = addr - mm.RAM_BASE
        if off < 0 or off + len(data) > self.ram_size:
            raise ValueError(f"segment {addr:#x}+{len(data):#x} outside RAM")
        self.ram[off:off + len(data)] = data

    def phys_read(self, pa: int, size: int, acc: int) -> int:
        off = pa - mm.RAM_BASE
        if 0 <= off <= self.ram_size - size:
            return int.from_bytes(self.ram[off:off + size], "little")
        if acc != FETCH and mm.is_mmio(pa):
            self.last_mmio = True
            v = self._mmio_read(pa, size)
            if self.mmio_override is not None:
                v = self.mmio_override & ((1 << (8 * size)) - 1)
                self.mmio_override = None
            return v
        raise Trap(_ACCESS_FAULT[acc], 0)

    def phys_write(self, pa: int, size: int, val: int) -> None:
        off = pa - mm.RAM_BASE
        val &= (1 << (8 * size)) - 1
        self.last_store = (pa, size, val)
        if 0 <= off <= self.ram_size - size:
            self.ram[off:off + size] = val.to_bytes(size, "little")
            if pa == self.tohost and val:
                self._tohost(val)
            return
        if mm.is_mmio(pa):
            self.last_mmio = True
            self._mmio_write(pa, size, val)
            return
        raise Trap(STORE_ACCESS, 0)

    def _tohost(self, val: int) -> None:
        if val & 1:
            self.halted = True
            self.exit_code = val >> 1

    def _mmio_read(self, pa: int, size: int) -> int:
        self.clint.instret_ref = self.instret
        if mm.CLINT_BASE <= pa < mm.CLINT_BASE + mm.CLINT_SIZE:
            return self.clint.read(pa - mm.CLINT_BASE, size)
        if mm.PLIC_BASE <= pa < mm.PLIC_BASE + mm.PLIC_SIZE:
            self._update_plic()
            return self.plic.read(pa - mm.PLIC_BASE, size)
        if mm.UART_BASE <= pa < mm.UART_BASE + mm.UART_SIZE:
            return self.uart.read(pa - mm.UART_BASE, 1)
        return 0

    def _mmio_write(self, pa: int, size: int, val: int) -> None:
        self.clint.instret_ref = self.instret
        if mm.CLINT_BASE <= pa < mm.CLINT_BASE + mm.CLINT_SIZE:
            self.clint.write(pa - mm.CLINT_BASE, size, val)
        elif mm.PLIC_BASE <= pa < mm.PLIC_BASE + mm.PLIC_SIZE:
            self._update_plic()
            self.plic.write(pa - mm.PLIC_BASE, size, val)
        elif mm.UART_BASE <= pa < mm.UART_BASE + mm.UART_SIZE:
            self.uart.write(pa - mm.UART_BASE, 1, val)
        elif pa == mm.FINISHER_BASE:
            kind = val & 0xFFFF
            if kind in (mm.FINISHER_PASS, mm.FINISHER_RESET):
                self.halted, self.exit_code = True, 0
            elif kind == mm.FINISHER_FAIL:
                self.halted, self.exit_code = True, (val >> 16) or 1

    def _update_plic(self) -> None:
        self.plic.update((1 << mm.UART_IRQ) if self.uart.irq() else 0)

    # ------------------------------------------------------------ translation
    def translate(self, va: int, acc: int) -> int:
        priv = self.priv
        if acc != FETCH and self.mstatus & MPRV:
            priv = (self.mstatus >> MPP_SHIFT) & 3
        if priv == 3 or not (self.satp >> 60):
            return va
        key = (va >> 12, acc, priv, self.mstatus & (SUM | MXR))
        hit = self._tlb.get(key)
        if hit is not None:
            return hit | (va & 0xFFF)
        pa = self._walk(va, acc, priv)
        self._tlb[key] = pa & ~0xFFF
        return pa

    def _walk(self, va: int, acc: int, priv: int) -> int:
        pf = _PAGE_FAULT[acc]
        if sx(va, 39) & M64 != va:
            raise Trap(pf, va)
        a = (self.satp & ((1 << 44) - 1)) << 12
        for level in (2, 1, 0):
            pte_pa = a + ((va >> (12 + 9 * level)) & 0x1FF) * 8
            off = pte_pa - mm.RAM_BASE
            if not (0 <= off <= self.ram_size - 8):
                raise Trap(_ACCESS_FAULT[acc], va)
            pte = int.from_bytes(self.ram[off:off + 8], "little")
            ppn = (pte >> 10) & ((1 << 44) - 1)
            v, r, w, x, u = pte & 1, pte & 2, pte & 4, pte & 8, pte & 16
            if not v or (not r and w) or (pte >> 54):
                raise Trap(pf, va)
            if not (r or x):
                if level == 0 or pte & (0x40 | 0x80 | 0x10):  # A, D, U on a pointer
                    raise Trap(pf, va)
                a = ppn << 12
                continue
            # leaf
            if priv == 0 and not u:
                raise Trap(pf, va)
            if priv == 1 and u and (acc == FETCH or not (self.mstatus & SUM)):
                raise Trap(pf, va)
            if acc == FETCH and not x:
                raise Trap(pf, va)
            if acc == LOAD and not (r or (x and self.mstatus & MXR)):
                raise Trap(pf, va)
            if acc == STORE and not w:
                raise Trap(pf, va)
            if level and ppn & ((1 << (9 * level)) - 1):
                raise Trap(pf, va)  # misaligned superpage
            if not (pte & 0x40) or (acc == STORE and not (pte & 0x80)):
                raise Trap(pf, va)  # Svade: A/D are managed by software
            mask = (1 << (12 + 9 * level)) - 1
            return ((ppn << 12) & ~mask) | (va & mask)
        raise Trap(pf, va)  # unreachable

    # --------------------------------------------------------- data accesses
    def load(self, va: int, size: int) -> int:
        if va & (size - 1):
            raise Trap(MISALIGNED_LOAD, va)
        pa = self.translate(va, LOAD)
        try:
            return self.phys_read(pa, size, LOAD)
        except Trap as t:
            t.tval = va
            raise

    def store(self, va: int, size: int, val: int) -> None:
        if va & (size - 1):
            raise Trap(MISALIGNED_STORE, va)
        pa = self.translate(va, STORE)
        try:
            self.phys_write(pa, size, val)
        except Trap as t:
            t.tval = va
            raise

    def amo_pa(self, va: int, size: int) -> int:
        if va & (size - 1):
            raise Trap(MISALIGNED_STORE, va)
        pa = self.translate(va, STORE)
        if not mm.is_ram(pa, self.ram_size):
            raise Trap(STORE_ACCESS, va)
        return pa

    # ------------------------------------------------------------------ CSRs
    def mip(self) -> int:
        v = self.mip_sw
        if self.lockstep:
            return v | (self.mip_hw_override or 0)
        self.clint.instret_ref = self.instret
        if self.clint.msip:
            v |= 1 << 3
        if self.clint.mtime() >= self.clint.mtimecmp:
            v |= 1 << 7
        self._update_plic()
        if self.plic.irq(0):
            v |= 1 << 11
        if self.plic.irq(1):
            v |= 1 << 9
        return v

    mip_hw_override = 0

    def counter_ok(self, bit: int) -> bool:
        if self.priv == 3:
            return True
        if not (self.mcounteren >> bit) & 1:
            return False
        if self.priv == 0 and not (self.scounteren >> bit) & 1:
            return False
        return True

    def csr_read(self, a: int) -> int:
        p = self.priv
        if ((a >> 8) & 3) > p:
            raise Trap(ILLEGAL)
        if a == 0x300:
            return self.mstatus
        if a == 0x100:
            return self.mstatus & SSTATUS_MASK
        if a == 0x301:
            return MISA
        if a == 0x302:
            return self.medeleg
        if a == 0x303:
            return self.mideleg
        if a == 0x304:
            return self.mie
        if a == 0x104:
            return self.mie & self.mideleg
        if a == 0x344:
            return self.mip()
        if a == 0x144:
            return self.mip() & self.mideleg
        if a == 0x305:
            return self.mtvec
        if a == 0x105:
            return self.stvec
        if a == 0x340:
            return self.mscratch
        if a == 0x140:
            return self.sscratch
        if a == 0x341:
            return self.mepc
        if a == 0x141:
            return self.sepc
        if a == 0x342:
            return self.mcause
        if a == 0x142:
            return self.scause
        if a == 0x343:
            return self.mtval
        if a == 0x143:
            return self.stval
        if a == 0x306:
            return self.mcounteren
        if a == 0x106:
            return self.scounteren
        if a == 0x180:
            if p == 1 and self.mstatus & TVM:
                raise Trap(ILLEGAL)
            return self.satp
        if a == 0xB00:
            return (self.instret + self.cycle_off) & M64
        if a == 0xB02:
            return self.instret & M64
        if a in (0xC00, 0xC02):
            if not self.counter_ok(a & 3):
                raise Trap(ILLEGAL)
            return (self.instret + (self.cycle_off if a == 0xC00 else 0)) & M64
        if a == 0xC01:
            if not self.counter_ok(1):
                raise Trap(ILLEGAL)
            if self.mtime_override is not None:
                v, self.mtime_override = self.mtime_override, None
                return v
            self.clint.instret_ref = self.instret
            return self.clint.mtime()
        if 0xC03 <= a <= 0xC1F:
            if not self.counter_ok(a & 31):
                raise Trap(ILLEGAL)
            return 0
        if a == 0xF12:
            return MARCHID
        if a in (0xF11, 0xF13, 0xF14, 0xF15, 0x30A, 0x10A, 0x320):
            return 0
        if 0xB03 <= a <= 0xB1F or 0x323 <= a <= 0x33F:
            return 0
        if 0x3A0 <= a <= 0x3EF or 0x7A0 <= a <= 0x7A3:
            return 0
        raise Trap(ILLEGAL)

    def csr_write(self, a: int, v: int) -> None:
        if (a >> 10) == 3:
            raise Trap(ILLEGAL)
        v &= M64
        if a == 0x300:
            self._set_mstatus((self.mstatus & ~MSTATUS_WMASK) | (v & MSTATUS_WMASK))
        elif a == 0x100:
            m = SSTATUS_MASK & ~(3 << 32)
            self._set_mstatus((self.mstatus & ~m) | (v & m))
        elif a == 0x302:
            self.medeleg = v & MEDELEG_MASK
        elif a == 0x303:
            self.mideleg = v & MIDELEG_MASK
        elif a == 0x304:
            self.mie = v & MIE_MASK
        elif a == 0x104:
            self.mie = (self.mie & ~self.mideleg) | (v & self.mideleg)
        elif a == 0x344:
            self.mip_sw = v & MIP_SW_MASK
        elif a == 0x144:
            m = self.mideleg & 0x2
            self.mip_sw = (self.mip_sw & ~m) | (v & m)
        elif a == 0x305:
            self.mtvec = v & ~2
        elif a == 0x105:
            self.stvec = v & ~2
        elif a == 0x340:
            self.mscratch = v
        elif a == 0x140:
            self.sscratch = v
        elif a == 0x341:
            self.mepc = v & ~3
        elif a == 0x141:
            self.sepc = v & ~3
        elif a == 0x342:
            self.mcause = v
        elif a == 0x142:
            self.scause = v
        elif a == 0x343:
            self.mtval = v
        elif a == 0x143:
            self.stval = v
        elif a == 0x306:
            self.mcounteren = v & M32
        elif a == 0x106:
            self.scounteren = v & M32
        elif a == 0x180:
            if self.priv == 1 and self.mstatus & TVM:
                raise Trap(ILLEGAL)
            mode = v >> 60
            if mode in (0, 8):
                self.satp = (mode << 60) | (v & ((1 << 44) - 1))  # ASIDLEN = 0
                self._tlb.clear()
        elif a == 0xB02:
            # the write wins over this instruction's own retirement increment
            self.instret = (v - 1) & M64
        elif a == 0xB00:
            self.cycle_off = v - (self.instret + 1)
        # everything else that is readable is WARL read-only-zero
        elif a in (0x301, 0x30A, 0x10A, 0x320) or 0xB03 <= a <= 0xB1F or 0x323 <= a <= 0x33F \
                or 0x3A0 <= a <= 0x3EF or 0x7A0 <= a <= 0x7A3:
            pass
        else:
            raise Trap(ILLEGAL)

    def _set_mstatus(self, v: int) -> None:
        mpp = (v >> MPP_SHIFT) & 3
        if mpp == 2:
            v &= ~(3 << MPP_SHIFT)
        self.mstatus = (v & MSTATUS_WMASK) | MSTATUS_FIXED
        self._tlb.clear()

    # ------------------------------------------------------------------ traps
    def take_trap(self, cause: int, tval: int, irq: bool) -> None:
        pc = self.pc
        deleg = self.priv <= 1 and (((self.mideleg if irq else self.medeleg) >> cause) & 1)
        c = cause | (SIGN64 if irq else 0)
        ms = self.mstatus
        if deleg:
            self.scause, self.sepc, self.stval = c, pc, tval & M64
            ms = (ms & ~(SPIE | SPP | SIE)) | (SPIE if ms & SIE else 0) | (SPP if self.priv else 0)
            self.priv = 1
            base = self.stvec
        else:
            self.mcause, self.mepc, self.mtval = c, pc, tval & M64
            ms = (ms & ~(MPIE | (3 << MPP_SHIFT) | MIE)) | (MPIE if ms & MIE else 0) | (
                self.priv << MPP_SHIFT)
            self.priv = 3
            base = self.mtvec
        self.mstatus = ms
        self.resv = None
        self._tlb.clear()
        if irq and base & 1:
            self.pc = (base & ~3) + 4 * cause
        else:
            self.pc = base & ~3

    def pending_irq(self) -> int:
        """Highest-priority interrupt that would be taken now, or -1."""
        pend = self.mip() & self.mie
        if not pend:
            return -1
        p = self.priv
        ms = self.mstatus
        m = pend & ~self.mideleg if (p < 3 or ms & MIE) else 0
        s = pend & self.mideleg if (p < 1 or (p == 1 and ms & SIE)) else 0
        for src in (m, s):
            if src:
                for i in IRQ_ORDER:
                    if (src >> i) & 1:
                        return i
        return -1

    # --------------------------------------------------------------- execution
    def step(self) -> None:
        """Execute one instruction (or take one trap)."""
        if not self.lockstep:
            irq = self.pending_irq()
            if irq >= 0:
                self.wfi = False
                if self.trace is not None:
                    self.trace.append(("irq", irq, self.pc))
                self.take_trap(irq, 0, True)
                return
        if self.wfi:
            return
        pc = self.pc
        self.last_mmio = False
        self.last_store = None
        priv = self.priv
        try:
            if pc & 3:
                raise Trap(MISALIGNED_FETCH, pc)
            ppc = self.translate(pc, FETCH)
            off = ppc - mm.RAM_BASE
            if not (0 <= off <= self.ram_size - 4):
                raise Trap(FETCH_ACCESS, pc)
            insn = self.ram[off] | (self.ram[off + 1] << 8) | (self.ram[off + 2] << 16) | (
                self.ram[off + 3] << 24)
            d = self._dcache.get(insn)
            if d is None:
                d = decode(insn)
                self._dcache[insn] = d
            fn, rd = d
            self.pc = fn(self, pc)
            self.x[0] = 0
            self.instret += 1
            if self.trace is not None:
                self.trace.append((priv, pc, insn, rd, self.x[rd] if rd else 0, self.last_mmio,
                                   self.last_store))
        except Trap as t:
            self.pc = pc
            if self.trace is not None:
                self.trace.append(("trap", t.cause, pc, t.tval))
            self.take_trap(t.cause, t.tval, False)

    def run(self, max_insns: int = 10_000_000, until=None) -> int:
        n = 0
        step = self.step
        while not self.halted and n < max_insns:
            step()
            n += 1
            if self.wfi and not self._wfi_wake():
                if self.on_idle is None or not self.on_idle(self):
                    break
            if until is not None and until(self):
                break
        return n

    def _wfi_wake(self) -> bool:
        """Standalone mode: advance time to the next timer event while WFI."""
        if self.mip() & self.mie:
            self.wfi = False
            return True
        if self.mie & (1 << 7) or (self.mie & (1 << 5)):
            self.clint.instret_ref = self.instret
            if self.clint.mtimecmp != M64 and self.clint.mtimecmp > self.clint.mtime():
                self.clint.set_mtime(self.clint.mtimecmp)
                return True
        return False


# =====================================================================  decode
def _illegal(insn):
    def f(c, pc):
        raise Trap(ILLEGAL, insn)
    return f, 0


def decode(insn: int):
    """Decode one 32-bit instruction into (exec_fn(hart, pc) -> next_pc, rd_written)."""
    op = insn & 0x7F
    rd = (insn >> 7) & 31
    f3 = (insn >> 12) & 7
    rs1 = (insn >> 15) & 31
    rs2 = (insn >> 20) & 31
    f7 = insn >> 25
    imm_i = sx(insn >> 20, 12)
    imm_s = sx(((insn >> 25) << 5) | ((insn >> 7) & 31), 12)
    imm_b = sx((((insn >> 31) & 1) << 12) | (((insn >> 7) & 1) << 11) | (((insn >> 25) & 0x3F) << 5)
               | (((insn >> 8) & 0xF) << 1), 13)
    imm_u = sx(insn & 0xFFFFF000, 32)
    imm_j = sx((((insn >> 31) & 1) << 20) | (((insn >> 12) & 0xFF) << 12) | (((insn >> 20) & 1) << 11)
               | (((insn >> 21) & 0x3FF) << 1), 21)

    if (insn & 3) != 3:
        return _illegal(insn)

    if op == 0x37:  # LUI
        v = imm_u & M64

        def f(c, pc):
            c.x[rd] = v
            return pc + 4
        return f, rd
    if op == 0x17:  # AUIPC
        def f(c, pc):
            c.x[rd] = (pc + imm_u) & M64
            return pc + 4
        return f, rd
    if op == 0x6F:  # JAL
        def f(c, pc):
            t = (pc + imm_j) & M64
            if t & 3:
                raise Trap(MISALIGNED_FETCH, t)
            c.x[rd] = pc + 4
            return t
        return f, rd
    if op == 0x67:
        if f3 != 0:
            return _illegal(insn)

        def f(c, pc):
            t = (c.x[rs1] + imm_i) & M64 & ~1
            if t & 3:
                raise Trap(MISALIGNED_FETCH, t)
            c.x[rd] = pc + 4
            return t
        return f, rd
    if op == 0x63:  # branches
        cmp = {
            0: lambda a, b: a == b,
            1: lambda a, b: a != b,
            4: lambda a, b: s64(a) < s64(b),
            5: lambda a, b: s64(a) >= s64(b),
            6: lambda a, b: a < b,
            7: lambda a, b: a >= b,
        }.get(f3)
        if cmp is None:
            return _illegal(insn)

        def f(c, pc):
            if cmp(c.x[rs1], c.x[rs2]):
                t = (pc + imm_b) & M64
                if t & 3:
                    raise Trap(MISALIGNED_FETCH, t)
                return t
            return pc + 4
        return f, 0
    if op == 0x03:  # loads
        spec = {0: (1, True), 1: (2, True), 2: (4, True), 3: (8, False),
                4: (1, False), 5: (2, False), 6: (4, False)}.get(f3)
        if spec is None:
            return _illegal(insn)
        size, signed = spec
        bits = size * 8

        def f(c, pc):
            v = c.load((c.x[rs1] + imm_i) & M64, size)
            if signed:
                v = sx(v, bits) & M64
            c.x[rd] = v
            return pc + 4
        return f, rd
    if op == 0x23:  # stores
        if f3 > 3:
            return _illegal(insn)
        size = 1 << f3

        def f(c, pc):
            c.store((c.x[rs1] + imm_s) & M64, size, c.x[rs2])
            return pc + 4
        return f, 0
    if op == 0x13:  # OP-IMM
        sh = (insn >> 20) & 0x3F
        f6 = insn >> 26
        if f3 == 0:
            def f(c, pc):
                c.x[rd] = (c.x[rs1] + imm_i) & M64
                return pc + 4
        elif f3 == 2:
            def f(c, pc):
                c.x[rd] = 1 if s64(c.x[rs1]) < imm_i else 0
                return pc + 4
        elif f3 == 3:
            u = imm_i & M64

            def f(c, pc):
                c.x[rd] = 1 if c.x[rs1] < u else 0
                return pc + 4
        elif f3 == 4:
            u = imm_i & M64

            def f(c, pc):
                c.x[rd] = c.x[rs1] ^ u
                return pc + 4
        elif f3 == 6:
            u = imm_i & M64

            def f(c, pc):
                c.x[rd] = c.x[rs1] | u
                return pc + 4
        elif f3 == 7:
            u = imm_i & M64

            def f(c, pc):
                c.x[rd] = c.x[rs1] & u
                return pc + 4
        elif f3 == 1:
            if f6 != 0:
                return _illegal(insn)

            def f(c, pc):
                c.x[rd] = (c.x[rs1] << sh) & M64
                return pc + 4
        else:  # f3 == 5
            if f6 == 0:
                def f(c, pc):
                    c.x[rd] = c.x[rs1] >> sh
                    return pc + 4
            elif f6 == 0x10:
                def f(c, pc):
                    c.x[rd] = (s64(c.x[rs1]) >> sh) & M64
                    return pc + 4
            else:
                return _illegal(insn)
        return f, rd
    if op == 0x1B:  # OP-IMM-32
        sh = (insn >> 20) & 0x1F
        if f3 == 0:
            def f(c, pc):
                c.x[rd] = sext32(c.x[rs1] + imm_i)
                return pc + 4
        elif f3 == 1 and f7 == 0:
            def f(c, pc):
                c.x[rd] = sext32(c.x[rs1] << sh)
                return pc + 4
        elif f3 == 5 and f7 == 0:
            def f(c, pc):
                c.x[rd] = sext32((c.x[rs1] & M32) >> sh)
                return pc + 4
        elif f3 == 5 and f7 == 0x20:
            def f(c, pc):
                c.x[rd] = sext32(sx(c.x[rs1], 32) >> sh)
                return pc + 4
        else:
            return _illegal(insn)
        return f, rd
    if op == 0x33:  # OP
        fn = _OP.get((f7, f3))
        if fn is None:
            return _illegal(insn)

        def f(c, pc):
            c.x[rd] = fn(c.x[rs1], c.x[rs2]) & M64
            return pc + 4
        return f, rd
    if op == 0x3B:  # OP-32
        fn = _OP32.get((f7, f3))
        if fn is None:
            return _illegal(insn)

        def f(c, pc):
            c.x[rd] = sext32(fn(c.x[rs1] & M32, c.x[rs2] & M32))
            return pc + 4
        return f, rd
    if op == 0x0F:
        if f3 == 0:  # FENCE (all variants behave as a full fence here)
            return (lambda c, pc: pc + 4), 0
        if f3 == 1:  # FENCE.I
            return (lambda c, pc: pc + 4), 0
        return _illegal(insn)
    if op == 0x2F:  # AMO
        if f3 not in (2, 3):
            return _illegal(insn)
        size = 4 if f3 == 2 else 8
        f5 = insn >> 27
        if f5 == 2:  # LR
            if rs2:
                return _illegal(insn)

            def f(c, pc):
                va = c.x[rs1]
                if va & (size - 1):
                    raise Trap(MISALIGNED_LOAD, va)
                pa = c.translate(va, LOAD)
                if not mm.is_ram(pa, c.ram_size):
                    raise Trap(LOAD_ACCESS, va)
                v = c.phys_read(pa, size, LOAD)
                c.x[rd] = sx(v, 32) & M64 if size == 4 else v
                c.resv = pa
                return pc + 4
            return f, rd
        if f5 == 3:  # SC
            def f(c, pc):
                va = c.x[rs1]
                pa = c.amo_pa(va, size)
                if c.resv == pa:
                    c.phys_write(pa, size, c.x[rs2])
                    c.x[rd] = 0
                else:
                    c.x[rd] = 1
                c.resv = None
                return pc + 4
            return f, rd
        op_fn = _AMO.get(f5)
        if op_fn is None:
            return _illegal(insn)
        bits = size * 8
        mask = (1 << bits) - 1

        def f(c, pc):
            va = c.x[rs1]
            pa = c.amo_pa(va, size)
            old = c.phys_read(pa, size, LOAD)
            new = op_fn(sx(old, bits), sx(c.x[rs2], bits), old, c.x[rs2] & mask) & mask
            c.phys_write(pa, size, new)
            c.x[rd] = sx(old, bits) & M64
            return pc + 4
        return f, rd
    if op == 0x73:
        if f3 == 0:
            if insn == 0x00000073:  # ECALL
                def f(c, pc):
                    raise Trap((ECALL_U, ECALL_S, 10, ECALL_M)[c.priv], 0)
                return f, 0
            if insn == 0x00100073:  # EBREAK
                def f(c, pc):
                    raise Trap(BREAKPOINT, pc)
                return f, 0
            if insn == 0x30200073:  # MRET
                def f(c, pc):
                    if c.priv < 3:
                        raise Trap(ILLEGAL, insn)
                    ms = c.mstatus
                    mpp = (ms >> MPP_SHIFT) & 3
                    ms = (ms & ~(MIE | (3 << MPP_SHIFT))) | (MIE if ms & MPIE else 0) | MPIE
                    if mpp != 3:
                        ms &= ~MPRV
                    c.mstatus = ms
                    c.priv = mpp
                    c._tlb.clear()
                    return c.mepc
                return f, 0
            if insn == 0x10200073:  # SRET
                def f(c, pc):
                    if c.priv < 1 or (c.priv == 1 and c.mstatus & TSR):
                        raise Trap(ILLEGAL, insn)
                    ms = c.mstatus
                    spp = 1 if ms & SPP else 0
                    ms = (ms & ~(SIE | SPP)) | (SIE if ms & SPIE else 0) | SPIE
                    ms &= ~MPRV
                    c.mstatus = ms
                    c.priv = spp
                    c._tlb.clear()
                    return c.sepc
                return f, 0
            if insn == 0x10500073:  # WFI
                def f(c, pc):
                    if c.priv == 0 or (c.priv == 1 and c.mstatus & TW):
                        raise Trap(ILLEGAL, insn)
                    if not c.lockstep and not (c.mip() & c.mie):
                        c.wfi = True
                    return pc + 4
                return f, 0
            if f7 == 0x09 and rd == 0:  # SFENCE.VMA
                def f(c, pc):
                    if c.priv == 0 or (c.priv == 1 and c.mstatus & TVM):
                        raise Trap(ILLEGAL, insn)
                    c._tlb.clear()
                    return pc + 4
                return f, 0
            return _illegal(insn)
        if f3 == 4:
            return _illegal(insn)
        csr = insn >> 20
        kind = f3 & 3
        use_imm = f3 & 4
        src = rs1

        def f(c, pc):
            v = src if use_imm else c.x[src]
            writes = kind == 1 or src != 0
            if (csr >> 10) == 3 and writes:
                raise Trap(ILLEGAL, insn)
            try:
                old = c.csr_read(csr)  # reads have no side effects in this hart
                if writes:
                    # read-modify-write of mip/sip uses the software-writable bits only
                    # (privileged spec 3.1.9: SEIP's RMW sees the software bit)
                    base = c.mip_sw if csr in (0x344, 0x144) else old
                    if kind == 1:
                        new = v
                    elif kind == 2:
                        new = base | v
                    else:
                        new = base & ~v
                    c.csr_write(csr, new)
            except Trap as t:
                if t.cause == ILLEGAL:
                    t.tval = insn
                raise
            c.x[rd] = old
            return pc + 4
        return f, rd
    return _illegal(insn)


def _div(a, b):
    a, b = s64(a), s64(b)
    if b == 0:
        return M64
    if a == -(1 << 63) and b == -1:
        return a & M64
    q = abs(a) // abs(b)
    return (-q if (a < 0) != (b < 0) else q) & M64


def _rem(a, b):
    a, b = s64(a), s64(b)
    if b == 0:
        return a & M64
    if a == -(1 << 63) and b == -1:
        return 0
    r = abs(a) % abs(b)
    return (-r if a < 0 else r) & M64


def _div32(a, b):
    a, b = sx(a, 32), sx(b, 32)
    if b == 0:
        return M32
    if a == -(1 << 31) and b == -1:
        return a
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _rem32(a, b):
    a, b = sx(a, 32), sx(b, 32)
    if b == 0:
        return a
    if a == -(1 << 31) and b == -1:
        return 0
    r = abs(a) % abs(b)
    return -r if a < 0 else r


_OP = {
    (0, 0): lambda a, b: a + b,
    (0x20, 0): lambda a, b: a - b,
    (0, 1): lambda a, b: a << (b & 63),
    (0, 2): lambda a, b: 1 if s64(a) < s64(b) else 0,
    (0, 3): lambda a, b: 1 if a < b else 0,
    (0, 4): lambda a, b: a ^ b,
    (0, 5): lambda a, b: a >> (b & 63),
    (0x20, 5): lambda a, b: s64(a) >> (b & 63),
    (0, 6): lambda a, b: a | b,
    (0, 7): lambda a, b: a & b,
    (1, 0): lambda a, b: a * b,
    (1, 1): lambda a, b: (s64(a) * s64(b)) >> 64,
    (1, 2): lambda a, b: (s64(a) * b) >> 64,
    (1, 3): lambda a, b: (a * b) >> 64,
    (1, 4): _div,
    (1, 5): lambda a, b: M64 if b == 0 else a // b,
    (1, 6): _rem,
    (1, 7): lambda a, b: a if b == 0 else a % b,
}
_OP32 = {
    (0, 0): lambda a, b: a + b,
    (0x20, 0): lambda a, b: a - b,
    (0, 1): lambda a, b: a << (b & 31),
    (0, 5): lambda a, b: a >> (b & 31),
    (0x20, 5): lambda a, b: sx(a, 32) >> (b & 31),
    (1, 0): lambda a, b: a * b,
    (1, 4): _div32,
    (1, 5): lambda a, b: M32 if b == 0 else a // b,
    (1, 6): _rem32,
    (1, 7): lambda a, b: a if b == 0 else a % b,
}
_AMO = {  # f(signed_old, signed_src, unsigned_old, unsigned_src)
    0x01: lambda so, ss, uo, us: us,                 # SWAP
    0x00: lambda so, ss, uo, us: uo + us,            # ADD
    0x04: lambda so, ss, uo, us: uo ^ us,            # XOR
    0x0C: lambda so, ss, uo, us: uo & us,            # AND
    0x08: lambda so, ss, uo, us: uo | us,            # OR
    0x10: lambda so, ss, uo, us: min(so, ss),        # MIN
    0x14: lambda so, ss, uo, us: max(so, ss),        # MAX
    0x18: lambda so, ss, uo, us: min(uo, us),        # MINU
    0x1C: lambda so, ss, uo, us: max(uo, us),        # MAXU
}
