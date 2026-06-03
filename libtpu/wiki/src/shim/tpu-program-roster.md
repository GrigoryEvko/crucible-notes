# TpuProgram Roster

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped). `.text` VMA equals file offset. The C-ABI names (`TpuProgram_*`, `XLA_TpuProgram`) are IDA-recovered from `.rodata` reference strings and the `tpu_program_c_api.cc` source-path/line-number assertions baked into the binary. Other versions will differ.*

## Abstract

`TpuProgram_*` is the C-ABI roster for the **compiled-program handle** — the opaque object a TPU compilation produces and that the StreamExecutor TPU executable layer carries until the runtime loads it onto a core. Where [`TpuCompiler_*`](tpu-compiler-roster.md) turns an HLO module into one of these, and [`TpuExecutable_*`](tpu-executable-roster.md) wraps the *running* form, `TpuProgram_*` owns the *serializable, queryable* form in between: it is the thing fingerprinted for the compilation cache, serialized into a `GetTpuProgramResponse` proto for transport, and torn back apart into the metadata the executor needs (executable info, host-transfer descriptors, HLO module protos, may-modify-variables flags). The 18 free functions are exactly the `extern "C"` surface of `learning/45eac/tfrc/executor/stream_executor/tpu_program_c_api.cc`, recovered from the source path embedded in every `CHECK` and `MakeErrorImpl` call site in the binary.

The handle behind the roster is `XLA_TpuProgram`, and the binary makes its shape unambiguous: `TpuProgram_New` does `operator new(0xB8)` (184 bytes) and constructs a `tensorflow::TPUExecutableProto` *as the object's own base* — the handle **is** the executable proto, with five extra trailing pointer slots stitched on after the proto body. Those trailing slots hold the compiler-metadata HLO proto, a refcounted shared object, the loaded `tpu::TpuCoreProgram`, and two recursively-owned `XLA_TpuProgram` children that are the sharding sub-programs. Every accessor on this page is a read of one of those slots; every lifecycle function manages their ownership.

This page owns the per-function roster, the impl-symbol/address map, and the `XLA_TpuProgram` struct layout. The opaque-handle + `*ApiFn()` accessor model that *frames* this roster (how the host reaches `TpuProgram_New` through a function-pointer slot rather than by symbol) is on [The TfTpu C-API Shim](overview.md); the PJRT executable lifecycle that ultimately drives a serialized program is on [PJRT Executable Execution](../pjrt/executable-execution.md); the runtime path that loads a `TpuCoreProgram` onto a core and enqueues it is on [Load Program & Enqueue](../runtime/load-program-enqueue.md).

For reimplementation, the contract is:

- **The `XLA_TpuProgram` handle layout** — a `TPUExecutableProto` base plus five trailing slots, and which slot each accessor reads.
- **The 18-function roster** — lifecycle (`New`/`Free`/`NewArray`/`FreeArray`/`UnloadAndDestroy`), serialization (`SerializeTpuExecutable`/`SerializeCompilerMetadata`/`DeserializeFromGetTpuProgramResponseProto`/`GetExecutableInfo`), metadata accessors (`GetProgramSize`/`GetHostTransferInfo`/`GetHloMetadata`/`GetMayModifyVariables`/`GetFingerprint`/`DestroyFingerprint`/`GetTpuProgram`/`HasSharding`), and the memory-summary log.
- **The out-param/status idiom** — serialization and metadata functions take an out `{ptr,len}` pair plus a `StatusRep**` the callee fills; the `(ptr,len)` byte blob is heap-owned by `operator new` and freed by the caller.
- **The fetch-target enum and sharding split** — `GetTpuProgram` switches on a `CompilationCacheFetchTarget` value to return the main handle or one of two sharding children.

| | |
|---|---|
| **Roster size** | 18 `extern "C"` `TpuProgram_*` functions (verified against the function table) |
| **Handle type** | `XLA_TpuProgram` — `operator new(0xB8)` = 184 bytes |
| **Handle base** | `tensorflow::TPUExecutableProto` (the handle *is* the proto) |
| **Handle dtor** | `XLA_TpuProgram::~XLA_TpuProgram @ 0xe8bdb20` (171 bytes) |
| **Source file** | `learning/45eac/tfrc/executor/stream_executor/tpu_program_c_api.cc` |
| **Address span** | `0xe8bda60` (`New`) → `0xe8bef20` (`DestroyFingerprint`) |
| **Reached via** | `ExecutorApiFn()` slot (see [overview](overview.md)) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile |

> **Scope —** the `*ApiFn()` accessor pattern, the opaque-handle convention, and how the host calls these functions through a function-pointer slot are owned by [The TfTpu C-API Shim](overview.md). This page documents only the `TpuProgram_*` functions themselves and the `XLA_TpuProgram` handle they operate on.

---

## 1. The `XLA_TpuProgram` Handle

### Layout

`TpuProgram_New @ 0xe8bda60` allocates `0xB8` (184) bytes, runs `tensorflow::TPUExecutableProto::TPUExecutableProto(this, /*arena=*/0)` over the first 0x98 bytes, then zero-fills the remaining trailing slots. The handle therefore is a `TPUExecutableProto` with five extra pointer slots appended; every accessor reads one of them. The destructor `XLA_TpuProgram::~XLA_TpuProgram @ 0xe8bdb20` walks those slots in reverse and confirms each slot's type by how it is released.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `executable_proto` | `+0` | `tensorflow::TPUExecutableProto` (base) | The serializable executable proto; `New` constructs it, `SharedDtor` finalizes it | CERTAIN |
| `isa_program` | `+104` (q13) | `xla::IsaProgramProto*` | Backing ISA program; source of host-transfer info; logged in `UnloadAndDestroy` | HIGH |
| `may_modify_variables` | `+136` (q17) | `uint8` | Whether the program may mutate resource variables | CERTAIN |
| `compiler_metadata` | `+144` (q18) | HLO/`CompilerMetadata` proto ptr | HLO module proto + program-memory metadata; read by `GetHloMetadata`, `GetProgramSize`, `LogProgramMemorySummary`, `SerializeCompilerMetadata` | HIGH |
| `shared_obj` | `+152` (q19) | refcounted shared object | Released via `__shared_weak_count::__release_weak`; set during deserialize | MEDIUM |
| `tpu_core_program` | `+160` (q20) | `unique_ptr<tpu::TpuCoreProgram const>` | The loaded core program; fingerprint bytes live at `+648` inside it | HIGH |
| `sharding_child_0` | `+168` (q21) | `XLA_TpuProgram*` | Sharding sub-program; recursively destroyed; fetch-target 2 | HIGH |
| `sharding_child_1` | `+176` (q22) | `XLA_TpuProgram*` | Second sharding sub-program; recursively destroyed; fetch-target 3 | HIGH |

```c
// XLA_TpuProgram::~XLA_TpuProgram(this)                          0xe8bdb20
void ~XLA_TpuProgram(this):
    child1 = this[+176]; this[+176] = 0                  // q22 — sharding child 1
    if child1: ~XLA_TpuProgram(child1); free(child1)     // recursive
    child0 = this[+168]; this[+168] = 0                  // q21 — sharding child 0
    if child0: ~XLA_TpuProgram(child0); free(child0)     // recursive
    unique_ptr_reset(&this[+160], 0)                     // q20 — drop TpuCoreProgram
    rc = this[+152]                                      // q19 — refcounted shared object
    if rc && atomic_dec(rc.weak_count) == 0:             // weak-count release
        rc.vtable[+16](rc); __release_weak(rc)
    TPUExecutableProto::SharedDtor(this, 0)              // finalize the proto base
```

> **QUIRK —** the handle is not a wrapper *around* a proto; it *is* the proto, extended in place. A reimplementer who models `XLA_TpuProgram` as a small struct holding a `TPUExecutableProto*` will mismatch every offset: `GetProgramSize` calls `Message::SpaceUsedLong(handle)` directly on the handle pointer, treating it as the proto. The proto base and the trailing pointer slots share one 184-byte allocation.

> **GOTCHA —** the two sharding children at `+168`/`+176` are themselves full `XLA_TpuProgram` objects, freed by recursion through the *same* destructor. A reimplementation that frees them with a plain `free()` (skipping `~XLA_TpuProgram`) leaks each child's own `TpuCoreProgram`, refcount, and grandchildren. `TpuProgram_Free` is the only correct entry: `~XLA_TpuProgram(p); free(p)`.

---

## 2. Lifecycle

### Purpose

Allocate, free, and unload `XLA_TpuProgram` handles, and allocate/free the flat `XLA_TpuProgram*[]` arrays the compiler emits (one entry per program in a multi-program compilation). `New`/`NewArray` allocate; `Free`/`FreeArray`/`UnloadAndDestroy` release.

### Algorithm

```c
// TpuProgram_New()                                              0xe8bda60
XLA_TpuProgram* New():
    p = operator new(0xB8)                       // 184-byte handle
    zero(p, 0xB8)
    TPUExecutableProto::TPUExecutableProto(p, 0) // construct proto base, arena=null
    p[+144] = 0                                  // clear compiler_metadata slot
    p[+176] = 0                                  // clear trailing sharding slot
    return p

// TpuProgram_Free(p)                                            0xe8bdae0
void Free(p):
    if p: ~XLA_TpuProgram(p); free(p)            // dtor handles children + proto

// TpuProgram_NewArray(count)                                    0xe8bdbe0
XLA_TpuProgram** NewArray(count):
    CHECK(count > 0, "count > 0")                // tpu_program_c_api.cc:44 — fatal if 0
    return operator new(8 * count)               // array of handle pointers

// TpuProgram_FreeArray(arr)                                     0xe8bdc60
void FreeArray(arr): if arr: free(arr)           // frees the pointer array only

// TpuProgram_UnloadAndDestroy(p, status_out)                    0xe8bdc80
void UnloadAndDestroy(p, status_out):
    if LogEveryNSec():                           // tpu_program_c_api.cc:54
        hex = BytesToHexString(p.isa_program.id) // q13 ISA program id
        LOG("Unloading and destroying TPU program(%s)", hex)
    ... unload the core program from the device, fill status_out ...
```

> **QUIRK —** `FreeArray` frees only the *array of pointers*, never the handles it points at. `NewArray` (`operator new(8*count)`) is symmetric with `FreeArray` (`free`), but each `XLA_TpuProgram*` in the array must be released separately via `TpuProgram_Free` / `UnloadAndDestroy`. A reimplementation that frees the array and assumes the elements went with it leaks every program. `NewArray` also *fatally* aborts on `count == 0` (`CHECK(count > 0)` at line 44), so a caller must never request an empty array.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `TpuProgram_New` | `0xe8bda60` | 107 | Allocate + construct one 184-byte handle | CERTAIN |
| `TpuProgram_Free` | `0xe8bdae0` | 39 | `~XLA_TpuProgram` + `free` | CERTAIN |
| `TpuProgram_NewArray` | `0xe8bdbe0` | 103 | Allocate `XLA_TpuProgram*[count]` (CHECK count>0) | CERTAIN |
| `TpuProgram_FreeArray` | `0xe8bdc60` | 10 | `free` the pointer array (not its elements) | CERTAIN |
| `TpuProgram_UnloadAndDestroy` | `0xe8bdc80` | 382 | Unload core program from device + status-out | HIGH |

---

## 3. Serialization

### Purpose

Convert between an in-memory `XLA_TpuProgram` and the wire forms used for transport and for the compilation cache. Two directions: *serialize* the executable proto or the compiler-metadata proto into a heap blob, and *deserialize* a full `GetTpuProgramResponse` proto back into a populated handle. `GetExecutableInfo` is a third serializer that emits a stripped executable proto (ISA program and profile cleared) for metadata-only consumers.

### The out-param / status idiom

Every serializer takes a `_QWORD out[2]` it fills as `{void* bytes, size_t len}` (the byte blob is `operator new`-allocated, owned by the caller) plus a `StatusRep** status_out` it sets on failure. On the happy path the status is left untouched and the blob is returned; on failure the blob slot is zeroed and an `absl::Status` is installed. The `(bytes, len)` blob is freed by the caller, not by a paired `TpuProgram_*` function — it is a raw `operator new` buffer.

```c
// TpuProgram_SerializeTpuExecutable(handle, out, status_out)    0xe8be720
void SerializeTpuExecutable(handle, out, status_out):
    blob = GetTpuProgramResponseExternal_Blob()           // wrapper proto
    s = ArenaStringPtr::Mutable(&blob.data)
    if !MessageLite::SerializeToString(handle, s):         // serialize proto base
        status_out := MakeError("Failed to serialize proto, "
                                "invalid executable buffer.")   // line 201
        return
    len  = blob.ByteSizeLong()
    buf  = operator new(len)
    CHECK(blob.SerializePartialToArray(buf, len))          // proto_helper.h:45
    out[0] = buf; out[1] = len                             // {bytes, len}

// TpuProgram_SerializeCompilerMetadata(handle, out, status_out) 0xe8be840
void SerializeCompilerMetadata(handle, out, status_out):
    meta = handle[+144]                                    // compiler_metadata slot
    ... same blob/serialize/CHECK shape; error line 218 ...

// TpuProgram_GetExecutableInfo(handle, out, status_out)         0xe8bdf40
void GetExecutableInfo(handle, out, status_out):
    proto = copy of *handle                                // TPUExecutableProto copy
    proto.clear_isa_program(); proto.clear_profile()       // strip heavy fields
    if proto.ByteSizeLong() == 0:
        out := {0,0}
        status_out := MakeError("TPU executable proto to be "
                                "serialized is empty.")     // line 99
        return
    len = proto.ByteSizeLong(); buf = operator new(len)
    CHECK(proto.SerializePartialToArray(buf, len))
    out[0] = buf; out[1] = len
```

```c
// TpuProgram_DeserializeFromGetTpuProgramResponseProto(
//     exec_bytes, exec_len, handle, status_out)                 0xe8be960
void DeserializeFromGetTpuProgramResponseProto(exec_bytes, exec_len, handle, status_out):
    resp = DeserializeProto<GetTpuProgramResponseExternal>(...) // outer wrapper
    if !MessageLite::ParseFromString(handle, resp.blob):        // executable proto
        status_out := MakeError("Failed to deserialize proto, "
                                "invalid executable buffer.")   // line 241
        return
    meta = CompilerMetadata()
    if !meta.ParseFromString(resp.compiler_metadata_blob):
        status_out := MakeError("Failed to deserialize proto, "
                                "invalid compiler metadata buffer.")  // line 253
        return
    // install compiler metadata at handle[+144], refcount at handle[+152]
    handle[+144] = new CompilerMetadata(meta); handle[+136] = ...
    platform  = GetRegisteredDeepseaPlatform()
    topology  = platform.GetTopology()
    isa       = handle.isa_program                          // q13
    core_prog = TpuCoreProgramFromIsaProgramProto(topology, isa, /*flag=*/1)
    if core_prog.ok():
        handle[+160] = move(core_prog)                      // loaded TpuCoreProgram
    else:
        status_out := core_prog.status()
```

> **GOTCHA —** the blob returned by the serializers is a bare `operator new` buffer, *not* a `TpuProgram_*`-managed object. There is no `TpuProgram_FreeSerialized`; the caller frees `out[0]` itself. Confusing this blob with the fingerprint (which has its own `DestroyFingerprint`) or with the array (`FreeArray`) frees the wrong allocator. Match each producer to its release: serialized blob → caller `free`; fingerprint → `DestroyFingerprint`; pointer array → `FreeArray`; handle → `Free`.

> **NOTE —** `GetExecutableInfo` deliberately `clear_isa_program()`s and `clear_profile()`s a *copy* of the proto before serializing, so the result is the executable metadata without the heavy ISA/profile payload. Serializing an empty proto is treated as an error (line 99), not an empty success — a reimplementer must reproduce that the all-cleared-and-still-empty case is a failure.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `TpuProgram_SerializeTpuExecutable` | `0xe8be720` | 268 | Serialize the full executable proto → `{bytes,len}` blob (err line 201) | CERTAIN |
| `TpuProgram_SerializeCompilerMetadata` | `0xe8be840` | 275 | Serialize the compiler-metadata proto (slot +144) → blob (err line 218) | CERTAIN |
| `TpuProgram_GetExecutableInfo` | `0xe8bdf40` | 277 | Serialize a stripped executable proto (no ISA/profile) → blob (err line 99) | CERTAIN |
| `TpuProgram_DeserializeFromGetTpuProgramResponseProto` | `0xe8be960` | 792 | Parse a `GetTpuProgramResponse` proto → populate handle + load core program | HIGH |

---

## 4. Metadata Accessors

### Purpose

Read structured information out of a populated handle without re-parsing the proto. These back the executor-side `TpuProgramGroup::Construct*` / accessor methods that mint the C++ views XLA consumes: HLO module protos, host-transfer descriptors, the may-modify-variables list, the fingerprint, and the raw `TpuCoreProgram` pointer keyed by fetch target.

### Algorithm

```c
// TpuProgram_GetMayModifyVariables(handle, out_bool)            0xe8be520
void GetMayModifyVariables(handle, out_bool):
    CHECK(out_bool != null, "may_modify_variables != nullptr")  // line 163
    *out_bool = (uint8) handle[+136]                            // q17

// TpuProgram_HasSharding(handle) -> bool                        0xe8be580
bool HasSharding(handle):
    CHECK(handle != null, "tpu_program != nullptr")             // line 168
    return handle[+168] != 0 && handle[+176] != 0               // both children present

// TpuProgram_GetTpuProgram(handle, fetch_target) -> void*       0xe8be600
void* GetTpuProgram(handle, fetch_target):
    switch (fetch_target):                                      // CompilationCacheFetchTarget
        case 1: return handle                                   // MAIN — the handle itself
        case 2: return handle[+168]                             // sharding child 0
        case 3: return handle[+176]                             // sharding child 1
        default: LOG_FATAL("Invalid fetch target: " + enum_name) // line 185

// TpuProgram_GetProgramSize(handle) -> int64                    0xe8bde00
int64 GetProgramSize(handle):
    n = Message::SpaceUsedLong(handle)                          // proto base size
    m = handle[+144]                                            // compiler_metadata
    if m: n += Message::SpaceUsedLong(m)
    return n

// TpuProgram_GetFingerprint(handle) -> char* (heap copy)        0xe8bed60
char* GetFingerprint(handle):
    core = handle[+160]                                         // q20 TpuCoreProgram
    if !handle || !core:
        VLOG("The underlying `TpuProgram` was not initialized. "
             "Returning empty fingerprint.")                    // line 275
        return null
    bytes = core[+648]                                          // fingerprint string
    return strdup-style heap copy of bytes                      // operator new + memcpy

// TpuProgram_DestroyFingerprint(p)                              0xe8bef20
void DestroyFingerprint(p): if p: free(p)

// TpuProgram_GetHostTransferInfo(handle, out, status_out)       0xe8be060
void GetHostTransferInfo(handle, out, status_out):
    isa = handle.isa_program                                    // q13
    if isa.host_transfer_count > 0:
        info = TPUHostTransferInfoProto()
        for each transfer in isa: info.add(...)                 // SerializeAsCord per entry
        serialize info → out blob
    // else out left empty

// TpuProgram_GetHloMetadata(handle, out, status_out)           0xe8be260
void GetHloMetadata(handle, out, status_out):
    out := {0,0}
    meta = handle[+144]                                         // compiler_metadata
    if meta && meta.hlo_module:
        hlo = HloProto(); copy hlo_module + input_output_alias
        serialize hlo → out blob

// TpuProgram_LogProgramMemorySummary(handle) -> bool           0xe8bde40
bool LogProgramMemorySummary(handle):
    meta = handle[+144]
    if !meta: return false
    if meta.flags[+17] & 0x10:                                  // has memory metadata
        summary = ProgramMemorySummary(meta.program_memory_metadata)  // +152 inside meta
        LOG("\n" + summary)                                     // line 80
    return true
```

> **QUIRK —** `GetTpuProgram`'s argument is a `CompilationCacheFetchTarget` enum, not an index. `1` (`MAIN`) returns the handle unchanged; `2`/`3` return the two sharding children; anything `> 3` or unknown triggers a `LOG_FATAL` with the decoded enum name (line 185). A reimplementer must wire this to the same enum the cache uses — passing a raw 0 or an out-of-range value aborts the process rather than returning null. `HasSharding` is the safe predicate to call first: it returns true only when *both* sharding slots are populated.

> **GOTCHA —** `GetFingerprint` returns a *fresh heap copy* (`operator new` + `memcpy` of the fingerprint string at `core_program+648`) that the caller must release with `TpuProgram_DestroyFingerprint`. It returns `null` — not an empty string — when the `TpuCoreProgram` slot (`+160`) is unset, after emitting a `VLOG` (line 275). A caller that assumes a non-null fingerprint, or that frees it with the wrong deleter, breaks. The fingerprint and the serialized blobs are different allocations with different lifetimes.

### Function Map

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `TpuProgram_GetProgramSize` | `0xe8bde00` | 50 | `SpaceUsedLong(proto) + SpaceUsedLong(compiler_metadata)` | CERTAIN |
| `TpuProgram_GetMayModifyVariables` | `0xe8be520` | 83 | Read `+136` bool into out-param (CHECK out!=null, line 163) | CERTAIN |
| `TpuProgram_HasSharding` | `0xe8be580` | 98 | True iff both sharding children (`+168`,`+176`) set (CHECK handle, line 168) | CERTAIN |
| `TpuProgram_GetTpuProgram` | `0xe8be600` | 282 | Fetch-target switch → handle / child0 / child1 (FATAL otherwise, line 185) | CERTAIN |
| `TpuProgram_GetFingerprint` | `0xe8bed60` | 447 | Heap copy of fingerprint at `core+648`; null if uninitialized (VLOG line 275) | HIGH |
| `TpuProgram_DestroyFingerprint` | `0xe8bef20` | 10 | `free` the fingerprint buffer | CERTAIN |
| `TpuProgram_GetHostTransferInfo` | `0xe8be060` | 508 | Serialize `TPUHostTransferInfoProto` from ISA program → blob | HIGH |
| `TpuProgram_GetHloMetadata` | `0xe8be260` | 694 | Serialize `HloProto` (module + IO-alias) from compiler metadata → blob | HIGH |
| `TpuProgram_LogProgramMemorySummary` | `0xe8bde40` | 249 | `LOG` the program-memory summary if present (line 80); false if no metadata | HIGH |

---

## 5. Roster At A Glance

The full 18-function `TpuProgram_*` surface, grouped by area, as recovered from the function table. Span `0xe8bda60`–`0xe8bef20`.

| Function | Address | Area | Confidence |
|---|---|---|---|
| `TpuProgram_New` | `0xe8bda60` | Lifecycle | CERTAIN |
| `TpuProgram_Free` | `0xe8bdae0` | Lifecycle | CERTAIN |
| `TpuProgram_NewArray` | `0xe8bdbe0` | Lifecycle | CERTAIN |
| `TpuProgram_FreeArray` | `0xe8bdc60` | Lifecycle | CERTAIN |
| `TpuProgram_UnloadAndDestroy` | `0xe8bdc80` | Lifecycle | HIGH |
| `TpuProgram_GetProgramSize` | `0xe8bde00` | Metadata | CERTAIN |
| `TpuProgram_LogProgramMemorySummary` | `0xe8bde40` | Metadata | HIGH |
| `TpuProgram_GetExecutableInfo` | `0xe8bdf40` | Serialization | CERTAIN |
| `TpuProgram_GetHostTransferInfo` | `0xe8be060` | Metadata | HIGH |
| `TpuProgram_GetHloMetadata` | `0xe8be260` | Metadata | HIGH |
| `TpuProgram_GetMayModifyVariables` | `0xe8be520` | Metadata | CERTAIN |
| `TpuProgram_HasSharding` | `0xe8be580` | Metadata | CERTAIN |
| `TpuProgram_GetTpuProgram` | `0xe8be600` | Metadata | CERTAIN |
| `TpuProgram_SerializeTpuExecutable` | `0xe8be720` | Serialization | CERTAIN |
| `TpuProgram_SerializeCompilerMetadata` | `0xe8be840` | Serialization | CERTAIN |
| `TpuProgram_DeserializeFromGetTpuProgramResponseProto` | `0xe8be960` | Serialization | HIGH |
| `TpuProgram_GetFingerprint` | `0xe8bed60` | Metadata | HIGH |
| `TpuProgram_DestroyFingerprint` | `0xe8bef20` | Metadata | CERTAIN |

> **NOTE —** there is no `TpuProgram_SerializedSize` or `TpuProgram_HasSparseCoreProgram` in this build — the size query is `GetProgramSize` (proto `SpaceUsedLong`, not a serialized byte count), and SparseCore presence is not a `TpuProgram_*` predicate here. The closest sharding/sub-program predicate is `HasSharding`, and the host-transfer surface is `GetHostTransferInfo`. The roster is exactly the 18 functions above.

---

## Related Components

| Name | Relationship |
|---|---|
| `XLA_TpuProgram` (`~XLA_TpuProgram @ 0xe8bdb20`) | the opaque handle every `TpuProgram_*` function operates on |
| `tensorflow::TPUExecutableProto` | the proto that *is* the handle base; `New` constructs it, serializers emit it |
| `tensorflow::tpu::GetTpuProgramResponseExternal` / `_Blob` | the wrapper proto used by the serialize/deserialize pair |
| `tensorflow::tpu::TpuProgramGroup` (`0xe978a20` et al.) | host-side consumer; `ConstructExecutableInfo` / `ConstructHostTransferInfo` / `ConstructHloMetadata` call the accessors here |
| `tpu::TpuCoreProgram` | the loaded program at handle `+160`; holds the fingerprint at `+648`; loaded by the runtime |
| `xla::IsaProgramProto` | handle `+104`; source of host-transfer info and the program id logged on unload |

## Cross-References

- [The TfTpu C-API Shim](overview.md) — the `*ApiFn()` accessor pattern, opaque-handle convention, and how `TpuProgram_*` is reached through a function-pointer slot
- [TpuCompiler Roster](tpu-compiler-roster.md) — the `TpuCompiler_*` / `TpuCompile_*` surface that *produces* an `XLA_TpuProgram`
- [TpuExecutable Roster](tpu-executable-roster.md) — the `TpuExecutable_*` running-executable handle that wraps a loaded program
- [TpuExecutor Roster](tpu-executor-roster.md) — the `TpuExecutor_*` per-device runtime that the loaded program runs on
- [TpuTransferManager Roster](tpu-transfer-manager.md) — the host↔device transfer C ABI that consumes the host-transfer info this page exposes
- [PJRT Executable Execution](../pjrt/executable-execution.md) — the PJRT-level executable lifecycle that ultimately drives a serialized program
- [Load Program & Enqueue](../runtime/load-program-enqueue.md) — the runtime path that loads a `TpuCoreProgram` onto a core and enqueues it
