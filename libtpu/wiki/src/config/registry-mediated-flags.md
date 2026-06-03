# Registry-Mediated Flags

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

Most TPU compile knobs are read the way [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) describes: a generated accessor loads an `AutoProto*` at a fixed offset off the `TpuCompilationEnvironment` (TCE) struct, calls `AutoOr<T>::FromProtoOrDie`, and applies a polarity test. That path is *offset-keyed* — the field's position in the struct is baked into the accessor at compile time. A smaller class of flags is **not** read that way. Their consumer never loads a field at a fixed offset. Instead it looks the value up *by name* at runtime, through a reflection read over the TCE proto mediated by a flag↔field registry. This page owns that registry-mediated read path: the `GetFieldValueIfNotDefault<T>` by-name reflection reader `@ 0x1c6f1a80`, the `FlagFieldMappings` flag↔field bridge `@ 0x2257ef50`, the per-`TpuVersion` `LegacyEvictions`/`LegacyPrefetches` sync-flag registries, the `SetFieldFromFlagString` write side `@ 0x1d73fcc0`, and the worked example of a flag driving a compiler transform — `xla_tpu_impure_use_iteration_mask` → the `RaggedDotExpander` iteration-mask lowering.

The reference frame is XLA's `xla::DebugOptions` proto-backed flags, but with a libtpu-specific twist a reimplementer must reproduce. A CLI flag (`absl::CommandLineFlag`, the `FLAGS_<name>` object in `.data`) and a TCE proto field are *two separate things* bridged at runtime: the `FlagFieldMappings` `NoDestructor` ctor walks every TCE proto field, calls `absl::FindCommandLineFlag(field_name)`, and inserts the pair into two `FlatHashMap`s (flag→field and field→flag). At env-assembly time `SetFieldFromFlagString` resolves a flag back to its `FieldDescriptor` and writes the parsed value *into the proto field*; at read time `GetFieldValueIfNotDefault<T>` looks the field up *by name*, reads it by reflection, and returns it only if it differs from the all-defaults env. The flag value travels CLI string → TCE proto field → by-name reflection read — never a `lea FLAGS_<name>` at the consumer. This indirection is what makes a flag "registry-mediated": the consumer is bound to a *name*, not to a struct offset or a `FLAGS_` symbol.

The third, sharp consequence of this design is that a flag can be **registered but unwired**. `enable_lem_scheduler` and `explicit_evict_memory_limit_kib` are both registered (so `--helpfull` lists them and the CLI parses them) yet have *no* statically reachable consumer: their `FLAGS_` objects have exactly one code xref — the registration `lea` — and their *names* are absent from the serialized TCE `FileDescriptorProto`, so the by-name reader's `FindFieldByName` fails for them. They are vestigial. The page is structured as: the three read classes; the by-name reflection reader and the bridge; the proof that the two named flags are unwired and what actually drives those behaviors; and the worked `RaggedDot` flag→transform example end to end.

For reimplementation, the contract is:

- **The three read classes** — (A) `FLAGS_`-pinned direct (`lea FLAGS_`; `FlagImpl+0x58`; `AutoOr` resolved inline), (B) by-name TCE reflection (the registry-mediated path), (C) registered-but-unwired (name not a proto field). A reimplementer must distinguish them by the consumer's *read instruction*, not by the flag's name or type.
- **The by-name read mechanism** — `GetFieldValueIfNotDefault<T>` = `FindFieldByName` → `GetFieldValue` reflection → diff against the all-defaults env → `optional<T>`; the `FlagFieldMappings` dual `FlatHashMap` bridge; the write-side `SetFieldFromFlagString`.
- **The worked flag→transform** — `impure_use_iteration_mask` (read class A, `AutoOr<bool>`, AUTO=ON, `TpuVersion>=3` gate) → `RaggedDotExpander` → the `Iota + Compare + AND + Select(mask, conv, 0)` iteration mask that zeroes cross-group products in the ragged convolution.

| | |
|---|---|
| **By-name reader** | `GetFieldValueIfNotDefault<long> @ 0x1c6f1a80` — `FindFieldByName` + `TpuCompEnvReflection::GetFieldValue` + diff vs defaults |
| **Flag↔field bridge** | `FlagFieldMappings::GetInstance @ 0x2257ef50` · ctor `@ 0x1d753ce0` (dual `FlatHashMap`, `FindCommandLineFlag` per field) |
| **Write side** | `SetFieldFromFlagString @ 0x1d73fcc0` — `GetFieldForFlag` + `ParseFlagFromString` into the proto field |
| **Legacy sync-flag registries** | `LegacyEvictionsFlagRegistry @ 0x22579860` · `LegacyPrefetchesFlagRegistry @ 0x225798d8` (per-`TpuVersion` `StaticMap`) |
| **MSA consumer** | `ComputeMemoryManagementSflagUsage @ 0x1c6f1580` — 2× `GetFieldValueIfNotDefault<long>` (`@ 0x1c6f185e`/`0x1c6f1888`) |
| **Unwired flags** | `FLAGS_xla_tpu_enable_lem_scheduler @ 0x223c72a8` · `FLAGS_xla_tpu_explicit_evict_memory_limit_kib @ 0x223c50d0` — 1 xref each (registration only) |
| **Registrar** | `_GLOBAL__sub_I_tpu_compilation_environment.cc @ 0x2135cba0..0x21360ef0` (`RegisterCommandLineFlag @ 0x21114cc0` per flag) |
| **Worked example flag** | `FLAGS_xla_tpu_impure_use_iteration_mask @ 0x223a7dd0` — `AutoOr<bool>`, FlagOps `0x1d6b5840`, 6 xrefs (5 consumers + registrar) |
| **Worked example transform** | `RaggedDotExpander::RunImpl @ 0x10fae060` → `CreateOutputMask @ 0x10fb2900` → `CreateConvolutionSelectFusion @ 0x10fb31e0` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The Three Read Classes

### Purpose

A TCE compile knob's *value* can reach a consumer by one of three distinct paths. The distinction matters because it changes both where a reimplementer looks for the consumer and whether the flag has any runtime effect at all. The classes are mutually exclusive per flag, and a flag's class is visible only in its consumer's read instruction.

### The classes

| Class | Read instruction at the consumer | Example flags | Effect |
|---|---|---|---|
| **(A)** `FLAGS_`-pinned direct | `lea FLAGS_<name>(%rip)` → `FlagImpl::ReadOneWord`/`ReadOneBool` (`FlagImpl+0x58` cached) → `AutoOr` polarity inline | `impure_use_iteration_mask`, `impure_enable_masked_fusion_iteration_skipper` | live |
| **(B)** by-name TCE reflection | *no* `lea FLAGS_`; `GetFieldValueIfNotDefault<T>(name, env)` → `FindFieldByName` + `GetFieldValue` reflection | `xla_jf/vf/zf/gf_vmem_max_outstanding_evictions`/`_prefetches`, `explicit_prefetch_memory_limit_kib` | live |
| **(C)** registered but unwired | none — the `FLAGS_` object's only xref is the registration `lea`; name is not a TCE proto field | `enable_lem_scheduler`, `explicit_evict_memory_limit_kib` | dead in v0.0.40 |

Class (A) is the path [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) and [flag-families.md](flag-families.md) document for the bulk of the knob surface: a generated accessor band loads the value directly from a `FLAGS_` object or a TCE struct offset. Class (B) is the subject of this page. Class (C) is a degenerate (B) — the read mechanism exists, but the flag's name was never added to the TCE proto, so the by-name reader can never find it.

> **GOTCHA —** classes (B) and (C) share the same *static signature* at the `FLAGS_` level: exactly one code xref, the registration `lea`. A whole-`.text` RIP-relative `lea`/`mov` scan cannot tell them apart, because neither has a consumer that references the `FLAGS_` object. The discriminator is the *serialized TCE proto*: a class-(B) flag's name is a proto field token (so `FindFieldByName` succeeds); a class-(C) flag's name is not. A reimplementer who classifies only by xref count will misfile every unwired flag as merely registry-mediated.

### How a flag's value enters the proto (write side)

For class (B), the flag value must be written *into* the TCE proto field before any by-name read can observe it. That bridge is `SetFieldFromFlagString @ 0x1d73fcc0`:

```c
function SetFieldFromFlagString(CommandLineFlag* flag, string_view value,    // 0x1d73fcc0
                                TpuCompilationEnvironment* env):
    field = TpuCompEnvReflection::GetFieldForFlag(flag)    // flag → FieldDescriptor (the field->flag map)
    if !field.ok():                                        // FATAL "Flag is not found for field" path
        return Status(... "tpu_compilation_environment.cc":5905)
    TpuCompEnvReflection::ParseFlagFromString(flag, field, value)  // parse + reflection-write into env[field]
    ...
```

The write resolves the flag back to its `FieldDescriptor` via the bridge (§2), then `ParseFlagFromString` parses the CLI string against the field's type and writes it into the proto by reflection. After this, `GetFieldValueIfNotDefault<T>(field_name, env)` will see a non-default value.

> **NOTE —** the write side was confirmed structurally: `SetFieldFromFlagString` calls `GetFieldForFlag` then `ParseFlagFromString`, the exact flag→field-then-write shape (CONFIRMED). The per-flag parse grammar for each typed field was not individually re-walked here (it is the `AutoOr`/`Tristate` parse grammar on [autoor-parse-grammar.md](autoor-parse-grammar.md)).

---

## 2. The By-Name Reflection Reader and the Bridge

### Purpose

The class-(B) read is a *reflection* read: the consumer holds a field *name* (a `string_view`), not a struct offset, and resolves the value at runtime against the TCE proto's descriptor. Two pieces implement it — the reader `GetFieldValueIfNotDefault<T>` and the `FlagFieldMappings` bridge that maps flags to fields and back.

### Entry Point

```text
<consumer>(name, env)                                    ── e.g. ComputeMemoryManagementSflagUsage 0x1c6f1580
  └─ GetFieldValueIfNotDefault<T>(name, env)             ── 0x1c6f1a80 (T=long instantiation)
       ├─ TpuCompilationEnvironment::GetMetadata()->descriptor   ── 0x1c6f1aa0
       ├─ Descriptor::FindFieldByName(name)              ── 0x20e57900; null → "... not found ..." error
       ├─ TpuCompEnvReflection::GetFieldValue(field, env)        ── 0x1d7523a0 → variant over every AutoOr arm
       ├─ GetFieldValue(field, GetTpuCompEnvWithDefaultValues())  ── default-env, 0x1d73f100
       └─ __fmatrix variant visitor: (val != def) ? optional<T>(val) : nullopt
```

### Algorithm

The `long` instantiation `@ 0x1c6f1a80` is the canonical body; other typed instantiations differ only in the variant arm they extract:

```c
function GetFieldValueIfNotDefault<long>(string_view name,                   // 0x1c6f1a80
                                         ObjectView<TCE> env):
    desc = TpuCompilationEnvironment::GetMetadata()->descriptor   // 0x1c6f1aa0 (from TCE_globals_)
    field = desc->FindFieldByName(name)                           // 0x1c6f1ab5
    if !field:                                                    // not a proto field
        return StatusError(StrCat(name, " ... not found ..."))    // 0x1c6f1c27 / StrCat @ 0x21174860
    val = TpuCompEnvReflection::GetFieldValue(field, env)         // 0x1c6f1ad3 → absl::variant
    def = TpuCompEnvReflection::GetFieldValue(field,
              GetTpuCompEnvWithDefaultValues())                   // 0x1c6f1b36 / 0x1c6f1b48
    // __fmatrix visitor compares the two variants element-wise   // 0x1c6f1bdb
    if val == def:
        return nullopt                                            // the "IfNotDefault" semantics
    return optional<long>(extract_long(val))
```

The variant `GetFieldValue` returns is a sum over **every** value type a TCE knob can carry — the visitor's type list in the decompile is `a h i l j m d f b` (bool/int8…/int64/uint64/double/float) plus `RangeSpecProto`, `RepeatedStrings`, `SparseDenseMatmulFdoConfig`, `SlicedPrefetchOptions`, `MemoryBoundLoopOptimizerOptions`, `PreferredPrefetchOverrides`, `MsaSortOrderOverrides`, `BufferContentsSanitizerConfig`, `BufferIsolationConfig`, and `AutoProto`. So a tri-state knob's `AutoProto` oneof is resolved *through reflection* on this path, not through the inline `AutoOr<T>::FromProtoOrDie` band — same storage cell (§ [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md)), different reader.

> **QUIRK —** the read is differential, not absolute. `GetFieldValueIfNotDefault` returns `nullopt` when the field equals the value in `GetTpuCompEnvWithDefaultValues()`, *not* a sentinel-or-stored value. A consumer that wants "did the user set this knob?" gets a true answer; a consumer that wants "what is the effective value, default included?" must supply its own default when the optional is empty. This is exactly how the MSA consumer (§3) uses it — it ignores the field unless it differs from the default.

### The flag↔field bridge

A flag and a proto field are linked at runtime by `FlagFieldMappings`, a `NoDestructor` singleton built once:

```c
function FlagFieldMappings::ctor():                              // 0x1d753ce0 (NoDestructor body)
    desc = TpuCompilationEnvironment::GetMetadata()->descriptor  // 0x1d753... (GetMetadata @ 0x1db635c0)
    n    = desc->field_count                                     // *(int*)(meta + 8)
    for i in 0 .. n-1:
        field = desc->field(i)                                   // field stride walk, meta+64 base
        flag  = absl::FindCommandLineFlag(field->name())         // 0x1d753dbf (FindCommandLineFlag @ 0x21115120)
        if flag:
            flag_to_field[flag]  = field                         // 0x1d753e1f  FlatHashMap<CommandLineFlag*, FieldDescriptor*>
            field_to_flag[field] = flag                          // 0x1d753e7f  FlatHashMap<FieldDescriptor*, CommandLineFlag*>
```

The companion accessor `TpuCompEnvReflection::GetFlagForField @ 0x1d74ad40` reads the field→flag map and FATALs (`"Flag is not found for field"` str `@ 0x86f971b`) when a field has no registered flag.

> **GOTCHA —** the bridge is built by iterating the *proto fields* and looking up a flag per field — not by iterating flags. So the bridge spans **only** flags whose name is a TCE proto field. A registered flag whose name is not a field (class C) is never inserted into either map; `GetFlagForField` would FATAL if asked, and the by-name reader's `FindFieldByName` returns null. The bridge's domain *is* the set of class-(B) flags.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `GetFieldValueIfNotDefault<long>` | `0x1c6f1a80` | by-name reflection reader — `FindFieldByName` + diff-vs-default | CONFIRMED |
| `Descriptor::FindFieldByName` | `0x20e57900` | name → `FieldDescriptor` (null ⇒ not a field) | CONFIRMED |
| `TpuCompEnvReflection::GetFieldValue` | `0x1d7523a0` | reflection read → variant over all arm types | CONFIRMED |
| `GetTpuCompEnvWithDefaultValues` | `0x1d73f100` | the all-defaults env the read diffs against | CONFIRMED |
| `FlagFieldMappings` ctor | `0x1d753ce0` | builds dual `FlatHashMap` (flag↔field) | CONFIRMED |
| `FlagFieldMappings::GetInstance` | `0x2257ef50` | the `NoDestructor` singleton accessor | CONFIRMED |
| `TpuCompEnvReflection::GetFlagForField` | `0x1d74ad40` | field→flag lookup (FATALs when absent) | CONFIRMED |
| `SetFieldFromFlagString` | `0x1d73fcc0` | write side — flag string → proto field by reflection | CONFIRMED |
| `RegisterCommandLineFlag` | `0x21114cc0` | the registrar's per-flag registration call | CONFIRMED |

---

## 3. The Legacy Sync-Flag Registries (a class-(B) example)

### Purpose

The cleanest live class-(B) consumer is the MSA (memory-space assignment) eviction/prefetch limit reader. The eviction and prefetch outstanding-op limits are *per-TPU-family* TCE fields with family-specific names (`xla_jf_…` for Jellyfish, `xla_vf_…`, `xla_zf_…`, `xla_gf_…`). Rather than hard-code an offset per family, the consumer looks the *field name* up in a per-`TpuVersion` registry, then reads that field by name.

### Algorithm

```c
function ComputeMemoryManagementSflagUsage(ObjectView<TCE> env, Target& target):  // 0x1c6f1580
    ver = target.tpu_version
    evict_name    = LegacyEvictionsFlagRegistry[ver]    // StaticMap<TpuVersion, string_view> @ 0x22579860
    prefetch_name = LegacyPrefetchesFlagRegistry[ver]   //                                    @ 0x225798d8
    evict_opt     = GetFieldValueIfNotDefault<long>(evict_name, env)       // 0x1c6f185e
    prefetch_opt  = GetFieldValueIfNotDefault<long>(prefetch_name, env)    // 0x1c6f1888
    // "legacy_non_default_value" (str @ 0x94adace): use the field only if it differs from default
    ...apply evict_opt / prefetch_opt to the sync-flag usage...
```

The two registries are `util_registration::StaticMapBase` singletons keyed by `tpu::TpuVersion` (verified in the decompile: `StaticMapBase<…LegacyEvictionsFlagRegistry, tpu::TpuVersion, string_view, …>::GetSingleton`). They are populated per family in `_GLOBAL__sub_I_sync_flag_util_{dragonfish,jellyfish,pufferfish,viperfish,ghostlite}.cc` via `InsertValue @ 0x1c6f8760`/`0x1c6f8940`.

### Registered field names

| Registry | Field-name pattern (per family) | Anchors |
|---|---|---|
| `LegacyEvictionsFlagRegistry @ 0x22579860` | `xla_{zf,vf,jf,gf}_vmem_max_outstanding_evictions` (+ `cmem` variant) | `"xla_jf_vmem_max_outstanding_evictions" @ 0x853d9d7` |
| `LegacyPrefetchesFlagRegistry @ 0x225798d8` | `xla_{zf,vf,jf,gf}_vmem_max_outstanding_prefetches` | `"xla_jf_vmem_max_outstanding_prefetches" @ 0x8569c57` |

All of these names ARE TCE proto fields (present in the serialized `descriptor_table_protodef_AiDtBo5TtCO`), so `FindFieldByName` succeeds and the differential read returns a real value when the user set the flag. This is the canonical registry-mediated path: a registry maps a runtime key (`TpuVersion`) to a field *name*, and the value is read by that name.

---

## 4. The Unwired Pair (class (C))

### Purpose

`enable_lem_scheduler` and `explicit_evict_memory_limit_kib` are the documented class-(C) exceptions: registered, parseable on the CLI, listed by `--helpfull`, but with no statically reachable value consumer in v0.0.40. The proof is multi-angle and byte-exact; this section records it so a reimplementer does not waste time wiring a dead flag.

### The proof

```text
1. Whole-.text RIP-relative lea/mov scan (0xe635560..0x213f81a0):
     FLAGS_xla_tpu_enable_lem_scheduler              @0x223c72a8 → 1 xref: lea @0x21360e44
     FLAGS_xla_tpu_explicit_evict_memory_limit_kib   @0x223c50d0 → 1 xref: lea @0x21360976
   Each xref is `lea FLAGS_…(%rip),%rdi ; mov %rbx,%rsi ; call RegisterCommandLineFlag@0x21114cc0`
   inside the registrar _GLOBAL__sub_I_tpu_compilation_environment.cc (0x2135cba0..0x21360ef0).
   (Contrast: impure_use_iteration_mask @0x223a7dd0 has 6 xrefs = 5 consumers + 1 registrar.)
2. Name-string xref scan: 0 `lea name-string` sites → no by-name FindCommandLineFlag at any call site.
3. The serialized TCE FileDescriptorProto descriptor_table_protodef_AiDtBo5TtCO @0xbf9d5d0 contains
   "TpuCompilationEnvironment", "mxu_latency", "AutoProto",
   "explicit_prefetch_memory_limit_kib" (@protodef+0x1a45f) — but NOT "enable_lem_scheduler",
   NOT "explicit_evict_memory_limit_kib". ⇒ FindFieldByName FAILS ⇒ the by-name reader cannot reach them.
```

Each flag's `FLAGS_` object is reloc-filled from `.rela.dyn` exactly like every other libtpu flag — `+0x08` name str, `+0x10` typename str, `+0x20` FlagOps (the `AutoOr` TypeId), `+0x28` HelpGen — and its default generator emits AUTO (`enable_lem_scheduler`: `movw $0` ⇒ AUTO; `explicit_evict`: `movq $0; movb $0,0x8` ⇒ `{0, has0}` ⇒ AUTO). They are well-formed flags with no reader.

| Flag | `FLAGS_` | Type | Sole xref | What actually drives the behavior |
|---|---|---|---|---|
| `xla_tpu_enable_lem_scheduler` | `0x223c72a8` | `AutoOr<bool>` (FlagOps `0x1d6b5840`) | `lea @ 0x21360e44` (registration) | runtime predicate `ModuleContainsLEMSparseCoreInstruction @ 0x13853280` |
| `xla_tpu_explicit_evict_memory_limit_kib` | `0x223c50d0` | `AutoOr<int64_t>` (FlagOps `0x1d700120`) | `lea @ 0x21360976` (registration) | the legacy per-version vmem-evict TCE fields (§3) |

### What actually drives the LEM scheduler

The LEM-scheduler decision is *not* the flag. In `RunHloScheduler @ 0x1096fac0`:

```c
function RunHloScheduler(module, env, target):                  // 0x1096fac0
    use_lem = offloader_util::ModuleContainsLEMSparseCoreInstruction(module)   // 0x13853280 → r8b
    cfg     = GetSchedulerConfig(module, env, target, /*bool=*/use_lem)        // 0x10974aa0
    est     = GetLatencyEstimator(target, cfg, env)             // 0x10974e00
              // → CostModelLatencyEstimator @ 0x10ff8a60  (the "LEM" cost estimator)
```

`ModuleContainsLEMSparseCoreInstruction` scans for `SparseCoreDataFormatOffloader` / `SparseCoreOffloadingOptions_OffloadFeature` instructions (`@ 0x21925478`/`0x21925488`). "LEM" here is Large-Embedding-Model SparseCore offload, and the LEM-aware scheduler is selected by the module *containing* such instructions — the vestigial flag's intended role.

> **NOTE —** the eviction-limit asymmetry is the sharpest signal: `explicit_prefetch_memory_limit_kib` IS a TCE proto field (`@ protodef+0x1a45f`, class B), but its twin `explicit_evict_memory_limit_kib` is NOT a field (class C). Whether the evict field was removed (deprecation) or never landed is not recoverable from the binary; both yield "unwired." A reimplementer wiring the prefetch path must not assume the symmetric evict path exists.

---

## 5. Worked Example — `impure_use_iteration_mask` → `RaggedDotExpander`

### Purpose

The end-to-end worked example shows a flag driving a compiler transform: `xla_tpu_impure_use_iteration_mask` (read **class (A)** — `FLAGS_`-pinned direct, the contrast to the registry-mediated path) selects the iteration-mask lowering of `RaggedDot` to a windowed ragged convolution. It demonstrates the whole flag→gate→pass→HLO chain a reimplementer must reproduce, and how a `FLAGS_`-pinned `AutoOr<bool>` is consumed inline rather than by name.

### Stage 1 — the gate

`RaggedDotExpanderShouldUseIterationMask @ 0x1d6b5d60` is the gate. Its decompiled body is small and exact:

```c
function RaggedDotExpanderShouldUseIterationMask(ObjectView<TCE> view, TpuTopology& topo):  // 0x1d6b5d60
    if *(int*)(*(view+8)) < 3:                              // TpuVersion >= 3 gate
        return false
    if cached_word(FLAGS_..._use_iteration_mask) set:       // FlagImpl+0x58 fast path (dword_223A7E28)
        return (cached & 0x101) != 0x100                    // AUTO=ON: true unless present-and-false
    return (FlagImpl::ReadOneWord(&FLAGS_xla_tpu_impure_use_iteration_mask) & 0x101) != 0x100
```

The `& 0x101; cmp 0x100; != ` test is the AUTO=ON polarity from [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) §3 Idiom B: AUTO (`0x000`) and explicit-true (`0x101`) both yield `true`; only explicit-false (`0x100`) yields `false`. Note this is read directly off the `FLAGS_` object via `FlagImpl::ReadOneWord @ 0x21111a60` (the `FlagImpl+0x58` cached word) — a class-(A) inline read, *not* `GetFieldValueIfNotDefault`. There is no by-name lookup here precisely because `impure_use_iteration_mask` has consumers that `lea` its `FLAGS_` object.

Two sibling gates share the `TpuVersion>=3` prefix: `ShouldUseIterationMask @ 0x1d6b5dc0` (used at the LLO `SpatialMajorConvolution` emit level; `use_iteration_mask` OR the masked-fusion skipper) and `ShouldEnableMaskedFusionIterationSkipper @ 0x1d6b5d20` (a plain-bool flag via `ReadOneBool`). The expander gate consults only `use_iteration_mask`.

### Stage 2 — pass wiring

`PostMainFusionHloOptimize @ 0x109673b3` builds the pass with three impure flags feeding the constructor:

```text
AddPass<RaggedDotExpander, bool, RaggedConvContractionMode, vector<string>, Target&>   ── 0x1096d2e0
  use_iteration_mask   ← RaggedDotExpanderShouldUseIterationMask        (0x109673b3 → ctor +0x08)
  contraction_mode     ← FLAGS_xla_tpu_impure_contract_ragged_conv_with (0x223a7cf8, RaggedConvContractionMode → +0x09)
  window_bounds (g,m,k,n) ← FLAGS_xla_tpu_impure_ragged_dot_window_bounds (0x223a7d58, vector<string> → +0x10/+0x20)
make_unique<RaggedDotExpander>  ── 0x1096e360 (0x30 bytes: vtable+0x00, bool+0x08, mode+0x09, vec+0x10/+0x20)
```

### Stage 3 — `RunImpl` validation + window parse

```c
function RaggedDotExpander::RunImpl(module):                    // 0x10fae060
    for inst in MakeComputationPostOrder(module) where inst is RaggedDot:
        require inst.ragged_dot_mode != kBatch                  // CHECK @0x8644813
        validate dims: 1 contracting, 1 lhs-non-contracting,
                       1 rhs-non-contracting, 0 batch           // CHECKs @0x9e726a5/70/3b, 0x9fc1ce3
        spec = RaggedConvSpec::FromRaggedDot(inst)              // closure @0x10fb2160
        if member.use_iteration_mask == 1:                      // cmpb $1, 0x8(this) @0x10faef92/0x10faf121
            require window_bounds.size() == 4                   // CHECK @0x9b28192
            window = PipelineWindowSpec{ g, m, k, n }           // safe_strto64_base, [0..3] @0xa117494/a109010/a115712/a108547
        ExpandRaggedDot(inst, spec, member.use_iteration_mask, window, ..., target)  // 0x10fafa20
```

### Stage 4 — the iteration mask (the value of the example)

`ExpandRaggedDot @ 0x10fafa20` replaces the `RaggedDot` with a windowed ragged **convolution** subgraph and, when `use_iteration_mask`, builds a boolean mask that zeroes the cross-group products the dense convolution computes. The mask generator `CreateOutputMask @ 0x10fb2900` is byte-confirmed:

```c
function CreateOutputMask(...):                                // 0x10fb2900
    iota   = CreateIota(output_index_dim)                       // 0x10fb2d86
    lo     = CreateBroadcast(group_lower_bound, out_shape)      // 0x10fb2d52
    hi     = CreateBroadcast(group_upper_bound, out_shape)      // 0x10fb2e9a
    ge     = CreateCompare(iota, lo, kGe=5)                      // 0x10fb2de5  (iota >= group_start)
    lt     = CreateCompare(iota, hi, kLt=?)                      // 0x10fb2ec2  (iota <  group_end)
    band   = CreateBinary(kAnd=13, ge, lt)                      // 0x10fb2f9f  (in-group range)
    mask   = CreateFusion(band)                                 // 0x10fb314b  (wrap the boolean mask)
    return mask
```

The decompile shows `CreateIota`, two `CreateBroadcast` (the per-group bounds), three `CreateCompare` (compare codes 4/5/5), one `CreateBinary 13` (`kAnd`), and a `CreateFusion` — exactly the `iota∈[group_start, group_end)` predicate. `CreateConvolutionSelectFusion @ 0x10fb31e0` then applies it:

```c
function CreateConvolutionSelectFusion(...):                   // 0x10fb31e0
    conv = CreateConvolve(ragged_conv operands)                 // 0x10fb4c9c
    sel  = CreateTernary(kSelect, mask, conv, broadcast_zero)   // 0x10fb4923/0x10fb51c2
    out  = CreateReduce(sel, AddComputation)                    // 0x10fb5346 (+ 0x10fb63a0)
    return CreateFusion(out)                                    // 0x10fb4a96/0x10fb5449
```

### Semantics

The ragged contracting dimension becomes a convolution spatial dimension with a `g/m/k/n` window. The dense convolution computes the full block-Cartesian product across groups; the iteration mask `Select( (iota >= group_start) AND (iota < group_end), conv, 0 )` zeroes every cross-group product so only each ragged group's valid range survives, and the `Reduce` sums the masked windows into the dense output. Setting `use_iteration_mask` (AUTO=ON when `TpuVersion>=3`) selects this masked lowering; disabling it (explicit `false`) takes the non-masked path. This is the full reimplementation contract for the example: the flag's polarity and gate, the pass's member layout, and the mask's HLO graph.

> **QUIRK —** the same flag is read by two *different* gates with the *same* polarity but different scope. `RaggedDotExpanderShouldUseIterationMask` consults only `use_iteration_mask`; `ShouldUseIterationMask @ 0x1d6b5dc0` (the LLO conv emit level) ORs it with the masked-fusion skipper. A reimplementer wiring one gate's behavior to the other will diverge on the skipper case. The flag is class (A) at both sites — read inline off `FLAGS_`, never by name.

---

## Related Components

| Component | Relationship |
|---|---|
| `GetFieldValueIfNotDefault<T> @ 0x1c6f1a80` | the by-name reflection reader — the registry-mediated read |
| `FlagFieldMappings @ 0x2257ef50` (ctor `0x1d753ce0`) | the flag↔field bridge that makes a name resolvable |
| `SetFieldFromFlagString @ 0x1d73fcc0` | the write side — CLI flag string → TCE proto field |
| `LegacyEvictions/PrefetchesFlagRegistry @ 0x22579860 / 0x225798d8` | per-`TpuVersion` field-name registries (canonical class-B example) |
| `RaggedDotExpander @ 0x10fae060` + `CreateOutputMask @ 0x10fb2900` | the worked flag→transform: iteration-mask ragged-conv lowering |
| `AutoOr<bool>::FromProtoOrDie @ 0xf795300` | the inline resolver class-(A) consumers use instead of the by-name path |

## Cross-References

- [overview.md](overview.md) — the three-layer config pipeline; this page owns the by-name (registry-mediated) read variant of Stage 3
- [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) — the offset-keyed `AutoOr` model; the contrast to the by-name read here, and the AUTO=ON polarity reused by the worked example
- [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md) — the byte-exact field#→offset→default map; the direct offset reads this page's by-name path bypasses
- [flag-families.md](flag-families.md) — the flag families and the `impure_…` family the worked example draws from
- [xla-flag-atlas.md](xla-flag-atlas.md) — the broader flag catalog and naming conventions
