/* Minimal freestanding <sys/signal.h>: riscv-tests benchmarks/common/syscalls.c uses SIGABRT. */
#ifndef ORBIT_RV_SHIM_SYS_SIGNAL_H
#define ORBIT_RV_SHIM_SYS_SIGNAL_H
#define SIGABRT 6
#endif
