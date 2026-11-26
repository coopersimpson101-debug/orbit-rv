/* Minimal freestanding <ctype.h> for riscv-tests env/v. */
#ifndef ORBIT_RV_SHIM_CTYPE_H
#define ORBIT_RV_SHIM_CTYPE_H
static inline int isspace(int c) { return c == ' ' || (c >= '\t' && c <= '\r'); }
static inline int isdigit(int c) { return c >= '0' && c <= '9'; }
#endif
