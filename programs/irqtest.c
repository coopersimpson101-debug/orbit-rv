// irqtest: machine timer and software interrupts through the CLINT.
// Takes 5 timer interrupts (re-arming mtimecmp each time) and one software
// interrupt (msip), sleeping in WFI in between. Exit code 0 = pass.
#include <stdint.h>
#include "util.h"

int printf(const char *fmt, ...);

#define CLINT_MSIP ((volatile uint32_t *)0x02000000)
#define CLINT_MTIMECMP ((volatile uint64_t *)0x02004000)
#define CLINT_MTIME ((volatile uint64_t *)0x0200BFF8)

static volatile int ticks, soft;

uintptr_t handle_trap(uintptr_t cause, uintptr_t epc, uintptr_t regs[32]) {
  (void)regs;
  if (cause == 0x8000000000000007ull) {        // machine timer
    ticks++;
    *CLINT_MTIMECMP = ticks < 5 ? *CLINT_MTIME + 2000 : ~0ull;
    return epc;
  }
  if (cause == 0x8000000000000003ull) {        // machine software
    soft++;
    *CLINT_MSIP = 0;
    return epc;
  }
  printf("unexpected trap cause %lx epc %lx\n", cause, epc);
  exit(3);
  return epc;
}

int main(void) {
  *CLINT_MTIMECMP = *CLINT_MTIME + 2000;
  write_csr(mie, (1 << 7) | (1 << 3));
  set_csr(mstatus, 1 << 3);
  while (ticks < 5)
    asm volatile("wfi");
  *CLINT_MSIP = 1;
  while (!soft)
    asm volatile("wfi");
  clear_csr(mstatus, 1 << 3);
  printf("irqtest: %d timer, %d software interrupts\n", ticks, soft);
  return (ticks == 5 && soft == 1) ? 0 : 1;
}
