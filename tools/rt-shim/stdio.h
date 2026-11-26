/* Minimal freestanding <stdio.h> for riscv-tests env/v and benchmarks/common
 * (the functions are implemented in their syscalls.c / not used at all). */
#ifndef ORBIT_RV_SHIM_STDIO_H
#define ORBIT_RV_SHIM_STDIO_H
int printf(const char *fmt, ...);
int sprintf(char *str, const char *fmt, ...);
int putchar(int ch);
#endif
