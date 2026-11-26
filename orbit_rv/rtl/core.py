"""orbit-rv core: 5-stage in-order RV64IMA_Zicsr_Zifencei pipeline.

    F  fetch: PC, BTB/2-bit prediction, ITLB, I-cache tag compare
    D  decode, register read (WB bypass), load-use hazard detection
    X  execute: ALU, branch resolution (redirect on mispredict), M unit, address
    M  memory: DTLB, D-cache / MMIO, AMO/LR/SC, CSRs, traps and interrupts
    W  write-back + commit trace

The commit point is the end of M: an instruction that leaves M can no longer
trap; exceptions and interrupts are taken there, precisely. Instructions that
change machine state seen by younger instructions (CSR writes, xRET, WFI,
SFENCE.VMA, FENCE.I) redirect fetch to the next PC after they commit.
"""
from __future__ import annotations

from amaranth import Array, C, Cat, Elaboratable, Module, Mux, Signal
from amaranth.lib import memory

from .. import memmap as mm
from . import decode as dc
from .bus import Bus
from .cache import DCache, ICache
from .mmu import ACC_FETCH, ACC_LOAD, ACC_STORE, FAULT_ACCESS, FAULT_PAGE, PTW, TLB
from .muldiv import MulDiv

# exception causes
MISALIGNED_FETCH, FETCH_ACCESS, ILLEGAL, BREAKPOINT = 0, 1, 2, 3
MISALIGNED_LOAD, LOAD_ACCESS, MISALIGNED_STORE, STORE_ACCESS = 4, 5, 6, 7
ECALL_U, FETCH_PAGE, LOAD_PAGE, STORE_PAGE = 8, 12, 13, 15

MSTATUS_FIXED = (2 << 32) | (2 << 34)
MISA = (2 << 62) | (1 << 0) | (1 << 8) | (1 << 12) | (1 << 18) | (1 << 20)


def sext39(v):
    return Cat(v[:39], v[38].replicate(25))


class Core(Elaboratable):
    def __init__(self, ram_size: int = mm.RAM_SIZE_SIM, icache: int = 4096, dcache: int = 4096,
                 btb_entries: int = 32, itlb: int = 4, dtlb: int = 8):
        self.ram_size = ram_size
        self.icache_size, self.dcache_size = icache, dcache
        self.btb_n = btb_entries
        self.itlb_n, self.dtlb_n = itlb, dtlb
        self.ibus = Bus("ibus")
        self.dbus = Bus("dbus")
        self.mtip = Signal()
        self.msip = Signal()
        self.meip = Signal()
        self.seip = Signal()
        self.mtime = Signal(64)
        self.boot_pc = Signal(64, init=mm.RESET_PC)  # sampled once after reset
        # commit trace (W stage)
        self.tr_kind = Signal(2)    # 0 none, 1 commit, 2 exception, 3 interrupt
        self.tr_pc = Signal(64)
        self.tr_insn = Signal(32)
        self.tr_priv = Signal(2)
        self.tr_rd = Signal(5)
        self.tr_data = Signal(64)
        self.tr_cause = Signal(5)
        self.tr_tval = Signal(64)
        self.tr_nondet = Signal()
        self.tr_store = Signal()
        self.tr_addr = Signal(32)
        self.tr_wdata = Signal(64)
        # performance counters
        self.perf = {k: Signal(64, name=f"perf_{k}") for k in (
            "cycles", "instret", "ic_access", "ic_miss", "dc_access", "dc_miss", "branches",
            "mispredicts", "itlb_miss", "dtlb_miss", "walks", "stall_load_use", "stall_mem",
            "traps")}

    def elaborate(self, platform):
        m = Module()
        m.submodules.ic = ic = ICache(self.icache_size)
        m.submodules.dcache = dcc = DCache(self.dcache_size)
        m.submodules.itlb = itlb = TLB(self.itlb_n, "itlb")
        m.submodules.dtlb = dtlb = TLB(self.dtlb_n, "dtlb")
        m.submodules.ptw = ptw = PTW(self.ram_size)
        m.submodules.md = md = MulDiv()
        m.submodules.dec = dec = dc.Decoder()

        ic.bus.connect_master_to(m, self.ibus)

        # ================================================================ CSR state
        priv = Signal(2, init=3)
        st_sie, st_mie, st_spie, st_mpie, st_spp = (Signal(name=n) for n in
                                                    ("st_sie", "st_mie", "st_spie", "st_mpie", "st_spp"))
        st_mpp = Signal(2)
        st_mprv, st_sum, st_mxr, st_tvm, st_tw, st_tsr = (Signal(name=n) for n in (
            "st_mprv", "st_sum", "st_mxr", "st_tvm", "st_tw", "st_tsr"))
        mstatus = Signal(64)
        m.d.comb += mstatus.eq(Cat(C(0, 1), st_sie, C(0, 1), st_mie, C(0, 1), st_spie, C(0, 1),
                                   st_mpie, st_spp, C(0, 2), st_mpp, C(0, 4), st_mprv, st_sum,
                                   st_mxr, st_tvm, st_tw, st_tsr, C(0, 9), C(0xA, 4)))
        medeleg = Signal(16)
        mideleg = Signal(12)
        mie = Signal(12)
        mip_sw = Signal(12)
        mtvec, stvec, mepc, sepc = (Signal(64, name=n) for n in ("mtvec", "stvec", "mepc", "sepc"))
        mcause, scause, mtval, stval = (Signal(64, name=n) for n in ("mcause", "scause", "mtval", "stval"))
        mscratch, sscratch = Signal(64), Signal(64)
        mcounteren, scounteren = Signal(32), Signal(32)
        satp_mode = Signal()
        satp_ppn = Signal(44)
        mcycle, minstret = Signal(64), Signal(64)
        mip = Signal(12)
        m.d.comb += mip.eq((mip_sw & 0x222) | (self.msip << 3) | (self.mtip << 7)
                           | (self.meip << 11) | (self.seip << 9))
        m.d.comb += ptw.satp_ppn.eq(satp_ppn)
        resv_valid = Signal()
        resv_addr = Signal(32)

        # interrupt selection
        pend = mip & mie
        m_en = (priv != 3) | st_mie
        s_en = (priv == 0) | ((priv == 1) & st_sie)
        m_pend = Mux(m_en, pend & ~mideleg, 0)
        s_pend = Mux(s_en, pend & mideleg, 0)
        irq_any = Signal()
        irq_cause = Signal(4)
        sel_src = Mux(m_pend != 0, m_pend, s_pend)
        m.d.comb += irq_any.eq((m_pend | s_pend) != 0)
        for bit in (5, 1, 9, 7, 3, 11):  # last assignment wins: highest priority last
            with m.If(sel_src[bit]):
                m.d.comb += irq_cause.eq(bit)

        # ================================================================ pipeline regs
        # F
        f_pc = Signal(64, init=mm.RESET_PC)
        f_valid = Signal(init=0)
        booted = Signal()
        f_exc_pend = Signal()
        f_exc_cause = Signal(5)
        # D
        d_valid, d_exc = Signal(), Signal()
        d_pc = Signal(64)
        d_insn = Signal(32)
        d_cause = Signal(5)
        d_tval = Signal(64)
        d_pred, d_pred_tgt = Signal(), Signal(64)
        # X
        x_valid, x_exc = Signal(), Signal()
        x_pc = Signal(64)
        x_insn = Signal(32)
        x_cause = Signal(5)
        x_tval = Signal(64)
        x_pred, x_pred_tgt = Signal(), Signal(64)
        x_rs1v, x_rs2v = Signal(64), Signal(64)
        x_rs1, x_rs2, x_rd = Signal(5), Signal(5), Signal(5)
        x_rd_we, x_imm = Signal(), Signal(64)
        x_alu_op, x_alu_w, x_a_sel, x_b_sel = Signal(4), Signal(), Signal(2), Signal()
        x_branch, x_jal, x_jalr = Signal(), Signal(), Signal()
        x_mem, x_size, x_uns, x_amo = Signal(3), Signal(2), Signal(), Signal(5)
        x_md, x_csr, x_sys, x_late = Signal(), Signal(), Signal(3), Signal()
        md_pending = Signal()
        # M
        m_valid, m_exc = Signal(), Signal()
        m_pc = Signal(64)
        m_insn = Signal(32)
        m_cause = Signal(5)
        m_tval = Signal(64)
        m_rd, m_rd_we = Signal(5), Signal()
        m_res = Signal(64)
        m_addr = Signal(64)
        m_sdata = Signal(64)
        m_mem, m_size, m_uns, m_amo = Signal(3), Signal(2), Signal(), Signal(5)
        m_csr, m_sys, m_late = Signal(), Signal(3), Signal()
        m_rs1 = Signal(5)
        m_started = Signal()
        m_pf = Signal(2)         # latched walker fault for this instruction
        m_fi_done = Signal()
        m_waiting = Signal()     # M was stalled last cycle
        amo_buf = Signal(64)     # AMO result computed in the first hit cycle
        amo_ready = Signal()
        # W
        w_valid, w_we = Signal(), Signal()
        w_rd = Signal(5)
        w_data = Signal(64)

        # ================================================================ register file
        m.submodules.rf = rf = memory.Memory(shape=64, depth=32, init=[])
        rf_w = rf.write_port()
        rf_r1 = rf.read_port(domain="comb")
        rf_r2 = rf.read_port(domain="comb")
        m.d.comb += [rf_w.en.eq(w_valid & w_we & (w_rd != 0)), rf_w.addr.eq(w_rd),
                     rf_w.data.eq(w_data)]

        # ================================================================ control signals
        mem_redirect = Signal()
        mem_target = Signal(64)
        x_redirect = Signal()
        x_target = Signal(64)
        mem_stall = Signal()
        x_stall = Signal()
        d_stall = Signal()
        load_use = Signal()
        trap_now = Signal()

        # ================================================================ F stage
        btb_n = self.btb_n
        bi = (btb_n - 1).bit_length()
        btb_valid = Signal(btb_n)
        btb_tag = Array(Signal(13, name=f"btb_tag{i}") for i in range(btb_n))
        btb_tgt = Array(Signal(39, name=f"btb_tgt{i}") for i in range(btb_n))
        btb_ctr = Array(Signal(2, name=f"btb_ctr{i}", init=0) for i in range(btb_n))
        f_bidx = f_pc[2:2 + bi]
        f_btb_hit = btb_valid.bit_select(f_bidx, 1) & (btb_tag[f_bidx] == f_pc[2 + bi:15 + bi])
        f_pred = Signal()
        f_pred_tgt = Signal(64)
        m.d.comb += [f_pred.eq(f_btb_hit & btb_ctr[f_bidx][1]), f_pred_tgt.eq(sext39(btb_tgt[f_bidx]))]

        i_tr_on = (priv != 3) & satp_mode
        f_canon = (f_pc[38:64] == 0) | (f_pc[38:64] == (1 << 26) - 1)
        m.d.comb += [itlb.va.eq(f_pc), itlb.acc.eq(ACC_FETCH), itlb.priv.eq(priv),
                     itlb.sum.eq(st_sum), itlb.mxr.eq(st_mxr)]
        f_pa = Signal(32)
        f_pa_ok = Signal()      # translation available
        f_f0 = Signal()
        f_c0 = Signal(5)
        with m.If(i_tr_on):
            m.d.comb += [f_pa.eq(itlb.pa), f_pa_ok.eq(itlb.hit & f_canon)]
            with m.If(~f_canon):
                m.d.comb += [f_f0.eq(1), f_c0.eq(FETCH_PAGE)]
            with m.Elif(itlb.hit & itlb.fault):
                m.d.comb += [f_f0.eq(1), f_c0.eq(FETCH_PAGE)]
        with m.Else():
            m.d.comb += [f_pa.eq(f_pc[:32]), f_pa_ok.eq(f_pc[32:] == 0)]
            with m.If(f_pc[32:] != 0):
                m.d.comb += [f_f0.eq(1), f_c0.eq(FETCH_ACCESS)]
        f_in_ram = (f_pa >= mm.RAM_BASE) & (f_pa < mm.RAM_BASE + self.ram_size)
        f_fault = Signal()
        f_fault_cause = Signal(5)
        with m.If(f_exc_pend):
            m.d.comb += [f_fault.eq(1), f_fault_cause.eq(f_exc_cause)]
        with m.Elif(f_f0):
            m.d.comb += [f_fault.eq(1), f_fault_cause.eq(f_c0)]
        with m.Elif(f_pa_ok & ~f_in_ram):
            m.d.comb += [f_fault.eq(1), f_fault_cause.eq(FETCH_ACCESS)]
        m.d.comb += [ptw.ireq.eq(f_valid & i_tr_on & f_canon & ~itlb.hit & ~f_exc_pend),
                     ptw.iva.eq(f_pc)]
        with m.If(ptw.done & ptw.side & (ptw.va[12:39] == f_pc[12:39]) & (ptw.fault != 0)
                  & f_valid & ~mem_redirect & ~x_redirect):
            m.d.sync += [f_exc_pend.eq(1),
                         f_exc_cause.eq(Mux(ptw.fault == FAULT_PAGE, FETCH_PAGE, FETCH_ACCESS))]
        m.d.comb += [ic.s1_valid.eq(f_valid & f_pa_ok & ~f_fault), ic.s1_pa.eq(f_pa),
                     ic.s1_kill.eq(0)]
        f_has = f_valid & (ic.s1_hit | f_fault)  # F delivers something to D this cycle
        f_insn = Mux(f_pc[2], ic.s1_rdata[32:], ic.s1_rdata[:32])
        f_take = f_has & ~d_stall
        npc = Signal(64)
        with m.If(mem_redirect):
            m.d.comb += npc.eq(mem_target)
        with m.Elif(x_redirect):
            m.d.comb += npc.eq(x_target)
        with m.Elif(f_take & ~f_fault):
            m.d.comb += npc.eq(Mux(f_pred, f_pred_tgt, f_pc + 4))
        with m.Else():
            m.d.comb += npc.eq(f_pc)
        f_adv = mem_redirect | x_redirect | (f_take & ~f_fault)
        m.d.comb += [ic.s0_addr.eq(npc[:12]), ic.advance.eq(f_adv)]
        m.d.sync += f_pc.eq(npc)
        with m.If(mem_redirect | x_redirect):
            m.d.sync += [f_valid.eq(1), f_exc_pend.eq(0)]
        with m.Elif(f_take & f_fault):
            m.d.sync += [f_valid.eq(0), f_exc_pend.eq(0)]
        with m.If(~booted):
            m.d.sync += [f_pc.eq(self.boot_pc), f_valid.eq(1), booted.eq(1)]
        # ITLB refill from the walker
        m.d.comb += [itlb.ins.eq(ptw.done & ptw.side & (ptw.fault == 0)),
                     itlb.ins_vpn.eq(ptw.va[12:39]), itlb.ins_ppn.eq(ptw.leaf_ppn),
                     itlb.ins_level.eq(ptw.leaf_level), itlb.ins_flags.eq(ptw.leaf_flags)]

        # F -> D
        with m.If(mem_redirect | x_redirect):
            m.d.sync += d_valid.eq(0)
        with m.Elif(~d_stall):
            m.d.sync += [d_valid.eq(f_has), d_pc.eq(f_pc), d_insn.eq(Mux(f_fault, 0, f_insn)),
                         d_exc.eq(f_fault), d_cause.eq(f_fault_cause), d_tval.eq(f_pc),
                         d_pred.eq(f_pred & ~f_fault), d_pred_tgt.eq(f_pred_tgt)]

        # ================================================================ D stage
        m.d.comb += [dec.insn.eq(d_insn), rf_r1.addr.eq(dec.rs1), rf_r2.addr.eq(dec.rs2)]
        d_rs1v = Mux(w_valid & w_we & (w_rd == dec.rs1) & (dec.rs1 != 0), w_data, rf_r1.data)
        d_rs2v = Mux(w_valid & w_we & (w_rd == dec.rs2) & (dec.rs2 != 0), w_data, rf_r2.data)
        m.d.comb += load_use.eq(d_valid & ~d_exc & x_valid & x_late & x_rd_we & (x_rd != 0)
                                & ((dec.rs1_used & (dec.rs1 == x_rd))
                                   | (dec.rs2_used & (dec.rs2 == x_rd))))
        m.d.comb += d_stall.eq(x_stall | load_use)
        with m.If(mem_redirect | x_redirect):
            m.d.sync += x_valid.eq(0)
        with m.Elif(~x_stall):
            with m.If(load_use):
                m.d.sync += x_valid.eq(0)
            with m.Else():
                ok = ~(d_exc | dec.illegal)
                m.d.sync += [x_valid.eq(d_valid), x_pc.eq(d_pc), x_insn.eq(d_insn),
                             x_exc.eq(d_exc | dec.illegal),
                             x_cause.eq(Mux(d_exc, d_cause, ILLEGAL)),
                             x_tval.eq(Mux(d_exc, d_tval, d_insn)),
                             x_pred.eq(d_pred), x_pred_tgt.eq(d_pred_tgt),
                             x_rs1v.eq(d_rs1v), x_rs2v.eq(d_rs2v),
                             x_rs1.eq(dec.rs1), x_rs2.eq(dec.rs2), x_rd.eq(dec.rd),
                             x_rd_we.eq(dec.rd_we & ok & (dec.rd != 0)), x_imm.eq(dec.imm),
                             x_alu_op.eq(dec.alu_op), x_alu_w.eq(dec.alu_w),
                             x_a_sel.eq(dec.a_sel), x_b_sel.eq(dec.b_sel),
                             x_branch.eq(dec.branch & ok), x_jal.eq(dec.jal & ok),
                             x_jalr.eq(dec.jalr & ok),
                             x_mem.eq(Mux(ok, dec.mem, 0)), x_size.eq(dec.mem_size),
                             x_uns.eq(dec.mem_unsigned), x_amo.eq(dec.amo_op),
                             x_md.eq(dec.muldiv & ok), x_csr.eq(dec.csr & ok),
                             x_sys.eq(Mux(ok, dec.sys, 0)), x_late.eq(dec.late & ok)]

        # ================================================================ X stage
        m_fwd_ok = m_valid & m_rd_we & ~m_late & ~m_exc
        fwd1 = Mux(m_fwd_ok & (m_rd == x_rs1), m_res,
                   Mux(w_valid & w_we & (w_rd == x_rs1) & (x_rs1 != 0), w_data, x_rs1v))
        fwd2 = Mux(m_fwd_ok & (m_rd == x_rs2), m_res,
                   Mux(w_valid & w_we & (w_rd == x_rs2) & (x_rs2 != 0), w_data, x_rs2v))
        rs1 = Signal(64)
        rs2 = Signal(64)
        m.d.comb += [rs1.eq(Mux(x_rs1 == 0, 0, fwd1)), rs2.eq(Mux(x_rs2 == 0, 0, fwd2))]
        with m.If(x_stall):  # keep forwarded operands while waiting
            m.d.sync += [x_rs1v.eq(rs1), x_rs2v.eq(rs2)]
        op_a = Mux(x_a_sel == dc.A_PC, x_pc, Mux(x_a_sel == dc.A_ZERO, 0, rs1))
        op_b = Mux(x_b_sel == dc.B_IMM, x_imm, rs2)
        alu = Signal(64)
        shamt = Mux(x_alu_w, op_b[:5], op_b[:6])
        with m.Switch(x_alu_op):
            with m.Case(dc.ADD):
                m.d.comb += alu.eq(op_a + op_b)
            with m.Case(dc.SUB):
                m.d.comb += alu.eq(op_a - op_b)
            with m.Case(dc.SLL):
                m.d.comb += alu.eq(op_a << shamt)
            with m.Case(dc.SLT):
                m.d.comb += alu.eq(op_a.as_signed() < op_b.as_signed())
            with m.Case(dc.SLTU):
                m.d.comb += alu.eq(op_a < op_b)
            with m.Case(dc.XOR):
                m.d.comb += alu.eq(op_a ^ op_b)
            with m.Case(dc.SRL):
                m.d.comb += alu.eq(Mux(x_alu_w, op_a[:32] >> shamt, op_a >> shamt))
            with m.Case(dc.SRA):
                m.d.comb += alu.eq(Mux(x_alu_w, op_a[:32].as_signed() >> shamt,
                                       op_a.as_signed() >> shamt))
            with m.Case(dc.OR):
                m.d.comb += alu.eq(op_a | op_b)
            with m.Case(dc.AND):
                m.d.comb += alu.eq(op_a & op_b)
        alu_res = Mux(x_alu_w, alu[:32].as_signed(), alu)

        # branches
        f3 = x_insn[12:15]
        eq = rs1 == rs2
        lt = rs1.as_signed() < rs2.as_signed()
        ltu = rs1 < rs2
        cond = Signal()
        with m.Switch(f3):
            with m.Case(0):
                m.d.comb += cond.eq(eq)
            with m.Case(1):
                m.d.comb += cond.eq(~eq)
            with m.Case(4):
                m.d.comb += cond.eq(lt)
            with m.Case(5):
                m.d.comb += cond.eq(~lt)
            with m.Case(6):
                m.d.comb += cond.eq(ltu)
            with m.Case(7):
                m.d.comb += cond.eq(~ltu)
        taken = x_jal | x_jalr | (x_branch & cond)
        tgt = Signal(64)
        m.d.comb += tgt.eq(Mux(x_jalr, (rs1 + x_imm) & ~1, x_pc + x_imm))
        pc4 = x_pc + 4
        is_ctrl = x_branch | x_jal | x_jalr
        actual = Mux(taken, tgt, pc4)
        predicted = Mux(x_pred, x_pred_tgt, pc4)
        x_misalign = taken & tgt[1]
        x_ok = x_valid & ~x_exc
        x_fire = x_valid & ~x_stall & ~mem_redirect
        m.d.comb += [x_redirect.eq(x_fire & ~x_exc & ~x_misalign & (actual != predicted)),
                     x_target.eq(actual)]

        # BTB update
        x_bidx = x_pc[2:2 + bi]
        x_btb_hit = btb_valid.bit_select(x_bidx, 1) & (btb_tag[x_bidx] == x_pc[2 + bi:15 + bi])
        with m.If(x_fire & ~x_exc):
            with m.If(is_ctrl):
                with m.If(taken & ~x_misalign):
                    m.d.sync += [btb_valid.bit_select(x_bidx, 1).eq(1),
                                 btb_tag[x_bidx].eq(x_pc[2 + bi:15 + bi]),
                                 btb_tgt[x_bidx].eq(tgt[:39])]
                    with m.If(x_btb_hit & ~(x_jal | x_jalr)):
                        m.d.sync += btb_ctr[x_bidx].eq(Mux(btb_ctr[x_bidx] == 3, 3, btb_ctr[x_bidx] + 1))
                    with m.Else():
                        m.d.sync += btb_ctr[x_bidx].eq(Mux(x_jal | x_jalr, 3, 2))
                with m.Elif(x_btb_hit):
                    m.d.sync += btb_ctr[x_bidx].eq(Mux(btb_ctr[x_bidx] == 0, 0, btb_ctr[x_bidx] - 1))
            with m.Elif(x_pred):
                m.d.sync += btb_valid.bit_select(x_bidx, 1).eq(0)

        # M unit
        md_start = x_valid & x_md & ~md_pending & ~md.busy & ~mem_redirect
        m.d.comb += [md.start.eq(md_start), md.op.eq(f3), md.word.eq(x_alu_w),
                     md.a.eq(rs1), md.b.eq(rs2)]
        with m.If(mem_redirect):
            m.d.sync += md_pending.eq(0)
        with m.Elif(md_start):
            m.d.sync += md_pending.eq(1)
        with m.Elif(md_pending & md.done & ~x_stall):
            m.d.sync += md_pending.eq(0)
        x_busy = x_valid & x_md & ~(md_pending & md.done)
        m.d.comb += x_stall.eq(mem_stall | x_busy)

        x_result = Mux(x_jal | x_jalr, pc4, Mux(x_md, md.result, alu_res))
        x_addr = rs1 + x_imm
        x_exc_out = x_exc | (x_ok & x_misalign)

        with m.If(mem_redirect):
            m.d.sync += [m_valid.eq(0), amo_ready.eq(0)]
        with m.Elif(~mem_stall):
            m.d.sync += [m_started.eq(0), m_pf.eq(0), m_fi_done.eq(0), amo_ready.eq(0)]
            with m.If(x_stall):
                m.d.sync += m_valid.eq(0)
            with m.Else():
                m.d.sync += [m_valid.eq(x_valid), m_pc.eq(x_pc), m_insn.eq(x_insn),
                             m_exc.eq(x_exc_out),
                             m_cause.eq(Mux(x_exc, x_cause, MISALIGNED_FETCH)),
                             m_tval.eq(Mux(x_exc, x_tval, tgt)),
                             m_rd.eq(x_rd), m_rd_we.eq(x_rd_we & ~x_exc_out), m_res.eq(x_result),
                             m_addr.eq(x_addr), m_sdata.eq(Mux(x_csr, rs1, rs2)), m_mem.eq(x_mem),
                             m_size.eq(x_size), m_uns.eq(x_uns), m_amo.eq(x_amo),
                             m_csr.eq(x_csr), m_sys.eq(x_sys), m_late.eq(x_late),
                             m_rs1.eq(x_rs1)]

        # ================================================================ M stage
        is_mem = (m_mem != dc.MEM_NONE) & ~m_exc
        is_wr = (m_mem == dc.MEM_STORE) | (m_mem == dc.MEM_AMO) | (m_mem == dc.MEM_SC)
        acc = Mux(is_wr, ACC_STORE, ACC_LOAD)
        priv_d = Mux(st_mprv, st_mpp, priv)
        d_tr_on = (priv_d != 3) & satp_mode
        size_mask = Mux(m_size == 0, 0, Mux(m_size == 1, 1, Mux(m_size == 2, 3, 7)))
        misal = (m_addr[:3] & size_mask) != 0
        d_canon = (m_addr[38:64] == 0) | (m_addr[38:64] == (1 << 26) - 1)
        m.d.comb += [dtlb.va.eq(m_addr), dtlb.acc.eq(acc), dtlb.priv.eq(priv_d),
                     dtlb.sum.eq(st_sum), dtlb.mxr.eq(st_mxr)]
        m_pa = Signal(32)
        m_pa_ok = Signal()
        mx = Signal()             # memory-stage detected exception
        mx_cause = Signal(5)
        pf_cause = Mux(is_wr, STORE_PAGE, LOAD_PAGE)
        af_cause = Mux(is_wr, STORE_ACCESS, LOAD_ACCESS)
        with m.If(d_tr_on):
            m.d.comb += [m_pa.eq(dtlb.pa), m_pa_ok.eq(dtlb.hit & d_canon)]
        with m.Else():
            m.d.comb += [m_pa.eq(m_addr[:32]), m_pa_ok.eq(1)]
        in_ram = (m_pa >= mm.RAM_BASE) & (m_pa < mm.RAM_BASE + self.ram_size)
        in_io = (((m_pa >= mm.CLINT_BASE) & (m_pa < mm.CLINT_BASE + mm.CLINT_SIZE))
                 | ((m_pa >= mm.PLIC_BASE) & (m_pa < mm.PLIC_BASE + mm.PLIC_SIZE))
                 | ((m_pa >= mm.UART_BASE) & (m_pa < mm.UART_BASE + mm.UART_SIZE))
                 | ((m_pa >= mm.FINISHER_BASE) & (m_pa < mm.FINISHER_BASE + mm.FINISHER_SIZE)))
        atomic = (m_mem == dc.MEM_AMO) | (m_mem == dc.MEM_LR) | (m_mem == dc.MEM_SC)
        with m.If(is_mem):
            with m.If(misal):
                m.d.comb += [mx.eq(1), mx_cause.eq(Mux(m_mem == dc.MEM_LOAD, MISALIGNED_LOAD,
                                                       Mux(m_mem == dc.MEM_LR, MISALIGNED_LOAD,
                                                           MISALIGNED_STORE)))]
            with m.Elif(d_tr_on & ~d_canon):
                m.d.comb += [mx.eq(1), mx_cause.eq(pf_cause)]
            with m.Elif(m_pf != 0):
                m.d.comb += [mx.eq(1), mx_cause.eq(Mux(m_pf == FAULT_PAGE, pf_cause, af_cause))]
            with m.Elif(d_tr_on & dtlb.hit & dtlb.fault):
                m.d.comb += [mx.eq(1), mx_cause.eq(pf_cause)]
            with m.Elif(m_pa_ok & (~d_tr_on & (m_addr[32:] != 0))):
                m.d.comb += [mx.eq(1), mx_cause.eq(af_cause)]
            with m.Elif(m_pa_ok & ~in_ram & (~in_io | atomic)):
                m.d.comb += [mx.eq(1), mx_cause.eq(af_cause)]
        need_walk = is_mem & ~misal & d_tr_on & d_canon & ~dtlb.hit & (m_pf == 0)
        m.d.comb += [ptw.dreq.eq(m_valid & need_walk), ptw.dva.eq(m_addr)]
        with m.If(ptw.done & ~ptw.side & (ptw.va[12:39] == m_addr[12:39]) & m_valid & need_walk
                  & (ptw.fault != 0)):
            m.d.sync += m_pf.eq(ptw.fault)
        m.d.comb += [dtlb.ins.eq(ptw.done & ~ptw.side & (ptw.fault == 0)),
                     dtlb.ins_vpn.eq(ptw.va[12:39]), dtlb.ins_ppn.eq(ptw.leaf_ppn),
                     dtlb.ins_level.eq(ptw.leaf_level), dtlb.ins_flags.eq(ptw.leaf_flags)]
        mem_go = m_valid & is_mem & m_pa_ok & ~mx      # access may proceed
        cache_req = mem_go & in_ram
        io_req = mem_go & ~in_ram

        # load data / AMO
        word = dcc.s1_rdata
        lane = m_addr[:3]
        shifted = Signal(64)
        m.d.comb += shifted.eq(word >> (lane * 8))
        load_val = Signal(64)
        with m.Switch(Cat(m_size, m_uns)):
            with m.Case(0):
                m.d.comb += load_val.eq(shifted[:8].as_signed())
            with m.Case(1):
                m.d.comb += load_val.eq(shifted[:16].as_signed())
            with m.Case(2):
                m.d.comb += load_val.eq(shifted[:32].as_signed())
            with m.Case(3, 7):
                m.d.comb += load_val.eq(shifted)
            with m.Case(4):
                m.d.comb += load_val.eq(shifted[:8])
            with m.Case(5):
                m.d.comb += load_val.eq(shifted[:16])
            with m.Case(6):
                m.d.comb += load_val.eq(shifted[:32])
        is_w = m_size == 2
        old = Signal(64)
        src = Signal(64)
        m.d.comb += [old.eq(Mux(is_w, shifted[:32].as_signed(), shifted)),
                     src.eq(Mux(is_w, m_sdata[:32].as_signed(), m_sdata))]
        amo_new = Signal(64)
        with m.Switch(m_amo):
            with m.Case(0x01):
                m.d.comb += amo_new.eq(src)
            with m.Case(0x00):
                m.d.comb += amo_new.eq(old + src)
            with m.Case(0x04):
                m.d.comb += amo_new.eq(old ^ src)
            with m.Case(0x0C):
                m.d.comb += amo_new.eq(old & src)
            with m.Case(0x08):
                m.d.comb += amo_new.eq(old | src)
            with m.Case(0x10):
                m.d.comb += amo_new.eq(Mux(old.as_signed() < src.as_signed(), old, src))
            with m.Case(0x14):
                m.d.comb += amo_new.eq(Mux(old.as_signed() < src.as_signed(), src, old))
            with m.Case(0x18):
                m.d.comb += amo_new.eq(Mux(old < src, old, src))
            with m.Case(0x1C):
                m.d.comb += amo_new.eq(Mux(old < src, src, old))
        # AMOs take two hit cycles: compute (cache data -> ALU -> register), then write,
        # so the block-RAM read data never feeds the block-RAM write data in one cycle.
        is_amo = m_mem == dc.MEM_AMO
        st_val = Mux(is_amo, amo_buf, m_sdata)
        bytes_ = Mux(m_size == 0, 0x01, Mux(m_size == 1, 0x03, Mux(m_size == 2, 0x0F, 0xFF)))
        wmask = (bytes_ << lane)[:8]
        wdata = (st_val << (lane * 8))[:64]
        sc_ok = resv_valid & (resv_addr == m_pa)

        # D-cache port: owned by the walker while it is busy
        with m.If(ptw.busy):
            m.d.comb += [dcc.s0_addr.eq(ptw.c_s0), dcc.advance.eq(ptw.c_advance),
                         dcc.s1_valid.eq(ptw.c_valid), dcc.s1_pa.eq(ptw.c_pa)]
        with m.Else():
            # Timing: "advance" is predicted from last cycle's stall (a register) instead
            # of this cycle's hit, keeping the DTLB + tag compare off the RAM address
            # path. A wrong guess only costs a re-read (r_ok in the cache).
            m.d.comb += [dcc.s0_addr.eq(x_addr[:12]), dcc.advance.eq(~m_waiting),
                         dcc.s1_valid.eq(cache_req & ~((m_mem == dc.MEM_SC) & ~sc_ok)),
                         dcc.s1_pa.eq(m_pa),
                         dcc.s1_write.eq(cache_req & is_wr & ~((m_mem == dc.MEM_SC) & ~sc_ok)
                                         & (~is_amo | amo_ready)),
                         dcc.s1_wdata.eq(wdata), dcc.s1_wmask.eq(wmask)]
        m.d.comb += [dcc.s1_kill.eq(trap_now), ptw.c_hit.eq(dcc.s1_hit), ptw.c_rdata.eq(dcc.s1_rdata)]
        cache_hit = ~ptw.busy & dcc.s1_hit
        cache_done = cache_hit & (~is_amo | amo_ready)
        with m.If(cache_req & is_amo & cache_hit & ~amo_ready & ~trap_now):
            m.d.sync += [amo_buf.eq(amo_new), amo_ready.eq(1)]

        # uncached I/O
        io_done = Signal()
        io_data = Signal(64)
        io_active = Signal()
        with m.If(dcc.idle & ~ptw.busy & io_req & ~trap_now & ~io_done):
            m.d.comb += [io_active.eq(1), self.dbus.cyc.eq(1), self.dbus.we.eq(is_wr),
                         self.dbus.adr.eq(Cat(C(0, 3), m_pa[3:])), self.dbus.sel.eq(wmask),
                         self.dbus.dat_w.eq(wdata)]
            m.d.sync += m_started.eq(1)
            with m.If(self.dbus.ack):
                m.d.sync += [io_done.eq(1), io_data.eq(self.dbus.dat_r)]
        with m.If(~io_active):
            m.d.comb += [self.dbus.cyc.eq(dcc.bus.cyc), self.dbus.we.eq(dcc.bus.we),
                         self.dbus.adr.eq(dcc.bus.adr), self.dbus.sel.eq(dcc.bus.sel),
                         self.dbus.dat_w.eq(dcc.bus.dat_w), dcc.bus.ack.eq(self.dbus.ack)]
        m.d.comb += dcc.bus.dat_r.eq(self.dbus.dat_r)
        with m.If(~mem_stall | mem_redirect):
            m.d.sync += io_done.eq(0)
        io_shift = (io_data >> (lane * 8))[:64]
        io_val = Signal(64)
        with m.Switch(Cat(m_size, m_uns)):
            with m.Case(0):
                m.d.comb += io_val.eq(io_shift[:8].as_signed())
            with m.Case(1):
                m.d.comb += io_val.eq(io_shift[:16].as_signed())
            with m.Case(2):
                m.d.comb += io_val.eq(io_shift[:32].as_signed())
            with m.Case(3, 7):
                m.d.comb += io_val.eq(io_shift)
            with m.Case(4):
                m.d.comb += io_val.eq(io_shift[:8])
            with m.Case(5):
                m.d.comb += io_val.eq(io_shift[:16])
            with m.Case(6):
                m.d.comb += io_val.eq(io_shift[:32])

        # ---------------------------------------------------------------- CSRs
        csr_addr = m_insn[20:32]
        csr_f3 = m_insn[12:15]
        csr_src = Mux(csr_f3[2], m_insn[15:20], m_sdata)  # m_sdata holds rs1 for CSR ops
        csr_rdata = Signal(64)
        csr_exists = Signal()
        csr_nondet = Signal()
        cnt_ok = Signal()
        cbit = csr_addr[:5]
        m.d.comb += cnt_ok.eq((priv == 3) | (mcounteren.bit_select(cbit, 1)
                                            & ((priv == 1) | scounteren.bit_select(cbit, 1))))
        sstatus_mask = (1 << 1) | (1 << 5) | (1 << 8) | (1 << 18) | (1 << 19) | (3 << 32)
        with m.Switch(csr_addr):
            for a, v in ((0x300, mstatus), (0x100, mstatus & sstatus_mask), (0x301, C(MISA, 64)),
                         (0x302, medeleg), (0x303, mideleg), (0x304, mie), (0x104, mie & mideleg),
                         (0x344, mip), (0x144, mip & mideleg), (0x305, mtvec), (0x105, stvec),
                         (0x340, mscratch), (0x140, sscratch), (0x341, mepc), (0x141, sepc),
                         (0x342, mcause), (0x142, scause), (0x343, mtval), (0x143, stval),
                         (0x306, mcounteren), (0x106, scounteren), (0xB00, mcycle),
                         (0xB02, minstret)):
                with m.Case(a):
                    m.d.comb += [csr_rdata.eq(v), csr_exists.eq(1)]
                    if a in (0x344, 0x144, 0xB00):
                        m.d.comb += csr_nondet.eq(1)
            with m.Case(0x180):
                m.d.comb += [csr_rdata.eq(Cat(satp_ppn, C(0, 16), C(0, 3), satp_mode)),
                             csr_exists.eq(~((priv == 1) & st_tvm))]
            with m.Case(0xC00):
                m.d.comb += [csr_rdata.eq(mcycle), csr_exists.eq(cnt_ok), csr_nondet.eq(1)]
            with m.Case(0xC01):
                m.d.comb += [csr_rdata.eq(self.mtime), csr_exists.eq(cnt_ok), csr_nondet.eq(1)]
            with m.Case(0xC02):
                m.d.comb += [csr_rdata.eq(minstret), csr_exists.eq(cnt_ok)]
            with m.Case(*range(0xC03, 0xC20)):
                m.d.comb += csr_exists.eq(cnt_ok)
            with m.Case(0xF11, 0xF12, 0xF13, 0xF14, 0xF15, 0x30A, 0x10A, 0x320,
                        *range(0xB03, 0xB20), *range(0x323, 0x340), *range(0x3A0, 0x3F0),
                        0x7A0, 0x7A1, 0x7A2, 0x7A3):
                m.d.comb += csr_exists.eq(1)
        csr_writes = (csr_f3[:2] == 1) | (m_insn[15:20] != 0)
        csr_ill = m_csr & (~csr_exists | (csr_addr[8:10] > priv)
                           | (csr_writes & (csr_addr[10:12] == 3)))
        base = Mux((csr_addr == 0x344) | (csr_addr == 0x144), mip_sw, csr_rdata)
        csr_wval = Signal(64)
        with m.Switch(csr_f3[:2]):
            with m.Case(1):
                m.d.comb += csr_wval.eq(csr_src)
            with m.Case(2):
                m.d.comb += csr_wval.eq(base | csr_src)
            with m.Case(3):
                m.d.comb += csr_wval.eq(base & ~csr_src)

        # privileged instruction legality
        sys_ill = Signal()
        with m.Switch(m_sys):
            with m.Case(dc.SYS_MRET):
                m.d.comb += sys_ill.eq(priv != 3)
            with m.Case(dc.SYS_SRET):
                m.d.comb += sys_ill.eq((priv == 0) | ((priv == 1) & st_tsr))
            with m.Case(dc.SYS_WFI):
                m.d.comb += sys_ill.eq((priv == 0) | ((priv == 1) & st_tw))
            with m.Case(dc.SYS_SFENCE):
                m.d.comb += sys_ill.eq((priv == 0) | ((priv == 1) & st_tvm))

        # ---------------------------------------------------------------- trap decision
        exc = Signal()
        exc_cause = Signal(5)
        exc_tval = Signal(64)
        with m.If(m_exc):
            m.d.comb += [exc.eq(1), exc_cause.eq(m_cause), exc_tval.eq(m_tval)]
        with m.Elif(mx):
            m.d.comb += [exc.eq(1), exc_cause.eq(mx_cause), exc_tval.eq(m_addr)]
        with m.Elif(csr_ill | sys_ill):
            m.d.comb += [exc.eq(1), exc_cause.eq(ILLEGAL), exc_tval.eq(m_insn)]
        with m.Elif(m_sys == dc.SYS_ECALL):
            m.d.comb += [exc.eq(1), exc_cause.eq(ECALL_U + priv)]
        with m.Elif(m_sys == dc.SYS_EBREAK):
            m.d.comb += [exc.eq(1), exc_cause.eq(BREAKPOINT), exc_tval.eq(m_pc)]
        take_irq = m_valid & irq_any & ~m_started & (m_sys != dc.SYS_WFI)
        m.d.comb += trap_now.eq(m_valid & (take_irq | exc))
        t_cause = Mux(take_irq, irq_cause, exc_cause)
        t_tval = Mux(take_irq, 0, exc_tval)
        deleg = (priv != 3) & Mux(take_irq, mideleg.bit_select(t_cause[:4], 1),
                                  medeleg.bit_select(t_cause[:4], 1))
        tvec = Mux(deleg, stvec, mtvec)
        t_target = Mux(take_irq & tvec[0], Cat(C(0, 2), tvec[2:]) + (t_cause << 2),
                       Cat(C(0, 2), tvec[2:]))

        # completion of the instruction in M
        wfi_ok = (mip & mie) != 0
        fence_i = m_sys == dc.SYS_FENCEI
        m.d.comb += [dcc.flush.eq(m_valid & fence_i & ~m_fi_done & ~exc),
                     ic.invalidate.eq(dcc.flush_done & m_valid & fence_i & ~m_fi_done)]
        with m.If(dcc.flush_done & m_valid & fence_i):
            m.d.sync += m_fi_done.eq(1)
        sc_fail = (m_mem == dc.MEM_SC) & ~sc_ok
        m_done = Signal()
        with m.If(is_mem):
            m.d.comb += m_done.eq(Mux(in_ram, cache_done | (sc_fail & m_pa_ok), io_done))
        with m.Elif(m_sys == dc.SYS_WFI):
            m.d.comb += m_done.eq(wfi_ok)
        with m.Elif(fence_i):
            m.d.comb += m_done.eq(m_fi_done & ~ic.inv_busy)
        with m.Else():
            m.d.comb += m_done.eq(1)
        commit = m_valid & ~trap_now & m_done
        m.d.comb += mem_stall.eq(m_valid & ~trap_now & ~m_done)
        m.d.sync += m_waiting.eq(mem_stall & ~mem_redirect)

        serialize = m_csr & csr_writes | (m_sys == dc.SYS_WFI) | (m_sys == dc.SYS_SFENCE) | fence_i
        m.d.comb += [itlb.flush.eq(commit & ((m_sys == dc.SYS_SFENCE) | (m_csr & csr_writes & (csr_addr == 0x180)))),
                     dtlb.flush.eq(commit & ((m_sys == dc.SYS_SFENCE) | (m_csr & csr_writes & (csr_addr == 0x180))))]
        with m.If(trap_now):
            m.d.comb += [mem_redirect.eq(1), mem_target.eq(t_target)]
        with m.Elif(commit & (m_sys == dc.SYS_MRET)):
            m.d.comb += [mem_redirect.eq(1), mem_target.eq(mepc)]
        with m.Elif(commit & (m_sys == dc.SYS_SRET)):
            m.d.comb += [mem_redirect.eq(1), mem_target.eq(sepc)]
        with m.Elif(commit & serialize):
            m.d.comb += [mem_redirect.eq(1), mem_target.eq(m_pc + 4)]

        # result value
        m_value = Signal(64)
        with m.If(m_csr):
            m.d.comb += m_value.eq(csr_rdata)
        with m.Elif(m_mem == dc.MEM_SC):
            m.d.comb += m_value.eq(sc_fail)
        with m.Elif(is_mem & ~in_ram):
            m.d.comb += m_value.eq(io_val)
        with m.Elif(is_mem & ((m_mem == dc.MEM_AMO) | (m_mem == dc.MEM_LR))):
            m.d.comb += m_value.eq(old)
        with m.Elif(is_mem):
            m.d.comb += m_value.eq(load_val)
        with m.Else():
            m.d.comb += m_value.eq(m_res)

        # ---------------------------------------------------------------- CSR updates
        m.d.sync += mcycle.eq(mcycle + 1)
        with m.If(commit):
            m.d.sync += minstret.eq(minstret + 1)
            with m.If(m_mem == dc.MEM_LR):
                m.d.sync += [resv_valid.eq(1), resv_addr.eq(m_pa)]
            with m.If(m_mem == dc.MEM_SC):
                m.d.sync += resv_valid.eq(0)
            with m.If(m_sys == dc.SYS_MRET):
                m.d.sync += [priv.eq(st_mpp), st_mie.eq(st_mpie), st_mpie.eq(1), st_mpp.eq(0)]
                with m.If(st_mpp != 3):
                    m.d.sync += st_mprv.eq(0)
            with m.If(m_sys == dc.SYS_SRET):
                m.d.sync += [priv.eq(st_spp), st_sie.eq(st_spie), st_spie.eq(1), st_spp.eq(0),
                             st_mprv.eq(0)]
            with m.If(m_csr & csr_writes):
                v = csr_wval
                with m.Switch(csr_addr):
                    with m.Case(0x300):
                        m.d.sync += [st_sie.eq(v[1]), st_mie.eq(v[3]), st_spie.eq(v[5]),
                                     st_mpie.eq(v[7]), st_spp.eq(v[8]),
                                     st_mpp.eq(Mux(v[11:13] == 2, 0, v[11:13])),
                                     st_mprv.eq(v[17]), st_sum.eq(v[18]), st_mxr.eq(v[19]),
                                     st_tvm.eq(v[20]), st_tw.eq(v[21]), st_tsr.eq(v[22])]
                    with m.Case(0x100):
                        m.d.sync += [st_sie.eq(v[1]), st_spie.eq(v[5]), st_spp.eq(v[8]),
                                     st_sum.eq(v[18]), st_mxr.eq(v[19])]
                    with m.Case(0x302):
                        m.d.sync += medeleg.eq(v[:16] & 0xB3FF)
                    with m.Case(0x303):
                        m.d.sync += mideleg.eq(v[:12] & 0x222)
                    with m.Case(0x304):
                        m.d.sync += mie.eq(v[:12] & 0xAAA)
                    with m.Case(0x104):
                        m.d.sync += mie.eq((mie & ~mideleg) | (v[:12] & mideleg))
                    with m.Case(0x344):
                        m.d.sync += mip_sw.eq(v[:12] & 0x222)
                    with m.Case(0x144):
                        m.d.sync += mip_sw.eq((mip_sw & ~(mideleg & 2)) | (v[:12] & mideleg & 2))
                    with m.Case(0x305):
                        m.d.sync += mtvec.eq(v & ~2)
                    with m.Case(0x105):
                        m.d.sync += stvec.eq(v & ~2)
                    with m.Case(0x340):
                        m.d.sync += mscratch.eq(v)
                    with m.Case(0x140):
                        m.d.sync += sscratch.eq(v)
                    with m.Case(0x341):
                        m.d.sync += mepc.eq(v & ~3)
                    with m.Case(0x141):
                        m.d.sync += sepc.eq(v & ~3)
                    with m.Case(0x342):
                        m.d.sync += mcause.eq(v)
                    with m.Case(0x142):
                        m.d.sync += scause.eq(v)
                    with m.Case(0x343):
                        m.d.sync += mtval.eq(v)
                    with m.Case(0x143):
                        m.d.sync += stval.eq(v)
                    with m.Case(0x306):
                        m.d.sync += mcounteren.eq(v[:32])
                    with m.Case(0x106):
                        m.d.sync += scounteren.eq(v[:32])
                    with m.Case(0x180):
                        with m.If((v[60:64] == 0) | (v[60:64] == 8)):
                            m.d.sync += [satp_mode.eq(v[63]), satp_ppn.eq(v[:44])]
                    with m.Case(0xB00):
                        m.d.sync += mcycle.eq(v)
                    with m.Case(0xB02):
                        m.d.sync += minstret.eq(v)
        with m.If(trap_now):
            m.d.sync += resv_valid.eq(0)
            with m.If(deleg):
                m.d.sync += [scause.eq(Cat(t_cause, C(0, 58), take_irq)), sepc.eq(m_pc),
                             stval.eq(t_tval), st_spp.eq(priv[0]), st_spie.eq(st_sie),
                             st_sie.eq(0), priv.eq(1)]
            with m.Else():
                m.d.sync += [mcause.eq(Cat(t_cause, C(0, 58), take_irq)), mepc.eq(m_pc),
                             mtval.eq(t_tval), st_mpp.eq(priv), st_mpie.eq(st_mie),
                             st_mie.eq(0), priv.eq(3)]

        # ================================================================ W stage
        m.d.sync += [w_valid.eq(commit), w_we.eq(m_rd_we & (m_rd != 0)), w_rd.eq(m_rd),
                     w_data.eq(m_value)]
        # commit trace
        nondet = (is_mem & ~in_ram & ~is_wr) | (m_csr & csr_nondet)
        m.d.sync += [self.tr_kind.eq(Mux(trap_now, Mux(take_irq, 3, 2), Mux(commit, 1, 0))),
                     self.tr_pc.eq(m_pc), self.tr_insn.eq(m_insn), self.tr_priv.eq(priv),
                     self.tr_rd.eq(Mux(m_rd_we, m_rd, 0)), self.tr_data.eq(m_value),
                     self.tr_cause.eq(t_cause), self.tr_tval.eq(t_tval),
                     self.tr_nondet.eq(nondet),
                     self.tr_store.eq(is_mem & is_wr & ~sc_fail), self.tr_addr.eq(m_pa),
                     self.tr_wdata.eq(st_val)]

        # ================================================================ perf counters
        p = self.perf
        m.d.sync += [p["cycles"].eq(p["cycles"] + 1), p["instret"].eq(p["instret"] + commit)]
        m.d.sync += [p["ic_access"].eq(p["ic_access"] + (f_take & ~f_fault)),
                     p["ic_miss"].eq(p["ic_miss"] + ic.stat_miss),
                     p["dc_access"].eq(p["dc_access"] + (commit & is_mem & in_ram & ~sc_fail)),
                     p["dc_miss"].eq(p["dc_miss"] + (dcc.stat_miss & ~ptw.busy)),
                     p["branches"].eq(p["branches"] + (x_fire & ~x_exc & is_ctrl)),
                     p["mispredicts"].eq(p["mispredicts"] + x_redirect),
                     p["itlb_miss"].eq(p["itlb_miss"] + (ptw.stat_walks & ~ptw.dreq)),
                     p["dtlb_miss"].eq(p["dtlb_miss"] + (ptw.stat_walks & ptw.dreq)),
                     p["walks"].eq(p["walks"] + ptw.stat_walks),
                     p["stall_load_use"].eq(p["stall_load_use"] + (load_use & ~x_stall)),
                     p["stall_mem"].eq(p["stall_mem"] + mem_stall),
                     p["traps"].eq(p["traps"] + trap_now)]
        return m
