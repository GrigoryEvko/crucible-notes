# TpuCompiler Roster

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, ELF x86-64 DYN, not stripped; demangled C++ names and IDA-recovered C names quoted verbatim). `.text` VMA equals file offset. Other versions will differ.*

## Abstract

`TpuCompiler_*` is the C-ABI face of the StreamExecutor TPU compiler — the flat `extern "C"` surface through which the open-source `xla::TpuCompiler` shim (built into TensorFlow/XLA, *not* into this binary) drives the closed `libtpu.so` compilation pipeline without sharing a single C++ type across the `.so` boundary. Where the host-side shim has methods named `RunHloPasses`, `RunBackend`, `Compile`, `ShapeSize`, and `DefaultDeviceShapeRepresentation`, each forwards through a function-pointer slot of `TfTpu_ExecutorApiFn` into one of the seven `TpuCompiler_*` free functions inventoried here. Every one of them takes only opaque handles plus a serialized-proto blob and a status-out object, reconstructs the rich `xla::` objects inside libtpu's *own* statically-linked XLA, calls the real compiler vtable, and re-serializes the result. The compiler object behind the handle is a `xla::jellyfish::DeepseaCompiler` — the TPU ("Deepsea"/Jellyfish) subclass of `xla::Compiler`.

A second, lexically adjacent cluster — `TpuCompile_*` (six functions) — is **not** the same surface and is a frequent source of confusion. `TpuCompiler_*` is the SE class-method C-API reached through `ExecutorApiFn`; `TpuCompile_*` is the standalone support layer for the TensorFlow `TPUCompileOp` kernel: a one-shot `CompileAndBuild` that runs `tensorflow::tpu::TpuCompileOpKernelCommon` end-to-end and emits `XLA_TpuProgram` handles, plus compilation-cache-key construction, guaranteed-const fingerprinting, and two policy predicates. The two clusters share the `tpu_executor_c_api.cc` / `tpu_util_c_api.cc` translation-unit family and the `Compile` verb, but they sit at different layers: `TpuCompiler_*` is *one method of the compiler class*, `TpuCompile_CompileAndBuild` is *the whole TF compile op*.

This page owns the **per-function roster and impl-symbol map** for both clusters. The ABI seam itself — why a flat C interface exists, the `*ApiFn()` accessor pattern, and the opaque-handle / `ApiConverter::ToC`/`FromC` convention — is established once on the [shim overview](overview.md) and not restated here. The HLO pass *schedule* these functions invoke is owned by the [HLO Pass Registry](../compiler/hlo-pass-registry.md); the `XLA_TpuProgram` handle they produce is owned by the [TpuProgram Roster](tpu-program-roster.md).

For reimplementation, the contract is:

- **The seven `TpuCompiler_*` signatures and dispatch** — which proto crosses each (`HloModuleProto`, `HloModuleGroupProto`), which compiler vtable slot each indexes (`+24` RunHloPasses, `+32` RunBackend, `+96` ShapeSize, `+104` DefaultDeviceShapeRepresentation), and the handle's double-pointer shape.
- **The proto-in / proto-out marshalling discipline** — `DeserializeProto` in, `CreateFromProto` to build the `xla::HloModule`, run, `ToProto` + `SerializePartialToArray` out into a caller-freed byte buffer.
- **The `DeepseaCompiler` object model** — the handle is `Compiler**`; the real object is a `xla::jellyfish::DeepseaCompiler`; `RegisterAllPhases` is the one-time phase-registration hook that the open-source `TpuCompiler` constructor calls (the "Initialize" step).
- **The `TpuCompile_*` support cluster** — what `CompileAndBuild` runs, the cache-key/fingerprint helpers, and the abort-suppression policy predicates.

| | |
|---|---|
| **Handle type** | `xla::jellyfish::DeepseaCompiler**` (double-pointer; outer = 8-byte box, inner = the compiler) |
| **Constructor** | `TpuCompiler_New @ 0xeabc4a0` → `operator new(8)` ×2, `DeepseaCompiler::DeepseaCompiler` |
| **Destructor** | `TpuCompiler_Free @ 0xeabc4e0` → virtual dtor slot `+8`, then `free` of the box |
| **Compile dispatch** | `xla::Compiler` vtable: RunHloPasses `+24`, RunBackend `+32`, Compile via `xla::Compiler::Compile`, ShapeSize `+96`, DefaultDeviceShapeRepresentation `+104` |
| **Wire format in** | `TpuSerializedProto` (ptr+len) → `stream_executor::tpu::DeserializeProto<…>` |
| **Status channel** | trailing `absl::status_internal::StatusRep**` out-param (the SE "ok?-code-msg" idiom) |
| **C-ABI source TU** | `learning/45eac/tfrc/executor/stream_executor/tpu_executor_c_api.cc` (compiler), `tpu_util_c_api.cc` (TpuCompile helpers) |
| **Phase-registration hook** | `xla::TpuCompiler::RegisterAllPhases() @ 0xf849ec0` |
| **Roster size** | 7 `TpuCompiler_*` + 6 `TpuCompile_*` = 13 free functions |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **NOTE —** there is no `TpuCompiler_Initialize` symbol in this build. The "initialize" step a reimplementer expects is split in two: object construction is `TpuCompiler_New`, and one-time phase-table registration is `xla::TpuCompiler::RegisterAllPhases` (`0xf849ec0`), called from the host-side `xla::TpuCompiler` constructor — not from the C-ABI. Likewise there is no `TpuCompiler_*ToTpuProgram`; producing a serialized `XLA_TpuProgram` is the job of `TpuCompile_CompileAndBuild` in the second cluster, while `TpuCompiler_RunBackend` returns an in-process `xla::Executable*` handle rather than a serialized program.

---

## 1. Lifecycle — `New` / `Free`

### Purpose

Construct and destroy the opaque compiler handle. The host-side `xla::TpuCompiler` shim holds the handle returned by `New` for its lifetime and releases it through `Free`. The compiler is stateless across calls — `RunHloPasses` / `RunBackend` / `Compile` each take the handle and a fresh module — so a single handle services the whole process.

### Algorithm

```c
// TpuCompiler_New                                              0xeabc4a0
DeepseaCompiler** TpuCompiler_New():
    box   = operator new(8)                  // the handle the host receives
    inner = operator new(8)                  // the actual compiler object (8-byte: just a vptr)
    DeepseaCompiler::DeepseaCompiler(inner)  // base xla::Compiler ctor; sets vtable
    *box = inner
    return box                               // host holds a Compiler**

// TpuCompiler_Free                                             0xeabc4e0
void TpuCompiler_Free(Compiler** box):
    if *box:
        (*box)->vtable[+8](*box)             // virtual destructor — DeepseaCompiler::~DeepseaCompiler
    free(box)                                // release the outer box
```

> **QUIRK —** the handle is a *double* pointer. `New` allocates an 8-byte box, allocates the 8-byte compiler, and stores the compiler inside the box; every other `TpuCompiler_*` function dereferences twice (`**a1`) to reach the vtable. A reimplementer who hands back the compiler pointer directly will mis-align every downstream `(*(_QWORD *)*a1 + off)` vtable index. The compiler object itself is only 8 bytes — it is pure-virtual behavior over a vtable, with no instance state — so `DeepseaCompiler` carries its configuration through the `CompileOptions` passed per call, not through fields.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuCompiler_New` | `0xeabc4a0` | Allocate box + `DeepseaCompiler`, return `Compiler**` | CERTAIN |
| `TpuCompiler_Free` | `0xeabc4e0` | Virtual-destruct the compiler, free the box | CERTAIN |

### Considerations

`New` takes no arguments and cannot fail in the observed body (no status-out; raw `operator new` will `std::terminate` on OOM rather than return null). `Free` tolerates a null inner pointer (`if (*ptr)`) but not a null box. Registration of the phase table — the part a reimplementer might expect inside `New` — happens elsewhere: see [§5](#5-phase-registration-the-initialize-step).

---

## 2. HLO Passes & Backend — `RunHloPasses` / `RunBackend`

### Purpose

The two halves of XLA's two-phase compile. `RunHloPasses` runs the target-independent-then-TPU HLO optimization pipeline over a module and returns the *optimized module* (re-serialized). `RunBackend` consumes an already-optimized module and lowers it to a backend `xla::Executable`, returned as an in-process handle. The host shim calls them back-to-back when it wants to interpose between the two phases (e.g. to cache the optimized HLO); `Compile` ([§3](#3-compile--the-fused-entry)) is the fused form.

### Entry Point

```text
xla::TpuCompiler::RunHloPasses(...)            (host-side shim, not in this binary)
  └─ ExecutorApiFn()->slot[RunHloPasses]       0x20819360 = ExecutorApiFn() accessor
       (returns &executor_api_fn @ 0x2258c818; the RunHloPasses fn-ptr lives in that struct)
       └─ TpuCompiler_RunHloPasses             0xeabcd80   — C-ABI impl
            └─ DeepseaCompiler vtable[+24]      xla::Compiler::RunHloPasses override
```

`RunBackend` is identical with slot/offset `+32`.

### Algorithm

```c
// TpuCompiler_RunHloPasses                                     0xeabcd80
// args: a1=Compiler** handle, a2=TpuSerializedProto* (HloModuleProto in),
//       a3=XLA_HloModuleConfig*, a4=SE_StreamExecutor* (may be null),
//       a5=TpuSerializedProto* (out), a6=StatusRep** (status out)
int TpuCompiler_RunHloPasses(a1, a2, cfg, exec, out, status):
    proto = DeserializeProto<HloModuleProto, TpuSerializedProto>(a2)   // ptr+len → proto
    config = ApiConverter::FromC(cfg)                                  // XLA_HloModuleConfig → xla::HloModuleConfig
    module = HloModule::CreateFromProto(proto, config, /*flags*/1,0,1,0)
    if module is error: write status; return                          // StatusOr unwrap

    // build a CompileOptions whose device allocator wraps the SE executor, if given
    opts = {}
    if exec && exec->allocator:
        opts.device_allocator = new WrapperDeviceMemoryAllocator{      // vtable off_21616EF8
            platform = GetUnderlyingDeepseaPlatform(),                 // Meyers singleton
            executor = exec }
    opts.layout_canonicalization_callback = empty                      // default-constructed std::function

    optimized = (**a1).vtable[+24](compiler, module, exec, opts)       // xla::Compiler::RunHloPasses
    if optimized is error: write status; goto cleanup

    // re-serialize the optimized module back across the seam
    p = HloModuleProto(); optimized->ToProto(&p)
    n = p.ByteSizeLong()
    buf = operator new(n)
    if !p.SerializePartialToArray(buf, n): LOG(FATAL) "proto_helper.h:45"
    out->ptr = buf; out->len = n                                       // caller frees buf
cleanup:
    destroy module/optimized/opts; return
```

`RunBackend` (`0xeabd100`) is structurally the same up to the dispatch, then diverges at the result:

```c
// TpuCompiler_RunBackend                                       0xeabd100  (vtable +32)
    executable = (**a1).vtable[+32](compiler, module, exec, opts)  // xla::Compiler::RunBackend → xla::Executable*
    if ok:
        h = operator new(8); *h = executable                       // box the Executable*
        *out = h                                                   // out is an SE_Executable** handle, NOT a proto
```

> **QUIRK —** `RunHloPasses` returns a re-serialized `HloModuleProto` (a byte buffer the caller frees), but `RunBackend` returns a *live* `xla::Executable*` boxed in an 8-byte allocation — the backend result never crosses the seam as a proto. The asymmetry is deliberate: optimized HLO is portable and cacheable, so it is serialized; an `Executable` holds device-resident state and is consumed in-process, so only its pointer (an opaque [TpuExecutable](tpu-executable-roster.md) handle) is handed back.

> **GOTCHA —** both functions take an optional `SE_StreamExecutor*` (`a4`). When non-null, the impl lazily constructs a `WrapperDeviceMemoryAllocator` (vtable `off_21616EF8`, `Allocate`/`Deallocate`/`GetStream` over a `stream_executor::DeviceMemoryAllocator`) bound to the process-wide `DeepseaPlatform` singleton (`GetUnderlyingDeepseaPlatform`, guarded by a `_cxa_guard`) and threads it into `CompileOptions.device_allocator`. A reimplementer that ignores `a4` will compile without a device allocator and silently disable allocation-aware passes (e.g. memory-space assignment that needs real HBM sizing).

### Function Map

| Function | Address | Vtable slot | Result form | Confidence |
|---|---|---|---|---|
| `TpuCompiler_RunHloPasses` | `0xeabcd80` | `+24` | re-serialized `HloModuleProto` (byte buf) | CERTAIN |
| `TpuCompiler_RunBackend` | `0xeabd100` | `+32` | boxed `xla::Executable*` handle | CERTAIN |

### Considerations

The `CreateFromProto` flags `(1,0,1,0)` request prohibit-ill-formed / no-verifier-on-the-cheap-path behavior (exact flag semantics LOW — derived from arg positions, not a verifier trace). The `LOG(FATAL)` on a failed `SerializePartialToArray` (`proto_helper.h:45`) means a reimplementation must guarantee the buffer is sized by `ByteSizeLong()` before serializing — there is no error return for an undersized buffer, only process abort.

---

## 3. Compile — the Fused Entry

### Purpose

`TpuCompiler_Compile` is the single-call form: it takes an `HloModuleGroupProto`, builds the module, and runs the full optimize-then-lower pipeline via `xla::Compiler::Compile`, returning one boxed `Executable*` per module into a caller-provided array. It is the path the PJRT adapter and most callers use when they do not need to interpose between HLO passes and backend.

### Algorithm

```c
// TpuCompiler_Compile                                          0xeabc520
// a1=Compiler** handle, group proto in, a5/a3 = module count guard,
// per-executor list in, a6=array-of-StreamExecutor-lists, a7=out Executable* array, a8=StatusRep**
void TpuCompiler_Compile(a1, group, ..., out_array, status):
    grp = DeserializeProto<HloModuleGroupProto, TpuSerializedProto>(...)
    if module_count > 1:                                          // a5<=1 && v54<2 guard
        *status = MakeError("Can not compile multiple HLO modules at once.")   // c_api.cc:1040
        return
    config = ApiConverter::FromC(...)
    if grp.modules_size() <= 0: LogIndexOutOfBoundsAndAbort()      // proto bounds check
    module = HloModule::CreateFromProto(grp.modules(0), config, 1,0,1,0)
    if error: write status; cleanup

    // install the layout-canonicalization callback (TpuCompiler_Compile::$_0) on the module config
    module.config.set_layout_canonicalization_callback(&$_0)       // module+3864/+3872 fn-ptr pair

    // flatten the per-module vector<StreamExecutor*> (loop-unrolled by 8)
    execs = copy_stream_executors(...)
    opts  = CompileOptions{ device_allocator = WrapperDeviceMemoryAllocator(platform, execs) if present }
    result = xla::Compiler::Compile(compiler, module, execs, opts)  // StatusOr<vector<unique_ptr<Executable>>>
    if ok:
        for i in result:                                           // box each Executable*, transfer ownership
            h = operator new(8); *h = result[i].release()
            out_array[i] = h
    else:
        write status
    free temporaries; cleanup
```

### Function Map

| Function | Address | Proto in | Result form | Confidence |
|---|---|---|---|---|
| `TpuCompiler_Compile` | `0xeabc520` | `HloModuleGroupProto` | array of boxed `Executable*` | CERTAIN |

### Considerations

> **GOTCHA —** the single-call entry takes an `HloModuleGroupProto` (a *group*) but rejects any group with more than one module: `"Can not compile multiple HLO modules at once."` at `tpu_executor_c_api.cc:1040`. The group-shaped input is a vestige of the generic `xla::Compiler::Compile(HloModuleGroup, ...)` signature; the TPU C-API supports exactly one module per call. A reimplementer must keep the group container but enforce the size-1 invariant, and must still bounds-check `modules(0)` (the impl calls `LogIndexOutOfBoundsAndAbort` on an empty group).

> **QUIRK —** `Compile` installs a *layout-canonicalization callback* (`TpuCompiler_Compile::$_0`) directly onto the module config (`module+3864/+3872`) before dispatch, whereas `RunHloPasses`/`RunBackend` install an *empty* callback. This is how the fused path lets the backend re-canonicalize entry-computation layouts that the split path leaves to the host. Reproducing `Compile` as "RunHloPasses then RunBackend" without this callback will diverge on layout-sensitive modules.

The per-module executor list is copied with an 8-way unrolled loop and the source pointers are double-dereferenced (`**(_QWORD**)(base + 8*i)`) — the input is a `vector<vector<StreamExecutor*>>` flattened to one inner list, since only one module is allowed.

---

## 4. Shape Queries — `ShapeSize` / `DefaultDeviceShapeRepresentation`

### Purpose

Two pure functions of the compiler that the host needs for buffer sizing and layout, independent of any compile. `ShapeSize` returns the byte size a shape occupies on device; `DefaultDeviceShapeRepresentation` maps a host (logical) shape to the device (physical, tiled/padded) shape the TPU actually stores.

### Algorithm

```c
// TpuCompiler_ShapeSize                                        0xeabd400
int64 TpuCompiler_ShapeSize(Compiler** a1, XLA_Shape* a2):
    shape = ApiConverter::FromC(a2)                          // XLA_Shape → xla::Shape
    fn = (**a1).vtable[+96]()                                // returns a ShapeSizeFunction object (closure)
    size = fn.call(shape)                                    // invoke the size functor
    fn.dtor()                                                // release the functor
    return size

// TpuCompiler_DefaultDeviceShapeRepresentation                0xeabd480
void TpuCompiler_DefaultDeviceShapeRepresentation(Compiler** a1, XLA_Shape* in, XLA_Shape* out):
    host_shape = ApiConverter::FromC(in)
    dev_shape  = (**a1).vtable[+104](compiler, host_shape)   // xla::Compiler::DefaultDeviceShapeRepresentation
    ApiConverter::ToC(dev_shape, out)                        // xla::Shape → XLA_Shape (caller owns 'out')
```

> **NOTE —** `ShapeSize` does not call a vtable method directly; slot `+96` returns a *functor* (a `std::function`-like `ShapeSizeFunction`, the 6-qword `v6` block: vtable, state, invoke-ptr, deleter), which the C-ABI then invokes and destroys. This indirection lets the compiler expose its size policy as a first-class callable to the host. A reimplementer must invoke the returned functor and run its deleter (slot `+8` of the functor's vtable), not assume `+96` is the sizer itself.

### Function Map

| Function | Address | Vtable slot | Returns | Confidence |
|---|---|---|---|---|
| `TpuCompiler_ShapeSize` | `0xeabd400` | `+96` (functor factory) | `int64` byte size | CERTAIN |
| `TpuCompiler_DefaultDeviceShapeRepresentation` | `0xeabd480` | `+104` | device `XLA_Shape` (out-param) | CERTAIN |

### Considerations

`DefaultDeviceShapeRepresentation` fills a caller-provided `XLA_Shape` via `ApiConverter::ToC`, so the caller owns the result and must pair it with `ApiConverter::Destroy(XLA_Shape*)` (the interior-free overload). The device shape it returns is the tiled/padded representation the TPU uses for the input host shape — the mechanism by which XLA learns the TPU's `(8, 128)`-style tiling for a buffer without libtpu exposing the layout assignment internals.

---

## 5. Phase Registration — the "Initialize" Step

### Purpose

The open-source `xla::TpuCompiler` constructor performs one-time registration of every TPU compilation phase into the global phase registry. In this binary that hook is `xla::TpuCompiler::RegisterAllPhases() @ 0xf849ec0`. It is *not* part of the `TpuCompiler_*` C-ABI — it is a C++ method compiled into libtpu because XLA is statically linked here — but a reimplementer building the host side must call it exactly once before any compile, which is why it is the de-facto "Initialize."

### Considerations

`RegisterAllPhases` is also the bridge to the PJRT **PhaseCompile** extension: the same phase set that `RegisterAllPhases` populates is what the PJRT `PhaseCompile` extension enumerates and drives phase-by-phase. The extension's wrapping of this registration is owned by [PJRT PhaseCompile Extension](../pjrt/ext-compile-phasecompile.md); the concrete pass *schedule* the phases run is owned by the [HLO Pass Registry](../compiler/hlo-pass-registry.md). This page records only that the registration entry point exists and is the missing "Initialize" half of `TpuCompiler_New`.

| Function | Address | Role | Confidence |
|---|---|---|---|
| `xla::TpuCompiler::RegisterAllPhases` | `0xf849ec0` | one-time phase-registry population (host-side ctor hook) | HIGH |

---

## 6. The `TpuCompile_*` Support Cluster

### Purpose

A separate six-function cluster that backs the TensorFlow `TPUCompileOp` kernel rather than the SE compiler class. Its centerpiece, `CompileAndBuild`, runs an *entire* TF compile op — metadata proto in, `tensorflow::tpu::TpuCompileOpKernelCommon` driven to completion, `XLA_TpuProgram` handles out — while the other five are small utilities for the TF compilation cache and abort policy. These live in `tpu_util_c_api.cc` / the compilation-cache-key TU, not `tpu_executor_c_api.cc`.

### Algorithm — the standalone compile

```c
// TpuCompile_CompileAndBuild                                   0xe8bc1e0
// a1=TPUCompileMetadataProto (serialized), a2=mlir/computation input,
// a4/a5 = out program list, a6=StatusRep**
int TpuCompile_CompileAndBuild(meta, input, ..., out_programs, status):
    metadata = parse TPUCompileMetadataProto(meta)
    kernel   = TpuCompileOpKernelCommon(metadata, ...)            // the TF compile-op core
    platform = GetRegisteredDeepseaPlatform()                     // deepsea::executor singleton
    topology = platform->GetTopology()
    result   = kernel.Compile(... topology ...)                   // -> CompiledProgramsAndMetadatas
    if ok:
        for each compiled program in result:
            out_programs[i] = (XLA_TpuProgram*) program           // hand back program handles
    else:
        *status = result.status()
```

`CompileAndBuild` is the one `TpuCompile_*` function that produces a [TpuProgram](tpu-program-roster.md) — it bridges the TF op layer to the serialized `XLA_TpuProgram` handle, which is where the `*ToTpuProgram` behavior a reimplementer expects actually lives.

### The cache / fingerprint / policy helpers

```c
// TpuCompile_CreateGuaranteedConstFingerprint                  0xf6a2040
uint64 CreateGuaranteedConstFingerprint(uint64 seed, const char* data, int64 len):
    if len < 0: BUG()                                            // size sanity → trap
    return FingerprintCat2011(seed, Fingerprint2011(data, len))  // 64-bit non-crypto fp

// TpuCompile_DestroyCompilationCacheKey                        0xf6a2e60
void DestroyCompilationCacheKey(void* key, void* prefix):
    if key:    free(key)                                         // two heap strings owned by the key
    if prefix: free(prefix)

// TpuCompile_IsTpuCompilationEnabled                           0xf6a1b40
bool IsTpuCompilationEnabled(): return true                      // constant in this build

// TpuCompile_ShouldTpuCompileOpIgnoreCancellation              0xf6a1b60
bool ShouldTpuCompileOpIgnoreCancellation():
    if !TpuCompilationCancellationTerminatesProcess():
        LOG(WARNING) "...process abort is suppressed... only meant for tests b/79359718"
        return true
    if GetCommandLineOption("xla_jf_exit_process_on_compilation_success") == "true":
        LOG(WARNING) "...abort suppressed when --XLA_jf_exit_process_on_compilation_success... b/72471718"
        return true
    return false
```

> **QUIRK —** `TpuCompile_IsTpuCompilationEnabled` is a hard-coded `return 1` in this build. The function exists because the open-source TF op calls it as a runtime gate, but the shipped libtpu always reports compilation enabled — there is no flag that turns it off. A reimplementer can treat the gate as a no-op for this version, but must keep the symbol because the host op binds to it by name through the API table.

### Function Map

| Function | Address | Role | Source TU | Confidence |
|---|---|---|---|---|
| `TpuCompile_CompileAndBuild` | `0xe8bc1e0` | Run the whole `TpuCompileOpKernelCommon`; emit `XLA_TpuProgram` handles | `tpu_util_c_api.cc` (HIGH) | CERTAIN |
| `TpuCompile_CreateCompilationCacheKey` | `0xf6a2080` | Build the TF compilation-cache key (config + fingerprints) | cache-key TU | HIGH |
| `TpuCompile_CreateGuaranteedConstFingerprint` | `0xf6a2040` | `FingerprintCat2011(seed, Fingerprint2011(data,len))` | cache-key TU | CERTAIN |
| `TpuCompile_DestroyCompilationCacheKey` | `0xf6a2e60` | `free` the two heap strings inside a cache key | cache-key TU | CERTAIN |
| `TpuCompile_IsTpuCompilationEnabled` | `0xf6a1b40` | Constant `true` runtime gate | `tpu_util_c_api.cc` | CERTAIN |
| `TpuCompile_ShouldTpuCompileOpIgnoreCancellation` | `0xf6a1b60` | Abort-suppression policy for cancelled compiles (test-only) | `tpu_util_c_api.cc` | CERTAIN |

### Considerations

The cache-key cluster is the data path for XLA's TPU compilation cache: `CreateCompilationCacheKey` assembles a key string from the metadata and the guaranteed-const fingerprint, `CreateGuaranteedConstFingerprint` produces the 64-bit fingerprint over const inputs, and `DestroyCompilationCacheKey` frees the two heap buffers the key owns (the key proper and a prefix). `Fingerprint2011` is the non-cryptographic 64-bit hash; a reimplementer must reproduce both `Fingerprint2011` and the `FingerprintCat2011` combiner bit-exactly or cache keys will not collide across the host/plugin split.

> **GOTCHA —** the two abort-suppression branches in `ShouldTpuCompileOpIgnoreCancellation` are explicitly test-only (cited bug refs `b/79359718`, `b/72471718`) and both *log a warning* before returning `true`. In production both predicates are false, so the function returns `false` and a cancelled compile aborts the process. A reimplementation that returns `true` by default to "be safe" inverts the intended fail-fast behavior of the TPU compile op.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::jellyfish::DeepseaCompiler` | the real `xla::Compiler` subclass behind every `TpuCompiler_*` handle |
| `xla::TpuCompiler` (host-side shim) | the open-source class whose methods forward into this roster; ctor calls `RegisterAllPhases` |
| `xla::Compiler::Compile` / `RunHloPasses` / `RunBackend` | the vtable methods (`+24`/`+32`) the C-ABI dispatches into |
| `ApiConverter::ToC` / `FromC` | marshals `XLA_Shape` / `XLA_HloModuleConfig` across the seam (see overview) |
| `stream_executor::tpu::DeserializeProto<…>` | unpacks the `TpuSerializedProto` (ptr+len) blobs the compile entries take |
| `deepsea::executor::DeepseaPlatform` | the process-wide platform singleton bound into `CompileOptions.device_allocator` |
| `WrapperDeviceMemoryAllocator` | the `DeviceMemoryAllocator` subclass (vtable `off_21616EF8`) the compile entries wrap around an `SE_StreamExecutor*` |
| `tensorflow::tpu::TpuCompileOpKernelCommon` | the TF op core that `TpuCompile_CompileAndBuild` drives |
| `XLA_TpuProgram` | the serialized-program handle `CompileAndBuild` emits and `RunBackend` does *not* |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the ABI seam, the `*ApiFn()` accessor pattern, and the opaque-handle / `ApiConverter` convention this roster relies on
- [TpuProgram Roster](tpu-program-roster.md) — the `XLA_TpuProgram` serialized-program handle that `TpuCompile_CompileAndBuild` produces
- [TpuExecutable Roster](tpu-executable-roster.md) — the boxed `xla::Executable*` handle that `TpuCompiler_RunBackend` / `Compile` return
- [HLO Pass Registry](../compiler/hlo-pass-registry.md) — the concrete pass schedule that `RunHloPasses` executes inside the `DeepseaCompiler` vtable
- [PJRT PhaseCompile Extension](../pjrt/ext-compile-phasecompile.md) — the PJRT extension that wraps `xla::TpuCompiler::RegisterAllPhases` and drives compilation phase-by-phase
