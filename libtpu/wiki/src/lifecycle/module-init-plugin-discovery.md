# Module-Init & Plugin Discovery

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

A PJRT plugin is a `.so` that a framework `dlopen`s and drives entirely through one exported C entry symbol. For libtpu the discovery contract is intentionally narrow: the framework — JAX, TensorFlow, or PyTorch-XLA — finds `libtpu.so` through its own PJRT plugin registry (a Python-side path, the `PJRT_PLUGIN`/`PJRT_NAMES_AND_LIBRARY_PATHS` env mapping, or a packaged entry point), `dlopen`s it, `dlsym`s the single symbol `GetPjrtApi`, and calls it with no arguments. Nothing about TPU internals crosses that boundary; the framework knows only the symbol name and the first few `PJRT_Api` field offsets. This page owns three things that page-`overview.md` and `get-pjrt-api-thunk.md` only point at: the **discovery handshake** (what name resolves and what the framework is allowed to assume), the **load-time init chain** the dynamic linker drives at `dlopen` (the CPU-feature hard gate, the 2900-entry `.init_array` static-ctor storm, and what it does *not* run), and the **one-time bootstrap gate** that turns a freshly-loaded `.so` into a live TPU driver session.

The decisive structural fact is that *almost nothing happens at `dlopen`*. The dynamic linker runs a CPU-feature fail-fast probe, then ~2900 C++ static constructors — but those constructors only **register**: they populate absl flag tables, protobuf descriptors, LLVM/MLIR backends, and a Google-style module-init dependency DAG. The order-sensitive TPU bring-up (HAL factories, XLA target functors, the StreamExecutor platform) is *registered* here but *not run*. The `PJRT_Api` table itself is not built either — it is a zero-filled Meyers singleton in `.lbss`. Two later, lazy, one-shot events do the real work: the **first `GetPjrtApi` call** materializes the 140-slot table under a chain of 17 `__cxa_guard`s, and the **first `PJRT_Plugin_Initialize` call** (slot 8) acquires the cross-process TPU lock and runs the module DAG in topological order, executing the registrations the linker only recorded. Silicon detection is deferred even further — to `PJRT_Client_Create`.

This separation is the classic Google `base/init_google` pattern (`REGISTER_MODULE_INITIALIZER`): static registration at load, ordered execution at first init. It exists precisely so that cross-translation-unit static-init order — the only guarantee of which is link order — never decides correctness for the order-critical TPU stack.

For reimplementation, the contract is:

- **The discovery handshake** — exactly one exported entry symbol (`GetPjrtApi`, lowercase `jrt`), `@@VERS_1.0`; the spelling, casing, and zero-argument signature are load-bearing.
- **The load-time init chain** — a PREINIT CPU gate that `raise(SIGILL)`s on a missing ISA feature *before any constructor runs*, then a register-only `.init_array` storm that builds the `GoogleInitializer` module DAG without executing any module.
- **The one-time bootstrap gate** — `PJRT_Plugin_Initialize` (slot 8): a `struct_size` compat check, a `kPjRtCApiTpuInitType` selector, `TryAcquireTpuLock`, `GetLibTpuInitArguments`, `InitializeDriver` → `RealInitGoogle` → `RunInitializers` (the DAG run), all idempotent through stacked once-guards.

| | |
|---|---|
| **Exported entry symbol** | `GetPjrtApi @ 0xe6a83a0` — 5-byte `jmp` thunk, `GetPjrtApi@@VERS_1.0` |
| **Real engine** | `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` (1336 B) |
| **Table storage** | `GetTpuPjrtApi()::pjrt_api @ 0x227BA840`, `.lbss` (NOBITS), 1120 B = 140 × 8 |
| **PREINIT_ARRAY** | `@ 0x22048b30` (16 B, 2 entries): CPU gate + dl-debug hook |
| **INIT_ARRAY** | `@ 0x215f26f0` (23200 B = 2900 entries, all `R_X86_64_RELATIVE`) |
| **FINI_ARRAY** | `@ 0x215f8190` (16 B, 2 entries) |
| **Bootstrap gate** | `pjrt::tpu_plugin::PJRT_Plugin_Initialize @ 0xe6a9d00` (303 B), PJRT slot 8 |
| **Init-type selector** | `kPjRtCApiTpuInitType` (statically `= 2`) `@ 0x22255b40` (`.data`) |
| **DAG run driver** | `GoogleInitializer::RunInitializers @ 0x210b2d20` (PHASE B, at first init) |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The Plugin-Discovery Handshake

### Purpose

PJRT's entire reason to exist is one ABI-stable rendezvous point. The framework knows nothing about libtpu's internals — it knows the entry-symbol name and the layout of the first few `PJRT_Api` fields, and discovers everything else at run time through `struct_size` and the extension chain. This section fixes that rendezvous so a reimplementer can ship a `.so` a stock JAX/PyTorch-XLA build will load. The deep `GetTpuPjrtApi` body and `tpu_plugin` object are owned by [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md); the `PJRT_Api` struct shape is owned by [../pjrt/overview.md](../pjrt/overview.md) and [../pjrt/api-vtable-reconstruction.md](../pjrt/api-vtable-reconstruction.md). This page owns only the handshake itself.

### How the framework finds libtpu

The plugin path is established *before* the C boundary, on the framework side. There is no libtpu code that "registers" the plugin with the OS; discovery is the framework's job:

```text
framework PJRT plugin registry (Python side)
  ├─ a packaged entry point / pip-installed plugin descriptor names libtpu, OR
  ├─ env PJRT_NAMES_AND_LIBRARY_PATHS / a "tpu" name → /path/to/libtpu.so, OR
  └─ a default search of the installed wheel's libtpu/libtpu.so
       │
       dlopen("libtpu.so")                 ── dynamic linker runs the load-time init chain (§2)
       dlsym(handle, "GetPjrtApi")         ── the ONLY name that resolves
         │                                    "GetTpuPjrtApi" is an INTERNAL helper, not exported
         └─ GetPjrtApi  0xe6a83a0          ── 5-byte: jmp 0xe6aa440
              └─ pjrt::tpu_plugin::GetTpuPjrtApi  0xe6aa440   (lazy 140-slot build — §1.2)
```

> **GOTCHA —** spelling and casing are load-bearing. The exported symbol is `GetPjrtApi` (lowercase `jrt`), matching the public PJRT plugin convention, versioned `GetPjrtApi@@VERS_1.0`. It is the *only* `GLOBAL FUNC` export matching `/Pjrt/`. `GetTpuPjrtApi` is an internal helper and is **not** exported. A loader that `dlsym`s `GetTpuPjrtApi`, or a build that exports only `Tpu`-prefixed names, fails discovery silently. The 194 `Tpu*_*` exports that share this binary are the *legacy* StreamExecutor C-ABI (`TpuExecutor_*` ×25, `TpuTransferManager_*` ×19, `TpuProgram_*` ×18, `TpuTopology_*` ×17, `TpuPlatform_*` ×11, …), all `@@VERS_1.0`, linked directly by `tensorflow/core/tpu/` — never reached through PJRT.

### The entry contract

```c
// The one symbol the framework dlsym's. No arguments, returns the table.
const PJRT_Api* GetPjrtApi(void);             // exported, GetPjrtApi@@VERS_1.0
```

`GetPjrtApi @ 0xe6a83a0` is a pure tail-call thunk — confirmed in the decompile as a one-line `return pjrt::tpu_plugin::GetTpuPjrtApi(a1)` (the `a1` register is dead; the canonical signature takes no arguments). The thunk exists to give the public name external linkage while the engine stays in the anonymous `pjrt::tpu_plugin` namespace.

The caller then reads `api->struct_size` to learn how many slots this plugin provides, reads `api->pjrt_api_version` (minor `103` in this build), walks `api->extension_start` for optional capabilities, and only then calls `api->PJRT_Plugin_Initialize` (slot 8) and `api->PJRT_Client_Create` (slot 15). Those two slots reach into the bootstrap gate (§3) and silicon detection respectively.

### Why the table is built on first call, not at load

`GetTpuPjrtApi`'s `pjrt_api` is a function-local static in `.lbss` (NOBITS, `@ 0x227BA840`), zero-filled at load. On the first `GetPjrtApi`, the engine runs 17 `__cxa_guard`-protected one-shot blocks: 16 build the `.bss` extension chain (each chained to the previous as its `.next`, seeded from the `.data`-static profiler), and the 17th calls `pjrt::CreatePjrtApi` to write all 140 slots. The decompile confirms the 16-builder ladder verbatim:

```c
function GetTpuPjrtApi():                                  // 0xe6aa440
    // 16 one-shot extension builders, construction order (each takes the prior as .next):
    once: CreateRawBufferExtension(&raw_buffer_ext, &profiler_extension)   // seed = .data profiler
    once: CreateLayoutsExtension(&layouts_ext, &raw_buffer_ext)
    once: CreateMemoryDescriptionsExtension(&mem_desc_ext, &layouts_ext)
    once: CreateExecutableMetadataExtension(&exec_meta_ext, &mem_desc_ext, GetTpuExecutableMetadata)
    once: CreateHostAllocatorExtension(&host_alloc_ext, &exec_meta_ext, GetPreferredAlignment, Allocate, Free)
    once: CreateCrossHostTransfersExtension(...)
    once: CreatePhaseCompileExtension(..., GetTpuPhaseCompiler, DestroyTpuPhaseCompiler)
    once: CreateCallbackExtension(...)
    once: CreateTpuTopologyExtension(...)
    once: CreateTpuExecutableExtension(...)
    once: CreateMegascaleExtension(...)
    once: CreateShardingsExtension(...)
    once: CreateTpuAbiVersionExtension(...)
    once: CreateCollectivesExtension(...)
    once: CreateMultiSliceExtension(...)
    once: CreateHostMemoryAllocatorExtension(&hma_ext, &multi_slice_ext)   // last-built = chain head
    // 17th guard: write all 140 slots, chain head = host_memory_allocator_extension
    once: CreatePjrtApi(&pjrt_api,
                        PJRT_Client_Create, PJRT_ExecuteContext_Create,
                        PJRT_TopologyDescription_Create, PJRT_Plugin_Initialize,
                        &host_memory_allocator_extension /*chain head*/,
                        PJRT_Plugin_Attributes_Xla)
    return &pjrt_api                                        // 0x227BA840
```

> **NOTE —** because the table is materialized on first call, static disassembly cannot show populated slot values — the `.lbss` image is all zeros until run time. The 140-slot → impl mapping is reconstructed from `CreatePjrtApi`'s body, not from the binary's data sections. After the one-shot, the struct is immutable for process lifetime; readers take no lock, and concurrent first-callers serialize through Itanium-ABI `__cxa_guard_acquire/release`. The chain-building detail (the 16+1 builders, the newest-first chain, the five TPU-injected slots) is fully owned by [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md) and [../pjrt/extension-chain.md](../pjrt/extension-chain.md); it is shown here only to make the "first call, not load" boundary concrete.

---

## 2. The Load-Time Init Chain (what `dlopen` drives)

### Purpose

Everything in this section runs synchronously inside the `dlopen` call, driven by the dynamic linker — *before* `GetPjrtApi` is ever called. The reimplementer's mental model must be: `dlopen` runs a hard CPU gate and a register-only constructor storm, and then *stops*. No TPU hardware is touched, no `PJRT_Api` table exists, no driver session is live. The ELF entry/`init_proc` and `__do_init/__do_fini` mechanics are owned by the `elf-entry-and-init-proc` and `do-init/do-fini` pages; this section covers the *plugin-discovery-relevant* content of the chain — the CPU gate and what the constructor storm registers vs. runs.

### The four linker-driven stages

```text
dlopen("libtpu.so")
  1. Relocation        ── ALL of INIT_ARRAY (2900 slots), PREINIT_ARRAY (2),
                          FINI_ARRAY (2) are R_X86_64_RELATIVE — in-file slots are
                          zero, the linker fills every target VA at load.
  2. DT_INIT (.init @ 0xe635524)
                       ── vestigial glibc __gmon_start__ check-and-call stub.
                          ALL real init runs through .init_array, not here.
  3. PREINIT_ARRAY @ 0x22048b30   (runs BEFORE any C++ constructor)
       [0] (anon)::cpu_feature_fail_fast  0x2110abc0   ── CPU ISA hard gate (§2.1)
       [1] setup_dl_debug_hook            0x2114eec0   ── dl debug rendezvous
  4. INIT_ARRAY @ 0x215f26f0      (2900 entries, in array order)
       __cpu_indicator_init → Rust std::sys args ARGV init → 2898 C++ static ctors
                                                            (register-only — §2.2)
```

### 2.1 The CPU-feature hard gate (PREINIT_ARRAY[0])

`(anonymous namespace)::cpu_feature_fail_fast @ 0x2110abc0` runs *first*, before any constructor, and fences off the entire static-init storm. It calls `__cpu_indicator_init` (the GCC ifunc support routine) to populate a global feature mask (`dword_22598A0C`), then checks each ISA feature the binary was compiled to require. On a missing feature it `write(2, …)`s a `"FATAL ERROR: This binary was compiled with <feat> enabled, but this feature is not available on this processor (go/sigill-fail-fast)."` message to stderr and `raise(SIGILL)` (`raise(4)`) — a hard abort with no chance to recover.

> **CORRECTION (P-3-185) —** the raw reconstruction listed only SSSE3 and CMPXCHG16B as the gated features (and left "is the gate wider?" as an open question). The decompile of `cpu_feature_fail_fast` resolves it: the gate checks **eleven** features via the `__cpu_indicator_init` mask, in a fixed fall-through order, plus a separate `cpuid` leaf-1 ECX probe for CMPXCHG16B. The full set (mask bit → feature) is below; SSSE3 + CMPXCHG16B were merely the *last two* checks the earlier trace reached.

| Order | Mask test (`dword_22598A0C &`) | Feature | Confidence |
|---|---|---|---|
| 1 | `0x40000` | AES | CONFIRMED |
| 2 | `0x200` | AVX | CONFIRMED |
| 3 | `0x2` | MMX | CONFIRMED |
| 4 | `0x80000` | PCLMUL | CONFIRMED |
| 5 | `0x4` | POPCNT | CONFIRMED |
| 6 | `0x8` | SSE | CONFIRMED |
| 7 | `0x10` | SSE2 | CONFIRMED |
| 8 | `0x20` | SSE3 | CONFIRMED |
| 9 | `0x80` | SSE4.1 | CONFIRMED |
| 10 | `0x100` | SSE4.2 | CONFIRMED |
| 11 | `0x40` | SSSE3 | CONFIRMED |
| 12 | `cpuid(1).ecx & 0x2000` | CMPXCHG16B | CONFIRMED |

```c
function cpu_feature_fail_fast():                 // 0x2110abc0
    __cpu_indicator_init()                         // fills dword_22598A0C
    mask = dword_22598A0C
    // fall-through chain: each missing feature writes a FATAL string + raise(SIGILL),
    // then continues testing the next (so all missing features are reported).
    if !(mask & 0x40000): fatal("aes");      ...   // AES
    if !(mask & 0x200):   fatal("avx");      ...   // AVX
    ... MMX, PCLMUL, POPCNT, SSE, SSE2, SSE3, SSE4.1, SSE4.2, SSSE3 ...
    // CMPXCHG16B is a direct cpuid probe, not the indicator mask:
    eax = 1; cpuid
    if !(ecx & 0x2000): fatal("cmpxchg16b"); raise(SIGILL)
    return
```

> **GOTCHA —** this gate runs at `dlopen`, not at first use. A host whose CPU lacks any of these eleven baseline features will SIGILL the instant the framework `dlopen`s `libtpu.so` — long before `GetPjrtApi`, and with a stderr message but no PJRT-level error return. A reimplementer porting to an exotic host must satisfy the *entire* SSE/SSSE3/AES/AVX/PCLMUL/POPCNT/CMPXCHG16B baseline; there is no graceful-degradation path.

### 2.2 The constructor storm registers; it does not run TPU bring-up

`INIT_ARRAY @ 0x215f26f0` is 23200 bytes = 2900 entries, every one an `R_X86_64_RELATIVE` reloc (in-file slots zero, linker-filled). The first entries are `__cpu_indicator_init`, then the Rust runtime's `std::sys::args::unix::imp::ARGV_INIT_ARRAY` (libtpu statically links a Rust component), then the remaining 2898 C++ static constructors. By symbol category (counts byte-exact over all 2900 slots):

| Constructor kind | Count | What it does | Confidence |
|---|---|---|---|
| `_GLOBAL__sub_I_<file>.cc/.cpp` | 1885 | per-translation-unit static init | CONFIRMED |
| `_GLOBAL__I_NNNNNN` | 759 | grouped C++ ctors | CONFIRMED |
| `__cxx_global_var_init[.N]` | 221 | single global-var inits | CONFIRMED |
| anon / no-symbol ctors + `__do_init` + `upb_GeneratedRegistry_Constructor` | 33 | remaining C++ ctors | CONFIRMED |
| `__cpu_indicator_init` | 1 | GCC ifunc support (first slot) | CONFIRMED |
| Rust `ARGV_INIT_ARRAY` | 1 | Rust std args bootstrap | CONFIRMED |
| **Total** | **2900** | matches `INIT_ARRAYSZ`/8 | CONFIRMED |

These constructors register, but do not execute, the order-critical TPU stack. They populate: absl command-line flag tables (`_GLOBAL__sub_I_*_flags.cc`, `commandlineflags.cc`) and absl logging; protobuf descriptors (7 `*proto/descriptor` TUs + the upb `linkarr_upb_AllExts` mini-table array `@ 0x224c2480..0x224c2920`); LLVM target backends (X86/AArch64/AMDGPU/ARM `*TargetMachine.cpp`, `AsmPrinter.cpp`); MLIR dialect/pass registrations (mhlo/stablehlo/`mlir_bridge_pass`); and — the discovery-critical part — the `GoogleInitializer` **module descriptors** plus their dependency edges.

```c
// Each _GLOBAL__sub_I_<module>_registration.cc ctor, at dlopen, does ONLY:
function _GLOBAL__sub_I_tpu_platform_registration():       // 0x2121f040
    // bind module NAME → google_init_module_tpu_platform fn-ptr, insert into registry
    GoogleInitializer(LiteralTag, "tpu_platform", file, &google_init_module_tpu_platform)  // 0x210b2780
    // ... and register FLAGS_tf_jf_* absl flags ...
    // the module FUNCTION is NOT called here.

// e.g. the HAL modules also register a dependency edge:
function _GLOBAL__sub_I_tpu_hal_jxc_hardware_impl_registration():   // 0x2121bcb0
    GoogleInitializer(LiteralTag, "tpu_hal_jxc_hardware_impl", file, &google_init_module_...)
    GoogleInitializer::DependencyRegisterer(..., Dependency)        // 0x210b29e0
        // edge: tpu_hal_jxc_hardware_impl → depends on tpu_hal
```

> **NOTE —** C++ static-init order *within* `INIT_ARRAY` is link order — the only guarantee. libtpu deliberately does **not** trust it for correctness: every order-sensitive registration (HAL factories, XLA target functors, the StreamExecutor platform) is recorded into the `GoogleInitializer` dependency DAG here at `dlopen`, and *run* later in topological order at first init (§3). So a reimplementer can register module ctors in any link order; the DAG, not the linker, decides execution order.

---

## 3. The One-Time Bootstrap Gate

### Purpose

`PJRT_Plugin_Initialize` (slot 8) is the single point that turns a loaded-but-inert `.so` into a live TPU driver session. It is the bridge between the "registered everything, ran nothing" state left by `dlopen` and the running module DAG. It is idempotent, gated by a runtime selector, and the only place the cross-process TPU lock is acquired. The deeper option-ingest and `TfTpu_Initialize`-family detail is owned by [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md); this section owns the gate's control flow and its once-guard discipline.

### Algorithm

`pjrt::tpu_plugin::PJRT_Plugin_Initialize @ 0xe6a9d00` (303 B), called by the framework after `GetPjrtApi`:

```c
function PJRT_Plugin_Initialize(args):              // 0xe6a9d00, PJRT slot 8
    // (a) backward-compat size gate — accepts args struct from min=27 down to cur=16
    ok = ActualStructSizeIsGreaterOrEqual("PJRT_Plugin_Initialize_Args", 27, 16, args->struct_size)
    if ok != 1:
        return new uint8_t[8]{ ok }                 // error wrapper (heap-boxed status)

    // (b) runtime init-type selector (statically kPjRtCApiTpuInitType == 2)
    if kPjRtCApiTpuInitType != 0:                   // @ 0x22255b40 (.data)
        if TryAcquireTpuLock("PJRT_Plugin_Initialize_Args") == 1:   // 0x20ccbc40
            GetLibTpuInitArguments(&argv)            // 0x20ccca20 — reads LIBTPU_INIT_ARGS env
            InitializeDriver(flag, argc, argv,
                             init_type_is_2 = (kPjRtCApiTpuInitType == 2))   // 0x204cecc0
            free(argv …)                             // release the temporary arg vectors
            return NULL                              // success
        else:
            return new uint8_t[8]{ status }          // lock-fail / size-mismatch error wrapper

    // (c) init-type 0 → no-op
    return NULL
```

The decompile confirms each branch: the `ActualStructSizeIsGreaterOrEqual(…, 27, 16, *a1)` head; the `if (kPjRtCApiTpuInitType)` selector; `TryAcquireTpuLock` → on `== 1`, `GetLibTpuInitArguments` then `InitializeDriver(…, kPjRtCApiTpuInitType == 2, …)` then the argv-free loop and `return 0`; the `operator new(8u)` error-wrapper on the failure path; and `return 0` on the init-type-0 short-circuit.

### The lock, the args, and the DAG run

```text
PJRT_Plugin_Initialize (slot 8)  0xe6a9d00
  ├─ TryAcquireTpuLock            0x20ccbc40  ── cross-process TPU acquisition:
  │     function-static absl::Mutex (guard 0x225925d0 / obj 0x225925c8),
  │     env TPU_LOAD_LIBRARY (str @ file 0x887356a), scans /dev for TPU device
  │     nodes (opendir/readdir/stat). Returns 1 iff the lock is taken.
  ├─ GetLibTpuInitArguments       0x20ccca20  ── reads env LIBTPU_INIT_ARGS
  │     (str @ file 0x918c880), splits into an argv-style vector.
  └─ InitializeDriver             0x204cecc0  ── driver bring-up:
        ├─ AppendNewCloudTPUArgs                ── fold Cloud-TPU default flags into argv
        ├─ InitGoogleExceptChangeRootAndUser    0x210b0180  ── tail-jmp →
        │     RealInitGoogle                    0x210ae860
        │       ├─ absl::ParseCommandLine
        │       └─ GoogleInitializer::RunInitializers  0x210b2d20   *** PHASE B ***
        │             └─ run all registered modules in topological dep order:
        │                 google_init_module_tpu_hal_{jxc,pxc,vxc,glc,gfc}_*
        │                   → TpuHalFactory::Register(PlatformType, TpuVersion, factory)  0x1fbb16a0
        │                 google_init_module_xla_target_{jellyfish,…,ghostlite}
        │                   → RegisterTargetCreationFunctor(N, …)
        │                 google_init_module_tpu_platform  0x213eabc0 (jmp)
        │                   → RegisterTpuPlatform → PlatformManager::RegisterPlatform
        │                 … all other registered modules …
        └─ telemetry: RegisterLibtpuGaugeTelemetry, RegisterMegascaleErrorHandler,
                      EnableRuntimeUptimeTelemetry, InitializeUptimeMetricViaEnvironmentVariables
```

This is where the registrations from §2.2 finally run (**PHASE B**). `RunInitializers` drives the DAG through `TypeData::RunIfNecessary @ 0x210b3320` / `RunOne @ 0x210b3000`, gated by an `absl::Mutex` and per-module run-state, with `State::CanRun @ 0x210b3c80` deciding readiness and `kInitGoogleDone @ 0x22040038` / `CheckInitGoogleIsDone @ 0x210adec0` marking completion.

> **QUIRK —** the HAL factories register *per TpuVersion* but do **not** scan silicon. `google_init_module_tpu_hal_jxc_hardware_impl @ 0x213e9d80` calls `TpuHalFactory::Register` twice — TpuVersion 0 (kJellyfish) and 1 (kDragonfish) — both with `TpuHalJxcHardwareFactory`; pxc registers v2 (kPufferfish), vxc v3 (kViperfish), glc v4 (kGhostlite), gfc v5. At init the registry is merely *populated* by `(PlatformType, TpuVersion)`. The actual silicon scan — matching PCI device IDs to a `TpuVersion` via the `*HardwareScanner::Create` HAL components — runs **later**, inside `PJRT_Client_Create` (slot 15). TpuVersion detection is therefore deferred two stages past `dlopen`: register at first init, detect at first client.

### StreamExecutor platform registration

One module in the DAG is the StreamExecutor `TpuPlatform`. `google_init_module_tpu_platform @ 0x213eabc0` is a thunk → `tensorflow::tpu::RegisterTpuPlatform @ 0xe99a3a0`:

```c
function RegisterTpuPlatform():                     // 0xe99a3a0
    fn = stream_executor::tpu::ExecutorApiFn()       // 0x20819360
    if IsStreamExecutorEnabled(fn)                   // 0x20819380
       and !tpu_platform_registered:                 // byte guard @ 0x224c5388 (one-shot)
        p = new tensorflow::tpu::TpuPlatform()        // 0xe999960, sizeof = 0x98 (152 B)
        tpu_registered_platform = p
        st = PlatformManager::RegisterPlatform(p)     // 0x1d0fe120
        if st != OK:
            LOG(FATAL) at tpu_platform.cc:178         // CHECK-fail
        tpu_platform_registered = 1
    return 1
```

This installs the legacy StreamExecutor `TpuPlatform` beneath the PJRT `PjRtClient`. The 194 `Tpu*_*` C-ABI exports (§1) — `TpuPlatform_*` among them — wrap this same object family — it is the device layer the modern PJRT stack sits on top of.

### Once-guard discipline

Three distinct once-guard mechanisms keep the whole chain idempotent — re-calling `GetPjrtApi` or `PJRT_Plugin_Initialize` is a fast no-op:

| Mechanism | Where | Address(es) | Confidence |
|---|---|---|---|
| C++ `__cxa_guard` (libtpu's own libc++abi, not glibc's) | `GetTpuPjrtApi` 16 ext + `pjrt_api` (×17) | acquire/release/abort `0x213e9ac0`/`0x213e9be0`/`0x213e9c20`; e.g. raw_buffer guard `0x224c39e0` | CONFIRMED |
| `absl::Mutex` once-lock | `TryAcquireTpuLock::mu` | guard `0x225925d0` / obj `0x225925c8` | CONFIRMED |
| `absl::Mutex` registry lock | `GoogleInitializer::RunInitializers` | inside `0x210b2d20` | HIGH |
| function-static byte guard | `RegisterTpuPlatform::tpu_platform_registered` | `0x224c5388` | CONFIRMED |
| function-static byte guard | `__do_init` / `__do_fini` | `0xe63c000` / `0xe63c020` | HIGH |
| env-var gate | `TPU_LOAD_LIBRARY` (in `TryAcquireTpuLock`) | str `@ 0x887356a` | CONFIRMED |
| env-var args | `LIBTPU_INIT_ARGS` (in `GetLibTpuInitArguments`) | str `@ 0x918c880` | CONFIRMED |
| init-type selector | `kPjRtCApiTpuInitType` (= 2) | `0x22255b40` (`.data`) | CONFIRMED |

> **NOTE —** `kPjRtCApiTpuInitType` is statically `2` in `.data`. Type 2 takes the full TPU bring-up path (`InitializeDriver(…, init_type_is_2 = true, …)`); type 0 makes `PJRT_Plugin_Initialize` a no-op. Whether any path rewrites the selector to `0`/`1` (e.g. to select the legacy TF init-type) was not traced (LOW confidence on the rewrite existence; the static value `2` is CONFIRMED).

---

## 4. Teardown

### Purpose

For symmetry, the unload path. There is no large `atexit` teardown of the PJRT surface — the `PJRT_Api` table and the 17 extensions are leaked-on-exit function-local statics (`.lbss`/`.data`/`.bss`), the normal Meyers-singleton lifetime for a plugin `.so`. Clients, executables, and profiler handles are torn down through their explicit `PJRT_*_Destroy` C-API calls, not at process exit.

### What runs at unload

```text
dlclose / process exit
  ├─ DT_FINI (.fini @ 0xe63553c)  ── empty (sub/add/ret)
  └─ FINI_ARRAY @ 0x215f8190 (2 entries, R_X86_64_RELATIVE)
       [0] __do_fini                    0xe63c020  ── trivial guarded dtor stub
       [1] rand_thread_state_clear_all  0x2063df60  ── clears per-thread BoringSSL/RNG state
```

libtpu also provides `atexit` / `__cxa_thread_atexit` shims (`@ 0x21217360` / `0x2120f1e0`) and a `threadlogger::FlushLogsAtExit` (`@ 0x20f3dfe0`) for log flushing, but no PJRT-object destruction is wired into them.

---

## Related Components

| Component | Relationship |
|---|---|
| `GetPjrtApi @ 0xe6a83a0` | The single exported entry symbol; the discovery rendezvous |
| `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` | Lazy 140-slot build engine (owned by `get-pjrt-api-thunk.md`) |
| `pjrt::CreatePjrtApi @ 0xf874160` | Writes all 140 slots into `.lbss` on the 17th guard |
| `cpu_feature_fail_fast @ 0x2110abc0` | PREINIT CPU ISA hard gate (SIGILL on missing baseline) |
| `GoogleInitializer` registry / `RunInitializers @ 0x210b2d20` | The register-at-load / run-at-first-init module DAG |
| `PJRT_Plugin_Initialize @ 0xe6a9d00` | The one-time bootstrap gate (PJRT slot 8) |
| `TryAcquireTpuLock @ 0x20ccbc40` | The cross-process TPU acquisition lock |
| `RegisterTpuPlatform @ 0xe99a3a0` | Installs the StreamExecutor `TpuPlatform` beneath PJRT |
| `Tpu*_*` C-ABI (194 exports) | The legacy StreamExecutor surface that shares the binary, never reached through PJRT |

## Cross-References

- [overview.md](overview.md) — the lifecycle section map: from `dlopen` to a usable client
- [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md) — the `GetPjrtApi` thunk, the `GetTpuPjrtApi` engine body, the 16+1 `__cxa_guard` chain, and the `tpu_plugin` object in full
- [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md) — the initialize entry, `LIBTPU_INIT_ARGS` option ingest, and the `InitializeDriver` flag set
- [../pjrt/overview.md](../pjrt/overview.md) — the PJRT C-ABI map: the `PJRT_Api` struct shape, the handshake, and the extension chain by region
- [../pjrt/api-vtable-reconstruction.md](../pjrt/api-vtable-reconstruction.md) — the full 140-slot field-by-field table (every slot → impl symbol + address)
- [../pjrt/extension-chain.md](../pjrt/extension-chain.md) — the 17-node extension linked list, node-by-node layout, and `PJRT_Extension_Base` mechanics
