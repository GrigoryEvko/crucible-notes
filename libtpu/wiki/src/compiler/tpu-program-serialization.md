# TpuProgram Serialization

> *All addresses, field tags, and struct offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

A compiled TPU executable does not leave the process as one flat protobuf. `xla::TpuExecutable::SerializeExecutable` (`0xf8a9300`) assembles a `xla::TpuExecutableProto` container — an HLO module proto, a per-core LLO bundle, compiler metadata, host-transfer descriptors, the original compile options, and a target/topology block — and then a separate framing helper, `xla::TpuExecutableProtoToString` (`0xf8a8880`), writes that container to a byte string as a sequence of *length-delimited segments* rather than a single message. The split exists to evade protobuf's 2 GiB single-message ceiling: the LLO bundle and the HLO proto are each emitted as their own delimited frame, cleared from the parent, and the parent is serialized last carrying only what remains. This is the wire format PJRT's `Executable::Serialize` round-trips, and the format the AOT compiler path (`DeepseaCompiler::DeserializeExecutable`, `0xfaa2660`) and the C runtime shims (`TpuExecutable_Serialize`/`_Deserialize`) read back.

Two container schemas stack. The PJRT-facing `xla::TpuExecutableProto` is the outer envelope. Inside it, field 1 is a `xla::DeepseaExecutableProto` — the compiler-internal AOT container produced by `DeepseaExecutable::ToProto` (`0x134282e0`) — which itself nests the per-core `tpu::TpuCoreProgramProto` (the LLO/ISA program bundle) and the `xdb::CompilerMetadata`. The runtime C-API also exposes a third, narrower path: `TpuProgram_SerializeTpuExecutable` (`0xe8be720`) packs a single `TpuCoreProgram` into a `tensorflow::tpu::GetTpuProgramResponseExternal_Blob` — the form used to ship one core's program across the cache RPC boundary, distinct from the full PJRT serialize.

This page documents the outer envelope, the nested AOT container, the per-core program bundle, the delimited-segment framing, and the version/compatibility stamp. The cache *key* — how a fingerprint is computed and looked up before serialization is ever invoked — lives on [`compilation-cache.md`](compilation-cache.md); this page owns only the wire format. The reimplementation contract:

- **Outer envelope** `xla::TpuExecutableProto`: six populated fields, their tag numbers, presence (`has_bits`) semantics, and the source struct offsets each is copied from.
- **Nested containers**: `DeepseaExecutableProto` (LLO bundle + metadata) and `TpuCoreProgramProto` (the per-core ISA program, a `TensorCore`/`BarnaCore`/`SparseCore` oneof).
- **Framing**: the delimited multi-segment string layout written by `TpuExecutableProtoToString`, the field-clearing dance that produces a "reduced proto", and the 2 GiB-evasion rationale.
- **Version/compat stamp**: the `deepsea_version` / `deepsea_variant` / `deepsea_chip_config_name` flag triple and the `TpuConfiguredProperties`/`TpuTopologyArgs` target block that pins a serialized program to a chip generation.

| | |
|---|---|
| **Outer serializer** | `xla::TpuExecutable::SerializeExecutable` — `0xf8a9300` |
| **Framing helper** | `xla::TpuExecutableProtoToString` — `0xf8a8880` |
| **Outer proto serializer** | `xla::TpuExecutableProto::_InternalSerialize` — `0xf8d0c00` |
| **AOT container** | `xla::DeepseaExecutableProto` (field 1 of envelope); `ToProto` `0x134282e0`, `FromProto` `0x134283e0` |
| **Per-core bundle** | `tpu::TpuCoreProgramProto` — serializer `0x1e82ea40` |
| **AOT deserialize** | `xla::jellyfish::DeepseaCompiler::DeserializeExecutable` — `0xfaa2660` |
| **C-API serialize** | `TpuProgram_SerializeTpuExecutable` `0xe8be720`; `TpuExecutable_Serialize` `0xeabea80` |
| **C-API deserialize** | `TpuExecutable_Deserialize` `0xeabede0`; `TpuProgram_DeserializeFromGetTpuProgramResponseProto` `0xe8be960` |
| **Source TU** | `learning/45eac/research/pjrt/tpu_pjrt_executable.cc` |
| **Version flags** | `FLAGS_deepsea_version`, `FLAGS_deepsea_variant`, `FLAGS_deepsea_chip_config_name` (`tpu_version_flag.cc`) |

---

## The Outer Envelope — `xla::TpuExecutableProto`

### Purpose

`TpuExecutableProto` is the PJRT-level serialized executable: everything the runtime needs to reload and enqueue a program without re-running the compiler. It is built field-by-field from the live `xla::TpuExecutable` object inside `SerializeExecutable`, then handed to the framing helper. It is also the message `TpuExecutable_Deserialize` (`0xeabede0`) parses on the way back in — though note the deserialize C-shim parses a `DeepseaExecutableProto`, the inner container, not the full envelope (see [Framing](#framing--length-delimited-segments)).

### Field Map

Field numbers and presence bits are read directly from `TpuExecutableProto::_InternalSerialize` (`0xf8d0c00`); the source struct offsets are read from `SerializeExecutable` (`0xf8a9300`). The `has_bits` byte lives at object offset `+16`.

| Field | Tag | Type | `has_bit` | Populated from (`a2 = TpuExecutable`) | Confidence |
|---|---|---|---|---|---|
| `deepsea_executable` | 1 | `DeepseaExecutableProto` (message) | `0x08` | nested: `TpuCoreProgram` @ `+2600`/`+1374` + `CompilerMetadata` @ `+2584` | HIGH |
| `hlo_module_with_config` | 2 | `HloModuleProtoWithConfig` (message) | `0x10` | `HloModule::ToProtoWithConfig`, `HloModule*` @ `+2560` | HIGH |
| `host_transfers` | 3 | repeated `HostTransferProto` | `0x01` | array @ `+2616`, count @ `+2624`, stride 120 B | HIGH |
| `compile_options` | 4 | `CompileOptionsProto` (message) | `0x20` | `CompileOptions::ToProto`, `CompileOptions` @ `+8` | HIGH |
| `target_arguments` | 5 | `TargetArgumentsProto` (message) | `0x40` | built inline (topology + configured properties), see below | HIGH |
| `host_executions` | 8 | repeated `HostExecutionProto` | `0x02` | array @ `+2640`, count @ `+2648`, stride 80 B | HIGH |
| `source_uri` | 9 | `string` (UTF-8 verified) | `0x04` | `TpuExecutable::GetSourceUri`, copied to proto `+56` | HIGH |

> **QUIRK —** the field numbers are not contiguous. The serializer jumps 1, 2, 3, 5, 4, 8, 9 in emission order (the writer groups by the `has_bits` it tests, not by tag), and there is no field 6 or 7 in the populated set. A reimplementation that assumes a dense `1..N` schema will mis-tag `host_executions` (8) and `source_uri` (9). The tag for `source_uri` is observed as the raw byte `74` (`0x4A` = field 9, wire type 2) in the inline string writer at `0xf8d0c00` line 167.

### `target_arguments` (field 5) sub-assembly

`target_arguments` is not copied from a single source field; it is constructed in place inside `SerializeExecutable` as a `TargetArgumentsProto` and filled from three sources:

```c
// SerializeExecutable @ 0xf8a9300, lines 422-568
target = proto.mutable_target_arguments();           // has_bit 0x40

// (a) topology, distilled then ToProto'd
TpuTopologySerdes::Distill(&serdes, exe->topology);  // topology @ +2536
serdes.ToProto(target->mutable_topology_args());     // TpuTopologyArgsProto, sub has_bit 0x04

// (b) configured properties
exe->configured_properties.ToProto(                  // @ +? via TpuConfiguredProperties
    target->mutable_configured_properties());        // sub has_bit 0x01

// (c) a scalar copied verbatim
target->field_48 = *(int*)(exe + 2576);              // sub has_bit 0x08
```

If a `MultiSliceConfig` is present (`exe + 1832` non-null), the path additionally deserializes an `AotMegaScaleMultiSliceConfig` and writes a `MultiSliceTopologyAndLocationProto` into `target_arguments` (sub has_bit `0x02`). When that branch is skipped, `CompileOptions::ToProto` is taken directly (lines 569-644). Failures along the topology/multislice path are turned into `absl::Status` via `AddSourceLocationImpl` at `tpu_pjrt_executable.cc` lines 421/439/445/448/453 — those line numbers anchor each sub-step.

---

## The AOT Container — `xla::DeepseaExecutableProto`

### Purpose

`DeepseaExecutableProto` is the compiler-internal container that wraps the *machine program* — the per-core LLO/ISA bundle and the compiler's own bookkeeping. It is field 1 of the envelope, and it is also the standalone unit the AOT compiler and the C-API serialize/deserialize round-trip. `DeepseaExecutable::ToProto` (`0x134282e0`) and `FromProto` (`0x134283e0`) are its converters.

### Field Map

From `DeepseaExecutable::ToProto` (`0x134282e0`):

| Field | Tag | Type | `has_bit` | Source | Confidence |
|---|---|---|---|---|---|
| `core_program` | 1 | `tpu::TpuCoreProgramProto` (message) | `0x01` | `TpuCoreProgram::ToProto`, `TpuCoreProgram` @ `DeepseaExecutable+96` | HIGH |
| `compiler_metadata` | 2 | `xdb::CompilerMetadata` (message) | `0x02` | `Message::CopyFrom`, source @ `DeepseaExecutable+104` | HIGH |

> **NOTE —** `xdb::CompilerMetadata` is `platforms_deepsea::jellyfish::xdb::CompilerMetadata`. It is the same metadata object carried alongside the program through the whole `TpuJitResult`/`TpuCompilationCacheEntry::Program` lifetime (constructors at `0xf7bbd60`, `0xf7bc500`, `0xf8b7720`), holding the data a reloaded program needs that is not in the ISA itself — buffer assignment, alias config, and host-compute descriptors are tracked as siblings of it in those constructors.

When `SerializeExecutable` builds the envelope's field 1, it does not call `DeepseaExecutable::ToProto`; it inlines the same two sub-fields directly into the envelope's `mutable_deepsea_executable()` (lines 144-377): `TpuCoreProgram::ToProto` into sub-field 1, and a `CopyFrom` of the metadata at `exe+2584` into sub-field 2. The standalone `ToProto` and the inlined path produce the same two-field message.

---

## The Per-Core Bundle — `tpu::TpuCoreProgramProto`

### Purpose

`TpuCoreProgramProto` is the serialized form of one TPU core's compiled program — the output of [TPU-to-LLO lowering](tpu-to-llo-ods.md). It is a tagged union over the three core dialects plus a small block of shared metadata. Its serializer is `tpu::TpuCoreProgramProto::_InternalSerialize` (`0x1e82ea40`).

### Field Map

Tags and presence read from the serializer at `0x1e82ea40`. The `has_bits` are at object offset `+16`.

| Field | Tag (byte) | Wire type | `has_bit` | Meaning | Confidence |
|---|---|---|---|---|---|
| (field 2) | `16` = `0x10` | varint | `0x10` | scalar selector / count (4-byte field @ `+64` low word) | MEDIUM |
| (field 3) | `26` = `0x1A` | LEN | `0x02` | byte/string blob (the raw program image) | HIGH |
| (field 4) | `32` = `0x20` | varint | `0x08` | varint (`this+28`) | MEDIUM |
| `TensorCore`/`BarnaCore`/`SparseCore` | 5 / 6 / 7 | LEN (message) | oneof @ `+88` | the core-program oneof; case = `field_88` ∈ {5,6,7} | HIGH |
| (field 9) | int64 | varint | `0x40` | int64 (`this+72`) — see `WriteInt64ToArrayWithField<9>` | HIGH |
| (field 10) | `80` = `0x50` | varint (bool) | `0x20` | bool (`this+68`, emitted only when value == 1) | HIGH |
| (field 9, msg) | `byte_9[3]` | LEN (message) | `0x04` | trailing sub-message (`this+48`) | MEDIUM |

> **QUIRK —** the oneof is dispatched by `v20 = *((_DWORD*)this + 22)` and the test `(unsigned)(v20 - 5) <= 2` at `0x1e82ea40` line 135 — i.e. the active arm is whichever of tags 5/6/7 the discriminant holds, and exactly one of `TpuCoreProgramProto_TensorCore` (`0x1e82dae0`), `_BarnaCore` (`0x1e82dde0`), `_SparseCore` (`0x1e82e240`) is written. A reimplementation must treat 5/6/7 as mutually exclusive, not three independent optional messages.

The repeated message at sub-loop (tag `byte_8`, `this+24`) and the raw blob at field 3 together carry the actual instruction stream; the surrounding varints carry the core-type discriminant and sizes. The blob is *not* re-parsed during PJRT deserialize — it is the opaque LLO/ISA image handed to `TpuChip::NewProgramLoaded` (`0xe72b320`) at load time.

---

## Framing — Length-Delimited Segments

### Purpose

`TpuExecutableProtoToString` (`0xf8a8880`) is where the wire bytes are actually produced. It does **not** call `proto.SerializeToString()` on the whole `TpuExecutableProto`. Instead it writes a `StringOutputStream` as an ordered run of *delimited* (length-prefixed) sub-messages, peeling the largest payloads out of the parent and serializing the depleted parent last.

### Algorithm

```c
function TpuExecutableProtoToString(out, proto):          // 0xf8a8880
    stream = StringOutputStream(&result)

    // Segment 1: the per-core LLO bundle
    SerializeDelimitedToZeroCopyStream(                    // length-prefixed
        proto.deepsea_executable.core_program, stream)
    proto.deepsea_executable.clear_core_program()         // peel it out

    // Segment 2: compiler metadata
    SerializeDelimitedToZeroCopyStream(
        proto.deepsea_executable.compiler_metadata, stream)
    proto.deepsea_executable.clear_compiler_metadata()

    // Invariant: the inner container must now be empty
    if proto.deepsea_executable.ByteSizeLong() != 0:
        return Error("Not all fields in DeepseaExectuable "
                     "have been serialized")               // line 272

    // Segment 3: the HLO module
    SerializeDelimitedToZeroCopyStream(
        proto.hlo_module_with_config, stream)
    proto.clear_hlo_module_with_config()

    // Segment 4: the reduced parent (host transfers/executions,
    //            compile_options, target_arguments, source_uri)
    SerializeDelimitedToZeroCopyStream(proto, stream)      // "Reduced Proto"

    out = OK(move(result))
```

Each `SerializeDelimitedToZeroCopyStream` writes a varint byte-length prefix followed by the message bytes, so the output is self-describing: a reader pulls four delimited frames in order — core program, compiler metadata, HLO module, reduced envelope. Each failure site names the offending segment's byte size in its error string ("Core program byte size is:", "Compiler Metadata byte size is:", "HLO Module byte size is", "Reduced Proto byte size is:"), which is the diagnostic fingerprint of this framing in the binary.

> **GOTCHA —** because the parent is *mutated* (`clear_core_program`, `clear_compiler_metadata`, `clear_hlo_module_with_config`) before the final frame is written, the "reduced proto" on the wire is missing fields 1's children and field 2 entirely. A naive deserializer that does a single `ParseFromString` on the concatenation will read only the first delimited frame and silently drop the rest. The reader must consume all four frames and re-stitch the inner `DeepseaExecutableProto` from segments 1 and 2.

> **NOTE —** the split is a 2 GiB-evasion measure. Protobuf caps a single serialized message at `INT_MAX` bytes; for a large fused model the LLO bundle or HLO proto alone can approach that. Emitting each as its own delimited frame keeps every individual `SerializeToArray` call under the cap while still producing one contiguous byte string. The error strings ("is it too large?") confirm the size ceiling is the motivating concern.

### Deserialize paths

There are three readers, at three layers, and they are *not* symmetric with the four-segment writer:

```text
PjRtClient::DeserializeExecutable (0xe6ecfa0) ── base class: returns
    "Deserializing serialized executable not supported." (unimplemented)

DeepseaCompiler::DeserializeExecutable (0xfaa2660) ── AOT path:
    DeepseaExecutableProto p;
    p.ParseFromString(bytes)                  // inner container only
    DeepseaExecutable::FromProto(p)           // 0x134283e0
    on failure: "Failed to parse serialized executable."
                (deepsea_compiler.cc:165)

TpuExecutable_Deserialize (0xeabede0) ── C runtime shim:
    DeepseaExecutableProto p;
    p.ParseFromArray(bytes, len)              // inner container only
    DeepseaExecutable::FromProto(p)
    on failure: "TpuExecutable_Deserialize: proto deserialization failed"
```

> **CORRECTION (TPSER-1) —** the AOT/C-API deserializers parse a `DeepseaExecutableProto`, **not** the full delimited `TpuExecutableProto` stream that `TpuExecutableProtoToString` produces. These are two different serialized forms sharing the inner container: the AOT path (`DeepseaCompiler`) round-trips just the compiler container as a plain protobuf, while the PJRT path (`TpuExecutableProtoToString`) wraps it plus the HLO module, compile options, and target block in the four-segment delimited envelope. A reimplementer must pick the matching reader for the writer that produced the bytes; they are not interchangeable.

---

## The C-API Program Blob — `GetTpuProgramResponseExternal_Blob`

### Purpose

The runtime C-API ships a *single core's* program over the cache/RPC boundary in a narrower wrapper than either container above. `TpuProgram_SerializeTpuExecutable` (`0xe8be720`) takes a `TpuCoreProgram`, serializes it, and wraps the result in a `tensorflow::tpu::GetTpuProgramResponseExternal_Blob`.

### Algorithm

```c
function TpuProgram_SerializeTpuExecutable(core_program, out_bytes, out_status): // 0xe8be720
    blob = GetTpuProgramResponseExternal_Blob()
    s = blob.mutable_blob()                                 // ArenaStringPtr
    if !core_program.SerializeToString(s):                  // MessageLite
        out_status = Error("Failed to serialize proto, "
                           "invalid executable buffer.")    // tpu_program_c_api.cc:201
        return
    size = blob.ByteSizeLong()
    buf  = operator new(size)
    CHECK(blob.SerializePartialToArray(buf, size))          // proto_helper.h:45
    out_bytes = {buf, size}                                 // caller owns buf
```

The companion reader `TpuProgram_DeserializeFromGetTpuProgramResponseProto` (`0xe8be960`) and the generic `stream_executor::tpu::DeserializeProto<T, TpuSerializedProto>` template (e.g. instantiated for `barna_core::HbmBuffersConfig` at `0xf767580`) close the loop on this layer.

> **NOTE —** despite the name, `TpuProgram_SerializeTpuExecutable` serializes a `TpuCoreProgram`, not a `TpuExecutable`. It is the per-core wire unit; the full-executable serialize is `TpuExecutable::SerializeExecutable` (`0xf8a9300`). The companion `TpuProgram_SerializeCompilerMetadata` (`0xe8be840`) does the same wrapping for the `CompilerMetadata` half.

---

## Version and Compatibility Stamp

### What pins a serialized program to a target

A serialized executable is only loadable on hardware that matches the chip generation it was compiled for. Two mechanisms carry that pin:

1. **The target block (`target_arguments`, field 5).** As assembled in `SerializeExecutable` (lines 432-568), this embeds a distilled `TpuTopologyArgsProto` (from `TpuTopologySerdes::Distill`) and a `TpuConfiguredPropertiesProto` (`TpuConfiguredProperties::ToProto`). Together they record the topology and the resolved hardware properties the program was built against. On reload, a mismatch here is what makes a program from one TPU generation unusable on another.

2. **The compile-time version flags.** The static initializer `_GLOBAL__sub_I_tpu_version_flag.cc` (`0x21367f50`) registers three Abseil command-line flags from `learning/45eac/tpu/runtime/tpu_version_flag.cc`:

| Flag symbol | Role | Confidence |
|---|---|---|
| `FLAGS_deepsea_version` | TPU generation / version selector | HIGH |
| `FLAGS_deepsea_variant` | hardware variant within a generation | HIGH |
| `FLAGS_deepsea_chip_config_name` | named chip configuration | HIGH |

These flags select the `Target` the compiler builds against, and the resolved values land in `TpuConfiguredProperties` → `target_arguments`. They are the upstream source of the stamp, not a separate field in the wire format.

> **NOTE —** no standalone magic number, format-version integer, or build-id field was found inside `TpuExecutableProto` itself. Compatibility is enforced structurally — by the embedded topology/properties block and by protobuf's own version handshake (the linker emits the standard "Version verification failed" / "make sure that your headers are from the same version of Protocol Buffers" guard at `sdk.so` `0xca2758`). A reimplementation should treat the `target_arguments` block as the compatibility stamp; there is no separate single-integer version field to check. (Absence confirmed across the serialize/deserialize functions surveyed; **MEDIUM** confidence that no such field exists anywhere in the schema, since not every nested proto was exhaustively traced.)

> **GOTCHA —** because the stamp is the topology/properties block and not a cheap integer, a fast "is this blob loadable here?" check cannot be done by reading a header word. The reader must parse the reduced-envelope frame far enough to reach `target_arguments`. The cheap pre-serialization compatibility gate is the cache key, not the serialized blob — see [`compilation-cache.md`](compilation-cache.md).

---

## Object Layout Reference

The source offsets into the live `xla::TpuExecutable` (argument `a2` in `SerializeExecutable`) that the serializer reads. Offsets are from the object base; confirmed against `0xf8a9300`.

| Offset | Field | Used for | Confidence |
|---|---|---|---|
| `+8` | `CompileOptions` | envelope field 4 (`compile_options`) | HIGH |
| `+1374` | flag byte | gates whether `TpuCoreProgram`/`CompilerMetadata` are emitted as defaults | MEDIUM |
| `+1504` | flag byte | source-URI / core-program branch selector | MEDIUM |
| `+1832` | `MultiSliceConfig*` | multi-slice topology sub-assembly in field 5 | HIGH |
| `+2536` | `TpuTopology*` (shared_ptr target) | `target_arguments.topology_args` | HIGH |
| `+2560` | `HloModule*` | envelope field 2 (`hlo_module_with_config`) | HIGH |
| `+2576` | int32 | `target_arguments` scalar (sub field 8) | MEDIUM |
| `+2584` | `CompilerMetadata` | inner `DeepseaExecutableProto.compiler_metadata` | HIGH |
| `+2600` | `TpuCoreProgram*` | inner `DeepseaExecutableProto.core_program` | HIGH |
| `+2616` / `+2624` | `HostTransferProto[]` / count | envelope field 3, stride 120 B | HIGH |
| `+2640` / `+2648` | `HostExecutionProto[]` / count | envelope field 8, stride 80 B | HIGH |

---

## Serialization Infrastructure Functions

| Function | Address | Role | Confidence |
|---|---|---|---|
| `xla::TpuExecutable::SerializeExecutable` | `0xf8a9300` | builds the envelope from the live executable | HIGH |
| `xla::TpuExecutableProtoToString` | `0xf8a8880` | four-segment delimited framing | HIGH |
| `xla::TpuExecutableProto::_InternalSerialize` | `0xf8d0c00` | envelope proto wire writer | HIGH |
| `xla::DeepseaExecutable::ToProto` | `0x134282e0` | AOT container writer | HIGH |
| `xla::DeepseaExecutable::FromProto` | `0x134283e0` | AOT container reader | HIGH |
| `tpu::TpuCoreProgramProto::_InternalSerialize` | `0x1e82ea40` | per-core bundle writer | HIGH |
| `tpu::TpuCoreProgramProto_TensorCore::_InternalSerialize` | `0x1e82dae0` | oneof arm 5 | HIGH |
| `tpu::TpuCoreProgramProto_BarnaCore::_InternalSerialize` | `0x1e82dde0` | oneof arm 6 | HIGH |
| `tpu::TpuCoreProgramProto_SparseCore::_InternalSerialize` | `0x1e82e240` | oneof arm 7 | HIGH |
| `xla::jellyfish::DeepseaCompiler::DeserializeExecutable` | `0xfaa2660` | AOT deserialize entry | HIGH |
| `xla::PjRtClient::DeserializeExecutable` | `0xe6ecfa0` | base class (unimplemented) | HIGH |
| `TpuExecutable_Serialize` | `0xeabea80` | C-API: DeepseaExecutable → proto | HIGH |
| `TpuExecutable_Deserialize` | `0xeabede0` | C-API: bytes → DeepseaExecutable | HIGH |
| `TpuProgram_SerializeTpuExecutable` | `0xe8be720` | C-API: core program → response blob | HIGH |
| `TpuProgram_SerializeCompilerMetadata` | `0xe8be840` | C-API: metadata → blob | MEDIUM |
| `TpuProgram_DeserializeFromGetTpuProgramResponseProto` | `0xe8be960` | C-API: response proto → program | HIGH |
| `tensorflow::tpu::internal::TpuCompileOpKernelImpl::BuildExecutable` | `0xe8c0be0` | compile → `TpuProgramGroup` producer | HIGH |

---

## Cross-References

- [Compilation Cache](compilation-cache.md) — owns the cache key, fingerprint, and lookup path; the cheap pre-serialization compatibility gate lives there, not in the blob.
- [Compiler Overview](overview.md) — where serialization sits in the end-to-end compile pipeline.
- [Compile Phases](compile-phases.md) — the phase ordering that produces the `TpuCoreProgram` this page serializes.
- [TPU-to-LLO ODS](tpu-to-llo-ods.md) — the lowering that emits the per-core ISA program bundled inside `TpuCoreProgramProto`.
- [PJRT Executable Execution](../pjrt/executable-execution.md) — the reload-and-enqueue path that consumes a deserialized program (`TpuExecutable_LoadProgramAndEnqueueToStream`).
- [PJRT Overview](../pjrt/overview.md) — the PJRT `Executable::Serialize`/`DeserializeExecutable` C-API surface this format round-trips through.
