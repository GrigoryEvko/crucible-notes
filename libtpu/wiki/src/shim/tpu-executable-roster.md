# TpuExecutable Roster

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped). `.text` VMA equals file offset. The C-ABI names (`TpuExecutable_*`) are IDA-recovered from call targets and the `tpu_executor_c_api.cc` / `tpu_execute_c_api.cc` source-path strings baked into the `CHECK`/`MakeErrorImpl` call sites; demangled C++ targets are quoted verbatim. Other versions will differ.*

## Abstract

`TpuExecutable_*` is the C-ABI roster for the **running form of a compiled TPU program** — the handle a StreamExecutor host holds after a compilation has been deserialized onto a device, and through which it enqueues launches, queries the program's HLO, fingerprints it for caching, and re-serializes it. Where [`TpuProgram_*`](tpu-program-roster.md) owns the *serializable, queryable* compiled artifact and [`TpuCompiler_*`](tpu-compiler-roster.md) produces it, `TpuExecutable_*` is the thin `extern "C"` skin over the live `xla::Executable` subtree: the host never sees an `xla::TpuExecutable` by value, it sees an opaque handle and a fixed set of free functions that un-marshal C structs and bounce into the C++ executable.

The roster is exactly **9 free functions**, the smallest of the named device-runtime clusters. They split cleanly into four areas: **execution** (`LoadProgramAndEnqueueToStream`, `ExecuteAsyncOnStream` — the two large marshalling functions that drive the device), **serialization** (`Serialize`, `Deserialize` — proto round-trip through `xla::DeepseaExecutableProto`), **metadata** (`Fingerprint`, `HloModule` — read-only accessors), and **lifecycle / array frees** (`Free`, `FreeXlaShapeIndexArray`, `FreeMaybeOwningDeviceAddressArray`). Every entry is a forwarder: the C-ABI function holds an opaque `XLA_TpuExecutable*`-style handle whose first slot points at a C++ object, and dispatches into a method of `xla::TpuExecutable` / `xla::legacy::TpuExecutableInterface`, whose concrete leaf is `xla::jellyfish::DeepseaExecutable`.

The C++ side is a three-layer class tower. `xla::Executable` is the upstream base; `xla::legacy::TpuExecutableInterface` (`ExecuteAsyncOnStream` @ `0x1342cd20`) overrides the argument/output marshaling once; the concrete `xla::jellyfish::DeepseaExecutable` implements the device enqueue (`LoadProgramAndEnqueueToStream` @ `0x13426260`, `ToProto` @ `0x134282e0`, `FromProto` @ `0x134283e0`, `fingerprint` @ `0x13428a80`). The wrapper class `xla::TpuExecutable` (methods clustered at `0xf8a7560`–`0xf8adce0`) is the legacy StreamExecutor face that the C-ABI roster mirrors slot-for-slot. This page owns the per-function roster and the impl-symbol map; it does not re-derive the runtime execution algorithm.

For reimplementation, the contract is:

- **The 9-function roster** — name, impl symbol + address, the C++ method it bounces to, and which of the four areas it belongs to.
- **The handle convention** — every entry takes an opaque handle whose `*handle` (or `**handle`) is a `C++ xla::Executable`-subtree pointer; the C-ABI function only un-marshals C structs and dispatches.
- **The serialization wire form** — `xla::DeepseaExecutableProto` is the on-the-wire shape; `Serialize` = `ToProto` + proto copy, `Deserialize` = `ParseFromArray` + `FromProto`, both returning a `StatusOr`-style C-ABI status object.
- **The ownership rules** — `Free` runs the C++ virtual destructor through slot `+8` then `free()`s the handle box; the two `FreeArray` helpers are bare `free()`; `HloModule` is the only metadata accessor that allocates (an `XLA_HloModule` via `ApiConverter::ToC`).

| | |
|---|---|
| **Roster size** | 9 `extern "C"` `TpuExecutable_*` functions (verified against the function table) |
| **Source files** | `tpu_executor_c_api.cc` (most), `tpu_execute_c_api.cc` (`LoadProgramAndEnqueueToStream`) |
| **Wrapper class** | `xla::TpuExecutable` (methods `0xf8a7560`–`0xf8adce0`) — legacy SE face |
| **Marshaling base** | `xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream` @ `0x1342cd20` |
| **Concrete leaf** | `xla::jellyfish::DeepseaExecutable` (`LoadProgram` @ `0x13426260`, `ToProto` @ `0x134282e0`, `FromProto` @ `0x134283e0`, `fingerprint` @ `0x13428a80`) |
| **Serialization proto** | `xla::DeepseaExecutableProto` (`operator new(0x28)` = 40 bytes) |
| **Handle marshalling** | `ApiConverter::ToC(const xla::HloModule*)` for `HloModule` |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the opaque-handle + `*ApiFn()` accessor model that *frames* this roster (how the host reaches `TpuExecutable_Fingerprint` through a function-pointer slot rather than by symbol) is on [The TfTpu C-API Shim](overview.md). The compiled-program handle this executable wraps is on [TpuProgram Roster](tpu-program-roster.md). The *runtime execution algorithm* behind the two big entries is owned by [Execute Async on Stream](../runtime/execute-async-on-stream.md) and [Load Program & Enqueue](../runtime/load-program-enqueue.md) — this page is the *C-ABI roster over* those entries, not a second copy of them. The PJRT executable lifecycle that coexists with (and does **not** route through) this legacy path is on [PJRT Executable Execution](../pjrt/executable-execution.md).

---

## 1. The Roster at a Glance

The nine functions, grouped by area, with the impl symbol (always the C-ABI free function itself), its address and size, and the C++ method it dispatches into. Confidence reflects how directly the bounce target was observed in the decompiled body.

| `TpuExecutable_*` | Address | Size | Bounces to (C++) | Area | Confidence |
|---|---|---|---|---|---|
| `LoadProgramAndEnqueueToStream` | `0xeaafba0` | 4496 | `xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` @ `0x13426260` (via vtable+96) | Execution | CERTAIN |
| `ExecuteAsyncOnStream` | `0xeabd500` | 4708 | `xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream` @ `0x1342cd20` (via vtable+24) | Execution | CERTAIN |
| `Serialize` | `0xeabea80` | 178 | `xla::jellyfish::DeepseaExecutable::ToProto` @ `0x134282e0` | Serialization | CERTAIN |
| `Deserialize` | `0xeabede0` | 288 | `xla::jellyfish::DeepseaExecutable::FromProto` @ `0x134283e0` | Serialization | CERTAIN |
| `Fingerprint` | `0xeabea40` | 54 | `DeepseaExecutable::fingerprint` @ `0x13428a80` (cached field at `obj+96` → `+648/+656`) | Metadata | CERTAIN |
| `HloModule` | `0xeabef00` | 86 | `ApiConverter::ToC(const xla::HloModule*)` over `executable.hlo_module_` | Metadata | CERTAIN |
| `Free` | `0xeabef60` | 51 | C++ virtual destructor via vtable `+8`, then `free()` | Lifecycle | CERTAIN |
| `FreeXlaShapeIndexArray` | `0xeabea00` | 10 | bare `free(ptr)` | Lifecycle | CERTAIN |
| `FreeMaybeOwningDeviceAddressArray` | `0xeabea20` | 10 | bare `free(ptr)` | Lifecycle | CERTAIN |

> **NOTE —** the `PJRT_TpuExecutable_*` family (`PJRT_TpuExecutable_RunHloCostAnalysis_Args`, `..._GetCompiledMemoryStats_Args`, `..._GetHloModuleWithConfig_Args`, `..._SetTpuCompilationEnv_Args`, and friends) is a **different surface** and is *not* on this roster. Those are PJRT-extension argument structs handled by `pjrt::(anonymous namespace)::RunHloCostAnalysis` / `GetCompiledMemoryStats` / etc., and belong to the PJRT pages. The C-ABI roster owned here is exactly the nine functions whose dynamic-symbol name *begins* with `TpuExecutable_` (no `PJRT_` prefix). Filtering on the prefix is the only reliable separator — the two families share verbs (`HloModule`, cost analysis) but live on opposite sides of the ABI.

> **QUIRK —** the wrapper class is named `xla::TpuExecutable` (so `TpuExecutable_HloModule` ↔ a member that reads `hlo_module_`), but the *execution* entries bounce one or two layers deeper than `xla::TpuExecutable`'s own methods: `ExecuteAsyncOnStream` lands on the `xla::legacy::TpuExecutableInterface` base (the class that implements marshaling), and `LoadProgramAndEnqueueToStream` lands on the `xla::jellyfish::DeepseaExecutable` leaf. A reimplementer who maps every `TpuExecutable_*` C function 1:1 onto an `xla::TpuExecutable` method will mis-place the two big ones — they target the interface/leaf, not the wrapper.

---

## 2. Execution Entries

### Purpose

The two execution functions are the only large bodies in the roster (4496 B and 4708 B); the other seven total under 700 B combined. Both are C-ABI marshalling layers: they receive a flat launch/run struct from the SE shim, un-marshal it into the typed C++ objects the device runtime expects (`xla::ExecutableRunOptions`, `xla::ExecutionInput` vectors, `DeviceAddressBase` spans, a deserialized `DeviceAssignment`), dispatch through a vtable slot into the actual enqueue logic, and re-marshal the result (`ExecutionOutput` / `absl::Status`) back into C structs for the caller. Neither contains the device-enqueue algorithm — they are the C boundary over it.

### Entry Point

```text
TpuExecutable_LoadProgramAndEnqueueToStream   0xeaafba0   (tpu_execute_c_api.cc)
  └─ xla::TpuExecutable::LoadProgramAndEnqueueToStream  (legacy SE marshal, same body)
       └─ vtable+96 ──► xla::jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream
                          0x13426260  (deepsea_executable.cc) ── real load+enqueue

TpuExecutable_ExecuteAsyncOnStream            0xeabd500   (tpu_executor_c_api.cc)
  └─ vtable+24 ──► xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream
                     0x1342cd20  ── arg/output marshal + dispatch
                       └─ (leaf) DeepseaExecutable::LoadProgramAndEnqueueToStream  0x13426260
```

### Algorithm

The C-ABI shell is uniform; the device work is on the runtime pages. The shell shape for `ExecuteAsyncOnStream`:

```c
// TpuExecutable_ExecuteAsyncOnStream                          0xeabd500
function TpuExecutable_ExecuteAsyncOnStream(handle /*a1*/, run_opts /*a2*/,
                                            inputs /*a3*/, n_inputs /*a4*/,
                                            out /*a5*/, status /*a6*/):
    exe   = *handle                          // opaque box → xla::Executable subtree
    // un-marshal C structs into ExecutableRunOptions + vector<ExecutionInput>
    cpp_opts   = FromC(run_opts)
    cpp_inputs = FromC(inputs, n_inputs)
    // dispatch through the executable vtable slot +24 (ExecuteAsyncOnStream)
    result = (*exe->vtable[+24])(exe, &cpp_opts, move(cpp_inputs))   // 0x1342cd20
    // re-marshal ExecutionOutput / Status back to C
    if result.ok: ToC(result.value, out)
    else:         set_status(status, result.status)
```

`LoadProgramAndEnqueueToStream` follows the same pattern but dispatches through vtable slot `+96` into the `DeepseaExecutable` leaf, and its un-marshal step is heavier — it rebuilds an `HloModule` (for the shape signature only), an `ExecutableRunOptions`, a deserialized `DeviceAssignment`, and two `DeviceAddressBase` vectors. The full line-by-line reconstruction of both bodies — including the `absl::Status` handling and the device-side `DeepseaExecutor::LoadProgram → DeepseaStream::EnqueueRequest` chain — lives on the runtime pages.

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `TpuExecutable_LoadProgramAndEnqueueToStream` @ `0xeaafba0` | 4496 | C-ABI marshal → `DeepseaExecutable::LoadProgramAndEnqueueToStream` (vtable+96) | CERTAIN |
| `TpuExecutable_ExecuteAsyncOnStream` @ `0xeabd500` | 4708 | C-ABI marshal → `TpuExecutableInterface::ExecuteAsyncOnStream` (vtable+24) | CERTAIN |

### Considerations

The two entries are *two public doors into one device runtime*. `ExecuteAsyncOnStream` is reached by the `xla::LocalClient` / `xla::Service` StreamExecutor stack (`LocalExecutable::RunAsync → Executable::ExecuteAsyncOnStreamWrapper → vtable+24`); the C-ABI shim is a second door that un-marshals C structs and calls the same `+24` slot. `LoadProgramAndEnqueueToStream` is the lower-level enqueue primitive that the interface's `ExecuteAsyncOnStream` itself eventually drives via the leaf. The modern PJRT path (`xla::TpuClient` over `tpu::System`) does **not** route through either of these — it reaches the same device runtime through `tpu::System::Execute` (`0x1d0b33e0`). A reimplementer wiring the *legacy* SE backend reproduces these two C functions; one wiring PJRT ignores them.

---

## 3. Serialization Entries

### Purpose

`Serialize` and `Deserialize` round-trip the executable through `xla::DeepseaExecutableProto`, the on-the-wire form. `Serialize` produces a heap-owned proto the caller later frees; `Deserialize` parses a byte buffer into a fresh executable, returning a C-ABI status object. Both are short (178 B / 288 B) because the real work is in the `DeepseaExecutable::ToProto` / `FromProto` C++ methods; the C functions are proto-allocation + status plumbing.

### Algorithm

```c
// TpuExecutable_Serialize                                     0xeabea80
function TpuExecutable_Serialize(self /*_XMM0/a1*/, out_proto /*a3*/):
    *out_proto = NULL
    proto = operator new(0x28)                  // 40-byte DeepseaExecutableProto
    DeepseaExecutableProto::DeepseaExecutableProto(proto, 0)
    *out_proto = proto
    tmp = DeepseaExecutable::ToProto(self)       // 0x134282e0 — fill a stack proto
    // move-or-copy tmp into the heap proto, fast-pathing the arena-equal case
    if tmp.arena == proto.arena: InternalSwap(proto, tmp)   // same arena → swap
    else:                        CopyFrom(proto, tmp)       // cross-arena → deep copy
    ~DeepseaExecutableProto(tmp)
```

```c
// TpuExecutable_Deserialize                                   0xeabede0
function TpuExecutable_Deserialize(len /*a1*/, bytes /*a2*/, out /*a3*/, status /*a4*/):
    proto = DeepseaExecutableProto(0)                          // stack proto
    if !MessageLite::ParseFromArray(proto, bytes, len):
        set_status(status,
            MakeErrorImpl<INTERNAL>(                            // code 13
                "TpuExecutable_Deserialize: proto deserialization failed"))
        goto cleanup
    exe = DeepseaExecutable::FromProto(proto)                  // 0x134283e0 → StatusOr
    if exe.ok():
        box = operator new(8)                                  // 8-byte handle box
        *box = exe.value;  *out = box                          // hand off ownership
    else:
        set_status(status, exe.status)
        if exe.value: (*exe.value->vtable[+8])(exe.value)      // destroy partial leaf
cleanup:
    ~DeepseaExecutableProto(proto)
```

> **GOTCHA —** the deserialized handle is an 8-byte heap box (`operator new(8u)`) whose single slot holds the C++ `DeepseaExecutable*`. That is the same box shape `TpuExecutable_Free` expects: `*handle` is the C++ object, the box itself is the thing `free()` releases. A reimplementer that returns the bare C++ pointer instead of boxing it will make `Free` dereference a non-box and corrupt the heap. The box is one pointer of indirection on purpose — it lets the host hold an opaque `XLA_TpuExecutable*` while the plugin keeps the real object behind it.

> **NOTE —** `Deserialize` returns the standard SE-shim status object via `a4` (a refcounted `absl::status_internal::StatusRep**`), with `code 13` = `INTERNAL` for a proto-parse failure. The decompiled body is dominated by `StatusRep::Unref` / `_InterlockedIncrement` refcount juggling, not by deserialization logic — that is entirely inside `DeepseaExecutable::FromProto`.

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `TpuExecutable_Serialize` @ `0xeabea80` | 178 | `ToProto` + arena-aware swap/copy into a heap `DeepseaExecutableProto` | CERTAIN |
| `TpuExecutable_Deserialize` @ `0xeabede0` | 288 | `ParseFromArray` + `FromProto`; boxes the result, sets a `StatusRep` out-param | CERTAIN |

### Considerations

The proto type `xla::DeepseaExecutableProto` is the *executable* serialization, distinct from the `tensorflow::TPUExecutableProto` that backs the [`TpuProgram_*`](tpu-program-roster.md) handle (which is the *program* serialization fed to the compilation cache). `Serialize` here produces the executable proto for transport/caching of a *loaded* executable; `Deserialize` reconstructs one from bytes. The arena-equal fast path in `Serialize` (`InternalSwap` vs `CopyFrom`) is a proto2 idiom — when the source and destination share an arena the move is a pointer swap, otherwise it is a deep copy. Reproduce both branches: a swap across distinct arenas would alias freed memory.

---

## 4. Metadata Entries

### Purpose

Two read-only accessors. `Fingerprint` returns the executable's cached fingerprint (a `string`/`Cord` pair) for cache keying; `HloModule` marshals the executable's `xla::HloModule` out into an `XLA_HloModule` C struct for the host to inspect. `Fingerprint` allocates nothing — it returns pointers into the executable's own fields; `HloModule` is the only metadata function that allocates (through `ApiConverter::ToC`).

### Algorithm

```c
// TpuExecutable_Fingerprint                                   0xeabea40
function TpuExecutable_Fingerprint(handle /*a1*/, out_ptr /*a2*/, out_len /*a3*/):
    fp = *(*handle + 96)            // DeepseaExecutable cached fingerprint field
    if fp.is_long_form(fp+671 >= 0):      // SSO discriminator byte
        *out_len = fp+671                 // inline (short) string: len byte, data at fp+648
        *out_ptr = fp+648
    else:
        *out_len = *(fp+656)              // heap string: length at +656
        *out_ptr = *(fp+648)              //              data ptr at +648
    return *out_ptr
```

```c
// TpuExecutable_HloModule                                     0xeabef00
function TpuExecutable_HloModule(out /*a1*/, handle /*a2*/):
    mod = *(*handle + 8)                  // xla::Executable::hlo_module_
    if mod == NULL:
        LOG(FATAL) "hlo_module_ != nullptr"  // executable.h:335
    ApiConverter::ToC(out, mod)           // fill caller-provided XLA_HloModule
    return out
```

> **QUIRK —** `Fingerprint` reads the cached fingerprint directly out of the C++ object at offset `+96` and decodes a `std::string`'s short-string-optimization layout inline (the sign of the byte at `+671` discriminates inline vs. heap, length at `+671` or `+656`, data at `+648` or `*(+648)`). It does **not** call `DeepseaExecutable::fingerprint` (`0x13428a80`) at call time — that method *populates* the field; this accessor just exposes it. A reimplementer who recomputes the fingerprint on every `Fingerprint` call will be correct but slow; the binary reads a precomputed field and returns borrowed pointers (so the caller must not free them).

> **GOTCHA —** `HloModule` `LOG(FATAL)`s if `hlo_module_` (at `executable+8`) is null — it does not return an error status. The HLO module is retained only when the executable was built with module retention enabled; an executable compiled without it will *crash* here, not fail gracefully. The fatal-log source anchor is `executable.h:335`. This is upstream XLA behavior surfaced through the C ABI unchanged.

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `TpuExecutable_Fingerprint` @ `0xeabea40` | 54 | borrow `(ptr,len)` of the cached fingerprint field at `obj+96`; SSO-decoded | CERTAIN |
| `TpuExecutable_HloModule` @ `0xeabef00` | 86 | `ApiConverter::ToC` of `hlo_module_` into a caller `XLA_HloModule`; FATAL if null | CERTAIN |

### Considerations

`Fingerprint` returns *borrowed* pointers into the executable; there is no paired free, and the data is valid only while the handle lives. `HloModule` is the lone allocator: `ApiConverter::ToC(XLA_HloModule*, const HloModule*)` fills a caller-provided struct with heap-owned interior (the marshalling convention on [the shim overview](overview.md#3-the-opaque-handle-convention)), so the host must run the matching `Destroy`/free for the `XLA_HloModule`. The related richer metadata accessors (`GetCompiledMemoryStats`, `GetCostAnalysis`, `GetOutputLayouts`, …) are C++ methods on `xla::TpuExecutable` (`0xf8a7560`–`0xf8aab40`) reached through the *PJRT* extension surface, not through this C-ABI roster — see the note in [§1](#1-the-roster-at-a-glance).

---

## 5. Lifecycle and Array-Free Entries

### Purpose

Three deallocators. `Free` tears down a full executable handle (destruct the C++ object, then free the box); the two `FreeArray` helpers free arrays that other roster functions (on this and sibling pages) hand back to the caller as raw heap pointers.

### Algorithm

```c
// TpuExecutable_Free                                          0xeabef60
function TpuExecutable_Free(box /*ptr*/):
    if box != NULL:
        if *box != NULL:
            (*(*box)->vtable[+8])(*box)      // C++ virtual destructor (delete-style)
        free(box)                            // free the 8-byte handle box

// TpuExecutable_FreeXlaShapeIndexArray                        0xeabea00
function TpuExecutable_FreeXlaShapeIndexArray(p):  if p: free(p)

// TpuExecutable_FreeMaybeOwningDeviceAddressArray             0xeabea20
function TpuExecutable_FreeMaybeOwningDeviceAddressArray(p):  if p: free(p)
```

> **NOTE —** the two array-free helpers are byte-for-byte identical (`if (a1) free(a1);`, 10 bytes each at adjacent addresses `0xeabea00` / `0xeabea20`). They are kept as two distinct symbols deliberately: each pairs with a different array-producing call elsewhere in the ABI (a `XLA_ShapeIndex[]` and a `SE_MaybeOwningDeviceAddress[]`), so a reimplementer must keep both names even though the implementation is the same `free`. Collapsing them into one function would break the ABI's named-pairing contract, not the runtime behavior.

> **GOTCHA —** `TpuExecutable_Free` expects the *boxed* handle: `box` is the `operator new(8)` box from `Deserialize`, `*box` is the C++ `DeepseaExecutable*`, and the virtual destructor is invoked through `(*box)->vtable[+8]`. The order is fixed — destruct the object via the vtable slot first, *then* `free()` the box. Calling `free(box)` before the virtual destructor leaks the C++ object and all its device-side state (loaded programs, output buffers); invoking the destructor on the box instead of on `*box` calls a destructor on garbage.

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `TpuExecutable_Free` @ `0xeabef60` | 51 | virtual-destruct `*box` via vtable+8, then `free(box)` | CERTAIN |
| `TpuExecutable_FreeXlaShapeIndexArray` @ `0xeabea00` | 10 | `if (p) free(p)` — frees a `XLA_ShapeIndex[]` | CERTAIN |
| `TpuExecutable_FreeMaybeOwningDeviceAddressArray` @ `0xeabea20` | 10 | `if (p) free(p)` — frees a `SE_MaybeOwningDeviceAddress[]` | CERTAIN |

### Considerations

`Free` is the destructor for the whole roster: every handle minted by `Deserialize` (and by the compiler/program path that produces a loaded executable) is reclaimed through it. The two array frees exist because the C ABI cannot return owning C++ containers — a function that conceptually returns `vector<ShapeIndex>` returns a raw `XLA_ShapeIndex*` + count and a named free, mirroring the `*FreeArray` pattern on [`TpuProgram_*`](tpu-program-roster.md) and [`TpuTopology_*`](tpu-topology.md). Pair each producer with its named free; do not cross them (a `ShapeIndex[]` freed by the device-address helper is still just `free`, but the named contract documents intent and may diverge in other builds).

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::TpuExecutable` (`0xf8a7560`–`0xf8adce0`) | the legacy StreamExecutor wrapper class the roster mirrors; also the home of the PJRT-only metadata methods |
| `xla::legacy::TpuExecutableInterface::ExecuteAsyncOnStream` @ `0x1342cd20` | the marshaling base that `TpuExecutable_ExecuteAsyncOnStream` dispatches into (vtable+24) |
| `xla::jellyfish::DeepseaExecutable` (leaf) | concrete impl: `LoadProgram` `0x13426260`, `ToProto` `0x134282e0`, `FromProto` `0x134283e0`, `fingerprint` `0x13428a80` |
| `xla::DeepseaExecutableProto` (40 bytes) | the serialization wire form behind `Serialize` / `Deserialize` |
| `ApiConverter::ToC(const xla::HloModule*)` | marshals `hlo_module_` into the `XLA_HloModule` C struct for `TpuExecutable_HloModule` |
| `TpuProgram_*` roster | the serializable *compiled-program* handle this *running* executable wraps |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the opaque-handle + `*ApiFn()` accessor model that frames this roster (the three-hop accessor → slot → C-ABI impl pattern)
- [TpuProgram Roster](tpu-program-roster.md) — the `TpuProgram_*` compiled-program handle (`tensorflow::TPUExecutableProto` base) that this executable wraps
- [TpuCompiler Roster](tpu-compiler-roster.md) — the `TpuCompiler_*` / `TpuCompile_*` surface that produces the programs loaded into these executables
- [TpuExecutor Roster](tpu-executor-roster.md) — the `TpuExecutor_*` per-device runtime the executable enqueues onto
- [Execute Async on Stream](../runtime/execute-async-on-stream.md) — the runtime algorithm behind `TpuExecutable_ExecuteAsyncOnStream` (this page is the C-ABI roster over it)
- [Load Program & Enqueue](../runtime/load-program-enqueue.md) — the runtime algorithm behind `TpuExecutable_LoadProgramAndEnqueueToStream` (load + device enqueue)
- [PJRT Executable Execution](../pjrt/executable-execution.md) — the modern PJRT executable lifecycle that coexists with, and does not route through, this legacy SE path
