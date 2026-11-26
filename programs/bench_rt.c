// Runtime for benchmarks on orbit-rv, replacing riscv-tests'
// benchmarks/common/syscalls.c (which needs GCC nested functions and an HTIF
// host). Same interface: _init/main/exit via tohost, setStats counters,
// printf/sprintf to the 16550 UART, and the small libc subset the
// benchmarks use. Written for this repo.
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

volatile uint64_t tohost __attribute__((section(".tohost")));
volatile uint64_t fromhost __attribute__((section(".tohost")));

#define UART ((volatile uint8_t *)0x10000000)

int main(int argc, char **argv);

static void uart_putc(char c) {
  while (!(UART[5] & 0x20))
    ;
  UART[0] = (uint8_t)c;
}

void __attribute__((noreturn)) tohost_exit(uintptr_t code) {
  tohost = (code << 1) | 1;
  for (;;)
    ;
}

void __attribute__((noreturn)) exit(int code) { tohost_exit((uintptr_t)code); }
void __attribute__((noreturn)) abort(void) { tohost_exit(134); }

uintptr_t __attribute__((weak)) handle_trap(uintptr_t cause, uintptr_t epc, uintptr_t regs[32]) {
  (void)epc; (void)regs;
  tohost_exit(1337 + (cause & 0xff));
}

void __attribute__((weak)) thread_entry(int cid, int nc) {
  (void)nc;
  while (cid != 0)
    ;
}

#define NUM_COUNTERS 2
static uintptr_t counters[NUM_COUNTERS];
static const char *counter_names[NUM_COUNTERS] = {"mcycle", "minstret"};

void setStats(int enable) {
  uintptr_t c, i;
  asm volatile("csrr %0, mcycle" : "=r"(c));
  asm volatile("csrr %0, minstret" : "=r"(i));
  if (!enable) {
    counters[0] = c - counters[0];
    counters[1] = i - counters[1];
  } else {
    counters[0] = c;
    counters[1] = i;
  }
}

void *memcpy(void *dst, const void *src, size_t n) {
  char *d = dst;
  const char *s = src;
  while (n--) *d++ = *s++;
  return dst;
}

void *memset(void *dst, int c, size_t n) {
  char *d = dst;
  while (n--) *d++ = (char)c;
  return dst;
}

size_t strlen(const char *s) { const char *p = s; while (*p) p++; return (size_t)(p - s); }
size_t strnlen(const char *s, size_t n) { size_t i = 0; while (i < n && s[i]) i++; return i; }
int strcmp(const char *a, const char *b) {
  while (*a && *a == *b) a++, b++;
  return (unsigned char)*a - (unsigned char)*b;
}
char *strcpy(char *d, const char *s) { char *r = d; while ((*d++ = *s++)) ; return r; }
long atol(const char *s) {
  long v = 0; int neg = 0;
  while (*s == ' ') s++;
  if (*s == '-' || *s == '+') neg = *s++ == '-';
  while (*s >= '0' && *s <= '9') v = v * 10 + (*s++ - '0');
  return neg ? -v : v;
}

// ---- formatted output: %d %i %u %x %c %s %p %%, with l/ll and width/0 padding
struct sink { char *buf; int n; };

static void emit(struct sink *k, char c) {
  if (k->buf) k->buf[k->n] = c; else uart_putc(c);
  k->n++;
}

static void vfmt(struct sink *k, const char *f, va_list ap) {
  for (; *f; f++) {
    if (*f != '%') { emit(k, *f); continue; }
    f++;
    int pad0 = 0, width = 0, lng = 0;
    if (*f == '0') { pad0 = 1; f++; }
    while (*f >= '0' && *f <= '9') width = width * 10 + (*f++ - '0');
    while (*f == 'l') { lng++; f++; }
    char tmp[24]; int len = 0, neg = 0;
    unsigned long long u = 0;
    switch (*f) {
      case 'd': case 'i': {
        long long v = lng ? va_arg(ap, long long) : va_arg(ap, int);
        if (v < 0) { neg = 1; u = (unsigned long long)-v; } else u = (unsigned long long)v;
        do tmp[len++] = (char)('0' + u % 10); while (u /= 10);
        break;
      }
      case 'u':
        u = lng ? va_arg(ap, unsigned long long) : va_arg(ap, unsigned);
        do tmp[len++] = (char)('0' + u % 10); while (u /= 10);
        break;
      case 'x': case 'p':
        u = (lng || *f == 'p') ? va_arg(ap, unsigned long long) : va_arg(ap, unsigned);
        do tmp[len++] = "0123456789abcdef"[u & 15]; while (u >>= 4);
        break;
      case 'c': tmp[len++] = (char)va_arg(ap, int); break;
      case 's': {
        const char *s = va_arg(ap, const char *);
        int sl = (int)strlen(s);
        for (int i = sl; i < width; i++) emit(k, ' ');
        while (*s) emit(k, *s++);
        continue;
      }
      case '%': emit(k, '%'); continue;
      default: emit(k, '%'); emit(k, *f); continue;
    }
    if (neg) tmp[len++] = '-';
    for (int i = len; i < width; i++) emit(k, pad0 ? '0' : ' ');
    while (len) emit(k, tmp[--len]);
  }
}

int printf(const char *fmt, ...) {
  struct sink k = {0, 0};
  va_list ap;
  va_start(ap, fmt);
  vfmt(&k, fmt, ap);
  va_end(ap);
  return k.n;
}

int sprintf(char *buf, const char *fmt, ...) {
  struct sink k = {buf, 0};
  va_list ap;
  va_start(ap, fmt);
  vfmt(&k, fmt, ap);
  va_end(ap);
  buf[k.n] = 0;
  return k.n;
}

int putchar(int c) { uart_putc((char)c); return c; }
void printstr(const char *s) { while (*s) uart_putc(*s++); }

void _init(int cid, int nc) {
  thread_entry(cid, nc);
  int ret = main(0, 0);
  for (int i = 0; i < NUM_COUNTERS; i++)
    if (counters[i]) printf("%s = %lu\n", counter_names[i], (unsigned long)counters[i]);
  exit(ret);
}
