/* Minimal freestanding <string.h> for riscv-tests env/v (implemented in env/v/string.c). */
#ifndef ORBIT_RV_SHIM_STRING_H
#define ORBIT_RV_SHIM_STRING_H
#include <stddef.h>
void *memcpy(void *dest, const void *src, size_t len);
void *memset(void *dest, int byte, size_t len);
int memcmp(const void *s1, const void *s2, size_t n);
size_t strlen(const char *s);
size_t strnlen(const char *s, size_t n);
int strcmp(const char *s1, const char *s2);
char *strcpy(char *dest, const char *src);
long atol(const char *str);
#endif
