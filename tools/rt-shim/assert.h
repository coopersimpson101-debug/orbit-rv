/* Freestanding <assert.h> (qsort_main.c): a failed assert exits with code 2. */
#ifndef ORBIT_RV_SHIM_ASSERT_H
#define ORBIT_RV_SHIM_ASSERT_H
void exit(int code);
#define assert(x) ((x) ? (void)0 : exit(2))
#endif
