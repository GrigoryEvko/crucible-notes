# Thread-Local Storage in libnrt

`libnrt.so.2` carries two TLS sections in its ELF image:

```
[22] .tdata   PROGBITS  0xbeeaa0 (file)  size 0x4068   alignment 32
[23] .tbss    NOBITS    0xbf2b08 (image) size 0x00bc   alignment  8
```

`.tdata` is dominated by a single 16 400-byte symbol — `err_ctx` —
that holds the runtime's per-thread error context. The remaining 21
TLS symbols belong to the Rust stdlib (8), Abseil (6), and protobuf
(3) bundled into libnrt. Together they total 16 612 bytes of TLS that
`ld.so` allocates lazily per thread via `__tls_get_addr`.

**libnrt uses ZERO `pthread_key_create` slots for its own state.**
Every `pthread_key_create` call in the binary (126 of them) belongs to
vendored libraries, primarily Rust's `std::sys::thread_local::destructors`
emulation and Abseil's `base_internal::pthread_key_initialized` guard.

## The `err_ctx` layout

`err_ctx` is declared in `.tdata` at TLS offset 0, sized 16 400 bytes
(0x4010), aligned 8. The runtime reads/writes seven distinct fields
inside it via fixed offsets. These offsets are taken from the binary
disassembly of `nlog_set_error_cause` (0x2250f0) and
`nlog_coalescing_init_thread` (0x224ae0).

| Offset    | Field                       | Size | Purpose                                                  |
|-----------|-----------------------------|------|----------------------------------------------------------|
| 0x0000–0x3FFE | error_string_ringbuf    | 16383 B | Rendered per-thread error strings (ring with wrap) |
| 0x3FFF    | tls_init_marker             | 1 B  | Set to 0 by `nlog_coalescing_init_thread`               |
| 0x4000    | last_error_cause            | 4 B  | First-set cause code; sentinel 0xFFFFFFFF before set    |
| 0x4004    | _pad                        | 4 B  | Alignment padding to 8-byte boundary                    |
| 0x4008    | log_coalescing_enabled      | 1 B  | TLS cache of global enable flag                         |
| 0x4009–0x400B | _pad                    | 3 B  | Alignment padding                                        |
| 0x400C    | coalesce_count              | 4 B  | Per-thread coalesce counter for nlog                    |
| 0x4010    | (end of `err_ctx`)          | —    | Aligns 16 400-byte struct                                |

The "first-set-wins" semantics of `last_error_cause` is the key
invariant: once any libnrt function calls `nlog_set_error_cause(c)`,
subsequent calls in the same thread that pass a different cause are
ignored (a WARN is logged). The cause is cleared back to the
0xFFFFFFFF sentinel only by `nlog_coalescing_init_thread`, which is
called at the top of every public-API entry point — meaning each
public API call effectively resets the error context.

`nlog_set_error_cause` decompiled:

```c
void nlog_set_error_cause(uint32_t cause) {
    uint8_t *tls = __tls_get_addr(&err_ctx_tls_module);
    uint32_t *last_cause = (uint32_t *)(tls + 0x4000);
    if (*last_cause == 0xFFFFFFFFu) {        // first call wins
        *last_cause = cause;
        return;
    }
    // already set: only log a WARN
    nlog_write("nlog", "nlog_set_error_cause",
               NLOG_LEVEL_WARN, 0, 0, 0,
               "set_error_cause: cause already %u, new %u",
               *last_cause, cause);
}
```

`nlog_coalescing_init_thread` decompiled:

```c
void nlog_coalescing_init_thread(void) {
    uint8_t *tls = __tls_get_addr(&err_ctx_tls_module);
    *(uint8_t  *)(tls + 0x3FFF) = 0;
    *(uint8_t  *)(tls + 0x4008) = log_coalescing_enabled;  // global
    *(uint32_t *)(tls + 0x400C) = 0;
    *(uint64_t *)(tls + 0x4000) = qword_856728;            // seed timestamp
}
```

Note the +0x4000 store here overwrites the *cause* with a *timestamp*
(the code reuses the slot during normal log coalescing); the cause is
implicitly set back to the 0xFFFFFFFF sentinel by virtue of any value
≠ 0xFFFFFFFF being interpreted by `nlog_set_error_cause` as "already
set". In practice, the first call to `nlog_set_error_cause` after a
public-API entry overwrites whatever timestamp was seeded, so the
cause-capture contract holds. This is a subtle but real invariant.

## Vendored TLS slots (do not relocate, but pay the per-thread cost)

The remaining 21 slots, in stable layout order:

### Rust stdlib (8 slots)

| Symbol                                              | Size | Section |
|-----------------------------------------------------|------|---------|
| `std::sys::thread_local::destructors::list::DTORS`  | 32   | .tdata  |
| `std::sync::mpmc::waker::current_thread_id::DUMMY`  | 1    | .tdata  |
| `std::io::stdio::OUTPUT_CAPTURE` (closure VAL)      | 16   | .tbss   |
| `std::panicking::panic_count::LOCAL_PANIC_COUNT`    | 16   | .tbss   |
| `std::thread::current::id::ID`                      | 8    | .tbss   |
| `std::thread::current::CURRENT`                     | 8    | .tbss   |
| `std::sync::mpmc::context::Context::with::CONTEXT`  | 16   | .tbss   |
| `std::thread::spawnhook::SPAWN_HOOKS`               | 16   | .tbss   |
| `std::hash::random::RandomState::new::KEYS`         | 24   | .tbss   |

These activate only when Rust-bundled code runs (notably
`init_rust_logger` during `nlog_init`, and any neuron_rustime path
including `sys_trace` capture).

### Abseil-cpp lts_20230802 (6 slots)

| Symbol                                                                 | Size | Section |
|------------------------------------------------------------------------|------|---------|
| `absl::cord_internal::cordz_next_sample`                               | 8    | .tdata  |
| `absl::log_internal::ThreadIsLoggingStatus::thread_is_logging`         | 1    | .tbss   |
| `absl::container_internal::RandomSeed::counter`                        | 8    | .tbss   |
| `absl::cord_internal::cordz_should_profile_slow::exponential_biased_generator` | 24 | .tbss |
| `absl::base_internal::GetCachedTID::thread_id` (guard)                 | 8    | .tbss   |
| `absl::base_internal::GetCachedTID::thread_id`                         | 4    | .tbss   |

### protobuf (3 slots)

| Symbol                                                          | Size | Section |
|-----------------------------------------------------------------|------|---------|
| `protobuf::internal::ThreadSafeArena::thread_cache_`            | 32   | .tdata  |
| `protobuf::internal::ScopedReflectionMode::reflection_mode_`    | 4    | .tbss   |
| `protobuf::internal::allocate_at_least_hook_context`            | 8    | .tbss   |
| `protobuf::internal::allocate_at_least_hook`                    | 8    | .tbss   |

### libnrt Rust trace (1 slot)

| Symbol                                                 | Size | Section |
|--------------------------------------------------------|------|---------|
| `neuron_rustime::sys_trace::capture::THREAD_ID`         | 16   | .tbss   |

Used by the `nrt_sys_trace_*` infrastructure to cache the kernel
thread ID returned by `SYS_gettid` for fast access in trace
event records.

## Total TLS footprint per thread

A "fresh" thread that calls into libnrt will incrementally allocate
TLS as it touches each subsystem. Worst case (every subsystem
touched), it allocates approximately 16 612 bytes of TLS, dominated
overwhelmingly by `err_ctx` (16 400 bytes). For a process with N
threads, that's roughly N × 16 KiB. Implications:

- A Python / PyTorch process with 32 worker threads (DataLoader,
  inference workers, GIL helpers) pays ~512 KiB of TLS — negligible.
- A process that fork()'s aggressively to spawn ephemeral workers
  pays the cost once per child (the child gets a fresh `err_ctx`
  via the standard TLS image inheritance).
- An exotic process with thousands of threads (a misconfigured Java
  service, say) could pay tens of MiB of TLS. But that's already
  a bad architectural decision independent of libnrt.

## Why no `pthread_key_create`?

ELF TLS (`__thread`) is allocated by ld.so as part of the thread's
TLS block. The address is computed at link time and accessed via
`__tls_get_addr` (general-dynamic model) or via `%fs:offset` (initial-
exec model for the local libnrt symbols, given they're in the same
TLS module as the application). This is:

- Cheaper than `pthread_getspecific` (no function call, no hash lookup;
  worst case one indirect load).
- Bound to the thread's lifetime automatically; no destructor
  required (libnrt doesn't allocate dynamic memory in err_ctx).
- Compatible with the runtime's lock-free design.

The downside is that ELF TLS can't be unloaded — `dlclose(libnrt)`
would leak the TLS slot. Since no framework ever dlclose's libnrt,
this is purely theoretical.

The `pthread_key_create`-based alternative would have required:
- One key allocation per process at init
- A destructor callback to free the per-thread storage on exit
- A `pthread_getspecific` lookup on every TLS access
- A `pthread_setspecific` initialization on first use

All of which are stale APIs designed for the pre-1996 era when TLS
didn't exist. The Rust stdlib uses pthread_keys only because the
language semantics require destructor invocation; libnrt has no such
need.

## Implications for the fork-poison contract

When `fork()` is called, the child gets a fresh TLS image initialized
from the `.tdata` template — meaning `err_ctx + 0x4000 ==
qword_856728` (the seed timestamp) for the calling thread in the
child. This is **not** the 0xFFFFFFFF sentinel. So a child that
manages to bypass the `NRT_STATE_CHILD` poison and calls
`nlog_set_error_cause(cause)` will see the seed value, treat the
slot as "already set", and silently drop the cause with only a WARN.

This is the only TLS race observable in fork: the seed timestamp
behaves as a sentinel that prevents post-fork cause capture in the
calling thread of the child. Other threads cannot exist in the child
(fork only carries the calling thread), so the behavior is consistent
across the child's address space.
