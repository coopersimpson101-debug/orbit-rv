/* Freestanding <alloca.h> (dhrystone_main.c). */
#ifndef ORBIT_RV_SHIM_ALLOCA_H
#define ORBIT_RV_SHIM_ALLOCA_H
#define alloca(n) __builtin_alloca(n)
#endif
