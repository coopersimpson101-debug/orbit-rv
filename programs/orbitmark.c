// orbitmark: a small CoreMark-style integer workload written for this repo
// (it is NOT CoreMark and its numbers are not comparable to CoreMark scores).
//
// Per iteration: linked-list find/reverse/insertion-sort, a 12x12 integer
// matrix multiply with MAC + shift/mask, a byte-driven state machine and a
// CRC-16 over all results. Built with riscv-tests' crt.S/syscalls (printf
// goes to the UART, exit to tohost).
#include <stdint.h>
#include "util.h"

int printf(const char *fmt, ...);

#define ITER 6
#define NLIST 48
#define N 12

static uint16_t crc16(uint16_t crc, uint32_t v) {
  for (int i = 0; i < 32; i++) {
    uint16_t x = (crc ^ (uint16_t)(v >> i)) & 1;
    crc >>= 1;
    if (x) crc ^= 0xA001;
  }
  return crc;
}

struct node { struct node *next; int32_t key; int32_t val; };
static struct node pool[NLIST];

static struct node *list_init(uint32_t seed) {
  struct node *head = 0;
  for (int i = 0; i < NLIST; i++) {
    seed = seed * 1103515245u + 12345u;
    pool[i].key = (int32_t)((seed >> 8) & 0x3ff);
    pool[i].val = i;
    pool[i].next = head;
    head = &pool[i];
  }
  return head;
}

static struct node *list_reverse(struct node *h) {
  struct node *p = 0;
  while (h) { struct node *n = h->next; h->next = p; p = h; h = n; }
  return p;
}

static struct node *list_sort(struct node *h) {  // insertion sort by key
  struct node *sorted = 0;
  while (h) {
    struct node *n = h->next, **pp = &sorted;
    while (*pp && (*pp)->key < h->key) pp = &(*pp)->next;
    h->next = *pp; *pp = h; h = n;
  }
  return sorted;
}

static int32_t list_find(struct node *h, int32_t key) {
  int32_t steps = 0;
  for (; h; h = h->next, steps++) if (h->key == key) return steps;
  return -1;
}

static int32_t A[N][N], B[N][N], Cm[N][N];

static uint32_t matrix(uint32_t seed) {
  for (int i = 0; i < N; i++)
    for (int j = 0; j < N; j++) {
      seed = seed * 69069u + 1u;
      A[i][j] = (int32_t)(seed >> 20) - 2048;
      B[i][j] = (int32_t)((seed >> 4) & 0xfff) - 2048;
    }
  uint32_t acc = 0;
  for (int i = 0; i < N; i++)
    for (int j = 0; j < N; j++) {
      int64_t s = 0;
      for (int k = 0; k < N; k++) s += (int64_t)A[i][k] * B[k][j];
      Cm[i][j] = (int32_t)(s >> 3);
      acc += (uint32_t)Cm[i][j] ^ (uint32_t)(s & 0xff);
    }
  return acc;
}

// classify a byte stream into numbers / words / punctuation
static uint32_t state_machine(uint32_t seed) {
  enum { START, NUM, WORD, PUNCT } st = START;
  uint32_t counts[4] = {0, 0, 0, 0};
  for (int i = 0; i < 512; i++) {
    seed = seed * 1664525u + 1013904223u;
    char c = " 0123456789abcdefgh.,;!"[(seed >> 16) % 23];
    switch (st) {
      case START: st = (c >= '0' && c <= '9') ? NUM : (c >= 'a' && c <= 'z') ? WORD : (c == ' ' ? START : PUNCT); break;
      case NUM: if (!(c >= '0' && c <= '9')) { counts[NUM]++; st = c == ' ' ? START : (c >= 'a' ? WORD : PUNCT); } break;
      case WORD: if (!(c >= 'a' && c <= 'z')) { counts[WORD]++; st = c == ' ' ? START : (c <= '9' ? NUM : PUNCT); } break;
      case PUNCT: counts[PUNCT]++; st = START; break;
    }
  }
  return counts[1] * 3 + counts[2] * 5 + counts[3] * 7;
}

int main(void) {
  uint16_t crc = 0;
  setStats(1);
  for (int it = 0; it < ITER; it++) {
    struct node *h = list_init(0x1234u + it);
    h = list_reverse(h);
    crc = crc16(crc, (uint32_t)list_find(h, h->next->next->key));
    h = list_sort(h);
    for (struct node *p = h; p; p = p->next) crc = crc16(crc, (uint32_t)p->key);
    crc = crc16(crc, matrix(0xBEEFu + it));
    crc = crc16(crc, state_machine(0xC0DEu * (it + 1)));
  }
  setStats(0);
  printf("orbitmark: %d iterations, crc 0x%x\n", ITER, crc);
  return 0;
}
