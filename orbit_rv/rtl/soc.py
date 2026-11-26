"""orbit-rv SoC: core + bus arbiter + address decoder + CLINT, PLIC, UART, finisher, RAM.

``SoC(external_ram=True)`` exposes the RAM bus as top-level ports so the
CXXRTL testbench can model any memory size; ``external_ram=False`` puts the
RAM into FPGA block RAM (optionally pre-loaded with a program image).
"""
from __future__ import annotations

from amaranth import Elaboratable, Module, Signal

from .. import memmap as mm
from .bus import Arbiter, Bus, Decoder
from .core import Core
from .periph import BlockRam, Clint, Finisher, Plic, Uart


class SoC(Elaboratable):
    def __init__(self, ram_size: int = mm.RAM_SIZE_SIM, external_ram: bool = True, ram_init=(),
                 clk_hz: int = 25_000_000, **core_kw):
        self.ram_size = ram_size
        self.external_ram = external_ram
        self.ram_init = ram_init
        # mtime += num/den per cycle -> 10 MHz
        from math import gcd
        g = gcd(mm.TIMEBASE_HZ, clk_hz)
        self.tb_num, self.tb_den = mm.TIMEBASE_HZ // g, clk_hz // g
        self.core = Core(ram_size=ram_size, **core_kw)
        self.ram = Bus("ram")
        self.uart_tx = Signal(init=1)
        self.uart_rx = Signal(init=1)
        self.uart_div = Signal(16)
        self.fin_done = Signal()
        self.fin_value = Signal(32)
        self.boot_pc = Signal(64, init=mm.RESET_PC)

    def ports(self):
        c = self.core
        ps = [self.boot_pc, self.uart_tx, self.uart_rx, self.uart_div, self.fin_done, self.fin_value,
              c.tr_kind, c.tr_pc, c.tr_insn, c.tr_priv, c.tr_rd, c.tr_data, c.tr_cause, c.tr_tval,
              c.tr_nondet, c.tr_store, c.tr_addr, c.tr_wdata, *c.perf.values()]
        if self.external_ram:
            r = self.ram
            ps += [r.cyc, r.we, r.adr, r.dat_w, r.sel, r.ack, r.dat_r]
        return ps

    def elaborate(self, platform):
        m = Module()
        m.submodules.core = core = self.core
        m.submodules.arb = arb = Arbiter()
        m.submodules.clint = clint = Clint(self.tb_num, self.tb_den)
        m.submodules.plic = plic = Plic()
        m.submodules.uart = uart = Uart()
        m.submodules.fin = fin = Finisher()
        core.dbus.connect_master_to(m, arb.m0)
        core.ibus.connect_master_to(m, arb.m1)
        if self.external_ram:
            ram_bus = self.ram
        else:
            m.submodules.bram = bram = BlockRam(self.ram_size, self.ram_init)
            ram_bus = bram.bus
        regions = [(mm.RAM_BASE, self.ram_size, ram_bus),
                   (mm.CLINT_BASE, mm.CLINT_SIZE, clint.bus),
                   (mm.PLIC_BASE, mm.PLIC_SIZE, plic.bus),
                   (mm.UART_BASE, mm.UART_SIZE, uart.bus),
                   (mm.FINISHER_BASE, mm.FINISHER_SIZE, fin.bus)]
        m.submodules.dec = dec = Decoder(regions)
        arb.out.connect_master_to(m, dec.bus)
        m.d.comb += [plic.src.eq(uart.irq << mm.UART_IRQ),
                     core.mtip.eq(clint.mtip), core.msip.eq(clint.msip),
                     core.meip.eq(plic.irq[0]), core.seip.eq(plic.irq[1]),
                     core.mtime.eq(clint.mtime), core.boot_pc.eq(self.boot_pc),
                     self.uart_tx.eq(uart.tx), uart.rx.eq(self.uart_rx),
                     self.uart_div.eq(uart.divisor),
                     self.fin_done.eq(fin.done), self.fin_value.eq(fin.value)]
        return m
