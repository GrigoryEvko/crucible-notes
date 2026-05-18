# jemalloc — Statically Linked Allocator

cicc v13.0 statically links **jemalloc 5.3.x** as its sole memory allocator. There is no fallback to glibc `ptmalloc2`: the binary's only memory-related dynamic imports are `mmap`, `munmap`, `madvise`, `posix_madvise`, and `sbrk`. Every `malloc`, `calloc`, `realloc`, `free`, `posix_memalign`, and `aligned_alloc` call in the compiler resolves to a jemalloc function inside the `0x12FC000`–`0x133FFFF` cluster.

This page documents what is actually visible in the binary, how to recognize jemalloc internals when triaging unfamiliar functions, and which user-facing controls are exposed.

## Binary Footprint

| Property | Value |
|---|---|
| Version (build identifier strings present) | 5.3.x |
| Code range | `0x12FC000`–`0x133FFFF` (~262 KB) |
| Function count in range | **767** (Hex-Rays decompiled count) |
| Largest function (`malloc_conf_init`) | `sub_12FCDB0`, **15,790 bytes** |
| Statistics formatter (`vsnprintf`-style) | `sub_40D5CA`, 21 KB |
| Statistics printer cluster | `0x40D000`–`0x42FFFF` (~80 KB, 7 functions) |
| Config strings (jemalloc-specific) | 159 unique knob names |
| Diagnostic message strings (`<jemalloc>: ...`) | 80+ |

The 767 functions inside the core range are a mix of arena management, tcache machinery, extent (chunk) allocators, the HPA (huge page allocator) backend, mutex profiling, ctl mibs, and the prof subsystem. They are bulk-labelable in IDA: see Methodology for the fingerprinting procedure.

## Key Functions

| Function | Address | Size | Role |
|---|---|---|---|
| `je_malloc_conf_init` | `0x12FCDB0` | 15.4 KB | Parses `MALLOC_CONF`, env vars, and the `/etc/malloc.conf` symlink against 159 known knob names |
| `je_malloc_vsnprintf` | `0x40D5CA` | 21 KB | Custom format printer; avoids reentrancy by not calling libc's `vsnprintf` from inside the allocator |
| `je_stats_print` | `0x417CBD` | 14 KB | Top-level stats: allocated, active, resident, mapped |
| `je_stats_print_arena` | `0x4134A7` | 83 KB | Per-arena stats including HPA shards |
| `je_stats_print_bins` | `0x40F894` | 37 KB | 18-column per-bin statistics table |
| `je_stats_print_large` | `0x40EF06` | 13 KB | Large-extent class statistics |
| `je_stats_general` | `0x411419` | 32 KB | Version, build config, runtime opts dump |
| `je_mutex_stats_read` | `0x40E5B5` | 7 KB | Mutex profiling counters |

Note that the statistics printers live in the **`0x40D000`–`0x42FFFF`** region — physically separated from the core allocator at `0x12FC000`. This is a linker artifact of how jemalloc's `stats.c` translation unit was placed.

## How to Recognize jemalloc in IDA

The fastest fingerprint is the diagnostic message prefix `<jemalloc>: ` (literal angle-brackets). Cross-references from any of these strings land directly inside the allocator. Representative messages:

- `<jemalloc>: invalid tcache id (%u).`
- `<jemalloc>: error in background thread creation for arena %u. Abort.`
- `<jemalloc>: perCPU arena getcpu() not available. Setting narenas to %u.`
- `<jemalloc>: narenas w/ percpuarena beyond limit (%d)`
- `<jemalloc>: HPA not supported in the current configuration; %s.`
- `<jemalloc>: Number of CPUs detected is not deterministic. Per-CPU arena disabled.`
- `<jemalloc>: Write-after-free detected on deallocated pointer %p (size %zu).`
- `<jemalloc>: Malformed conf string` / `Conf string ends with key` / `Conf string ends with comma`

The second fingerprint is the 159-string knob table consumed by `malloc_conf_init`. The presence of names like `opt.percpu_arena`, `opt.hpa_hugification_threshold`, `arenas.muzzy_decay_ms`, `opt.lg_tcache_nslots_mul`, and `opt.experimental_infallible_new` is unique to jemalloc — no other allocator uses this naming scheme.

## Configuration Surface

`malloc_conf_init` parses key/value pairs from five sources, applied in increasing precedence:

1. Default values compiled into the binary
2. The exported symbol `malloc_conf` (a `const char*` defined by NVIDIA at link time, if present)
3. The `/etc/malloc.conf` symlink (the symlink target is parsed as the conf string)
4. The `MALLOC_CONF` environment variable
5. The `MALLOC_CONF_2_CONF_HARDER` environment variable (jemalloc 5.3 addition; overrides everything)

Recognized knob namespaces present in the binary:

- **`opt.*`** — global allocator policy: `abort`, `abort_conf`, `cache_oblivious`, `metadata_thp`, `trust_madvise`, `retain`, `dss`, `tcache`, `narenas`, `percpu_arena`, `background_thread`, `dirty_decay_ms`, `muzzy_decay_ms`, `oversize_threshold`, `lg_extent_max_active_fit`, `junk`, `zero`, `tcache_max`, `lg_tcache_nslots_mul`, `tcache_gc_incr_bytes`, `tcache_gc_delay_bytes`, `lg_tcache_flush_small_div`, `lg_tcache_flush_large_div`, `experimental_infallible_new`, `debug_double_free_max_scan`, `confirm_conf`
- **`opt.hpa*`** — Hugepage Allocator (HPA) backend: `hpa`, `hpa_dirty_mult`, `hpa_hugification_threshold`, `hpa_hugify_delay_ms`, `hpa_min_purge_interval_ms`, `hpa_slab_max_alloc`, `hpa_sec_nshards`, `hpa_sec_max_alloc`, `hpa_sec_max_bytes`, `hpa_sec_bytes_after_flush`, `hpa_sec_batch_fill_extra`
- **`opt.prof*`** — heap profiling: `prof`, `prof_active`, `prof_gdump`, `prof_leak`, `prof_leak_error`, `prof_final`, `lg_prof_sample`, `lg_prof_interval`
- **`opt.san_*`** — sanitizer-style guards: `san_guard_small`, `san_guard_large`, `san_uaf_align`
- **`arenas.*`** — per-arena query mibs: `narenas`, `dirty_decay_ms`, `muzzy_decay_ms`, `tcache_max`, `nbins`, `nhbins`, `nlextents`, `bin`, `lextent`, `quantum`, `page`
- **`stats.*`** — runtime statistics readouts (queried via `je_mallctl`)
- **`config.*`** — compile-time configuration (read-only: `cache_oblivious`, `prof`, `stats`, `fill`, `xmalloc`, `lazy_lock`, `malloc_conf`, etc.)

The full enumeration of 199 conf strings observable in the binary appears in the env-vars and knobs configuration pages; see [Environment Variables](../config/env-vars.md) and [LLVM Knobs](../config/knobs.md).

## Why jemalloc

NVIDIA's choice of jemalloc over glibc's `ptmalloc2` is significant for compiler workloads:

1. **Thread-local caching (`tcache`)** removes the global mutex from the fast path. Each thread gets a per-size-class free list that refills from arenas only when empty. This matters in Phase II of cicc where multiple LLVM optimization threads concurrently allocate IR nodes, SCEV objects, and analysis result containers.
2. **Per-CPU arenas (`opt.percpu_arena`)** route allocations to a CPU-affine arena, removing false sharing of bin metadata between cores.
3. **Decay-based purging (`dirty_decay_ms`, `muzzy_decay_ms`)** keeps recently freed pages mapped but resettable via `MADV_FREE`, avoiding the unmap/remap churn that hurts when the compiler's working set grows and shrinks repeatedly across pass execution.
4. **The HPA backend (`opt.hpa`)** can promote arena slabs to 2 MB transparent hugepages once they cross a hugification threshold, reducing TLB pressure on the large IR datastructures that LLVM's instruction selection and register allocation construct.
5. **`opt.retain`** keeps the virtual address space mapped even when physical pages are released, eliminating address-space fragmentation across long compilation runs.

For the CRT/initialization sequence that wires jemalloc into the process and the static-constructor ordering involved, see [Entry Point and CLI](../pipeline/entry.md) §"Memory Management" and [Binary Layout](../binary-layout.md) §"jemalloc Integration".

## Reverse-Engineering Tips

1. **Label the cluster first.** Identifying jemalloc and bulk-renaming its 767 functions from `sub_12FCxxx` to `je_*` removes them from the compiler-logic analysis scope.
2. **Don't reverse the conf parser body.** `je_malloc_conf_init` is the largest single function in the allocator at 15.4 KB, but its structure is mechanical: linear scan of `MALLOC_CONF` matched against a static table. Decompile it once to recover the string table, then never look at it again.
3. **`getenv("bar")` is not a real environment variable.** The pattern `getenv("bar") == (char*)-1` is jemalloc's probe for sanitizer-intercepted `getenv`. The string `"bar"` is a dummy. Four global constructors (`ctor_106`, `ctor_107`, `ctor_376`, `ctor_614`) use this idiom.
4. **`mmap`/`munmap`/`madvise` callers are jemalloc.** Since the binary's only OS-memory imports come from jemalloc, every cross-reference to these glibc symbols lands somewhere in the extent/HPA backend.
5. **Stats printers are not on the hot path.** The 80 KB stats cluster at `0x40D000`–`0x42FFFF` is only reachable through `MALLOC_CONF="stats_print:true"` or explicit `je_mallctl("stats.print", ...)`. It can be deprioritized when triaging.
