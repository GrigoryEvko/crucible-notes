# do_init / do_fini

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

This page owns the **C++ static-constructor iteration** libtpu runs at load and the **symmetric destructor teardown** it runs at unload — the part of the lifecycle that lives entirely below the PJRT C-ABI and is driven by the C runtime, not by any framework call. When the dynamic linker walks `.init_array` (`INIT_ARRAY @ 0x215f26f0`, 2900 slots), it calls every translation-unit static-init function in link order; that storm is what fills libtpu's flag registries, protobuf descriptor pools, LLVM/MLIR backend tables, and — the one piece that matters for correctness — the `GoogleInitializer` module descriptors and their dependency edges. At unload the same machinery runs in reverse through `__cxa_finalize`, draining the LIFO list of destructors the constructors registered with `__cxa_atexit`.

The reference frame is the Itanium C++ ABI as any clang/libstdc++ binary implements it: per-TU `_GLOBAL__sub_I_<file>.cc` functions populate `.init_array`; function-local statics are made thread-safe with `__cxa_guard_acquire`/`release`/`abort`; non-trivial-destructor globals register a teardown callback with `__cxa_atexit(dtor, obj, &__dso_handle)`; and `__cxa_finalize(__dso_handle)` drains that list at `dlclose`/exit. libtpu is unusual in three ways. First, it ships its **own** libc++abi — `__cxa_guard_*` at `0x213e9ac0 / 0x213e9be0 / 0x213e9c20` and `__cxa_finalize` are libtpu-internal, not glibc's, so the guard word layout and the finalize list are private to the image. Second, there is no glibc `__do_global_ctors`/`__do_global_dtors` driver at all — this is an lld-linked clang-CRT image; instead two custom byte-guarded stubs, `__do_init @ 0xe63c000` (`.init_array` slot **761**, not slot 0) and `__do_fini @ 0xe63c020` (`.fini_array` slot 0), are themselves ordinary *array slots* among the rest. Third — the whole reason the design holds together — **the constructors only register; they run nothing order-critical.** Every cross-TU ordering hazard is deferred to the `GoogleInitializer` DAG, which runs later in topological order (PHASE B, at first `PJRT_Plugin_Initialize`), so the link order the constructors execute in never decides correctness.

The page is laid out as the two halves of the runtime's lifetime: §1 the load-time constructor walk (what `.init_array` calls, what the `_GLOBAL__sub_I_*` set constructs, the `__cxa_guard` singleton discipline, the ordering guarantees and where they are weak), and §2 the unload-time teardown (`__do_fini`, the `__cxa_finalize(__dso_handle)` LIFO drain, and what is *not* torn down because it is leaked-on-exit). The ELF `DT_INIT`/`DT_FINI`/`PREINIT_ARRAY` tags and the linker's `init_proc` trampoline live on [elf-entry-and-init-proc.md](elf-entry-and-init-proc.md); the `GoogleInitializer` module DAG and the `PJRT_Plugin_Initialize` bootstrap live on [module-init-plugin-discovery.md](module-init-plugin-discovery.md) and [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md).

For reimplementation, the static-init/fini contract is:

- **The constructor walk model** — `.init_array` is an array of function pointers the linker calls in array order; slot 0 is `__cpu_indicator_init` (CPU/IFUNC detection), `__do_init` is a guarded stub at slot 761, and the bulk are per-TU `_GLOBAL__sub_I_*` functions. Each constructs file-scope globals and, for any with a non-trivial destructor, registers teardown with `__cxa_atexit(dtor, obj, &__dso_handle)`.
- **The register-only discipline** — none of the ~2900 constructors runs order-critical TPU bring-up. They fill tables and build the `GoogleInitializer` registry; the order-sensitive work is deferred to the DAG. A reimplementer who runs HAL/platform setup inside a static ctor reintroduces the static-init-order fiasco the design exists to avoid.
- **The two byte guards** — `__do_init`'s `__do_init.__initialized` and `__do_fini`'s `__do_fini.__finalized` make the array-bracketing slots idempotent; the per-singleton `__cxa_guard` bytes make each function-local static one-shot and thread-safe.
- **The symmetric teardown** — `__do_fini` calls `__cxa_finalize(__dso_handle)`, which drains the `__cxa_atexit` list LIFO, calling each registered destructor once. The PJRT surface is deliberately *not* on this list — it is a leaked Meyers singleton.

| | |
|---|---|
| **Constructor array** | `INIT_ARRAY @ 0x215f26f0` — 23200 B = 2900 × 8, all `R_X86_64_RELATIVE` |
| **Constructor entry stub** | `__do_init @ 0xe63c000` — guard byte `__do_init.__initialized @ 0x224c3880` |
| **Destructor entry stub** | `__do_fini @ 0xe63c020` — guard byte `__do_fini.__finalized @ 0x224c3881` |
| **Per-TU ctor symbols** | `_GLOBAL__sub_I_<file>.cc/.cpp` — **1885 distinct** symbols (1764 distinct base names; 71 names recur across statically-linked components) |
| **Grouped / single-var ctors** | `_GLOBAL__I_NNNNNN` (759), `__cxx_global_var_init[.N]` (89 distinct names / 221 instances) |
| **Function-local-static guards** | libtpu's own `__cxa_guard_acquire/release/abort @ 0x213e9ac0 / 0x213e9be0 / 0x213e9c20` |
| **Teardown drain** | `__cxa_finalize(_dso_handle)` (libtpu's own libc++abi) |
| **Teardown array** | `FINI_ARRAY @ 0x215f8190` — 16 B = 2 × 8: `__do_fini`, `rand_thread_state_clear_all @ 0x2063df60` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. Load-Time — the static-constructor walk

### Purpose

`.init_array` is the Itanium-ABI mechanism by which every translation unit's file-scope dynamic initialization runs at `dlopen`. The dynamic linker reads `DT_INIT_ARRAY`/`DT_INIT_ARRAYSZ` (the section at `0x215f26f0`, 2900 entries), then calls each pointer in **array order** after `DT_INIT` and `PREINIT_ARRAY`. For libtpu this is where the entire register-only landscape is built: 2900 constructor calls construct globals across abseil, protobuf, LLVM, MLIR, the TPU runtime, and the `GoogleInitializer` registry. It is the single largest event at load, and it is deliberately shallow — registration only.

### Entry Point

```text
dynamic linker (DT_INIT_ARRAY walk)        ── one call per slot, array order
  └─ INIT_ARRAY @ 0x215f26f0   (2900 slots, all R_X86_64_RELATIVE)
       [0]   __cpu_indicator_init  0x21211240    ── clang/GCC ifunc + CPU-feature detector (first; addend of slot-0 reloc @ 0x215f26f0)
       [1]   ARGV_INIT_ARRAY::init_wrapper  0x20a0d2b0  ── Rust runtime argv capture
       ...   (slots 2..760 — early CRT/IFUNC + grouped C++ ctors) ...
       [761] __do_init             0xe63c000     ── guarded array-bracket stub (no work; reloc @ 0x215f3eb8)
       ...   (the long tail) ...
       [n]   _GLOBAL__sub_I_<file>.cc   × 1885    ── per-TU static init (1764 distinct base names)
             _GLOBAL__I_NNNNNN          × 759     ── grouped / priority-tagged ctors
             __cxx_global_var_init[.N]  × 221     ── single-global inits (89 distinct names)
```

> **NOTE —** the relocation step is part of [elf-entry-and-init-proc.md](elf-entry-and-init-proc.md): all 2900 in-file slots are zero on disk, and every slot is an `R_X86_64_RELATIVE` the linker fills with the target VA before the walk begins. This page assumes the array is already relocated and concerns itself only with *what the targets do* when called.

### Algorithm

The linker's loop is trivial; the interesting logic is inside each per-TU function. A representative `_GLOBAL__sub_I_*` constructs its file-scope globals and, for any global with a non-trivial destructor, registers a teardown callback — the symmetric half that §2 drains. The `_GLOBAL__sub_I_tpu_platform_registration.cc @ 0x2121f040` body is the canonical "register-only" shape: it constructs a single `GoogleInitializer` object and returns.

```c
// the linker's array walk (conceptual; not a libtpu function)
function run_init_array():
    for i in 0 .. DT_INIT_ARRAYSZ/8 - 1:        // 2900 iterations
        (*INIT_ARRAY[i])()                       // call in ARRAY order

// __do_init @ 0xe63c000 — the guarded array-bracket stub
function __do_init():
    if !__do_init.__initialized:                 // function-static byte guard
        __do_init.__initialized = 1              // set-and-return; NO ctor body
    // intentionally empty: real ctors are the OTHER array slots

// representative per-TU ctor — register-only, no order-critical work
// _GLOBAL__sub_I_tpu_platform_registration.cc @ 0x2121f040
function _GLOBAL__sub_I_tpu_platform_registration():
    // construct ONE file-scope global: a GoogleInitializer descriptor
    GoogleInitializer(                            // ctor @ 0x210b2780
        &google_initializer_module_tpu_platform,  // the .data object
        "module", "tpu_platform",                 // tag + module NAME
        &google_init_module_tpu_platform)         // fn to RUN LATER (PHASE B)
    // (other registration TUs additionally register FLAGS_tf_jf_* absl flags)
    // the module body does NOT run here — only the descriptor is built

// the destructor-registration pattern every non-trivial global ctor uses
// (observed pervasively across the decompiled _GLOBAL__sub_I_* bodies)
function some_ctor_with_destructible_global():
    construct_in_place(&GetThing()::thing)        // placement-new the global
    __cxa_atexit(&Thing::~Thing,                  // teardown callback ...
                 &GetThing()::thing,              // ... bound to the object ...
                 &_dso_handle)                     // ... tagged to THIS image
    // __cxa_atexit pushes onto libtpu's own __cxa_finalize LIFO list (§2)
```

> **QUIRK —** `__do_init` does *nothing* but flip a byte. It is not the dispatcher that calls the other constructors — glibc-style `__do_global_ctors_aux` would be. Here `__do_init` is simply one more entry in `.init_array` (slot 761), sitting alongside the 1885 real `_GLOBAL__sub_I_*` slots. Its only job is to be idempotent: if the array were ever walked twice (re-`dlopen` of an already-mapped image), the guard makes the second walk of *this slot* a no-op. The real per-TU constructors carry their own `__cxa_guard` bytes for the same reason. A reimplementer who treats `__do_init` as the constructor driver will look for ctor calls inside it and find none — that is correct, not a decompiler failure.

### What the `_GLOBAL__sub_I_*` set constructs

The 1885 `_GLOBAL__sub_I_*` constructors are not a flat list to enumerate — that is the anti-pattern. They are better understood by the *kinds of registries they populate*, all of which share one property: they register into a table or build a descriptor, and run no hardware or order-critical setup. The table below buckets the constructor set by what each TU's globals do, with the count of TUs matching each bucket (a keyword scan over the `_GLOBAL__sub_I_*.cc/.cpp` symbol set; buckets overlap, so they do not sum to 1885).

| Constructor bucket | What its globals register | Distinct TUs |
|---|---|---|
| **TPU/XLA/TSL runtime** | module descriptors, factory tables, runtime flags (the largest area) | ~162 (`*tpu*`) |
| **LLVM target backends** | `*TargetMachine.cpp`, `*AsmPrinter.cpp`, `*ISelLowering.cpp`, `*Subtarget.cpp`, `*CodeGen*` — `RegisterTarget`/`RegisterPass` into LLVM's global registries (X86, AArch64, AMDGPU, ARM, TPU) | ~51 |
| **`GoogleInitializer` module descriptors** | the `_GLOBAL__sub_I_*_registration.cc` set — bind module NAME → `google_init_module_*` fn + dependency edges | ~41 (`*registration*`/`*register*`) |
| **abseil flag registries** | `_GLOBAL__sub_I_absl_flags.cc`, `commandlineflags.cc`, `*_flags.cc` — `FLAGS_*` into the absl flag registry | ~28 (`*[Ff]lags*`) |
| **MLIR / HLO dialects + passes** | `mhlo`, `stablehlo`, `mlir_bridge_pass`, dialect/pass registrations | ~14 |
| **Metrics / telemetry** | gauge/monitor/metric registries | ~19 |
| **protobuf / upb descriptors** | proto descriptor pools + the `linkarr_upb_AllExts` mini-table extension array (`0x224c2480..0x224c2920`) | ~11 named + linker array |

> **NOTE —** the `GoogleInitializer`-descriptor bucket (~41 `*registration*` TUs) is the only one whose registrations are *order-critical at run time*, and it is precisely the one whose execution is deferred. The `_GLOBAL__sub_I_*_registration.cc` ctors run at load (building descriptors), but the `google_init_module_*` functions they point at run later, in the DAG, at first `PJRT_Plugin_Initialize`. See [module-init-plugin-discovery.md](module-init-plugin-discovery.md) for the descriptor → run mapping.

> **Note:** the symbol table (`nm -C libtpu.so`) carries **1885** distinct `_GLOBAL__sub_I_*` symbols, each at its own address, and **759** `_GLOBAL__I_*` symbols. The 1885 figure is distinct *symbols*; the 1764 figure is distinct base *names* — 71 names recur because the same source filename is statically linked from multiple components (`metrics.cc` appears 8×; `trace_codec_factory.cc`/`performance_counters.cc`/`kernel_firmware_factory.cc`/`hardware_attributes_factory.cc` 6× each), and each recurrence is a genuinely distinct TU initializer at a distinct address. Ground these counts in the deduped `nm` symbol table, not in a grep over a decompile tree (which inflates duplicate names). The 2900 total slot count is byte-anchored from `DT_INIT_ARRAYSZ` (0x5aa0 / 8). The full census is on [../forensics/static-init.md](../forensics/static-init.md).

### The `__cxa_guard` singleton discipline

Function-local statics with non-trivial initialization (Meyers singletons) are made thread-safe and one-shot by a per-static guard word and the `__cxa_guard_acquire`/`release`/`abort` triple. libtpu links its **own** libc++abi implementation of these at `0x213e9ac0 / 0x213e9be0 / 0x213e9c20`, not glibc's — confirmed by decompilation. The implementation is the standard libc++abi futex-backed guard: it CAS-installs an "in-progress" state, blocks contending threads on a `futex` syscall, and detects recursive initialization.

```c
// __cxa_guard_acquire @ 0x213e9ac0 (libtpu's own libc++abi) — abbreviated
function __cxa_guard_acquire(guard):
    if google_cxa_guard_acquire_begin: google_cxa_guard_acquire_begin(guard)  // hook
    if (guard->byte[0]) return 0                       // already initialized → skip
    prev = CAS8(&guard->byte[1], /*expect*/0, /*set*/2)  // try to claim "in-progress"
    if prev != 0:                                       // someone else is initializing
        loop:
            if prev == 1: return 0                      // became initialized → skip
            tid = syscall(186 /*gettid*/)
            if guard->owner_tid == tid:                 // SAME thread re-entered
                __abort_message("__cxa_guard_acquire detected recursive "
                                "initialization: ...")  // recursion → abort
            mark waiter bit; syscall(202 /*futex*/ wait)  // block until released
            prev = CAS8(&guard->byte[1], 0, 2)          // re-try claim on wake
    guard->owner_tid = syscall(186 /*gettid*/)          // record owner
    return 1                                            // caller runs the initializer
```

> **GOTCHA —** these are *not* glibc's `__cxa_guard_*`. libtpu carries its own libc++abi (same image that carries its own `__cxa_finalize` and `__cxa_atexit`), so the guard word is libtpu's two-byte `{initialized, in-progress}` layout and the recursion check uses `gettid` directly via `syscall(186)`. A reimplementer linking against the host libc's guard would get a *different* guard-word ABI; mixing the two on the same static is undefined. The 17 `GetTpuPjrtApi` guards (Stage 2 of the lifecycle) and every Meyers singleton in the TPU runtime use *this* implementation — see [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md#the-lazy-builder--gettpupjrtapi) for the 17-guard chain.

### Ordering guarantees

The constructor walk gives exactly one ordering guarantee and no more: **`.init_array` entries run in array order, which is the link order of their translation units.** There is no cross-TU dependency ordering — if TU A's global depends on TU B's global being constructed, the only thing that makes it work is that the linker happened to place B before A. This is the classic static-initialization-order fiasco, and libtpu's design choice is to **not rely on it for anything order-critical**:

- **Within a TU**, declaration order is honored (standard C++).
- **Across TUs**, only link order is guaranteed. `__cpu_indicator_init` (the clang/GCC ifunc + CPU-feature detector, slot 0) and the Rust `ARGV_INIT_ARRAY::init_wrapper` (slot 1) are placed first by the linker because nothing C++ may run before CPU-feature detection; the `__do_init` guard stub sits at slot 761, and the per-TU `_GLOBAL__sub_I_*` constructors fill the long tail.
- **For the order-critical TPU stack** (HAL factories, XLA targets, the StreamExecutor platform), the constructors register a `GoogleInitializer` *descriptor with explicit dependency edges* and defer execution to the DAG. The DAG runs in topological order at PHASE B regardless of static-ctor order. This is why a `tpu_hal_jxc_hardware_impl` module can depend on `tpu_hal` without any constraint on the link order of their `_GLOBAL__sub_I_*_registration.cc` files.

> **QUIRK —** the reimplementation-critical inversion: the things you would *expect* to be order-critical (platform/HAL/target bring-up) are the things explicitly *removed* from static-init ordering, and the things that genuinely run at load (flag tables, descriptor pools, LLVM/MLIR registries) are order-*insensitive* by construction — each registers into an independent table keyed by name/ID, so the order they register in does not change the result. The design has hollowed out the static-init phase precisely so that its one weak guarantee (link order) never has to be relied upon.

---

## 2. Unload-Time — `__do_fini` and the `__cxa_finalize` drain

### Purpose

At `dlclose` or process exit, the C runtime must run the destructors that were registered during the constructor walk. The Itanium ABI mechanism is symmetric to `__cxa_atexit`: `__cxa_finalize(dso_handle)` drains the registered-destructor list in **LIFO** order, calling each callback registered against this DSO exactly once, then clears them. libtpu drives this through `FINI_ARRAY`, whose first slot is the guarded `__do_fini` stub. The teardown is deliberately thin — the constructors registered far fewer destructors than they ran constructors, because the largest objects (the PJRT surface, the extension chain) are intentionally leaked.

### Entry Point

```text
dynamic linker (DT_FINI_ARRAY walk, reverse of init)
  └─ DT_FINI (.fini @ 0xe63553c)            ── empty stub (sub/add/ret)
  └─ FINI_ARRAY @ 0x215f8190   (2 slots, R_X86_64_RELATIVE)
       [0] __do_fini                  0xe63c020  ── guarded __cxa_finalize(_dso_handle)
       [1] rand_thread_state_clear_all 0x2063df60 ── per-thread BoringSSL/RNG cleanup
```

> **NOTE —** the array bracket is asymmetric to init in one way: `FINI_ARRAY` has only **2** slots versus `INIT_ARRAY`'s 2900, because per-TU teardown is not a `_GLOBAL__sub_D_*` array. Instead, every destructor was registered dynamically with `__cxa_atexit` *during* the constructor walk, and the single `__do_fini` slot drains all of them through `__cxa_finalize`. The linker walks `FINI_ARRAY` in reverse slot order, so `rand_thread_state_clear_all` (slot 1) runs *before* `__do_fini` (slot 0).

### Algorithm

`__do_fini` is the mirror of `__do_init`: a guard byte plus, this time, a real call. The guard makes the drain one-shot; the body calls `__cxa_finalize(_dso_handle)` guarded by a weak-symbol presence check.

```c
// __do_fini @ 0xe63c020 — the guarded teardown stub (byte-exact from decompile)
function __do_fini():
    int result                                   // uninitialized return (see GOTCHA)
    if !__do_fini.__finalized:                    // function-static byte guard
        __do_fini.__finalized = 1                 // set BEFORE the call → reentrancy-safe
        if &_cxa_finalize:                        // weak-symbol presence check
            return __cxa_finalize(_dso_handle)    // drain THIS image's atexit LIFO
    return result

// __cxa_finalize(dso) — libtpu's own libc++abi (conceptual, standard Itanium drain)
function __cxa_finalize(dso):
    // walk the __cxa_atexit list NEWEST-FIRST (LIFO)
    for entry in reverse(cxa_atexit_list):
        if dso == NULL or entry.dso_handle == dso:   // only THIS image's dtors
            d = entry.dtor; entry.dtor = NULL        // mark consumed (one-shot)
            d(entry.obj)                              // run the destructor
    // entries are cleared so a second finalize is a no-op
```

> **GOTCHA —** the `int result` in `__do_fini` is read uninitialized on the already-finalized path (`__do_fini.__finalized` already 1) and on the no-`__cxa_finalize` path. This is a decompiler artifact of a `void`-semantics tail-call function whose return register is simply not written on those paths — the caller (the linker's fini walk) ignores the return value, so the garbage `eax` is harmless. A reimplementer should model `__do_fini` as returning `void`; do not propagate the spurious `int`.

> **QUIRK —** `__do_fini` set-then-checks: it writes `__do_fini.__finalized = 1` *before* calling `__cxa_finalize`, so if a registered destructor (running inside `__cxa_finalize`) somehow re-enters `__do_fini`, the guard is already set and the re-entry is a no-op. The `if (&_cxa_finalize)` weak-symbol check is the standard `crtstuff` guard for the case where the image was linked without a finalizer; in libtpu the symbol is always present (libtpu carries its own), so the branch is effectively always taken. Both details mirror glibc's `__do_global_dtors_aux`, but the `__cxa_finalize` here is libtpu's internal one, draining libtpu's private atexit list.

### What is NOT torn down

The teardown is thin by design. The largest and most expensive objects built during the lifecycle are **leaked-on-exit function-local statics**, the normal Meyers-singleton lifetime for a plugin `.so` — they are never registered with `__cxa_atexit`, so `__cxa_finalize` never touches them:

| Object | Storage | Torn down at exit? | How it is actually released |
|---|---|---|---|
| `GetTpuPjrtApi()::pjrt_api` (140-slot table) | `.lbss @ 0x227BA840` | **No** — leaked | never; process death reclaims it |
| The 16 `.bss` extension nodes | `.bss @ 0x224c3880+` | **No** — leaked | never; process death reclaims it |
| `xla::PjRtClient` / `TpuPlatform` / executors | heap | **No** at exit | explicit `PJRT_*_Destroy` C-API calls |
| Per-thread BoringSSL/RNG state | TLS | **Yes** | `rand_thread_state_clear_all @ 0x2063df60` (FINI slot 1) |
| Globals with non-trivial dtors (caches, flag stores, `APFloat` constants, `StringMap`s) | `.data`/`.bss` | **Yes** | `__cxa_finalize` LIFO drain (FINI slot 0 → `__do_fini`) |

libtpu also provides its own `atexit` / `__cxa_thread_atexit` shims (`0x21217360 / 0x2120f1e0`) and a `threadlogger::FlushLogsAtExit @ 0x20f3dfe0` for log flushing at exit. These feed the same LIFO list that `__cxa_finalize` drains.

> **GOTCHA —** a reimplementer cannot assume `dlclose` frees the PJRT surface. The `PJRT_Api` table at `0x227BA840` and the extension chain are leaked Meyers singletons: their destructors were never registered, so `__cxa_finalize` does not call them, and a host that `dlopen`s, uses, `dlclose`s, and re-`dlopen`s libtpu in the same process will find the table *already built* on the second load (the `__cxa_guard` bytes in `.bss` survive because the image stays resident under PJRT's reference-counted plugin lifetime). Clients, executables, and buffers must be released through their explicit `PJRT_*_Destroy` calls *before* unload; nothing at fini does it for you.

---

## Related Components

| Component | Relationship |
|---|---|
| `INIT_ARRAY @ 0x215f26f0` | The 2900-slot constructor array the linker walks at `dlopen` |
| `__do_init @ 0xe63c000` | Guarded array-bracket stub at `.init_array` slot 761, sets `__do_init.__initialized` |
| `_GLOBAL__sub_I_*` (1885 symbols) | The per-TU static-init functions that do the actual registration |
| `GoogleInitializer` ctor `@ 0x210b2780` | Constructed by `*_registration.cc` ctors; binds module name → run-later fn |
| `__cxa_guard_acquire/release/abort @ 0x213e9ac0 / 0x213e9be0 / 0x213e9c20` | libtpu's own libc++abi function-local-static guards |
| `__cxa_atexit` / `_dso_handle` | The destructor-registration call every non-trivial global ctor emits |
| `FINI_ARRAY @ 0x215f8190` | The 2-slot teardown array (`__do_fini`, `rand_thread_state_clear_all`) |
| `__do_fini @ 0xe63c020` | Guarded `__cxa_finalize(_dso_handle)` — drains the atexit LIFO |
| `rand_thread_state_clear_all @ 0x2063df60` | FINI slot 1 — per-thread BoringSSL/RNG cleanup |

## Cross-References

- [overview.md](overview.md) — the full load-to-unload timeline; this page owns Stage 0's constructor walk and Stage 5's teardown
- [elf-entry-and-init-proc.md](elf-entry-and-init-proc.md) — the ELF `DT_INIT`/`DT_FINI`/`PREINIT_ARRAY` tags, array relocation, and the `init_proc` CRT trampoline that drives `.init_array`
- [module-init-plugin-discovery.md](module-init-plugin-discovery.md) — what the `*_registration.cc` ctors register (the `GoogleInitializer` descriptors) and how the DAG runs them at PHASE B
- [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md) — Stage 3, where the deferred `google_init_module_*` functions the constructors only registered finally execute
- [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md) — the 17 `__cxa_guard` Meyers-singleton builders that use the same libc++abi guard implementation documented here
