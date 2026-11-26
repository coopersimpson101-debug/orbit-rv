/* GCC-style <stdatomic.h> subset for riscv-tests benchmarks/common/util.h,
 * which applies atomic_* to plain (non-_Atomic) volatile ints. GCC accepts
 * that, clang's own header does not, so map onto the __atomic builtins. */
#ifndef ORBIT_RV_SHIM_STDATOMIC_H
#define ORBIT_RV_SHIM_STDATOMIC_H
typedef enum {
  memory_order_relaxed = __ATOMIC_RELAXED, memory_order_consume = __ATOMIC_CONSUME,
  memory_order_acquire = __ATOMIC_ACQUIRE, memory_order_release = __ATOMIC_RELEASE,
  memory_order_acq_rel = __ATOMIC_ACQ_REL, memory_order_seq_cst = __ATOMIC_SEQ_CST
} memory_order;
#define atomic_load_explicit(p, o) __atomic_load_n((p), (o))
#define atomic_store_explicit(p, v, o) __atomic_store_n((p), (v), (o))
#define atomic_fetch_add_explicit(p, v, o) __atomic_fetch_add((p), (v), (o))
#define atomic_fetch_add(p, v) __atomic_fetch_add((p), (v), __ATOMIC_SEQ_CST)
#define atomic_load(p) __atomic_load_n((p), __ATOMIC_SEQ_CST)
#define atomic_store(p, v) __atomic_store_n((p), (v), __ATOMIC_SEQ_CST)
#endif
