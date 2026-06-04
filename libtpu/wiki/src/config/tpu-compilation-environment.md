# TpuCompilationEnvironment

> *All symbols, addresses, and struct offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped). Field names, proto type names, and mangled C++ symbols are quoted verbatim from the binary. Other versions differ.*

## Abstract

`xla::jellyfish::TpuCompilationEnvironment` (TCE) is the TPU compiler's **master config proto** — a single 1121-field message that holds every per-compilation knob the Jellyfish backend reads. It is the TPU-side companion to the backend-shared [`xla::DebugOptions`](debugoptions-proto.md): where DebugOptions carries the ~290 generic/dump/GPU/CPU fields that travel with an HLO module across the PJRT boundary and are *mostly inert* on TPU, the TCE carries the 1121 fields the TPU compiler *actually consumes* — scheduler tunables, fusion gates, MSA memory-space ratios, SparseCore offloading switches, collective combiner thresholds, BarnaCore embedding controls, and the debug/trace toggles. Its defining property: **every TCE field is also a registered `absl::Flag`**, so the proto schema and the flag surface are the same 1121 names, 1:1.

The reference frame for a reimplementer is XLA's own `CompilationEnvironment` mechanism. The TCE is a concrete `proto2::Message` subclass registered into the per-module `xla::CompilationEnvironments` bag, retrieved *by C++ type* — `CompilationEnvironments::GetMutableEnv<TpuCompilationEnvironment>` (`@ 0xe6de1e0`) — rather than by a global singleton. A default instance is built by walking the proto descriptor and copying each field's registered `absl::Flag` default; this is `CreateDefaultTpuCompEnv @ 0x1d73dfa0` (the field-by-field constructor) and `GetTpuCompEnvWithDefaultValues @ 0x1d73f100` (the once-guarded cached default). On top of those flag defaults a per-`TpuVersion` MSA overlay rewrites a family of fields, so the value a JAX user sees is `flag-default ⊕ AUTO-resolution-polarity ⊕ per-version-overlay`.

This page is the **structure/map** of the TCE: what the proto is, how it is constructed, what subsystem areas its 1121 fields cluster into, the key nested sub-messages, and how the `AutoOr<T>` AUTO-vs-explicit wrapper applies. It deliberately does **not** reproduce the field list — the full field#→name dictionary is split across [tce-field-dictionary-a.md](tce-field-dictionary-a.md)/[tce-field-dictionary-b.md](tce-field-dictionary-b.md), the field→offset→default reference is on [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md), and the AUTO resolver internals are on [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md). This page owns the *taxonomy, the sub-message map, and the construction path*.

For reimplementation, the contract is:

- **The proto identity and registration** — TCE is a `proto2::Message` subclass registered as an `xla::CompilationEnvironment`, fetched off the HloModule by C++ type, not a global config struct.
- **The construction path** — how `CreateDefaultTpuCompEnv` builds a default by walking the descriptor and copying each field's `absl::Flag` default union, and how the cached default is materialized once.
- **The field-group taxonomy** — which subsystem areas the 1121 fields cluster into (so a reimplementer knows *where* a knob lives), and the proto-type census that bounds the schema.
- **The sub-message + AutoProto map** — the typed sub-messages (`RangeSpecProto`, the 12 AutoProto message arms, the nested `TpuCustomCallMemorySpaceSpec` policies) and where the `AutoOr<T>` tri-state applies.

| | |
|---|---|
| **Message** | `xla::jellyfish::TpuCompilationEnvironment` (package `xla.jellyfish`, proto2) |
| **Field count** | 1121 live fields (max field# 1218, gaps tombstoned) |
| **`sizeof`** | `0x15E8` (5608 B); field data region `+0xA8 .. +0x15E0` |
| **Parse table** | `_table_ @ 0x21cfa9e0` (`TcParseTableBase` + 1121-entry `FieldEntry` array @ `+0x370`) |
| **Default instance** | `TpuCompilationEnvironment_globals_ @ 0x227b87e0` |
| **Class data** | `TpuCompilationEnvironment_class_data_ @ 0x223c96a0` (`sizeof 0x15e8` at `+0x20`) |
| **Construct default** | `CreateDefaultTpuCompEnv @ 0x1d73dfa0` (takes `SparseDenseMatmulFdoConfig*`) |
| **Cached default** | `GetTpuCompEnvWithDefaultValues @ 0x1d73f100` (`NoDestructor`, `cxa_guard`) |
| **Flag→TCE bridge** | `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` · `SetFieldFromFlagString @ 0x1d73fcc0` |
| **Module fetch** | `GetTpuCompEnv(HloModule&) @ 0x1d73dd00` → `CompilationEnvironments::GetMutableEnv<TCE> @ 0xe6de1e0` |
| **Accessor band** | ~130 `ObjectView<TCE>` accessors in `0x1d6b0080 .. 0x1d6bf9e0` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. Proto Identity and Registration

### Purpose

The TCE is not a free-standing global config object; it is one entry in a per-module *bag* of compilation environments. Understanding this is the difference between fetching the TPU knobs correctly and chasing a singleton that does not exist. This section establishes what the message *is* (a generated proto2 message), how it joins the `CompilationEnvironments` registry, and how a consumer retrieves it.

### The CompilationEnvironment contract

XLA defines `xla::CompilationEnvironments` as a typed container: an HloModule owns one, and a backend stores its private config message inside it keyed by the message's `proto2::Descriptor`. The TCE participates through the generic registry API:

```text
xla::CompilationEnvironments
  ├─ RegisterProcessNewEnvFn(Descriptor*, fn)     0x1e63eb20  ── register a creator keyed by descriptor
  │     (guarded by process_new_env_fns_mu; stores into process_new_env_fns map)
  ├─ AddEnvImpl(Descriptor&, unique_ptr<Message>)  0x1e63ee20  ── install a concrete env into a module's bag
  ├─ GetMutableEnv<TpuCompilationEnvironment>      0xe6de1e0   ── fetch the TCE by C++ type
  └─ CreateFromProto(CompilationEnvironmentsProto) 0x1e63e5a0  ── deserialize a whole bag from the wire
```

`GetMutableEnv<T>` is the template that maps a C++ type to its registered descriptor and returns the live instance (lazily default-constructing through the registered creator if absent). The TCE is one of *several* registered env messages — the binary also carries `GpuCompilationEnvironment` (`0x1fa44060` etc.) and `TpuHloModuleBackendConfig` (`GetMutableEnv<…> @ 0x1c865ac0`) as siblings in the same registry. The TPU compiler's private knobs live in the TCE arm.

### Fetch off the HloModule

A consumer does not read a global; it reads the TCE out of the module it is compiling. The entry points are a small family:

```text
GetTpuCompEnv(HloModule&)        0x1d73dd00  ── GetMutableEnv<TCE>( module + 0xF28 )
GetTpuCompEnv(HloModule*)        0x1d73dd20
GetTpuCompEnv(HloInstruction&)   0x1d73dda0  ── via instruction → parent module
GetTpuCompEnv(HloInstruction*)   0x1d73de80
GetTpuCompEnvForAutotuner(...)   0x1305f820  ── autotuner variant
```

The decompile of `GetTpuCompEnv(HloModule&) @ 0x1d73dd00` is a single tail call:

```c
function GetTpuCompEnv(HloModule& m):            // 0x1d73dd00
    // module field 485 (== module+0xF28) is the CompilationEnvironments*
    return CompilationEnvironments::GetMutableEnv<TpuCompilationEnvironment>(m[485])
```

> **NOTE —** the module member at `+0xF28` (`*((QWORD**)module + 485)`) is the module's `CompilationEnvironments*`. A reimplementer must thread the env bag through the HloModule, not snapshot a flat config at compiler init. Every TPU pass that needs a knob calls `GetTpuCompEnv(module)` and reads the field — the config is module-scoped, not process-scoped.

### Reflection-mediated field access

The 1121 fields are not accessed by 1121 hand-written getters in the flag-binding path. A `TpuCompEnvReflection` layer (`GetFlagForField @ 0x1d74ad40`, `GetFieldValue @ 0x1d7523a0`, `SetEnvField @ 0x1d752ae0`, `ReadFlag @ 0x1d74af60`, `ParseFlagFromString @ 0x1d74e8a0`) looks a field up by name/number in the descriptor and reads or writes it generically. This reflection mediation is what lets one bridge serve all 1121 fields; the registry-mediated path is detailed on [registry-mediated-flags.md](registry-mediated-flags.md). The *consumer-side* hot path, by contrast, uses ~130 specialized `ObjectView<TpuCompilationEnvironment>` accessors (band `0x1d6b0080 .. 0x1d6bf9e0`) that read a field at a fixed struct offset — these are the AUTO-resolving accessors of [§6](#6-the-autoort-wrapper).

---

## 2. The Construction Path

### Purpose

There are two related construction functions and they answer two different questions: *"build me a fresh default env"* (`CreateDefaultTpuCompEnv`) versus *"give me the shared, cached default instance"* (`GetTpuCompEnvWithDefaultValues`). Both produce a TCE whose every field equals its registered `absl::Flag` default. This section traces the descriptor-walk-and-copy mechanism so a reimplementer can reproduce it.

### Algorithm — default materialization

```c
function CreateDefaultTpuCompEnv(SparseDenseMatmulFdoConfig* fdo):  // 0x1d73dfa0
    env = operator new(0x15E8)                       // sizeof(TCE) = 5608 B
    TpuCompilationEnvironment::ctor(env, /*arena=*/0) // generated proto2 ctor
    md  = TpuCompilationEnvironment::GetMetadata()    // descriptor of the default instance (globals_)
    n   = md.descriptor.field_count                   // == 1121
    for i in 0 .. n-1:                                // walk every declared field
        fd   = md.descriptor.field(i)                 // proto2::FieldDescriptor*
        flag = TpuCompEnvReflection::GetFlagForField(fd)  // 0x1d74ad40 — name → FLAGS_<name>
        SetFieldFromFlagString(env, fd, flag.default) // 0x1d73fcc0 — copy the flag default union
    if fdo != NULL:
        // fold the SparseDenseMatmul FDO config into the SparseCore-related fields
        ApplyFdoConfig(env, fdo)
    return env
```

The decompile of `0x1d73dfa0` confirms the shape: `operator new(0x15E8u)` → `TpuCompilationEnvironment::TpuCompilationEnvironment(env, 0)` → `GetMetadata()` on the `TpuCompilationEnvironment_globals_` default instance → a loop bounded by the descriptor's field count (`*(DWORD*)(Metadata + 8)`) iterating `proto2::FieldDescriptor` and `absl::CommandLineFlag` objects, with the `SparseDenseMatmulFdoConfig*` argument (`this`/`v3`) folded in at the tail. The per-field default *value* itself lives in the `FlagImpl` default-value union at `+0x48` — a `R_X86_64_RELATIVE` reloc there means the default is a generated function (`kGenFunc`), no reloc means the 8 bytes are the inline literal (`kOneWord`); the byte-exact census of all 1121 defaults is on [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md).

### Algorithm — the cached default

```c
function GetTpuCompEnvWithDefaultValues():            // 0x1d73f100
    static defaults;                                  // NoDestructor<TCE>, cxa_guard'd
    if first_call:                                    // __cxa_guard_acquire
        tmp = $_0()                                    // 0x1d73f1a0 — build a default TCE on stack
        NoDestructor<TCE>::PlacementImpl(&defaults, tmp)  // placement-new into static storage
        TCE::SharedDtor(tmp)                           // destroy the temporary
    return &defaults
```

The lambda `$_0 @ 0x1d73f1a0` is the descriptor-walk-and-copy body (the same mechanism as `CreateDefaultTpuCompEnv`, without the FDO argument). The result is a single immutable all-default TCE returned by reference. This is the baseline a real compile starts from before `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` applies the parsed `LIBTPU_INIT_ARGS` overrides (via `SetFieldFromFlagString @ 0x1d73fcc0` and `OverwriteFieldIfNotDefault @ 0x1d73f360`) and before the per-version MSA overlay rewrites a family of fields.

> **QUIRK —** the default is built by *reflection over the flag set*, not by a hand-written field-initializer list. `CreateDefaultTpuCompEnv` constructs an empty proto, then copies each field's registered `absl::Flag` default into it. This is why the TCE schema and the flag roster are guaranteed 1:1: the construction loop would fail to find a flag for any field that lacked one. A reimplementer must register a flag per field (or supply an equivalent default table) — there is no embedded proto-default for these fields beyond the flag union. (CONFIRMED — `GetFlagForField` loop in `0x1d73dfa0`.)

### The full default pipeline placement

```text
GetTpuCompEnvWithDefaultValues  0x1d73f100  ── all-flag-default baseline (cached)
   ⊕ OverrideTpuCompEnvByCmdLineFlags 0x1d73e640  ── apply parsed LIBTPU_INIT_ARGS overrides
        SetFieldFromFlagString    0x1d73fcc0
        OverwriteFieldIfNotDefault 0x1d73f360
   ⊕ per-TpuVersion MSA overlay (v0/1→jf, v2→cmem, v3→vf, v4/5→gf)   ── memory subsystem pages
   = effective TCE for this compilation
```

---

## 3. Field-Group Taxonomy

### Purpose

1121 fields is too many to navigate by number. They cluster into a small set of subsystem areas; knowing the cluster a knob belongs to is how a reimplementer locates it. This section gives the taxonomy by name-prefix and by functional area. It is the *map*, not the dictionary — the per-field names are on [tce-field-dictionary-a.md](tce-field-dictionary-a.md)/[tce-field-dictionary-b.md](tce-field-dictionary-b.md).

### Proto-type census

The 1121 fields decompose by proto type as follows. Each base type maps to exactly one 16-bit `FieldEntry.type_card` in the parse table, so this histogram is re-derivable byte-exact from `_table_ @ 0x21cfa9e0`:

| Proto type | Count | type_card | Notes |
|---|---:|---|---|
| bool | 418 | `0x0011` | 153 default `true`, 265 default `false` |
| message | 349 | `0x0416` | the typed sub-messages + AutoProto fields ([§4](#4-sub-message-and-autoproto-map)) |
| int64 | 148 | `0x10d1` | thresholds, fuel, ring limits |
| enum | 74 | `0x1891` | 67 `TristateProto.Value` + 7 wrapper enums |
| string | 37 | `0x0c15` | 30 empty, 7 non-empty defaults |
| float | 34 | `0x1893` | MSA ratios, margins |
| int32 | 32 | `0x1091` | trip counts, levels |
| double | 14 | `0x18d3` | MSA scaling factors |
| uint32 | 11 | `0x0891` | |
| uint64 | 4 | `0x08d1` | |

> **NOTE —** bool (418) + message (349) account for 68% of the schema. The TCE is overwhelmingly a bag of boolean feature gates and typed sub-message/AutoProto knobs; numeric tunables are the minority. The 349 message-typed fields are the structurally interesting ones — most are `AutoProto` oneofs resolved through `AutoOr<T>` ([§6](#6-the-autoort-wrapper)). The full type↔type_card mapping and per-type counts are byte-anchored on [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md).

### By name-prefix family

The TCE is the landing zone for several flag-name families. The split across the two protos (TCE vs DebugOptions) is owned by [overview.md](overview.md) and [flag-families.md](flag-families.md); the families that land *in the TCE* are:

| Prefix family | Subsystem | Detail |
|---|---|---|
| `xla_tpu_*` | TPU-private core: fusion, scheduling, MSA, collectives, SDC | [flag-families.md](flag-families.md) |
| `xla_jf_*` | Jellyfish backend: LLO codegen, bundle packing, rematerialization | [flag-families.md](flag-families.md) |
| `xla_sc_*` / `barna_core_*` | SparseCore / BarnaCore embedding | [flag-families.md](flag-families.md) |
| `xla_msa_*` / `xla_gf_*` / `xla_ior_*` | Memory-space assignment ratios, register file, CMEM | [flag-families.md](flag-families.md) |
| a subset of `xla_*` | scheduler, profiler, trace toggles shared in name with DebugOptions but stored in TCE | [flag-families.md](flag-families.md) |

### By functional area

Mapping the prefixes to the subsystem areas the prompt's reimplementer cares about, with byte-anchored representative fields (field# → name, offset, default from the parse table):

| Area | Representative fields | What they gate |
|---|---|---|
| **Scheduler** | #31 `xla_memory_scheduler` (`MemoryScheduler` enum, default `DEFAULT`), #41 `xla_hlo_scheduling_brkga_generation_limit`=1200, #151 `xla_tpu_licm_analysis_allowance`=100000 | memory-pressure scheduler selection, BRKGA genetic-scheduler budget, LICM allowance |
| **Fusion** | #63 `xla_jf_enable_multi_output_fusion`=true, #180 `xla_tpu_small_operand_count_for_loop_fusion`=13, #181 `xla_jf_fusion_max_instruction_count_for_window_config`=1000, #393 nested-dot-fusion custom ops | multi-output / loop / window fusion eligibility and limits |
| **MSA / memory** | #280/284/285 jf overlap max/min/pref (32.0/1.0/2.0), #592 `xla_tpu_msa_inefficient_use_to_copy_ratio`=0.5, #14 `xla_tpu_max_cmem_used_by_memory_space_assignment`=-1 | memory-space-assignment overlap ratios, copy heuristics, CMEM budget |
| **Layout** | #758 `move_dot_parameters_to_rhs` (Tristate ENABLED), #766 `enable_large_2nd_minor_layout_for_x8` (ENABLED) | dot operand placement, minor-dimension layout |
| **SparseCore** | #802 `enable_offloading_scatter_to_sparsecore` (ENABLED), #822/839/860 SC collective-offload (all-gather/all-reduce/reduce-scatter ENABLED), #827 `xla_sc_async_wrapper_fusion_type`=`SINGLE_TPU_CUSTOM_CALL` | scatter offload, SC collective offloading, async wrapper fusion |
| **Collectives** | #55/56/57 arf/ars/agf combiner thresholds=125,829,120 B (120 MiB), #58 `xla_jf_crs_combiner_threshold_count`=256, #12/13 net-router ring limits=8/16, #149 `max_concurrent_send_recv`=INT32_MAX | combiner thresholds, net-router rings, send/recv concurrency |
| **BarnaCore / embedding** | #30 `xla_tpu_embedding_table_oblongness_threshold`=50.0, #171 `internal_embedding_emitter_fraction_vmem_available`=0.9 | embedding emitter VMEM budgeting |
| **Debug / trace** | #32–#39 trace toggles (`xla_enable_profiler`=true, `xla_enable_hlo_trace`=true, `xla_enable_mxu_trace`=false, …), #583 `xla_tpu_sdc_checker_checksum_algo`, #723 `xla_tpu_precision_tracer_mode` | profiler/trace emission, SDC checker, precision tracer |

> **GOTCHA —** the absl flag default is byte-authoritative; the help/error text is not. Two flags read `=false` from their help string but their `FlagImpl+0x48` inline literal is `01 00 00 00` = `true` (`xla_tpu_rwb_fusion`, `xla_tpu_accumulate_into_mrb`). When you reconstruct a default, read it from the flag/proto initializer (the `+0x48` union), never from a help string. (CONFIRMED — byte-corrected on [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md).)

---

## 4. Sub-Message and AutoProto Map

### Purpose

Of the 1121 fields, 349 are message-typed. Almost all are not hand-written sub-messages but `AutoProto` oneofs — a single generic wrapper that holds a tri-state value of one of 30 possible types. A handful are typed helper messages (`RangeSpecProto`). This section maps the message-typed surface so a reimplementer knows which fields are AutoProto, which are typed sub-messages, and what the 12 message arms of AutoProto are.

### The typed helper message — `RangeSpecProto`

A small set of fields are typed `RangeSpecProto` (its own generated message class with parse table `_table_ @ 0x21cf9c90`), used for fields that name an instruction/buffer *range* rather than a scalar — e.g. #50 `xla_jf_naive_bundle_packer` (`+0x228`), #60 `xla_jf_bounds_check_annotate_only` (`+0x230`), #65 `xla_jf_lsra_v2_alloc_only` (`+0x238`). Their default is the empty message (an unset range = apply-to-all / apply-to-none per consumer). These sit at contiguous struct offsets in the message region because, as message fields, they are stored inline (not as the 8-byte scalar union that bool/int fields use).

### The AutoProto oneof — 30 arms

`xla.jellyfish.AutoProto` is a 30-arm oneof, byte-confirmed from its own parse table `AutoProto::_table_ @ 0x21cfa788`. It is the wrapper for the bulk of the message-typed TCE fields (~330): a field declared `AutoProto` can hold AUTO (unset, oneof-case 0), or any one of 30 typed alternatives. The 30 arms are 8 hardcoded scalar arms (bool/int64/uint64/int32/uint32/double/float/string), 10 enum arms, and **12 message arms** resolved to concrete sub-message types via the parse table's 12 aux pointers:

| Arm | Message type | Sub-message `_table_` | Governs |
|---|---|---|---|
| 9 | `RepeatedStrings` | `0x21cf9d18` | list-valued string knobs |
| 10 | `BufferContentsSanitizerConfig` | `0x21cf9f58` | buffer sanitizer |
| 13 | `CostModelFlagOptions` | `0x21cfa170` | cost-model tuning |
| 14 | `SparseCoreOffloadingOptions` | `0x21cfa110` | SparseCore offload config |
| 15 | `ShardyOptions` | `0x21cfa260` | Shardy SPMD partitioner |
| 18 | `IlpLatencyHidingSchedulerOptions` | `0x21cfa308` | ILP latency-hiding scheduler |
| 22 | `RepeatedIntegers` | `0x21cf9da0` | list-valued integer knobs |
| 23 | `EmitterLearnedCostModelOptions` | `0x21cff9a8` | learned cost model |
| 26 | `AccumulatorTransformations` | `0x21cf9c30` | accumulator transform set |
| 27 | `SparseCoreAssertLevel` | `0x21cfa550` | SC assertion level |
| 28 | `BundleInstrumentationOptions` | `0x21cfa5b0` | bundle instrumentation |
| 29 | `TpuCustomCallMemorySpaceSpec` | `0x21cfa708` | custom-call memory-space policy |

The 30-arm oneof, its scalar/enum arms, and the per-arm message-default decode are owned by [autoproto-message-arms.md](autoproto-message-arms.md). The default of an AutoProto field is the **empty AutoProto** (oneof-case 0 = AUTO/unset), so the *value* is supplied by the consumer's resolution polarity ([§6](#6-the-autoort-wrapper)).

### Nested policy sub-messages — `MsaReservationPolicy`, `HbmPolicy`

The MSA reservation and HBM policies named elsewhere in the memory subsystem are **nested under the `TpuCustomCallMemorySpaceSpec` message arm** (arm 29), not top-level TCE fields. The binary carries them as `TpuCustomCallMemorySpaceSpec_MsaReservationPolicy` (`Clear @ 0x1db25da0`, `ctor @ 0x1db25c80`) and `TpuCustomCallMemorySpaceSpec_HbmPolicy` (`ctor @ 0x1db25ea0`, `GetClassData @ 0x1db25ee0`), and they are consumed by `TpuCustomCallMemorySpacePolicy::RunMsaReservationPolicy @ 0x110367c0` and `…::RunHbmPolicy @ 0x11038120`.

> **QUIRK —** `MsaReservationPolicy` and `HbmPolicy` are nested sub-messages of an AutoProto *message arm* (`TpuCustomCallMemorySpaceSpec`), two levels below the TCE. A reimplementer cannot reach them through a top-level TCE field number; they are populated only when the `TpuCustomCallMemorySpaceSpec` arm of the relevant AutoProto field is set, and they steer the custom-call memory-space assignment that `RunMsaReservationPolicy`/`RunHbmPolicy` apply during MSA. (CONFIRMED — mangled `TpuCustomCallMemorySpaceSpec_MsaReservationPolicy` / `_HbmPolicy` symbols.)

---

## 5. Wire Format, Presence, and Memory Layout

### Purpose

The TCE is a real proto2 message: it serializes, it carries has-bit presence, and it round-trips through the module's `CompilationEnvironments` bag onto the wire. A reimplementer needs the layout constants to read a serialized env and to know *which fields the user touched* versus left at default. This section gives the parse-table header and the serialization entry, both byte-anchored.

### Parse-table header — the layout oracle

`TpuCompilationEnvironment::_table_ @ 0x21cfa9e0` is the `TcParseTableBase` that drives fast-path parsing, and its header constants double as the memory-layout map:

```text
TcParseTableBase @ 0x21cfa9e0
  +0x00 has_bits_offset      = 16   (0x10)   ── presence bitfield starts at struct +0x10
  +0x02 extension_offset     = 0              ── no proto extensions
  +0x04 max_field_number     = 1218 (0x4c2)  ── highest declared field# (gaps tombstoned)
  +0x08 fast_idx_mask        = 0xf8
  +0x10 field_entries_offset = 0x370          ── FieldEntry[1121] starts here
  +0x14 num_field_entries    = 1121
  +0x16 num_aux_entries      = 349            ── == the 349 message-typed fields
  +0x18 aux_offset           = 0x3800
  sizeof(TCE) = 0x15e8 (5608 B); field data region +0xA8 .. +0x15E0
```

Each `FieldEntry` is 12 bytes `{uint32 offset; uint32 has_idx; uint16 aux_idx; uint16 type_card}`, and the array is sorted by *ascending field number*, so `entry[i]` is the i-th live field and `struct_offset(field N) = FieldEntry[index_of(N)].offset`. The field#→offset mechanism and the deterministic offset formula are owned by [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md); this page only places the header.

### Presence model

`has_bits_offset = 16` means every field carries an explicit presence bit packed into the `+0x10 .. ~+0xB0` region (one per field, sequential by `FieldEntry.has_idx`). This is what lets the override path distinguish "user set this knob to `false`" from "left at the default `false`": `OverwriteFieldIfNotDefault @ 0x1d73f360` reads the has-bit before deciding whether the per-version overlay may rewrite a field. A reimplementer that drops proto2 presence cannot implement the `⊕`-overlay logic correctly.

> **NOTE —** the exact `has_idx → (struct word, bit)` packing was not re-walked from the binary; the `FieldEntry.has_idx` values are recovered, and the layout follows protobuf's standard sequential packing from `has_bits_offset = 16` over the `+0x10 .. ~+0xB0` region (~1121 bits). The presence-region *bounds* and per-field `has_idx` are CONFIRMED; the precise bit assignment is HIGH-confidence (standard packing) but not byte-verified. (HIGH)

### Serialization round-trip

The TCE travels on the wire as one entry of `CompilationEnvironmentsProto`. `CompilationEnvironments::CreateFromProto @ 0x1e63e5a0` deserializes a whole bag — for each serialized env it looks up the registered creator by the message's descriptor (the `RegisterProcessNewEnvFn` map from [§1](#1-proto-identity-and-registration)) and reconstructs the typed message. So a TCE produced on one host (e.g. an autotuner run) can be serialized into the module's `CompilationEnvironmentsProto` and faithfully reconstructed on another, provided both register the TCE descriptor.

---

## 6. The AutoOr<T> Wrapper

### Purpose

The ~330 AutoProto fields do not carry a plain value — they carry a tri-state (`AUTO` / `ENABLED` / `DISABLED` for booleans, or AUTO-vs-explicit for other types), and the concrete compile value is computed *lazily at the consumer* through an `AutoOr<T>` accessor whose polarity *is* the documented default. This section places the wrapper; the byte-exact resolver and the per-knob polarity census are owned by [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md).

### How it applies to a TCE field

A consumer reads an AutoProto field through an `ObjectView<TpuCompilationEnvironment>` accessor (band `0x1d6b0080 .. 0x1d6bf9e0`). The accessor fetches the AutoProto at the field's struct offset, checks the oneof discriminator, and applies a per-knob polarity:

```c
function read_auto_bool_field(ObjectView<TCE> env, offset OFF):  // one of the 0x1d6b* accessors
    p = env.field_at(OFF)                          // AutoProto sub-message
    r = AutoOr<bool>::FromProtoOrDie(p)            // 0xf795300 — (present<<8)|val, AUTO ⇒ 0
    if KNOB_POLARITY == AUTO_off:                  // only explicit true enables
        return (r == (1<<8 | 1))                   // present AND true
    else: # AUTO_on                                // only explicit false disables
        return (r != (1<<8 | 0))                   // not (present AND false)
```

The same all-AUTO storage therefore yields *opposite* defaults depending on the knob author's idiom. A field with `AUTO=off` polarity defaults OFF; a field with `AUTO=on` polarity defaults ON — both backed by the same empty AutoProto. A second storage class also exists: 67 fields are *inline* `TristateProto.Value` enums (a 4-byte field read by `cmpl $0x2, +OFF` where `ENABLED==2`), not AutoProto wrappers. The full polarity census (offset → polarity), the int64-sentinel idioms, and the per-type packing are on [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md).

### The wrapper enums

Beyond the 67 `TristateProto.Value` fields, 7 TCE enum fields use dedicated wrapper enums whose default is a *non-zero* value — e.g. #631 `xla_tpu_register_selection_policy` = `6` (`DISREGARD_RECENTLY_USED`), #827 `xla_sc_async_wrapper_fusion_type` = `3` (`SINGLE_TPU_CUSTOM_CALL`), #132 `xla_tpu_verify_or_assign_tiling_before_lowering` = `1` (`VERIFY`). The bridge that maps a parsed flag value into the correct TCE oneof arm is the `NormalizeFieldType<T>` / `AutoOr<T>` template family (one instantiation per wrapper type). The full 17-wrapper-enum value tables are on [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md).

> **GOTCHA —** there is no eager "resolve all knobs" pass. The `AutoOr<T>` resolution runs at *every* accessor call and falls back to the empty AutoProto default when the field is unset. A reimplementer who materializes a flat `bool`/`int` config struct at init and reads it thereafter will silently ship the wrong default for the ~330 AutoProto fields (and miss the per-version overlay). The config is resolved lazily, per consumer, per field. (CONFIRMED — `FromProtoOrDie @ 0xf795300` packing.)

---

## Related Components

| Component | Relationship |
|---|---|
| `xla::DebugOptions` ([debugoptions-proto.md](debugoptions-proto.md)) | the backend-shared sibling proto; carries the generic/dump/GPU/CPU fields, mostly inert on TPU |
| `CompilationEnvironments::GetMutableEnv<TCE>` @ `0xe6de1e0` | fetches the TCE off a module's env bag by C++ type |
| `CreateDefaultTpuCompEnv` @ `0x1d73dfa0` | builds a default TCE by descriptor-walk + flag-default copy |
| `GetTpuCompEnvWithDefaultValues` @ `0x1d73f100` | the once-guarded cached all-default TCE instance |
| `OverrideTpuCompEnvByCmdLineFlags` @ `0x1d73e640` | applies parsed `LIBTPU_INIT_ARGS` overrides on top of the default |
| `AutoProto::_table_` @ `0x21cfa788` | the 30-arm oneof parse table behind the ~330 AutoProto fields |
| `TpuHloModuleBackendConfig` / `GpuCompilationEnvironment` | sibling registered env messages in the same `CompilationEnvironments` registry |

## Cross-References

- [overview.md](overview.md) — the three-layer flag→proto→effective-value pipeline; the TCE is Stage 2b
- [debugoptions-proto.md](debugoptions-proto.md) — the backend-shared `xla::DebugOptions` schema; the TCE's generic sibling
- [tce-field-dictionary-a.md](tce-field-dictionary-a.md) / [tce-field-dictionary-b.md](tce-field-dictionary-b.md) — the per-field name/type/meaning dictionaries (the full 1121-field list)
- [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md) — the byte-exact field#→struct-offset→default reference and the parse-table mechanism
- [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) — the `AutoOr<T>` tri-state resolver, the ~130-accessor polarity census, the int64 sentinels, and the 17 wrapper-enum tables
- [autoproto-message-arms.md](autoproto-message-arms.md) — the 30-arm AutoProto oneof and its 12 message arms in detail
- [registry-mediated-flags.md](registry-mediated-flags.md) — the `TpuCompEnvReflection` flag↔field bridge
- [flag-families.md](flag-families.md) — which name-prefix families land in the TCE vs DebugOptions vs standalone flags
- [xla-flag-atlas.md](xla-flag-atlas.md) — the ~2107-name flag surface that mirrors the TCE field set 1:1
