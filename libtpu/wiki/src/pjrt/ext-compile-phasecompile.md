# PhaseCompile Extension (type 9)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (`libtpu_lts_20260413_b_RC00`, BuildID `89edbbe81c5b328a958fe628a9f2207d`, ELF x86-64 DYN, ~745 MB). The PJRT C-API surface is **v0.103**. Other builds renumber slots and shift addresses.*

## Abstract

The **PhaseCompile extension** (`PJRT_Extension_Type == 9`, a 64-byte struct) is libtpu's *incremental* compile ABI. Where the one-shot `PJRT_Client_Compile` (slot 25) hands a whole program plus a `CompileOptionsProto` to the client and returns a finished, device-loaded executable, the PhaseCompile extension lets a host framework drive the same `xla::TpuCompiler` **one named phase at a time** — running, for example, `phase0_stablehlo_to_hlo`, caching the intermediate HLO blob, then later running `phase1_hlo_opts` and onward. The phase boundary is a serialized `PjRtPartialProgramProto` blob, so partial results can be cached, inspected, or split across hosts. Both surfaces are backed by the *same* `xla::TpuCompiler` object and the *same* `CompileOptionsProto` deserialization chokepoint; they differ only in whether the intermediate artifacts are exposed.

The extension is a flat function-pointer table appended to the `PJRT_Api` extension chain. Three of its five slots are stock generic XLA C-ABI drivers (`pjrt::PJRT_PhaseCompile_*`, byte-for-byte the upstream `pjrt_c_api_phase_compile_internal.cc` code); two — `Get_Compiler` and `Destroy_Compiler` — are TPU-injected (`tpu_plugin::*TpuPhaseCompiler`) and are the only TPU-specific code on the surface. The injected `Get_Compiler` mints an `xla::TpuCompiler` and registers exactly **six** named phases on it; the generic `Run_Phase` / `Get_Phase_Names` drivers then dispatch through two virtual slots (`+48` `GetPhaseNames`, `+56` `RunPhases`) on the registered compiler.

This page owns the **PhaseCompile extension struct, the five-slot phase API, and the compile-options proto ingest** that every PhaseCompile call shares with the monolithic path. The finished executable that compilation produces — its C-ABI box, serialize / deserialize round-trip, and `Execute` — is on [Executable Loading & Execution](executable-execution.md). The HLO pass pipeline the phases actually drive (the body of `phase1_hlo_opts` and the lowering phases) is on [HLO Pass Registry](../compiler/hlo-pass-registry.md); the content-addressed store that caches partial and full results is on [Compilation Cache](../compiler/compilation-cache.md).

For reimplementation, the contract is:

- **The 64-byte extension struct.** A `PJRT_Extension_Base` header (`size=64`, `type=9`, `next`) followed by five function pointers at `+0x18 … +0x38`. The creator writes the two TPU-injected pointers from arguments and hard-codes the three generic drivers.
- **The compiler handle.** `Get_Compiler` `operator new`s a `0x40`-byte `xla::TpuCompiler` (vtable `off_2177D660`), calls `RegisterAllPhases`, wraps it in a `0x10`-byte `{ base, owner }` holder, and stores the holder in the args out-field. The holder — not the compiler — is the opaque handle the host carries; `Run_Phase`/`Get_Phase_Names` dereference `holder.base`, `Destroy_Compiler` frees `holder.owner`.
- **The six registered phases**, their order, and that `phase3_linking` / `phase3_linking_test_only` are two registrations of one function differing only by a bound `bool`.
- **The shared compile-options chokepoint.** `Run_Phase` (like every compile entry) does `CompileOptionsProto::ParseFromString` → `CompileOptions::FromProto`, returning the identical `"PJRT_Client_Compile: failed to deserialize CompileOptionsProto"` on parse failure.
- **The char-buffer marshaling.** Phase names and partial-program blobs cross the C-ABI as parallel `(char**, size_t*, count)` spans; `Convert*` helpers translate them to/from C++ `string` / `PjRtPartialProgramProto` vectors, and `C_Buffers_Destroy` frees the returned arrays.

| | |
|---|---|
| **Extension type** | `9` (`PJRT_Extension_Type::PhaseCompile`) |
| **Extension struct size** | `0x40` (64 B); 5 fn-ptrs at `+0x18..+0x38` |
| **Creator** | `pjrt::CreatePhaseCompileExtension @ 0x0E6F42A0` |
| **Storage** | `.bss` slot `GetTpuPjrtApi::phase_compile_extension @ 0x224C3B18` |
| **Get_Compiler (`+0x18`, TPU)** | `pjrt::tpu_plugin::GetTpuPhaseCompiler @ 0x0E6AA320` |
| **Destroy_Compiler (`+0x20`, TPU)** | `pjrt::tpu_plugin::DestroyTpuPhaseCompiler @ 0x0E6AA400` |
| **Run_Phase (`+0x28`, generic)** | `pjrt::PJRT_PhaseCompile_Run_Phase @ 0x0E6F42E0` |
| **Get_Phase_Names (`+0x30`, generic)** | `pjrt::PJRT_PhaseCompile_Get_Phase_Names @ 0x0E6F4A60` |
| **C_Buffers_Destroy (`+0x38`, generic)** | `pjrt::PJRT_PhaseCompile_C_Buffers_Destroy @ 0x0E6F4CC0` |
| **Compiler object** | `xla::TpuCompiler` (`0x40` B, vtable `off_2177D660`) |
| **Phase count** | 6 (5 distinct functions + 1 `test_only` alias) |
| **Source (asserts)** | `…/pjrt/c/pjrt_c_api_phase_compile_internal.cc`; `…/research/pjrt/tpu_pjrt_compiler.cc` |

---

## The Extension Struct

### Purpose

The PhaseCompile extension is one link in the `PJRT_Api` extension chain (see [Extension Chain](extension-chain.md)): a host walks the `PJRT_Extension_Base` `next` list, matches `type == 9`, and casts the link to `PJRT_PhaseCompile_Extension` to reach the five phase functions. The struct is assembled once at plugin init by `CreatePhaseCompileExtension` into a fixed `.bss` slot (`0x224C3B18`), populated from `GetTpuPjrtApi` with the two TPU-injected function pointers passed as arguments.

### Layout

```c
struct PJRT_PhaseCompile_Extension {       // struct_size 0x40 (64 B), type 9
    PJRT_Extension_Base base;              // +0x00 size=64; +0x08 type=9; +0x10 next
    /* +0x18 */ Get_Compiler*      get_compiler;       // TPU-injected (arg a3)
    /* +0x20 */ Destroy_Compiler*  destroy_compiler;   // TPU-injected (arg a4)
    /* +0x28 */ Run_Phase*         run_phase;          // generic pjrt:: driver
    /* +0x30 */ Get_Phase_Names*   get_phase_names;    // generic pjrt:: driver
    /* +0x38 */ C_Buffers_Destroy* c_buffers_destroy;  // generic pjrt:: driver
};
```

### Algorithm — the creator

```c
function CreatePhaseCompileExtension(ext, next, get_fn, destroy_fn):  // 0xe6f42a0
    ext[+0x00] = 64                                  // struct_size
    ext[+0x08] = 9                                   // PJRT_Extension_Type::PhaseCompile
    ext[+0x10] = next                                // chain link (arg a2)
    ext[+0x18] = get_fn                              // arg a3  (TPU GetTpuPhaseCompiler)
    ext[+0x20] = destroy_fn                          // arg a4  (TPU DestroyTpuPhaseCompiler)
    ext[+0x28] = pjrt::PJRT_PhaseCompile_Run_Phase           // generic driver
    ext[+0x30] = pjrt::PJRT_PhaseCompile_Get_Phase_Names     // generic driver
    ext[+0x38] = pjrt::PJRT_PhaseCompile_C_Buffers_Destroy   // generic driver
    return ext
```

The creator is a pure flat-table writer with no allocation or branching — verified at `0x0E6F42A0`, eight stores and a return. The split between *injected* (`get`/`destroy`) and *generic* (`run`/`names`/`destroy-buffers`) is the whole design: the backend supplies only the compiler factory and teardown, and the upstream C-ABI drivers handle all marshaling against whatever compiler object the factory returns.

> **NOTE —** the two injected pointers arrive as call arguments (`a3`, `a4`), not from a TPU symbol the creator references directly. This is the generic-creator pattern shared across the libtpu extension chain: `CreatePhaseCompileExtension` is itself stock XLA code that the TPU `GetTpuPjrtApi` calls with TPU function pointers. A reimplementer porting libtpu to another backend rewrites only `Get_Compiler`/`Destroy_Compiler`; the creator and the three drivers are unchanged.

### Function Map

| Slot | Field | Symbol | Addr | Origin | Confidence |
|---|---|---|---|---|---|
| `+0x18` | `get_compiler` | `tpu_plugin::GetTpuPhaseCompiler` | `0x0E6AA320` | TPU-injected | CERTAIN |
| `+0x20` | `destroy_compiler` | `tpu_plugin::DestroyTpuPhaseCompiler` | `0x0E6AA400` | TPU-injected | CERTAIN |
| `+0x28` | `run_phase` | `pjrt::PJRT_PhaseCompile_Run_Phase` | `0x0E6F42E0` | generic | CERTAIN |
| `+0x30` | `get_phase_names` | `pjrt::PJRT_PhaseCompile_Get_Phase_Names` | `0x0E6F4A60` | generic | CERTAIN |
| `+0x38` | `c_buffers_destroy` | `pjrt::PJRT_PhaseCompile_C_Buffers_Destroy` | `0x0E6F4CC0` | generic | CERTAIN |
| — | creator | `pjrt::CreatePhaseCompileExtension` | `0x0E6F42A0` | generic | CERTAIN |

---

## `Get_Compiler` — minting the phase compiler

### Purpose

`Get_Compiler` is the entry the host calls first: it produces the opaque compiler handle every subsequent `Run_Phase` / `Get_Phase_Names` call carries. For the TPU backend this is `tpu_plugin::GetTpuPhaseCompiler` (`0x0E6AA320`), which builds a fresh `xla::TpuCompiler`, registers the six phases on it, and boxes it in a small ownership holder.

### Entry Point

```text
GetTpuPhaseCompiler (0xe6aa320, ext +0x18)
  ── ActualStructSizeIsGreaterOrEqual("PJRT_PhaseCompile_Get_Compiler_Args", 35, 24)
  ── operator new(0x40)                  ── xla::TpuCompiler, vtable off_2177D660, body zeroed
  ── xla::TpuCompiler::RegisterAllPhases (0xf849ec0)   ── registers the 6 phases
  └─ operator new(0x10)                  ── holder { base, owner } → args[+0x10]
```

### Algorithm

```c
function GetTpuPhaseCompiler(args):                       // 0xe6aa320
    if !ActualStructSizeIsGreaterOrEqual(                 // line 14
            "PJRT_PhaseCompile_Get_Compiler_Args", min=35, cur=24, args->struct_size):
        return new PJRT_Error{status}                     // guard miss

    compiler = operator new(0x40)                         // xla::TpuCompiler
    zero(compiler+0x08 .. compiler+0x3F)                  // vmovups ymm 32B x2
    compiler->vtable = off_2177D660                       // line 24

    status = xla::TpuCompiler::RegisterAllPhases(compiler) // 0xf849ec0, line 26
    if status == OK:                                      // (== 1)
        holder = operator new(0x10)                       // line 29
        holder->base  = compiler                          //   +0x00 — Run/Names deref this
        holder->owner = compiler                          //   +0x08 — Destroy frees this
        args[+0x10]   = holder                            // args[2], line 32
        return NULL                                       // success
    else:
        err = new PJRT_Error{status}                      // line 38
        compiler->vtable[+8](compiler)                    // TpuCompiler dtor — tear down
        return err
```

### Considerations

- **The `0x10` holder is the handle, not the compiler.** The host receives `holder`, an indirection of `{ TpuCompiler* base; TpuCompiler* owner; }`. `Run_Phase`/`Get_Phase_Names` read `holder->base` and dispatch through *its* vtable; `Destroy_Compiler` nulls and frees `holder->owner`. The two pointers are identical at creation, but the holder exists so that ownership (what gets deleted) is separable from access (what gets called) — the upstream PJRT pattern for a transferable-but-borrowable handle.
- **Failure tears down cleanly.** If `RegisterAllPhases` fails, the half-built compiler is destroyed through `vtable+8` (its dtor) before the error is returned — no leak of the `0x40`-byte object. The error is a heap `PJRT_Error` holding the `absl::Status`, the same shape every PJRT wrapper returns.
- **`struct_size` envelope.** The args struct guard is `min=35, cur=24`. As elsewhere on this surface (see [API Vtable Reconstruction](api-vtable-reconstruction.md#backward-compatibility-guard)), this is `ActualStructSizeIsGreaterOrEqual(name, expected_size_for_min_version, current_size, host_supplied_size)`; a host whose struct is smaller than the minimum is rejected with a `PJRT_Error` and the body reads nothing.

### Destroy_Compiler

```c
function DestroyTpuPhaseCompiler(args):                   // 0xe6aa400
    holder = args[+0x10]                                  // args[2]
    if holder:
        owner = holder->owner                             // +0x08
        holder->owner = NULL
        if owner: owner->vtable[+8](owner)                // TpuCompiler dtor
        free(holder)                                      // free the 0x10 box
```

`Destroy_Compiler` takes no `struct_size` guard and returns `void` — it is pure cleanup. It frees the compiler through the same `vtable+8` slot `Get_Compiler` uses on the failure path, then frees the holder. Nulling `owner` before the dtor call guards against a double-free if the host erroneously destroys twice.

---

## `Run_Phase` — driving one named phase

### Purpose

`Run_Phase` (`pjrt::PJRT_PhaseCompile_Run_Phase @ 0x0E6F42E0`) is the generic driver that executes an ordered list of named phases against the compiler handle. It is the heart of the incremental ABI: it unmarshals the input partial-program blobs and the phase-name list from C-ABI char-buffer spans, deserializes the `CompileOptionsProto` (the shared chokepoint), dispatches into `RunPhases` (compiler `vtable+56`), and remarshals the output partial-program vector back into char-buffer spans.

### Entry Point

```text
PJRT_PhaseCompile_Run_Phase (0xe6f42e0, ext +0x28)
  ── ActualStructSizeIsGreaterOrEqual("PJRT_PhaseCompile_Run_Phase_Args", 32, 120)
  ── ConvertCharBuffersToCppStrings           (phase names    → vector<string>)
  ── ConvertCharBuffersToPjRtPartialProgramProtos (input progs → vector<PjRtPartialProgramProto>)
  ── CompileOptionsProto::ParseFromString → CompileOptions::FromProto   [shared chokepoint]
  ── null-check holder (args[+0x10])          ── "…Run_Phase: phase compiler is null"
  ── (*holder->base->vtable[+56])(…)          ── PjRtPhaseCompiler::RunPhases
  └─ ConvertPjRtPartialProgramProtosToCharBuffers (output vec → char buffers, written out)
```

### Algorithm

```c
function PJRT_PhaseCompile_Run_Phase(args):               // 0xe6f42e0
    if !ActualStructSizeIsGreaterOrEqual(                 // line 76
            "PJRT_PhaseCompile_Run_Phase_Args", min=32, cur=120, args->struct_size):
        return new PJRT_Error{status}

    // --- unmarshal the two parallel char-buffer spans ---
    phase_names = ConvertCharBuffersToCppStrings(         // line 84
                      ptrs=args[+0x30], count=args[+0x40],
                      sizes=args[+0x38])                  // → vector<string>
    input_programs = ConvertCharBuffersToPjRtPartialProgramProtos(  // line 85
                      ptrs=args[+0x18], count=args[+0x28],
                      sizes=args[+0x20])                  // → vector<PjRtPartialProgramProto>

    // --- the compile-options chokepoint (see §Compile-Options Ingest) ---
    proto = CompileOptionsProto{}                          // line 134
    if !proto.ParseFromString(args[+0x48], args[+0x50]):   // line 135 — ptr +0x48, len +0x50
        return new PJRT_Error{MakeErrorImpl<3>(
            "PJRT_Client_Compile: failed to deserialize CompileOptionsProto",
            …, "…/pjrt_c_api_phase_compile_internal.cc")}   // :46 (NOT wrapper_impl:1113)
    opts = CompileOptions::FromProto(proto)                // line 138

    holder = args[+0x10]                                   // args[2]
    if !holder:                                            // line ~230
        return new PJRT_Error{MakeErrorImpl<13>(
            "PJRT_PhaseCompile_Run_Phase: phase compiler is null",  // :77
            …, "…/pjrt_c_api_phase_compile_internal.cc")}

    // --- drive the pipeline: RunPhases via compiler vtable +56 ---
    out_programs = (*holder->base->vtable[+56])(           // line 238
                       holder->base, &opts, input_programs,
                       topology = *(args[+0x88]) + 8,      //   PjRtTopologyDescription
                       phase_names)                        // → StatusOr<vector<PjRtPartialProgramProto>>
    if !out_programs.ok(): return new PJRT_Error{out_programs.status}

    // --- remarshal results into the out char-buffer span ---
    ConvertPjRtPartialProgramProtosToCharBuffers(out_programs, args+0x60..)
    return NULL
```

### The args layout

The decompiled body reads the following offsets within `PJRT_PhaseCompile_Run_Phase_Args`. The two parallel char-buffer spans (programs and phase names) each carry a `(ptrs, sizes, count)` triple; the semantic labels of the two spans are inferred from the `Convert*` call argument order.

| Off | Field (recovered) | Meaning | Conf |
|---|---|---|---|
| `+0x00` | `struct_size` | guard input; min 32, cur 120 | CERTAIN |
| `+0x10` | `phase_compiler` | the `{base,owner}` holder from `Get_Compiler` (`args[2]`) | CERTAIN |
| `+0x18` | `input_programs` | `char**` — serialized `PjRtPartialProgramProto` blobs | HIGH |
| `+0x20` | `input_programs_sizes` | `size_t*` — per-blob byte length | HIGH |
| `+0x28` | `input_programs_count` | element count | HIGH |
| `+0x30` | `phase_names` | `char**` — phase-name strings | HIGH |
| `+0x38` | `phase_names_sizes` | `size_t*` — per-name length | HIGH |
| `+0x40` | `phase_names_count` | element count | HIGH |
| `+0x60` | `out_programs` | `char**` out — written on success | HIGH |
| `+0x68` | `out_programs_sizes` | `size_t*` out | HIGH |
| `+0x70` | `out_programs_count` | count out | HIGH |
| `+0x88` | `topology` | `*(args+0x88)+8` → `PjRtTopologyDescription` | HIGH |
| `+0x48` | `compile_options` | `char*` — serialized `CompileOptionsProto`, fed to `ParseFromString` | HIGH |
| `+0x50` | `compile_options_size` | `size_t` — proto byte length | HIGH |

> **CORRECTION (PHASE-1) —** an earlier reading of `Run_Phase` placed the input-program span at `+0x18` *and* the phase-name span overlapping at `+0x30/+0x40` with a shared count, and mislabeled the options blob (the decompiled body reads `*(a1+72)`/`*(a1+80)`, i.e. hex `+0x48`/`+0x50`, not `+0x72`/`+0x80`). The decompiled body (`0xe6f42e0`, lines 84-85, 134-135) shows the two char-buffer spans are distinct `(ptr, size, count)` triples — programs read from `(+0x18, +0x20, +0x28)`, phase names from `(+0x30, +0x38, +0x40)` — not a shared count, and the options proto is `(ptr=+0x48, len=+0x50)`. The exact per-field *names* still depend on the public `pjrt_c_api_phase_compile.h` header order, which is not in the binary; the offsets above are read directly from the body and are HIGH.

### Considerations

- **The dispatch is `vtable+56`.** The decompiled body calls `(*(holder->base->vtable + 56))(…)`, which is `PjRtPhaseCompiler::RunPhases @ 0x1D16AF20`. This is the same compiler object the monolithic `PJRT_Client_Compile` drives, reached through a different vtable slot — the phased ABI exposes the *intermediate* `PjRtPartialProgramProto` artifacts that the monolithic path discards.
- **The error string is misleading — it names `PJRT_Client_Compile` even here.** The `CompileOptionsProto` parse failure returns `"PJRT_Client_Compile: failed to deserialize CompileOptionsProto"` even though this is `Run_Phase`. The parse logic is the same two-call sequence every compile entry runs, but `Run_Phase` carries its *own* inlined copy (emitted at `pjrt_c_api_phase_compile_internal.cc:46`, `MakeErrorImpl<3>`), distinct from the `pjrt_c_api_wrapper_impl.cc:1113` site the monolithic entries share — the string was simply copied verbatim (same quirk as `DeserializeAndLoad`; see [Executable Execution](executable-execution.md#serialize-and-deserialize-load)). A reimplementer should not key error handling off the string.
- **Errors at every step surface as `PJRT_Error*`.** A failed unmarshal, a failed options parse, a null compiler, or a `RunPhases` failure each produces a heap `PJRT_Error` holding the `absl::Status`; success returns `NULL`.

---

## `Get_Phase_Names` — enumerating the registered phases

### Purpose

`Get_Phase_Names` (`pjrt::PJRT_PhaseCompile_Get_Phase_Names @ 0x0E6F4A60`) returns the ordered list of phase names the compiler was registered with, so a host can discover the pipeline before driving it. It is the read-only companion to `Run_Phase`.

### Algorithm

```c
function PJRT_PhaseCompile_Get_Phase_Names(args):         // 0xe6f4a60
    if !ActualStructSizeIsGreaterOrEqual(                 // line 25
            "PJRT_PhaseCompile_Get_Phase_Names_Args", min=38, cur=48, args->struct_size):
        return new PJRT_Error{status}

    holder = args[+0x10]                                   // args[2]
    if !holder:                                            // line 34
        return new PJRT_Error{MakeErrorImpl<13>(
            "PJRT_PhaseCompile_Get_Phase_Names: phase compiler is null",  // :100
            …, "…/pjrt_c_api_phase_compile_internal.cc")}

    names = (*holder->base->vtable[+48])(holder->base)     // PjRtPhaseCompiler::GetPhaseNames
                                                           //   0x1d16ae20, line 40
    args[+0x18] = ConvertCppStringsToCharBuffers(names, &args[+0x20])  // ptrs / sizes
    args[+0x28] = names.count                              // line 71
    free(names)                                            // line 60-114: release the vector
    return NULL
```

### Considerations

- **Dispatch is `vtable+48`.** `GetPhaseNames @ 0x1D16AE20` returns the registered phase-name vector (the order `RegisterAllPhases` appended them). The driver then `ConvertCppStringsToCharBuffers` into `args[+0x18]` / `args[+0x20]` (ptr array / sizes) with the count at `args[+0x28]`.
- **The args struct name has two spellings.** The `struct_size` assert string is `"PJRT_PhaseCompile_Get_Phase_Names_Args"` (`0xe6f4a60`, line 25), but the IDA-recovered mangled symbol for the function takes a `PJRT_PhaseCompile_Get_PhaseNames_Args*` (no underscore between `Phase` and `Names`). Both spellings appear in the binary; the on-the-wire struct uses the underscored form the assert names.
- **The returned vector is freed in-place.** After converting to char buffers, the body walks the `vector<string>` and frees each long-string allocation (the libc++ SSO sign-bit check `(char)x < 0`) then the backing array — the standard `0xAAAAAAAAAAAAAAAB` reciprocal-of-24 stride math. The host owns only the converted char buffers, which it later releases via `C_Buffers_Destroy`.

---

## `C_Buffers_Destroy` — releasing returned arrays

### Purpose

`C_Buffers_Destroy` (`pjrt::PJRT_PhaseCompile_C_Buffers_Destroy @ 0x0E6F4CC0`) frees the char-buffer arrays that `Run_Phase` (`out_programs`) and `Get_Phase_Names` (phase names) returned to the host. It is the host's mandatory cleanup hook for those allocations.

### Algorithm

```c
function PJRT_PhaseCompile_C_Buffers_Destroy(args):       // 0xe6f4cc0
    ptrs  = args[+0x10]                                   // args[2] — char** array
    count = args[+0x20]                                   // args[4] — element count
    if ptrs:
        for i in 0 .. count-1: free(ptrs[i])              // each buffer
        free(ptrs)                                        // the array
    sizes = args[+0x18]                                   // args[3] — size_t* array
    if sizes: free(sizes)
    // no status returned — pure cleanup
```

It returns no status; like `Destroy_Compiler` it is a void cleanup routine. A reimplementation must pair every `Run_Phase` / `Get_Phase_Names` that produces buffers with a `C_Buffers_Destroy`, or leak the returned arrays.

---

## The Six Registered Phases

### Purpose

`xla::TpuCompiler::RegisterAllPhases @ 0x0F849EC0` is where the TPU backend declares its phased pipeline. It calls `PjRtPhaseCompiler::RegisterPhase @ 0x1D16AB20` six times, each with a name, a compile function, and a shared `CommonPhaseValidator`. `RegisterPhase` inserts `{name → CompilationPhaseFunctions{compile_fn, validator_fn}}` into a hash map and appends the name to the ordered vector that `GetPhaseNames` returns.

### The phase table

The six registrations are verified in `RegisterAllPhases` (`0xf849ec0`, six `RegisterPhase` calls); the name strings are present verbatim in `.rodata`.

| Order | Phase name | Compile function | Addr | Output format |
|---|---|---|---|---|
| 1 | `phase0_stablehlo_to_hlo` | `CompilePhase0StablehloToHlo` | `0x0F84DE60` | `unopt_hlo` |
| 2 | `phase1_hlo_opts` | `CompilePhase1HloOptimizations` | `0x0F84EE00` | optimized HLO |
| 3 | `phase2a_tlp_lowering` | `CompilePhase2aTlpLowering` | `0x0F850840` | TLP-lowered |
| 4 | `phase2b_deduped_lowering` | `CompilePhase2bDedupedLowering` | `0x0F852180` | deduped-lowered |
| 5 | `phase3_linking` | `CompilePhase3Linking(test=false)` | `0x0F852F40` | device program |
| 6 | `phase3_linking_test_only` | `CompilePhase3Linking(test=true)` | `0x0F852F40` | device program |

### Algorithm — registration

```c
function TpuCompiler::RegisterAllPhases(this):            // 0xf849ec0
    RegisterPhase(this, "phase0_stablehlo_to_hlo",        // line 74-80
                  CompilePhase0StablehloToHlo,  CommonPhaseValidator)
    RegisterPhase(this, "phase1_hlo_opts",                // line 101-108
                  CompilePhase1HloOptimizations, CommonPhaseValidator)
    RegisterPhase(this, "phase2a_tlp_lowering",           // line 137-143
                  CompilePhase2aTlpLowering,    CommonPhaseValidator)
    RegisterPhase(this, "phase2b_deduped_lowering",       // line 175-181
                  CompilePhase2bDedupedLowering, CommonPhaseValidator)
    RegisterPhase(this, "phase3_linking",                 // line 204-212
                  bind_front(CompilePhase3Linking, false), CommonPhaseValidator)
    RegisterPhase(this, "phase3_linking_test_only",       // line 237-244
                  bind_front(CompilePhase3Linking, true),  CommonPhaseValidator)
    // each returns Status; a failure aborts and propagates (Get_Compiler tears down)
```

Each phase function has the signature

```c
StatusOr<vector<PjRtPartialProgramProto>>
    CompilePhaseN(CompileOptions,
                  Span<const PjRtPartialProgramProto> /* input */,
                  const PjRtTopologyDescription&);
```

and the shared validator is `Status CommonPhaseValidator(Span<const PjRtPartialProgramProto>)`, run on the *input* programs before each phase. `RegisterPhase` stores both in a `CompilationPhaseFunctions` record; `RunPhases` invokes the validator at `vtable+72` and the compile function at `vtable+40` on that record.

> **QUIRK —** `phase3_linking` and `phase3_linking_test_only` are *two registrations of one function* (`CompilePhase3Linking @ 0xf852f40`) differing only by a bound `bool test` argument (`false` production, `true` test). They are not a five-stage pipeline plus a sixth stage; they are a five-stage pipeline (`phase0` → `phase1` → `phase2a` → `phase2b` → `phase3`) with one test alias of the terminal linker. A host enumerating phases via `Get_Phase_Names` sees six names; a normal compile drives five.

### The driver — `RunPhases`

```c
function PjRtPhaseCompiler::RunPhases(opts, programs, topology, phase_names):  // 0x1d16af20
    current = programs                                    // vector<PjRtPartialProgramProto>
    for name in phase_names:                              // host-supplied order
        rec = phase_map.find(name)
        if !rec:
            return Error("No phase compiler/validator registered with phase name \"%s\"", name)  // :281
        rec->validator(current)                           // CompilationPhaseFunctions vtable +72
        current = rec->compile_fn(opts, current, topology) // vtable +40 → new vector
    return current                                        // final artifacts
```

The output of each phase becomes the input of the next (the vector is reassigned), so a host can run `["phase0_stablehlo_to_hlo"]` alone, persist the `unopt_hlo` blob, and later run `["phase1_hlo_opts", "phase2a_tlp_lowering", …]` resuming from that blob. `RegisterPhase` rejects a duplicate name with `"A phase compiler/validator with Phase name \"%s\" already exists"` (`pjrt_compiler.cc:259`) and an empty name or null function with the guards at lines 250/253/256.

### Partial-program artifact format

The blob crossing every phase boundary is a `PjRtPartialProgramProto` (proto3, package `xla`, `pjrt_partial_program.proto`):

```text
message PjRtPartialProgramProto {
  bytes  program          = 1;   // serialized stage output (e.g. unopt HLO)
  string program_format   = 2;   // e.g. "unopt_hlo"
  string producer_phase   = 3;   // e.g. "phase0_stablehlo_to_hlo"
  repeated string consumer_phases = 4;  // e.g. ["phase1_hlo_opts"]
  string version          = 5;
  string program_name     = 6;
}
```

`CompilePhase0StablehloToHlo` (`0xf84de60`) is the worked example: it `ParseMlirModuleString`s the input StableHLO module, runs `MlirToXlaComputation` (with `getDefaultChloToHighLevelMhloOptions`) to an `HloModuleProto`, stuffs the arg/output layout-mode and memory-space attributes into the HLO's `frontend_attributes` (keys `kArgLayoutModesAttr` / `kOutLayoutModesAttr` / `kArgMemorySpacesAttr` / `kOutMemorySpacesAttr`), and emits a `PjRtPartialProgramProto{ program=serialized HLO, program_format="unopt_hlo", producer_phase="phase0_stablehlo_to_hlo", consumer_phases=["phase1_hlo_opts"] }`. The HLO-pass content of the downstream phases is on [HLO Pass Registry](../compiler/hlo-pass-registry.md).

> **NOTE —** the inner bodies of `phase1_hlo_opts`, `phase2a_tlp_lowering`, `phase2b_deduped_lowering`, and `phase3_linking` are characterized here only at the in/out-contract level (each maps `vector<PjRtPartialProgramProto>` → `vector<PjRtPartialProgramProto>`); phase 3 is confirmed to build a `BufferAssignmentProto` and emit the loadable device program. Their internal pass pipelines are large and live on the compiler pages, not here.

---

## Compile-Options Ingest

### Purpose

Every compile entry in libtpu — `PJRT_Client_Compile`, `PJRT_Compile`, `PJRT_Client_Load`, `PJRT_Executable_DeserializeAndLoad`, and PhaseCompile `Run_Phase` — deserializes the *same* `CompileOptionsProto` through the *same two-call sequence* (`CompileOptionsProto::ParseFromString` → `CompileOptions::FromProto`) and emits the *byte-identical* failure string. The first four share one emission site (`pjrt_c_api_wrapper_impl.cc:1113`); `Run_Phase` re-implements the sequence inline with its own site (`pjrt_c_api_phase_compile_internal.cc:46`) — same logic, same diagnostic, different source line. This is the convergence point where wire options become live C++ `xla::CompileOptions`. The TPU compiler config (the 1,121-field `xla.jellyfish.TpuCompilationEnvironment`) and the `xla::DebugOptions` / XLA flags all ride inside this one proto.

### The chokepoint

```c
// PJRT_Client_Compile / PJRT_Compile / PJRT_Client_Load / DeserializeAndLoad → wrapper_impl.cc:1113
// PhaseCompile Run_Phase (line 134) → its own inline copy at phase_compile_internal.cc:46
proto = CompileOptionsProto{}                          // arena = 0
if !proto2::MessageLite::ParseFromString(&proto, ptr, len):
    return new PJRT_Error{MakeErrorImpl<3>(
        "PJRT_Client_Compile: failed to deserialize CompileOptionsProto",  // verbatim, all 5
        …, /* wrapper_impl.cc:1113  | Run_Phase: phase_compile_internal.cc:46 */)}
opts = xla::CompileOptions::FromProto(proto)           // wire → live C++ options
```

### The wire schema

`CompileOptionsProto` (proto3, package `xla`, `…/pjrt/proto/compile_options.proto`) is the carrier. The TPU compilation environment is nested three levels deep inside it:

```text
CompileOptionsProto {
  repeated ShapeProto argument_layouts        = 1;
  bool   parameter_is_tupled_arguments        = 2;
  ExecutableBuildOptionsProto executable_build_options = 3;  // ← env carrier
  bool   compile_portable_executable          = 4;
  int64  profile_version                      = 5;
  bytes  serialized_multi_slice_config        = 6;
  repeated EnvOptionOverridesEntry env_option_overrides = 7; // ← per-flag overrides
  GpuTargetConfigProto target_config          = 8;
  bool   allow_in_place_mlir_modification     = 9;
  PrecisionConfig.Precision matrix_unit_operand_precision = 10;
  string compiler_variant                     = 11;
}
ExecutableBuildOptionsProto {
  int64  device_ordinal                       = 1;
  DebugOptions debug_options                  = 3;   // ← XLA-standard flags
  CompilationEnvironmentsProto comp_envs      = 13;  // ← THE env nest
  int64  num_replicas = 4; num_partitions = 5;
  bool   use_spmd_partitioning = 6; use_auto_spmd_partitioning = 7;
  bytes  fdo_profile                          = 14;  // feedback-directed-opt
  ...
}
CompilationEnvironmentsProto { repeated google.protobuf.Any environments = 1; }
```

### The three injection channels

A reimplementer needs to know there are *three* concentric ways TPU compiler config reaches the compiler, narrowest to widest:

| Channel | Where | Scope | Confidence |
|---|---|---|---|
| Per-flag override | `CompileOptionsProto.env_option_overrides` (field 7) | one flag, this compile; `map<name, {string\|bool\|int\|double}>` | HIGH |
| Full env, per-compile | `executable_build_options.comp_envs.environments[Any → TpuCompilationEnvironment]` (3→13→1) | the whole 1,121-field table, this compile | HIGH |
| Process-global | TpuExecutable ext `SetTpuCompilationEnv` (type 17, slot `+0x40`, `0xe6dd400`) | a `CompilationEnvironments` singleton later compiles inherit | HIGH |

The first two ride inside `CompileOptionsProto` and so pass through `Run_Phase`'s chokepoint identically to every other compile entry. The third is a separate out-of-band surface on the TpuExecutable extension (type 17), not the PhaseCompile extension; it is documented with that extension and noted here only so a reimplementer knows the PhaseCompile path is *not* the only env channel.

### Decode of the nested environment

When `FromProto` runs, `CompilationEnvironments::CreateFromProto @ 0x1E63E5A0` walks each `Any` in `comp_envs.environments`, resolves its `type_url` (e.g. `xla.jellyfish.TpuCompilationEnvironment`) against the generated descriptor pool via `FindMessageTypeByName`, `MessageFactory::New`s a prototype, `InternalUnpackTo`s the `Any.value` bytes, and `AddEnvImpl`s the live message into a `FlatHashMap<Descriptor*, unique_ptr<Message>>`. Malformed `Any`s produce the diagnostics at `compilation_environments.cc:139..160` (`"Invalid/Unknown/Unsupported CompilationEnvironment message type"`, `"Unable to unpack…"`). The `TpuCompilationEnvironment` is thus reflection-decoded at compile time — queried by protobuf field name, never by fixed offset. The full field table is on the compiler pages.

> **QUIRK —** libtpu's compile is **fully in-process** — the `CompileOptionsProto` is consumed and reflection-decoded in the same address space; there is no compiler subprocess. `xla::CommonPjRtClient::supports_two_phase_launch @ 0xe6edbc0` hard-returns `1`, but "two-phase" means *compile/load are separable API calls*, not that compilation forks out of process. (This is the opposite of some other PJRT backends, whose `Compile` forks an external compiler binary and passes options as argv/temp files. libtpu's TPU compiler is statically linked.)

---

## Relationship to the Monolithic Path

The PhaseCompile extension and the one-shot `PJRT_Client_Compile` (slot 25) are two ABIs over one compiler:

| Aspect | Monolithic (`PJRT_Client_Compile`, slot 25) | Phased (PhaseCompile ext, type 9) |
|---|---|---|
| Caller hands | whole program + `CompileOptionsProto` | per-phase `PjRtPartialProgramProto` blobs + phase-name list + options |
| Returns | finished, device-loaded `PJRT_LoadedExecutable` | intermediate `PjRtPartialProgramProto` artifacts between phases |
| Backing object | `xla::TpuCompiler` (via client vtable) | `xla::TpuCompiler` (via ext `Get_Compiler` holder) |
| Compiler dispatch | client `Compile` virtual | `RunPhases` (`vtable+56`) over registered phases |
| Intermediate artifacts | discarded | exposed (cacheable, inspectable, host-splittable) |
| Options chokepoint | `CompileOptionsProto::ParseFromString` → `FromProto` | identical |

The phased ABI buys three things the monolithic one cannot: (a) caching a *partially*-compiled program (persist the `unopt_hlo` blob after `phase0`), (b) splitting phases across hosts, and (c) recompiling only downstream phases when only late-stage options change. The finished executable either path produces — its serialize/deserialize round-trip and `Execute` — is on [Executable Loading & Execution](executable-execution.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::TpuCompiler` | The `0x40`-byte compiler object `Get_Compiler` mints and `RegisterAllPhases` configures |
| `xla::PjRtPhaseCompiler` | Base class providing `RegisterPhase` / `RunPhases` / `GetPhaseNames` (vtable `+40/+48/+56/+72`) |
| `CompilationPhaseFunctions` | The `{compile_fn, validator_fn}` record stored per phase name |
| `pjrt::ActualStructSizeIsGreaterOrEqual` | The per-entry `struct_size` version guard shared with every PJRT wrapper |
| `xla::CompilationEnvironments` | Decodes the nested `TpuCompilationEnvironment` `Any` at `FromProto` time |
| TpuExecutable extension (type 17) | The *other* env channel (`SetTpuCompilationEnv`), out-of-band relative to compile |

## Cross-References

- [Extension Chain](extension-chain.md) — how a host walks `PJRT_Extension_Base.next` and matches `type == 9` to reach this struct
- [API Vtable Reconstruction](api-vtable-reconstruction.md) — the `PJRT_Api` slot table and the `struct_size` backward-compat guard every entry shares
- [Executable Loading & Execution](executable-execution.md) — the finished executable compilation produces: `PJRT_Client_Compile`, serialize / deserialize round-trip, and `Execute`
- [Client and Device](client-and-device.md) — `PJRT_Client_Create`, the injected slot that builds the `TpuClient` and installs the descriptor pool the env decode uses
- [PJRT Overview](overview.md) — where the compile surface sits in the plugin lifecycle
- [HLO Pass Registry](../compiler/hlo-pass-registry.md) — the HLO pass pipeline the `phase1`/`phase2`/`phase3` bodies drive
- [Compilation Cache](../compiler/compilation-cache.md) — the content-addressed store that caches partial (`PjRtPartialProgramProto`) and full results
