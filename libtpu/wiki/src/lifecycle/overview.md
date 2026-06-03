# Lifecycle Overview

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

This is the **map** for libtpu's load-to-unload lifecycle. It traces the single timeline from the framework's `dlopen("libtpu.so")` to a live TPU driver session and back out at unload, names the function that owns each stage, and points at the sibling page that documents that stage in full. It does **not** reproduce any stage's internals — it exists so a reimplementer can see the whole arc on one screen, understand *which event triggers which* and *what state each transition leaves behind*, and then jump to the page that owns the detail.

The arc has one decisive shape: **`dlopen` does almost nothing, and the real work is lazy and one-shot.** The dynamic linker runs a hard CPU-feature gate and a ~2900-entry static-constructor storm, but those constructors only *register* — they fill flag tables, protobuf descriptors, LLVM/MLIR backends, and a Google-style module-init dependency DAG without executing any order-critical TPU bring-up. No `PJRT_Api` table exists yet; it is a zero-filled Meyers singleton in `.lbss`. Two later, lazy, idempotent events do the real work: the **first `GetPjrtApi` call** materializes the 140-slot table under a chain of 17 `__cxa_guard`s, and the **first `PJRT_Plugin_Initialize` call** (PJRT slot 8) acquires the cross-process TPU lock and runs the module DAG in topological order — executing the registrations the linker only recorded. Silicon detection is deferred even further, to `PJRT_Client_Create` (slot 15). Teardown is correspondingly thin: the PJRT surface is leaked-on-exit, and `FINI_ARRAY` tears down only a trivial stub plus per-thread RNG state.

The familiar reference frame is any C-ABI plugin loaded as a shared object: a single versioned entry symbol, an ABI-stable function-pointer table discovered through a `struct_size` field, and a Meyers-singleton lifetime. What is unusual is the *staging* — the deliberate separation of **register-at-load** from **run-at-first-init** via the `GoogleInitializer` DAG (the classic Google `REGISTER_MODULE_INITIALIZER` pattern). That staging exists so cross-translation-unit static-init order — whose only guarantee is link order — never decides correctness for the order-critical TPU stack.

For reimplementation, the lifecycle contract is:

- **The trigger order.** Six transitions in a fixed order: linker init chain (at `dlopen`) → first `GetPjrtApi` (table build) → first `PJRT_Plugin_Initialize` (driver + DAG run) → first `PJRT_Client_Create` (silicon scan) → steady state → `FINI_ARRAY` (unload). Each is owned by exactly one function.
- **What state each transition leaves.** After `dlopen`: registered, nothing run, no table. After first `GetPjrtApi`: table built, hardware untouched. After first `PJRT_Plugin_Initialize`: driver live, modules run, silicon *not yet* scanned. After first `PJRT_Client_Create`: live client on detected silicon.
- **The idempotency discipline.** Every transition past the linker chain is a one-shot guarded by `__cxa_guard`, `absl::Mutex`, or a function-static byte guard; re-entry is a fast no-op.

| | |
|---|---|
| **Exported entry symbol** | `GetPjrtApi @ 0xe6a83a0` — 5-byte `jmp` thunk, `GetPjrtApi@@VERS_1.0` |
| **Real table builder** | `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` (1336 B, internal) |
| **Table storage** | `GetTpuPjrtApi()::pjrt_api @ 0x227BA840`, `.lbss` (NOBITS), 1120 B = 140 × 8 |
| **PREINIT_ARRAY** | `@ 0x22048b30` (16 B, 2 entries): CPU gate + dl-debug hook |
| **INIT_ARRAY** | `@ 0x215f26f0` (23200 B = 2900 entries, all `R_X86_64_RELATIVE`) |
| **FINI_ARRAY** | `@ 0x215f8190` (16 B, 2 entries) |
| **Bootstrap gate** | `pjrt::tpu_plugin::PJRT_Plugin_Initialize @ 0xe6a9d00` (303 B), PJRT slot 8 |
| **DAG run driver** | `GoogleInitializer::RunInitializers @ 0x210b2d20` (PHASE B, at first init) |
| **Silicon scan** | `pjrt::tpu_plugin::PJRT_Client_Create @ 0xe6a8840`, PJRT slot 15 |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## The Lifecycle Timeline

The whole arc is six transitions. Three happen synchronously inside `dlopen` (linker-driven); three are framework-driven calls into the `PJRT_Api` table, each one a lazy one-shot. The diagram below is the canonical load-order; each numbered band is owned by one sibling page (named in the band).

```text
═══════════════════════════════════════════════════════════════════════════════
  STAGE 0 — dlopen("libtpu.so")          [dynamic linker; synchronous]
═══════════════════════════════════════════════════════════════════════════════
  (0a) Relocation
        INIT_ARRAY (2900) + PREINIT_ARRAY (2) + FINI_ARRAY (2) are all
        R_X86_64_RELATIVE — in-file slots zero, linker fills every target VA.
  (0b) DT_INIT (.init @ 0xe635524)
        vestigial glibc __gmon_start__ check-and-call stub.    ── elf-entry-and-init-proc.md
  (0c) PREINIT_ARRAY @ 0x22048b30        ── runs BEFORE any C++ constructor
        [0] cpu_feature_fail_fast  0x2110abc0  ── 11-feature CPU ISA gate; SIGILL on miss
        [1] setup_dl_debug_hook    0x2114eec0
  (0d) INIT_ARRAY @ 0x215f26f0  (in array order)   ── do_init / static-ctor storm
        __cpu_indicator_init → Rust ARGV init → 2898 C++ static ctors
        └─ _GLOBAL__sub_I_* / _GLOBAL__I_* / __cxx_global_var_init
            REGISTER-ONLY: absl flags, protobuf descriptors, LLVM/MLIR backends,
            and the GoogleInitializer MODULE DESCRIPTORS + dependency edges.
            *** No HAL factory / platform / target actually RUNS here. ***
                                                   ── elf-entry-and-init-proc.md / do-init-do-fini.md
        STATE AFTER STAGE 0: everything registered, nothing run, no PJRT_Api table.
                             ↓
═══════════════════════════════════════════════════════════════════════════════
  STAGE 1 — framework: handle = dlopen; fn = dlsym(handle, "GetPjrtApi")
═══════════════════════════════════════════════════════════════════════════════
  GetPjrtApi  0xe6a83a0   (5-byte jmp)  →  pjrt::tpu_plugin::GetTpuPjrtApi  0xe6aa440
        The ONLY exported PJRT symbol. dlsym resolves it; nothing runs yet.
                                                   ── get-pjrt-api-thunk.md / module-init-plugin-discovery.md
                             ↓
═══════════════════════════════════════════════════════════════════════════════
  STAGE 2 — first GetPjrtApi() call: lazy 140-slot table build
═══════════════════════════════════════════════════════════════════════════════
  GetTpuPjrtApi  0xe6aa440
        17 __cxa_guard one-shot blocks:
          guards 1..16 → build the 16 .bss extension nodes (chain head = HostMemoryAllocator)
          guard 17     → CreatePjrtApi 0xf874160 writes all 140 slots into .lbss
        return &pjrt_api  =  0x227BA840   (.lbss, was zero at load)
                                                   ── get-pjrt-api-thunk.md
        STATE AFTER STAGE 2: table built; hardware UNTOUCHED; no driver session.
                             ↓
═══════════════════════════════════════════════════════════════════════════════
  STAGE 3 — first api->PJRT_Plugin_Initialize(args)  (PJRT slot 8)
═══════════════════════════════════════════════════════════════════════════════
  PJRT_Plugin_Initialize  0xe6a9d00
        ├─ ActualStructSizeIsGreaterOrEqual(... min=27, cur=16, struct_size)
        ├─ if kPjRtCApiTpuInitType != 0  (statically = 2):
        │     TryAcquireTpuLock        0x20ccbc40   ── cross-process TPU lock (env TPU_LOAD_LIBRARY)
        │     GetLibTpuInitArguments   0x20ccca20   ── env LIBTPU_INIT_ARGS → argv
        │     InitializeDriver         0x204cecc0   ── driver bring-up:
        │         └─ InitGoogleExceptChangeRootAndUser → RealInitGoogle
        │              └─ RunInitializers  0x210b2d20   *** PHASE B: run the module DAG ***
        │                   → HAL factories (per TpuVersion) + xla_target_* + tpu_platform
                                                   ── tftpu-initialize-bootstrap.md / module-init-plugin-discovery.md
        STATE AFTER STAGE 3: driver live, modules RUN; silicon NOT yet scanned.
                             ↓
═══════════════════════════════════════════════════════════════════════════════
  STAGE 4 — first api->PJRT_Client_Create(args)  (PJRT slot 15)
═══════════════════════════════════════════════════════════════════════════════
  PJRT_Client_Create  0xe6a8840
        silicon scan / HAL impl selection: TpuVersion DETECTED here
        (PCI device-id match via *HardwareScanner::Create), builds the live
        TpuPlatform / TpuExecutor / TpuStream stack and the xla PjRtClient.
                                                   ── ../pjrt/client-and-device.md
                             ↓
       ┌──────────────────── STEADY STATE ────────────────────┐
       │  framework drives the 140-slot PJRT_Api: compile,     │
       │  buffer transfer, execute, collectives, profiling.    │
       │  Re-entering GetPjrtApi / PJRT_Plugin_Initialize is a │
       │  guarded fast no-op.                                  │
       └───────────────────────────────────────────────────────┘
                             ↓
═══════════════════════════════════════════════════════════════════════════════
  STAGE 5 — dlclose / process exit
═══════════════════════════════════════════════════════════════════════════════
  DT_FINI (.fini @ 0xe63553c)   ── empty (sub/add/ret)
  FINI_ARRAY @ 0x215f8190
       [0] __do_fini                  0xe63c020  ── guarded __cxa_finalize(_dso_handle)
       [1] rand_thread_state_clear_all 0x2063df60 ── clears per-thread BoringSSL/RNG state
       No PJRT_Api / extension teardown — leaked Meyers-singleton lifetime.
                                                   ── do-init-do-fini.md
═══════════════════════════════════════════════════════════════════════════════
```

> **GOTCHA —** the linker does the *least* interesting work and the framework calls do the most. A reimplementer who assumes "the `.so` is ready after `dlopen`" is wrong on every axis: the table does not exist, no driver is live, and no silicon is detected. Conversely, a host that fails the CPU gate (Stage 0c) never reaches Stage 1 at all — it `raise(SIGILL)`s inside `dlopen` with only a stderr message, no PJRT-level error return.

> **NOTE —** Stages 2, 3, and 4 are independent one-shots, not a forced sequence. A framework that only wants AOT compilation can call `GetPjrtApi` (Stage 2) and then `PJRT_TopologyDescription_Create` (slot 87) without ever calling `PJRT_Client_Create` (Stage 4) — no silicon is touched. The lifecycle is a *dependency lattice*, not a straight line; the timeline above shows the common full-execution path.

---

## Stage Map

Each stage in 1–2 sentences, with the function that owns it and the page that documents it in full. This is the index a reimplementer navigates from; the internals are deliberately *not* here.

### Stage 0 — Linker-driven init chain (at `dlopen`)

The dynamic linker relocates all three init arrays (every slot an `R_X86_64_RELATIVE`), runs the vestigial `DT_INIT` glibc stub, then `PREINIT_ARRAY` (the CPU-feature hard gate at `cpu_feature_fail_fast @ 0x2110abc0` and a dl-debug hook), then the 2900-entry `INIT_ARRAY` static-constructor storm. The storm is **register-only**: it populates absl flag tables, protobuf descriptors, LLVM/MLIR backends, and — critically — the `GoogleInitializer` module descriptors and their dependency edges, but runs no order-critical TPU bring-up. The `__do_init @ 0xe63c000` byte-guard (`_do_init___initialized`) makes the constructor entry idempotent.

> *Owned by* [elf-entry-and-init-proc.md](elf-entry-and-init-proc.md) (ELF entry, `.init`, `.init_array`, GOT/PLT) *and* [do-init-do-fini.md](do-init-do-fini.md) (constructor/destructor ordering and global state). The CPU gate's full 11-feature decode is on [module-init-plugin-discovery.md](module-init-plugin-discovery.md#21-the-cpu-feature-hard-gate-preinit_array0).

### Stage 1 — Discovery handshake (`dlsym("GetPjrtApi")`)

The framework finds `libtpu.so` through its own (Python-side) PJRT plugin registry, `dlopen`s it, and `dlsym`s exactly one symbol: `GetPjrtApi @ 0xe6a83a0`, a 5-byte `jmp` thunk to the internal builder. Nothing about TPU internals crosses this boundary — the framework knows only the entry-symbol name (lowercase `jrt`, `@@VERS_1.0`) and the first few `PJRT_Api` field offsets.

> *Owned by* [module-init-plugin-discovery.md](module-init-plugin-discovery.md#1-the-plugin-discovery-handshake) (the handshake, what name resolves, what the framework may assume). The thunk shape itself is on [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md#the-exported-thunk--getpjrtapi).

### Stage 2 — Lazy table build (first `GetPjrtApi` call)

The thunk tail-calls `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440`, which runs 17 `__cxa_guard`-protected one-shot blocks: the first 16 build the `.bss` extension chain (each node chained to the previous as its `.next`), and the 17th calls `pjrt::CreatePjrtApi @ 0xf874160` to write all 140 slots into the zero-filled `.lbss` singleton `pjrt_api @ 0x227BA840`. After this the table is populated and immutable; the hardware is still untouched.

> *Owned by* [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md#the-lazy-builder--gettpupjrtapi) (the 17-guard builder, the singleton, the `tpu_plugin` injected slots). The 140-slot field table is on [../pjrt/api-vtable-reconstruction.md](../pjrt/api-vtable-reconstruction.md); the 16-node chain on [../pjrt/extension-chain.md](../pjrt/extension-chain.md).

### Stage 3 — Driver bootstrap + DAG run (first `PJRT_Plugin_Initialize`)

`PJRT_Plugin_Initialize @ 0xe6a9d00` (slot 8) is the one-time gate that turns the inert `.so` into a live driver session: a `struct_size` compat check, the `kPjRtCApiTpuInitType` selector (statically `2`), `TryAcquireTpuLock @ 0x20ccbc40` (the cross-process lock, env `TPU_LOAD_LIBRARY`), `GetLibTpuInitArguments @ 0x20ccca20` (env `LIBTPU_INIT_ARGS`), and `InitializeDriver @ 0x204cecc0`. `InitializeDriver` reaches `RealInitGoogle → GoogleInitializer::RunInitializers @ 0x210b2d20` — **PHASE B**, where the modules the linker only registered finally run in topological dependency order (HAL factories per `TpuVersion`, XLA target functors, the StreamExecutor `TpuPlatform`).

> *Owned by* [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md) (the initialize entry, `LIBTPU_INIT_ARGS` option ingest, the `InitializeDriver` flag set). The bootstrap-gate control flow, lock, and DAG run are on [module-init-plugin-discovery.md](module-init-plugin-discovery.md#3-the-one-time-bootstrap-gate).

### Stage 4 — Silicon detection + live client (first `PJRT_Client_Create`)

`PJRT_Client_Create @ 0xe6a8840` (slot 15) is where `TpuVersion` is finally *detected* — the silicon scan matches PCI device IDs to a version via the `*HardwareScanner::Create` HAL components, selects the HAL impl the matching factory registered in Stage 3, and builds the live `TpuPlatform` / `TpuExecutor` / `TpuStream` stack and the `xla::PjRtClient` on top of it. This is the third deferral: registered at Stage 0, run at Stage 3, *detected* here.

> *Owned by* [../pjrt/client-and-device.md](../pjrt/client-and-device.md) (client creation, device topology, the live executor stack). Listed here only to close the load-to-usable arc.

### Stage 5 — Teardown (unload / process exit)

There is no large `atexit` teardown of the PJRT surface — the `PJRT_Api` table and the 16 extension nodes are leaked-on-exit function-local statics, the normal Meyers-singleton lifetime for a plugin `.so`. `DT_FINI` is an empty stub; `FINI_ARRAY @ 0x215f8190` runs only `__do_fini @ 0xe63c020` (a guarded `__cxa_finalize(_dso_handle)`) and `rand_thread_state_clear_all @ 0x2063df60` (per-thread BoringSSL/RNG cleanup). Clients, executables, and buffers are released through their explicit `PJRT_*_Destroy` C-API calls, not at exit.

> *Owned by* [do-init-do-fini.md](do-init-do-fini.md) (destructor ordering and the `FINI_ARRAY` body) and summarized on [module-init-plugin-discovery.md](module-init-plugin-discovery.md#4-teardown).

---

## The Trigger Table

The single most reimplementation-critical fact is *which event triggers each transition* and *what state it leaves*. Drivers are linker, `dlsym`, or framework C-call; nothing is time- or thread-triggered.

| Stage | Trigger | Owning function | State after | Confidence |
|---|---|---|---|---|
| 0 | `dlopen` (dynamic linker) | `cpu_feature_fail_fast` + INIT_ARRAY storm | registered, nothing run, no table | CONFIRMED |
| 1 | `dlsym(handle, "GetPjrtApi")` | `GetPjrtApi @ 0xe6a83a0` (thunk) | symbol resolved, nothing run | CONFIRMED |
| 2 | first `GetPjrtApi()` call | `GetTpuPjrtApi @ 0xe6aa440` | 140-slot table built, hardware untouched | CONFIRMED |
| 3 | first `api->PJRT_Plugin_Initialize` | `PJRT_Plugin_Initialize @ 0xe6a9d00` | driver live, module DAG run, no silicon scan | CONFIRMED |
| 4 | first `api->PJRT_Client_Create` | `PJRT_Client_Create @ 0xe6a8840` | live client on detected silicon | HIGH |
| 5 | `dlclose` / process exit | `__do_fini @ 0xe63c020` + FINI_ARRAY | per-thread RNG cleared; PJRT surface leaked | CONFIRMED |

> **QUIRK —** the three "first call" stages (2/3/4) are each protected by a *different* once-guard mechanism, not one shared scheme: Stage 2 by libtpu's own libc++abi `__cxa_guard` (17 distinct `.bss` guard bytes, `acquire/release/abort @ 0x213e9ac0 / 0x213e9be0 / 0x213e9c20`); Stage 3 by an `absl::Mutex` once-lock (`TryAcquireTpuLock::mu`, guard `@ 0x225925d0`) plus a function-static byte guard for the platform registration (`tpu_platform_registered @ 0x224c5388`); the linker entry (Stage 0) by the trivial `_do_init___initialized` byte. A reimplementer must reproduce all three idempotency styles — collapsing them to one lock changes the concurrency and lifetime semantics the framework relies on.

> **NOTE —** the `kPjRtCApiTpuInitType` selector `@ 0x22255b40` is statically `2` (full TPU bring-up). Type `0` would make Stage 3 a no-op. Whether any path rewrites it to `0`/`1` (legacy TF vs PJRT init-type) was not traced — LOW confidence on the rewrite existence; the static value `2` is CONFIRMED.

---

## What Crosses the Boundary at Each Stage

A reimplementer's other essential mental model: what data/state flows in and out at each transition. Everything is C-ABI or in-process; no IPC except the cross-process TPU lock at Stage 3.

| Stage | Inputs | Outputs / side effects | Confidence |
|---|---|---|---|
| 0 | CPU feature mask (`__cpu_indicator_init`) | populated flag/descriptor/backend tables + `GoogleInitializer` DAG; SIGILL on a missing ISA feature | CONFIRMED |
| 1 | symbol name `"GetPjrtApi"` | function address (thunk) | CONFIRMED |
| 2 | none (no args) | `const PJRT_Api*` → `0x227BA840`; 16 `.bss` extension nodes built | CONFIRMED |
| 3 | `PJRT_Plugin_Initialize_Args` (`struct_size`); env `TPU_LOAD_LIBRARY`, `LIBTPU_INIT_ARGS` | cross-process TPU lock held; module DAG run; `TpuPlatform` registered; `NULL` on success / heap-boxed status on error | CONFIRMED |
| 4 | `PJRT_Client_Create_Args` | live `xla::PjRtClient`; detected `TpuVersion`; live executor stack | HIGH |
| 5 | `_dso_handle` | `__cxa_finalize`; per-thread RNG cleared | CONFIRMED |

---

## Related Components

| Component | Relationship |
|---|---|
| `GetPjrtApi @ 0xe6a83a0` | The single exported entry symbol; the Stage-1 discovery rendezvous |
| `pjrt::tpu_plugin::GetTpuPjrtApi @ 0xe6aa440` | Stage-2 lazy 140-slot table builder |
| `pjrt::CreatePjrtApi @ 0xf874160` | Writes all 140 slots into `.lbss` on the 17th guard (Stage 2) |
| `cpu_feature_fail_fast @ 0x2110abc0` | Stage-0 PREINIT CPU ISA hard gate (SIGILL on missing baseline) |
| `GoogleInitializer::RunInitializers @ 0x210b2d20` | The register-at-load (Stage 0) / run-at-first-init (Stage 3) module DAG |
| `PJRT_Plugin_Initialize @ 0xe6a9d00` | Stage-3 one-time bootstrap gate (PJRT slot 8) |
| `PJRT_Client_Create @ 0xe6a8840` | Stage-4 silicon scan + live client (PJRT slot 15) |
| `__do_fini @ 0xe63c020` + FINI_ARRAY | Stage-5 teardown (guarded `__cxa_finalize` + RNG clear) |
| `Tpu*_*` C-ABI (~217 exports) | The legacy StreamExecutor surface that shares the binary, never reached through PJRT |

## Cross-References

- [elf-entry-and-init-proc.md](elf-entry-and-init-proc.md) — Stage 0: ELF entry, `.init`, `.init_array`, GOT/PLT bring-up at `dlopen`
- [do-init-do-fini.md](do-init-do-fini.md) — Stage 0 / Stage 5: constructor/destructor ordering, the `__do_init`/`__do_fini` byte guards, and the `FINI_ARRAY` body
- [get-pjrt-api-thunk.md](get-pjrt-api-thunk.md) — Stages 1–2: the `GetPjrtApi` thunk, the `GetTpuPjrtApi` 17-guard builder, the `.lbss` singleton, and the `tpu_plugin` injected slots
- [module-init-plugin-discovery.md](module-init-plugin-discovery.md) — the discovery handshake, the load-time init chain (CPU gate + register-only storm), and the `PJRT_Plugin_Initialize` bootstrap gate in full
- [tftpu-initialize-bootstrap.md](tftpu-initialize-bootstrap.md) — Stage 3: the initialize entry, `LIBTPU_INIT_ARGS` option ingest, and the `InitializeDriver` flag set
- [../pjrt/overview.md](../pjrt/overview.md) — the PJRT C-ABI map: the `PJRT_Api` struct shape, the handshake, and the extension chain by region
- [../pjrt/client-and-device.md](../pjrt/client-and-device.md) — Stage 4: `PJRT_Client_Create`, silicon detection, and the live executor stack
- [../pjrt/api-vtable-reconstruction.md](../pjrt/api-vtable-reconstruction.md) — the full 140-slot field-by-field table the Stage-2 build materializes
