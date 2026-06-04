# Embedded tcmalloc

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. Other versions differ.*

## Abstract

The host-side allocator story in `libtpu.so` has a headline that contradicts this page's own title: **libtpu does not embed a working tcmalloc.** It links a *decapitated* tcmalloc — the `MallocExtension` / `MallocHook` / experiment-selection API surface plus the per-CPU `rseq` primitives, packed into a 18,162-byte `google_malloc` ELF section ([19] @ `0x0e6373c0`) and a 261-byte `__lcxx_override` section ([26] @ `0x213f0720`) — but **none of the allocator core**. There is no `PageHeap`, no `CentralFreeList`, no `PerCPUCache`, no `TransferCache`, no `SizeMap`, no `ThreadCache`, no `HugePageAwareAllocator`, no `PageAllocator`. The actual process `malloc` is glibc's: every `operator new` in `__lcxx_override` calls the PLT `malloc`, which resolves through a `R_X86_64_JUMP_SLOT` to `malloc@GLIBC_2.2.5`; every `operator delete` calls `free@GLIBC_2.2.5`. jemalloc is completely absent (0 symbols, 0 strings).

This matters to a reimplementer for a precise reason. A reader who knows tcmalloc expects the questions this page should answer — *what are the size classes, how big is the per-thread cache, what is the central-free-list geometry, which `TCMALLOC_*` knobs are honoured* — to have numeric answers. They do not, because the data structures that hold those numbers live entirely in the absent core. The honest answer to "the size-class structure" is *there is none in this binary*; to "the central/thread cache" is *absent*; to "TPU-specific tuning" is *one no-op soft-limit call and an experiment-name lookup table with no allocation effect*. The mechanism that *would* wire tcmalloc in is the classic Abseil weak-strong interposition (`tcmalloc/malloc_extension.cc` declares the `_Internal` providers `ABSL_ATTRIBUTE_WEAK`); in this wheel the strong definitions are not linked, so every weak reference stays `UND`→`NULL` and every `if (&MallocExtension_Internal_X)` guard evaluates false.

This page documents three things, in order: (1) the *linkage* — what is and is not in the binary, the four `operator new` bodies, the weak-symbol interposition that leaves them bound to glibc; (2) the *shim surface* that is present — the `MallocExtension` wrappers, `MallocHook`, the experiment lattice, and the lone soft-limit knob, all inert; (3) what an engineer who wants real per-thread caching and size classes must reach for instead (glibc tunables), plus where the device-side allocators live ([hbm-allocator.md](hbm-allocator.md), [overview.md](overview.md)) — because those, not tcmalloc, manage every byte that matters on a TPU.

> **Note —** the page title is a misnomer kept for continuity with the page family. tcmalloc is *present* as the API/hook/experiment shim (~55 KiB, ~110 symbols) but is *not* embedded as the allocator — its core is not linked. The `google_malloc` section contains no `malloc`, only support code; all host allocation routes through `__lcxx_override` → PLT → glibc. The [overview.md](overview.md) host-heap row states the same conclusion at a glance; this page is its detail.

For reimplementation, the orientation contract is:

- **The linkage decision** — tcmalloc's allocator-core TU is *not* linked; the `_Internal` providers are weak `UND`; glibc supplies the strong `malloc`/`free`/`aligned_alloc`/`posix_memalign`. There is no `IFUNC`, no `IRELATIVE`, no `--whole-archive` forced constructor.
- **The `operator new`/`delete` bodies** — the four throwing replaceable operators in `__lcxx_override`, their `malloc` / `aligned_alloc` retry loop, the `new_handler` dance, and the dropped `__hot_cold_t` hint.
- **The inert shim surface** — 8 `MallocExtension` wrappers (weak-null-guarded thunks), `MallocHook` (registrable but never fired), the 18-entry experiment table, and the one soft-limit call that is a runtime no-op.
- **The replacement contingency** — *because* the choice is "is the strong symbol present at final link", a google3-internal binary that statically links the real tcmalloc would light the whole shim up; the pip-wheel install does not.

| | |
|---|---|
| **Working process allocator** | glibc `malloc`/`free` (`malloc@GLIBC_2.2.5`, `free@GLIBC_2.2.5`) — **not** tcmalloc |
| **tcmalloc footprint** | ~110 symbols / ~55,313 B; support code only (no allocator core) |
| **`google_malloc` section** | [19] @ `0x0e6373c0`, **18,162 B** — `rseq`/experiments/`MallocHook`/crash-printer |
| **`google_malloc_bss`** | [48] @ `0x2285a180`, **20,736 B** NOBITS — shim state only (no PageMap/CFL) |
| **`__lcxx_override` section** | [26] @ `0x213f0720`, **261 B**, AX align 32 — 4 throwing `operator new` bodies |
| **`malloc_hook` section** | [25] @ `0x213efe80`, **2,206 B** (`0x89e`) — `mmap`/`munmap`/`sbrk`/`LowLevelAlloc` thunks |
| **`__rseq_cs` section** | [39] @ `0x224bf980`, **8,800 B** — 247 `RseqFunction_*` + 28 `CountingMutex` (shared, not tcmalloc-only) |
| **`operator new(size_t)`** | `0x213f0720` (69 B) → PLT `malloc` `0x213f10a0` → `malloc@GLIBC_2.2.5` |
| **jemalloc** | absent (0 `je_*`, 0 `mallctl`, 0 `MALLOC_CONF`) |
| **Size classes / ThreadCache / CentralFreeList** | **0 symbols** — absent from this build |
| **Only tuning attempt** | `MallocExtension::SetMemoryLimit` from `BarnaCoreManagerBase::Init` — **no-op** (weak `UND`) |
| **Confidence** | HIGH (the architectural finding is byte-confirmed at the symbol, relocation, section-size, and decompile levels) |

---

## 1. The Linkage Decision

### Purpose

Whether tcmalloc *is* the process allocator is decided at the final link, not at runtime, and not in this binary's favour. This section establishes the negative result that governs every other section: the allocator core is absent, glibc is the malloc, and the only thing tcmalloc contributes is an inert API/hook/experiment shim. A reimplementer who assumes the page-family name is literal will mis-model the entire host heap.

### What is present versus absent

The `nm`/symbol-table partition is decisive. The `google_malloc` section roster ([19], 18,162 B), recovered in full, is *support-only*: per-CPU `rseq` trampolines, experiment selection, `MallocHook` lifecycle, signal-safe I/O, a crash/OOM printer, and the `PbtxtRegion` stats serializer. The allocator-core classes have **zero** symbols.

| Category | Present? |
|---|---|
| Allocator core (`PageHeap`, `CentralFreeList`, `PerCPUCache`, `TransferCache`, `SizeMap`, `ThreadCache`, `HugePageAwareAllocator`, `PageAllocator`) | **No** — 0 symbols |
| Strong `malloc`/`free`/`tc_malloc`/`tc_new`/`tc_free` | **No** |
| Size-class table / `kPageSize` / `kHugePageSize` / `kMaxSize` / `kNumClasses` | **No** |
| `MallocExtension` / `TCMalloc_Internal` API | **Yes**, but weak `UND` |
| `MallocHook` (`Add/Remove{New,Delete,…}Hook`, `Invoke…HookSlow`) | **Yes**, registrable |
| Experiment selection (`SelectExperiments`, 18-entry table) | **Yes** |
| `rseq` per-CPU primitives | **Yes**, shared |
| jemalloc (anything) | **No** — 0 symbols, 0 strings |

> **GOTCHA —** the `rseq` primitives (`RseqFunction_PerCpuCmpxchg64`, `PerCpuTryLock`, `PerCpuReadCycleCounter`) are *not* proof that tcmalloc's per-CPU caches exist here. Of the 247 `RseqFunction_*` records in `__rseq_cs`, the consumers are abseil synchronization and RCU — a *shared* google3 per-CPU library. A reimplementer who infers "rseq present ⇒ tcmalloc per-CPU cache active" will model a cache that the binary does not contain. The cache lives in the absent core; the rseq trampolines outlive it because other subsystems use them.

### The replacement mechanism — weak-strong interposition (inert here)

The wiring tcmalloc uses to be *optional* is the standard Abseil pattern: `tcmalloc/malloc_extension.cc` declares the `_Internal` hooks `ABSL_ATTRIBUTE_WEAK`, so a binary can link the `MallocExtension` API without forcing tcmalloc to be the `malloc`. Each weak provider carries a `GLOB_DAT` + `JUMP_SLOT` reloc with addend 0:

```text
0x224c3700  JUMP_SLOT  MallocExtension_Internal_MarkThreadIdle      + 0
0x224c3708  JUMP_SLOT  MallocExtension_Internal_MarkThreadBusy      + 0
0x224c3710  JUMP_SLOT  MallocExtension_Internal_SetMemoryLimit      + 0
0x224c3718  JUMP_SLOT  MallocExtension_Internal_GetNumericProperty  + 0
0x224c3720  JUMP_SLOT  MallocExtension_Internal_GetAllocatedSize    + 0
0x224c3728  JUMP_SLOT  MallocExtension_Internal_GetProperties       + 0
0x224c3730  JUMP_SLOT  MallocExtension_Internal_ProcessBackgroundActions + 0
0x224c3628  JUMP_SLOT  TCMalloc_Internal_PossiblyCold               + 0
0x224c3698  JUMP_SLOT  TCMalloc_Internal_SetProfileSamplingInterval + 0
0x224c36a0  JUMP_SLOT  TCMalloc_Internal_GetStats                   + 0
            (each also has a paired GLOB_DAT in 0x22054ff0..0x22055130)
```

The resolution rule is binary:

- **With a real tcmalloc linked** (e.g. inside google3): tcmalloc's TU defines *strong* `malloc`/`free`/`MallocExtension_Internal_*`/`TCMalloc_Internal_*`. The weak refs bind to them; the guards are true; C++ `new`/`delete` and the `MallocExtension` wrappers all route to tcmalloc.
- **In this `libtpu.so`** (the pip wheel): the allocator-core TU is *not* linked. The strong definitions are absent. The weak symbols stay `UND`→`NULL`, so `malloc`/`free` bind to the only remaining provider — glibc, through the normal PLT against `libc.so` — and `if (&MallocExtension_Internal_X)` is false, so every wrapper returns a default (no-op / `0` / unset).

There is **no** `STT_GNU_IFUNC` and **no** `R_X86_64_IRELATIVE` for the allocator functions: no runtime allocator-selection resolver. There is no `--whole-archive`-forced tcmalloc constructor. The choice is purely "is the strong symbol present at link time", and it is not.

> **NOTE —** the one residual uncertainty is *process scope*. The weak `UND` symbols are resolved against the whole process image at load, not just `libtpu.so`. If some *other* DSO in a deployed JAX/TPU process statically linked a real tcmalloc whose strong `malloc`/`MallocExtension_Internal_*` were exported, libtpu's weak refs would bind to *that* and the shim would light up. In a standard pip-wheel install (libtpu loaded by CPython, which uses glibc malloc) it does not, so the no-op analysis holds. This is the "it depends on the final link" caveat, and it is the only path by which any size-class / cache behaviour returns. (LOW that any such DSO is present in the standard install.)

---

## 2. The `operator new` / `operator delete` Bodies

### Purpose

The 261-byte `__lcxx_override` section is the entire host-allocation hot path that libtpu *owns*. It holds the four throwing replaceable global `operator new` operators that libc++ groups into a dedicated section (the `-fexperimental-library` / google3 `__lcxx_override` placement) so the link can keep or replace them as a unit. In this binary they are *kept*, not replaced, so they forward to glibc. A reimplementer reproduces the heap by reproducing these four bodies and routing them to whatever `malloc` the final link provides.

### Section layout

```text
ELF section [26] __lcxx_override   VA 0x213f0720   size 0x105 (261 B)   AX align 32
  0x213f0720  operator new(unsigned long)                    69 B   ── canonical libc++ loop
  0x213f0780  operator new[](unsigned long)                          ── tail-call to op new
  0x213f07a0  operator new(unsigned long, std::align_val_t)          ── aligned
  0x213f0820  operator new[](unsigned long, std::align_val_t)        ── aligned
```

The cold / nothrow / `__hot_cold_t` variants live *outside* `__lcxx_override`, in ordinary `.text`, and forward to the hot ones: `operator new(size_t, __hot_cold_t)` @ `0x211646c0` (hint dropped), `operator new(size_t, nothrow_t const&)` @ `0x211eb3c0` (try/catch around the hot op), and a TPU-internal placement `operator new(size_t, NamedBufferAlloc const&)` @ `0x208b1000`. Every `operator delete` is a thunk to `free` (e.g. `operator delete(void*)` @ `0x211eb440`, `operator delete(void*, align_val_t)` @ `0x211eb540`).

### Algorithm

```c
// operator new(unsigned long)                      sub_213F0720 (__lcxx_override, 69 B)
// the canonical libc++ __libcpp_operator_new loop
function operator_new(size_t n):
    size_t s = n + (n == 0);              // bump 0 -> 1 so malloc(0) never returns NULL spuriously
    void* p;
    while ((p = malloc(s)) == NULL):      // call _malloc rel32 -> 0x213f10a0 (PLT) -> malloc@GLIBC_2.2.5
        new_handler h = std::get_new_handler();
        if (h == NULL): std::__throw_bad_alloc();
        h();                              // run the installed new-handler, then retry
    return p;

// operator new(unsigned long, std::align_val_t)     sub_213F07A0 (__lcxx_override)
function operator_new_aligned(size_t n, align_val_t a):
    size_t s  = n + (n == 0);
    size_t al = (a < 9) ? 8 : (size_t)a;  // minimum alignment 8 (std::align_val_t < 9 floored to 8)
    size_t sz = max(s, round_up(s, al));
    void* p;
    while ((p = aligned_alloc(al, sz)) == NULL):   // PLT aligned_alloc 0x213f1300 -> aligned_alloc@GLIBC_2.16
        new_handler h = std::get_new_handler();
        if (h == NULL): std::__throw_bad_alloc();
        h();
    return p;
```

The disassembly key line is `0x213f0734  call _malloc  ; rel32 -> 0x213f10a0`. The `__hot_cold_t` hot/cold-page hint that a *real* tcmalloc consumes (to segregate hot and cold allocations into different spans) is silently dropped here, because glibc has no such concept — the hot/cold operators forward to the plain ones with the hint discarded.

### The PLT thunks → glibc

```text
0x213f10a0  malloc          jmp  cs:off_224C2DC8   ── R_X86_64_JUMP_SLOT malloc@GLIBC_2.2.5 + 0
0x213f1300  aligned_alloc   jmp  through GOT slot  ── aligned_alloc@GLIBC_2.16
0x213f1e70  posix_memalign  jmp  through GOT slot  ── posix_memalign@GLIBC_2.2.5
            operator delete family 0x211eb440..0x211eb580  -> free@GLIBC_2.2.5
```

The dynamic symbol table marks every allocation primitive as a plain `FUNC GLOBAL UND` import: `free`/`malloc`/`calloc`/`realloc` @ `GLIBC_2.2.5`, `aligned_alloc` @ `GLIBC_2.16`, `posix_memalign` @ `GLIBC_2.2.5`, plus `memalign`/`pvalloc`/`valloc`/`reallocarray`. So: every C++ `new` ⇒ `malloc`/`aligned_alloc`, every `delete` ⇒ `free`, all routed through the PLT to glibc.

> **QUIRK —** the same `posix_memalign@GLIBC_2.2.5` thunk (`0x213f1e70`) is what the *device-side* host backings reach directly — `tpu::PremappedMemoryManager` and `tpu::AllocateAligned` call `posix_memalign` without going through `operator new`. So both the C++ heap (via `__lcxx_override`) and the DMA-staging pool (via `PremappedMemoryManager`) bottom out at the same glibc allocator, but by two different entry points. They never share a tcmalloc; see [overview.md](overview.md#3-the-per-space-allocator--alignment-matrix) for the device-side host paths.

---

## 3. The Inert Shim Surface

### Purpose

The shim is real code with real callers — it just does nothing at runtime in this build. A reimplementer needs to know which API the runtime *calls* (so the same calls are present), and that each is a weak-null-guarded thunk that returns a default. This section is the catalog of the live-but-dormant surface: `MallocExtension`, `MallocHook`, the experiment lattice, and the lone soft-limit knob.

### MallocExtension wrappers

Eight `MallocExtension` methods are compiled (from `malloc_extension.cc`, `0x21164xxx`), each a weak-null-guarded thunk over its `_Internal` provider. All are runtime no-ops here because the providers are `NULL`.

| Method | VA | Provider (weak `UND`) | Effect here | Caller(s) |
|---|---|---|---|---|
| `MarkThreadIdle()` | `0x21164480` | `…_MarkThreadIdle` | no-op | abseil per-thread sem wait; RCU domain thread; exit/liveness watchers |
| `MarkThreadBusy()` | `0x211644a0` | `…_MarkThreadBusy` | no-op | abseil per-thread sem wait; RCU domain thread |
| `SetMemoryLimit(n,kind)` | `0x211644c0` | `…_SetMemoryLimit` | no-op | `BarnaCoreManagerBase::Init` (LimitKind=0/kSoft) |
| `GetNumericProperty(sv)` | `0x211644e0` | `…_GetNumericProperty` | returns false/unset | `InstallSignalHandlers`; `LloDumper::AddHeapSizeRecord`; `Thread::Start` |
| `GetAllocatedSize(p)` | `0x21164540` | `…_GetAllocatedSize` | returns 0 | `tsl::port::MallocExtension_GetAllocatedSize` (TF stats shim) |
| `GetProperties()` | `0x21164560` | `…_GetProperties` + `GetExperiments` | only experiment map populated | — |
| `ProcessBackgroundActions()` | `0x211645c0` | `…_ProcessBackgroundActions` | no-op | `MemoryReleaser` daemon body |
| `NeedsProcessBackgroundActions()` | `0x211645e0` | (same) | returns false | `MemoryReleaser` daemon launcher |

The canonical wrapper shape (here `SetMemoryLimit`) is the weak-null guard plus the `n | -(n==0)` "0 ⇒ unlimited" idiom:

```c
// MallocExtension::SetMemoryLimit                   sub_211644C0
function SetMemoryLimit(size_t n, LimitKind k):
    if (&MallocExtension_Internal_SetMemoryLimit != NULL):    // weak symbol address test
        return MallocExtension_Internal_SetMemoryLimit(n | -(n == 0), k);   // 0 -> ULLONG_MAX
    return /* default-constructed result, the call is dropped */;
```

> **GOTCHA —** the `MemoryReleaser` daemon (`google_init_module_malloc_memory_release_thread` @ `0x213efc00`) is an init-module that *would* spawn a thread named `"MemoryReleaser"` (priority `0xE`) running `ProcessBackgroundActions` in a loop — but only `if BackgroundThreadsAllowed() && NeedsProcessBackgroundActions()`. Here `NeedsProcessBackgroundActions()` is false (weak `NULL`), so the thread is never created. A reimplementer who copies the init-module must keep the guard, or they will spawn a background releaser thread that spins on a no-op.

The methods *not* compiled at all (absent even as wrappers): `ReleaseMemory` / `ReleasePerCpuMemoryToOS`, `GetStats` / `SnapshotCurrent`, `GetMemoryLimit`, `SetMaxPerCpuCacheSize`, `SetMaxTotalThreadCacheBytes`, `GetRegionFactory`, `ActivateGuardedSampling`, `EnableForkSupport`. The two classic per-thread/per-cpu cache-sizing setters (`SetMaxTotalThreadCacheBytes`, `SetMaxPerCpuCacheSize`) are therefore *not even reachable* — there is no API to size a cache that does not exist.

### MallocHook — wired but dormant

`MallocHook` is fully compiled in `google_malloc` and registrable: `Add/Remove{New,Delete,SampledNew,SampledDelete}Hook`, `Invoke{…}HookSlow`, and `HookList<T>::{Add,Remove}` (8 fn-ptr-signature instantiations). Hook storage lives in `google_malloc_bss` (`new_hooks_` @ `.data 0x224c2940`, plus `delete_hooks_` / `sampled_new_hooks_` / `sampled_delete_hooks_` / `hooklist_spinlock_`).

Two real consumers register hooks:

- `HeapLeakChecker::{BeforeConstructorsLocked, TurnItselfOffLocked}` register `NewHook` (`0x210effa0`) / `DeleteHook` (`0x210e8fe0`) — the perftools heap-leak detector.
- `crash_analysis::reporting::remote_coredumper::MemoryAllocPreventer` installs `FailOnAlloc` (`0xfccc520`) as a `NewHook` so allocations are *banned* (FailOnAlloc aborts) while a crash core is written from a signal handler.

> **QUIRK —** the hooks register successfully but never fire on a normal allocation. The code that *invokes* `InvokeNewHookSlow` on each `malloc`/`operator new` lives in the absent allocator core; glibc's `malloc` does not call it. So heap-leak checking and sampled-allocation profiling are wired-but-dormant. The one exception is the `malloc_hook` section [25] (2,206 B), which wraps `mmap`/`mmap64`/`munmap`/`mremap`/`sbrk` and abseil `LowLevelAlloc` — those *page-level* hooks still see `mmap`/`munmap` because those calls go through libtpu's own thunks, not glibc's malloc. So a reimplementer gets page-granularity tracking for free, but not per-object allocation tracking.

### The experiment lattice — query-only

`SelectExperiments` (`0x0e638b40`) reads four env vars *once* (CallOnce) via `tcmalloc_internal::thread_safe_getenv` — `TEST_TARGET`, `BORG_EXPERIMENTS`, `BORG_DISABLE_EXPERIMENTS`, `BORG_PHYSICAL_CELL` — parses comma-separated `enable_`/disable lists, applies target-name heuristics, and runs a CRC32-hash rollout sampler keyed on cell name at ~1/7 fraction. It populates a per-experiment bool table queried by `IsExperimentActive` / `FindExperimentByName` / `WalkExperiments`. The `tcmalloc::experiments` table has 18 entries (stride 24 B), matched by name length then `bcmp`.

> **NOTE —** every `TCMALLOC_*` string in the binary is an *experiment name in this lookup table*, not a `getenv` key. `TCMALLOC_PGHO_EXPERIMENT`, `TCMALLOC_L3_AWARE_VCPU_V2`, `TEST_ONLY_TCMALLOC_{SPAN_LIFETIME_TRACKING,SHARDED_TRANSFER_CACHE,HEAP_PARTITIONING,MADV_COLD_HUGEPAGE,HUGE_CACHE_RELEASE_30S,POW2_SIZECLASS,ALWAYS_DISCARDING}` and friends are *names looked up*, never read from the environment. There is **no** reading of `TCMALLOC_MAX_TOTAL_THREAD_CACHE_BYTES`, `TCMALLOC_PER_CPU_CACHES`, `TCMALLOC_RELEASE_RATE`, or `MALLOCSTATS` — those getenv keys are absent. Experiment selection has *no allocation-behaviour effect* because the core that would consume the bools is absent; it only feeds `GetExperiments` / `IsExperimentActive` queries. The experiment lattice does, however, *date* the shim (see [§4](#4-version--feature-level)).

### The one tuning knob

The single tcmalloc parameter the runtime *attempts* to set is a soft memory limit:

```text
FLAGS_barna_core_tcmalloc_desired_usage_limit_bytes   (.data 0x222c51a8, type int)
  -> platforms_deepsea::jellyfish::barna_core::BarnaCoreManagerBase::Init (0xf977320)
       MallocExtension::SetMemoryLimit(limit, /*LimitKind=*/0 = kSoft)        -- runtime NO-OP
```

The flag is registered by an abseil flag constructor (help text stripped to `kStrippedFlagHelp`); its static storage is zero-init, and the wrapper's `n | -(n==0)` idiom coerces `0` to `ULLONG_MAX`, so the *intended* default is "unlimited unless overridden". But the call is a runtime no-op because `MallocExtension_Internal_SetMemoryLimit` is `NULL`. So even this one knob does nothing here. (The flag's compiled non-zero default, if any, was not traced — the `_GLOBAL__sub_I` initializer was not decoded; LOW.)

---

## 4. Version / Feature Level

### Purpose

No semantic-version string is preserved — Google's internal TCMalloc has no semver tags. The shim can only be *dated* by its feature-set fingerprint, which is useful to a reimplementer choosing which upstream tcmalloc revision to base a real integration on, and which confirms the build is from the 2024/2025 internal stream (consistent with the LLVM-trunk / clang 9999.0.0 build identity).

### Feature fingerprint

| Feature evidence | Implies |
|---|---|
| `RseqFunction_PerCpuCmpxchg64` / `PerCpuCmpxchgCheck64` / `PerCpuTryLock` / `PerCpuReadCycleCounter` | rseq-accelerated per-CPU caches → 2020+ TCMalloc |
| `__hot_cold_t` `operator new` overloads | TCMalloc hot/cold page separation (~2022) |
| `__alloc_token_{0,1,9}_*` instrumentation thunks (`malloc`/`calloc`/`realloc`/`memalign`/`posix_memalign`/`_Znwm…`) | `-falloc-token` typed-alloc profiling, LLVM/TCMalloc 2024+ |
| `__size_returning_new[_aligned][_hot_cold]` thunks | P0901R10 size-returning `operator new` (C++26) → trunk libc++ + TCMalloc 2024/2025 |
| Experiment `TCMALLOC_PGHO_EXPERIMENT` | Profile-Guided Heap Optimization, 2024+ |
| Experiment `TCMALLOC_L3_AWARE_VCPU_V2` + `TEST_ONLY_L3_AWARE` | L3-cache-aware vCPU caches v2, 2024+ |

**Conclusion:** the shim is the API/experiment surface of the 2024/2025 Google-internal TCMalloc stream, evidenced by `__size_returning_new`, `__alloc_token`, PGHO, and L3-aware-vcpu-v2. The concrete revision string is not surfaced. Because the core is absent, the "version" is the version *of the API/experiment shim*, not of a running allocator. (HIGH on the stream/era; the exact revision is an open gap.)

---

## 5. What a Reimplementer Reaches For Instead

### Purpose

The brief for this page — "the size-class structure, the central/thread cache, TPU-specific tuning" — has no answer inside tcmalloc here, because tcmalloc does not run. This section says where the equivalent behaviour actually comes from, so a reimplementer is not left looking for a cache that does not exist.

### Per-thread / per-process host sizing is glibc's

There is no tcmalloc `ThreadCache` and no `PerCPUCache`. The `MarkThreadBusy`/`MarkThreadIdle` calls (which in a *real* tcmalloc shrink the calling thread's cache back to the central free list when idle) are wired to abseil semaphore waits, the RCU domain thread, and liveness/exit watchers — but no-op. Actual per-thread and per-process host-heap sizing is therefore glibc's:

- **Per-thread (per-arena) heaps** are governed by glibc's own `MALLOC_ARENA_MAX` / `M_ARENA_MAX`, the `mmap` threshold, and the trim threshold — *none* of which libtpu configures. A deployment that wants to bound per-thread arena explosion sets `MALLOC_ARENA_MAX` in the environment; libtpu has no equivalent knob.
- **Per-process footprint** is bounded only by the (no-op) BarnaCore soft limit and the OS — there is no in-process memory-limit enforcement from the tcmalloc shim.

### The bytes that matter are on the device allocators, not the host heap

Every byte that a TPU program actually places — HBM tensors, VMEM/CMEM/SMEM/SFLAG operands — is managed by the device-side allocators, *not* by the host malloc:

- **Device HBM/VMEM/SMEM/CMEM/SFLAG** are serviced by `tpu::BestFitAllocator` (best-fit RB-tree + eager coalesce on free), one instance per tier, replaying compile-time MSA offsets. The size-class equivalent for the device is MSA's compile-time placement, not a runtime free-list bin scheme. See [hbm-allocator.md](hbm-allocator.md).
- **Host DMA-staging** uses `tpu::PremappedMemoryManager` (N power-of-two partitions, round-robin, each wrapping a `BestFitAllocator` under a mutex) over `posix_memalign`, and `tpu::internal::HostBufferPool` (a per-size-class recycling cache, `SizedBucket` in a `flat_hash_map<size_t, SizedBucket>`) over `tpu::AllocateAligned` → `posix_memalign`. *This* recycling pool is the closest thing libtpu has to a size-class cache — and it is on the host-transfer staging path, not the C++ heap.
- **Host-RAM spill** (HBM buffers MSA elected to offload) is the *only* genuine `tsl::BFCAllocator` (bin-bucketed best-fit-with-coalescing, 21 size-class bins, 256 GiB cap, 2 MiB region doubling), reached solely via `HostOffloadingTpuAllocator`.

So the host metadata for device allocations — the `BestFitAllocator` objects themselves (200 B, `operator new(0xC8)`), the `std::set` RB-tree nodes, the `absl::flat_hash_map` ctrl/slot arrays, the `ProgramMemoryMetadata` proto, MSA `AllocationValue` vectors, `HeapSimulator` chunk maps, and Eigen scratch — is all `operator new` / `std::aligned_alloc`-allocated, which is glibc malloc. Device HBM bytes are `BestFit`-managed. The two domains never share an allocator, and tcmalloc participates in neither. The device-side detail is owned by the pages below.

---

## Related Components

| Component | Relationship |
|---|---|
| [overview.md](overview.md) | The memory-hierarchy map; its host-heap row ("no tcmalloc/jemalloc; `PremappedMemoryManager` / `tsl::BFCAllocator` over `posix_memalign`") is what this page details |
| [hbm-allocator.md](hbm-allocator.md) | The universal `tpu::BestFitAllocator` algorithm — the *device* allocator that manages the bytes the host heap does not |
| [module-init-plugin-discovery.md](../lifecycle/module-init-plugin-discovery.md) | Module-init path; the `google_init_*` cold init-modules (incl. the `MemoryReleaser` launcher and `RemoveInitialHooksAndCallInitializers`) run here |

## Cross-References

- [overview.md](overview.md) — the six-region taxonomy; the host-heap row this page expands, and the device-side `posix_memalign` paths (`PremappedMemoryManager`, `BFCAllocator`)
- [hbm-allocator.md](hbm-allocator.md) — `tpu::BestFitAllocator` (best-fit + eager coalescing); the device allocator that actually manages TPU bytes
- [vmem-allocator.md](vmem-allocator.md) — the VMEM tier, also a `BestFitAllocator` instance — not a tcmalloc size-class
- [module-init-plugin-discovery.md](../lifecycle/module-init-plugin-discovery.md) — where the `MemoryReleaser` daemon launcher and the tcmalloc hook initializers are sequenced at module init
- [back to index](../index.md) — Part X — On-Chip Memory & DMA / Memory tiers
