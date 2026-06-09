# The `NRT_INIT_STATE` Lifecycle

A single 32-bit field at `.bss` `0xc5d1a0` drives every public-API
guard in `libnrt.so.2`. This page documents the four valid states,
every transition observed in the binary, and the API-level guards
that read it.

## The four states

The state is an `int32_t`; valid values 0–3 are mapped to strings via
the `nrt_init_state_strings` table at `.data` `0xc08900`:

| Value | Enum                | String (`.rodata` `0x83ed1f`+) |
|-------|---------------------|--------------------------------|
| 0     | `NRT_STATE_START`   | `"NRT_STATE_START"`            |
| 1     | `NRT_STATE_INIT`    | `"NRT_STATE_INIT"`             |
| 2     | `NRT_STATE_CHILD`   | `"NRT_STATE_CHILD"`            |
| 3     | `NRT_STATE_CLOSED`  | `"NRT_STATE_CLOSED"`           |

The state field is initialized to 0 by the ELF loader (zero-init
`.bss`). The runtime never reads it before the first
`nrt_state_set` call (the START state is implicit in the
zero-initialized field).

## State transitions

There are only four `nrt_state_set` call sites in the entire binary:

| Site address | Transition           | Trigger                                     |
|--------------|----------------------|---------------------------------------------|
| `0x951d6`    | `* → INIT`           | end of successful `nrt_init`                |
| `0x93115`    | `* → CHILD`          | `atfork_child` (post-fork in child only)    |
| `0x93da1`    | `* → CLOSED`         | success path of `nrt_close`                 |
| `0x93eb6`    | `* → CLOSED`         | already-closed cleanup in `nrt_close`       |

### Diagram

```
                  +---------------+
                  | START (0)     |
                  +-------+-------+
                          |
       nrt_init success   |
                          v
                  +---------------+
                  | INIT (1)      |
                  +-------+-------+
                          |
      atfork_child()      |             nrt_close
                          |                |
                  +-------+-------+        v
                  | CHILD (2)     |     +-----------+
                  +---------------+     | CLOSED (3)|
                  (terminal — must     +-----------+
                   _exit or execve)    (terminal — must
                                       _exit; cannot re-init)
```

There is no documented "re-init after close" transition: `nrt_init`
called when state is CLOSED returns `NRT_FAILURE_AFTER_CLOSE` (14).
Recovery from CLOSED requires a fresh process.

## Accessor functions

### `nrt_state_set(NRT_INIT_STATE s)` at `0xb9090`

```
b9090   lea 0xba4109(%rip), %rax       # &nrt_init_state
b9097   mov %edi, (%rax)               # *= s
b9099   xor %eax, %eax
b909b   ret
```

Three instructions. **Non-atomic** — the runtime relies on call sites
being mutually exclusive (only nrt_init, atfork_child, and nrt_close
ever call it, and these never overlap in time within one process).

### `nrt_state_get_string()` at `0xb9060`

```
b9060   lea 0xba4139(%rip), %rax       # &nrt_init_state
b9067   lea 0xb4f892(%rip), %rdx       # &nrt_init_state_strings
b906e   movslq (%rax), %rax            # state as 64-bit
b9071   mov (%rdx, %rax, 8), %rax      # string ptr
b9075   ret
```

Five instructions. No bounds check — out-of-range states would
return garbage. The four legal values are guaranteed by the call
sites, so this is safe in practice.

### `nrt_state_is_init()` at `0xb9080`

```
b9080   lea 0xba4119(%rip), %rax       # &nrt_init_state
b9087   mov (%rax), %eax
b9089   cmp $0x1, %eax
b908c   sete %al
b908f   ret
```

Returns 1 iff state == INIT.

## State guards in public APIs

Every public-facing entry point in libnrt consults the state at
function start. The guard pattern is identical across APIs:

### `nrt_init` guard

```c
if (nrt_init_state == NRT_STATE_INIT) {
    nlog_write(API_IN); nlog_write(API_OUT);
    return NRT_SUCCESS;        // idempotent re-init
}
if (nrt_init_state == NRT_STATE_CLOSED) {
    nlog_write("NRT already closed");
    return 14;                 // NRT_FAILURE_AFTER_CLOSE
}
if (nrt_init_state == NRT_STATE_CHILD) {
    nlog_write("Incompatible runtime state: CHILD");
    return 1;                  // NRT_FAILURE
}
// state == START: proceed with bootstrap
```

### `nrt_execute` / `nrt_load` / `nrt_unload` guard

```c
if (nrt_init_state != NRT_STATE_INIT) {
    switch (nrt_init_state) {
        case NRT_STATE_START:   return 13;  // NRT_UNINITIALIZED
        case NRT_STATE_CLOSED:  return 14;  // NRT_CLOSED
        case NRT_STATE_CHILD:   return 1;   // NRT_FAILURE (with descriptive log)
        default:                return 1;
    }
}
// state == INIT: proceed
```

### `nrt_close` guard

`nrt_close` accepts states INIT and CHILD; the latter is the only
API that can run cleanly in the post-fork child. Behavior:

```c
if (nrt_init_state == NRT_STATE_CLOSED)
    return NRT_FAILURE;        // already closed
if (nrt_init_state == NRT_STATE_START)
    return NRT_FAILURE;        // never initialised; nothing to close
// state is INIT or CHILD: proceed with cleanup
// ...
nrt_state_set(NRT_STATE_CLOSED);
```

In the CHILD path, `nrt_close` does NOT attempt to invoke the kernel
side (the kernel's CRWL claim belongs to the parent's fd, not the
child's). The child's `nrt_close` is purely a state transition and a
log write — safe but does nothing productive.

## Why non-atomic?

The state field is read concurrently from many threads (every worker
calls `nrt_state_is_init` or equivalent at the top of its dispatch
loop), but written only by:

- The main thread during `nrt_init` (no other libnrt activity)
- The post-fork child's main thread during `atfork_child` (single
  thread by definition)
- The main thread during `nrt_close` (workers are signaled to stop
  before state changes)

So the writes never race against the reads in well-behaved usage.
On x86-64, the `mov %edi, (%rax)` is atomic with respect to other
4-byte reads at the same address (the CPU guarantees this), so even
a concurrent reader will see either the old or new value — never a
torn value. This is sufficient for the use case.

On ARM64 (where libnrt is also built and shipped for Graviton hosts),
the absence of explicit memory barriers means a concurrent reader on
a different CPU could see a stale value briefly. This is acceptable
because the state machine is monotonic in the relevant directions
(START → INIT is set early enough to be visible before any worker is
spawned; INIT → CLOSED is set after all workers are joined).

## Implications of the START sentinel

Because `.bss` initializes to 0, the state field starts at
`NRT_STATE_START` (0) before any code runs. This has subtle
consequences:

1. **Failed `nrt_init` leaves state at START.** If `nrt_init` returns
   non-zero (e.g., no devices found, CRWL conflict, version
   mismatch), the state field never advances past 0. Subsequent
   API calls see UNINITIALIZED (13).

2. **A second `nrt_init` after a failed first one is OK.** Since the
   state is still START, the second call goes through the bootstrap
   logic from the beginning. The runtime is designed to be safely
   retryable from the START state.

3. **There is no "FAILED" state.** A failed `nrt_init` leaves state
   at START, which is indistinguishable from "never initialised".
   The framework must rely on the return code to distinguish.

## Implications of the CLOSED terminal

Once state is CLOSED, the only way back is to exit the process and
start fresh. This is deliberate:

- Re-init would require re-claiming CRWL on every NC range, which
  would race against any other process that grabbed them.
- Re-init would require re-mmaping BARs, but the previous mmaps may
  not have been fully unmapped (if `nrt_close` failed mid-cleanup).
- Re-init would require re-spawning workers, but the state of the
  per-XU eventfds is uncertain.

The "close means goodbye" policy keeps the runtime's invariants
simple at the cost of forcing process restart for error recovery.

## Implications of the CHILD terminal

Setting state to CHILD in `atfork_child` is the runtime's safety
mechanism for fork() safety. The child can still call:

- `nrt_close()` — state transitions to CLOSED, but no kernel-side
  cleanup is attempted (which would corrupt the parent).
- `nrt_state_get_string()` — pure-read, always safe.
- Frameworks' own error-reporting paths that check `nrt_state_is_init`
  and fail gracefully.

The child cannot legitimately call:

- `nrt_init`, `nrt_load`, `nrt_unload`, `nrt_execute`,
  `nrta_execute_schedule`, `nrt_get_*`, or any other inference API.
  All return NRT_FAILURE with a log.

See [atfork.md](atfork.md) for the full discussion.

## Race window during `nrt_init`

The most subtle race in the runtime is the window between:

1. `pthread_atfork` registration (in `nrt_init` at `0x95e9d`)
2. `nrt_state_set(NRT_STATE_INIT)` (in `nrt_init` at `0x951d6`)

During this window, a `fork()` from the application thread would
trigger `atfork_child` (now registered) which sets state to CHILD.
But state is currently START (init not yet complete), so the
transition is `START → CHILD`.

The child then sees state=CHILD when checking; any API call returns
NRT_FAILURE. This is consistent — the child cannot use libnrt
regardless of whether init was complete in the parent. The parent
continues `nrt_init` and reaches INIT.

This is not a bug; it is the desired behaviour. A child of a process
mid-`nrt_init` should not be able to use the partially-initialised
runtime.

## Race window during `nrt_close`

A symmetric window exists during `nrt_close`:

1. `nrt_state_set(NRT_STATE_CLOSED)` (at `0x93da1` or `0x93eb6`)
2. The final `return` from `nrt_close`

During this window (a few hundred ns), worker threads are already
joined and the kernel-side resources are released. A concurrent
`fork()` here would have state-CHILD in the child, which is also
the safe answer.

## State as the cornerstone of safety

The whole concurrency design of libnrt rests on the assumption that
the state field is consulted at the start of every potentially
unsafe operation. The atfork policy works because state-CHILD blocks
all unsafe operations. The close-then-can't-reuse policy works
because state-CLOSED blocks re-init. The runtime is otherwise free
to omit defensive locking on most of its internal data structures
because the state guard alone gates entry into them.

This is an elegant minimum: one int, four values, three writes,
many reads. Everything else in the threading model derives from it.
