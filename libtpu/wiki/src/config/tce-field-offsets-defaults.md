# TCE Field-Offsets & Flag Defaults

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, internal tag `libtpu_lts_20260413_b_RC00`). The ELF is **not** stripped; symbol names below are read verbatim from `.symtab`. Other libtpu builds will differ.*

## Abstract

The [`TpuCompilationEnvironment`](tpu-compilation-environment.md) (TCE) is the TPU-private master config proto — 1121 live fields, each one registered as an `absl::Flag`. Two questions remain after the [field dictionary](tce-field-dictionary-a.md) has named and typed every field: *where does each knob physically live in the materialized C++ object*, and *what literal value is it seeded to before any flag, AUTO arm, or per-codename overlay touches it*. This page owns both answers — the field# → struct-byte-offset map and the field# → flag-default-value map — plus the write-path functions that move a parsed value into the object.

The offset oracle is not a hand-disassembled accessor list. It is the protobuf fast-parse table `TpuCompilationEnvironment::_table_` (a `TcParseTableBase`), whose 1121-entry `FieldEntry` array carries the exact struct offset of every field, sorted by ascending field number. The default oracle is the union at `FlagImpl+0x48` inside each `FLAGS_<name>` object: either a relocation pointing at an `AbslFlagDefaultGenFor<name>::Gen` body (which `mov`s an immediate), or — when no reloc is present — eight inline literal bytes. 507 fields use a `Gen` function; 614 use an inline literal. The materialization driver, `CreateDefaultTpuCompEnv` @`0x1d73dfa0`, walks the descriptor and runs `GetFlagForField → ReadFlag → SetEnvField` once per field; the two surgical writers, `SetFieldFromFlagString` @`0x1d73fcc0` and `OverwriteFieldIfNotDefault` @`0x1d73f360`, override individual fields afterward.

The reader should treat this page as the bridge layer. The schema (name, type, wrapper enum, HOT tag) is on the [dictionary pages](tce-field-dictionary-a.md); the AUTO-arm resolution of message/oneof fields is on [AutoProto / AutoOr Resolution](autoproto-autoor-resolution.md); the structural overview of the object is on [TpuCompilationEnvironment](tpu-compilation-environment.md). Here, the field# is joined to *offset* and *default value*, and the flag-write path is traced.

For reimplementation, the contract is:

- **The offset formula** — how to compute `struct_offset(field N)` from the `TcParseTableBase` header constants and the `FieldEntry` array, deterministically, for any of the 1121 fields.
- **The default-value mechanism** — the `FlagImpl+0x48` union, the kGenFunc-vs-kOneWord discriminator (presence of a reloc), and how to decode each base type's immediate.
- **The seeding driver and the two overriders** — `CreateDefaultTpuCompEnv`'s per-field loop, and the `SetFieldFromFlagString` / `OverwriteFieldIfNotDefault` single-field write path with its "both set" conflict rule.
- **The default census** — which values are non-trivial (the 120 MiB combiner thresholds, the INT64_MAX fuel knobs, the MSA overlap-ratio triples, the 7 wrapper-enum defaults) so a reimplementer can reproduce a byte-identical default env.

| | |
|---|---|
| **Offset oracle** | `TpuCompilationEnvironment::_table_` @`0x21cfa9e0` (`.data.rel.ro`) |
| **`FieldEntry` array** | table `+0x370`, 1121 × 12 B, ascending field# |
| **`sizeof(TpuCompilationEnvironment)`** | `0x15e8` (5608 B); field data `+0xa8`..`+0x15e0` |
| **Default union** | `FlagImpl+0x48` per `FLAGS_<name>` (reloc ⇒ gen body, none ⇒ inline) |
| **Default split** | 507 `kGenFunc` / 614 `kOneWord` (1121 total, perfect 1:1 field↔flag) |
| **Seeding driver** | `CreateDefaultTpuCompEnv` @`0x1d73dfa0` |
| **Single-field writers** | `SetFieldFromFlagString` @`0x1d73fcc0`, `OverwriteFieldIfNotDefault` @`0x1d73f360` |
| **Default singleton** | `GetTpuCompEnvWithDefaultValues` @`0x1d73f100` (`NoDestructor`, lazy) |

---

## Field# → Offset: the Parse-Table Oracle

### Purpose

A serialized or in-memory `TpuCompilationEnvironment` is a flat 5608-byte C++ object. A consumer that wants to read a specific knob — say, the post-optimization pipeline string or the pipelined-loop-unroll Tristate — needs the byte offset of that field inside the object. The generated protobuf fast-parse table already encodes every offset; this section recovers the offsets from that table rather than from per-field accessor disassembly, so all 1121 are available, not just the handful that have hand-written gates.

### The `TcParseTableBase` header

`TpuCompilationEnvironment::_table_` @`0x21cfa9e0` opens with the standard protobuf `TcParseTableBase` header. Decoded byte-for-byte:

```text
TcParseTableBase @0x21cfa9e0
  +0x00  uint32  has_bits_offset      = 16     (0x10)   hasbits start at struct +0x10
  +0x02  uint16  extension_offset     = 0               no extensions
  +0x04  uint32  max_field_number     = 1218   (0x4c2)  matches the descriptor max
  +0x08  uint32  fast_idx_mask        = 0xf8
  +0x10  uint32  field_entries_offset = 0x370            FieldEntry array start
  +0x14  uint16  num_field_entries    = 1121
  +0x16  uint16  num_aux_entries      = 349              = the 349 message-typed fields
  +0x18  uint32  aux_offset           = 0x3800
```

`max_field_number` is **1218**, but `num_field_entries` is **1121**: the gap is the 97 declared-but-reserved field numbers. The `FieldEntry` array holds one entry per *live* field, so it is dense at 1121 entries even though the highest field number is 1218.

### The `FieldEntry` layout and the offset formula

Each `FieldEntry` is 12 bytes:

```text
FieldEntry (12 bytes, array @ table+0x370, 1121 entries)
  +0x00  uint32  offset      struct byte-offset of this field's storage
  +0x04  uint32  has_idx     presence-bit index (into the hasbit words at struct +0x10)
  +0x08  uint16  aux_idx     index into the aux array (message/enum types only)
  +0x0a  uint16  type_card   16-bit protobuf type card (see below)
```

The array is sorted by **ascending field number**, so `FieldEntry[i]` is the *i*-th live field. The offset of field number `N` is therefore:

```c
// Deterministic offset lookup — no disassembly needed per field.
uint32 struct_offset(int N):
    i = index_of(N)                       // = (N-1) - (count of reserved field#s below N)
    return FieldEntry[i].offset           // table+0x370 + 12*i, read uint32 at +0x00
```

Three offsets that were previously pinned by hand-disassembling consumer gates reproduce **exactly** from this table, validating the formula:

| Field# | Name | Index | Offset | Source of cross-check | Confidence |
|---|---|---|---|---|---|
| #132 | `xla_tpu_verify_or_assign_tiling_before_lowering` | 120 | `+0xDFC` | hand-pinned accessor gate | CERTAIN |
| #648 | `xla_tpu_post_optimization_pipeline` (region) | 591 | `+0x1328` | hand-pinned accessor gate | CERTAIN |
| #867 | `xla_tpu_enable_pipelined_loop_unroll` (region) | 787 | `+0x2f0` | hand-pinned accessor gate | CERTAIN |

> **CORRECTION (OFFMAP-1) —** an earlier ad-hoc analysis placed field **#2** (`xla_tpu_sdc_checker_instrument_megacore_fusion`, bool) "in the `+0x1206` region". The parse table places #2 at `+0xBC`. `+0x1206` (4614) is a *different* field — the collective-producer "must-fuse" bool that the producer-priority cost model reads. The hedged "region" attribution was imprecise; the `FieldEntry` array resolves it to the byte.

### The `type_card` cross-check

The 16-bit `type_card` in each `FieldEntry` is a 1:1 proxy for the field's proto type — every base type maps to exactly one card, and the per-card population reproduces the dictionary's type histogram with zero disagreement. This is an independent confirmation that the offset table and the [field dictionary](tce-field-dictionary-a.md) describe the same 1121 fields in the same order.

| Type | `type_card` | Count | Type | `type_card` | Count | Confidence |
|---|---|---|---|---|---|---|
| bool | `0x0011` | 418 | string | `0x0c15` | 37 | HIGH |
| int64 | `0x10d1` | 148 | float | `0x1893` | 34 | HIGH |
| message | `0x0416` | 349 | int32 | `0x1091` | 32 | HIGH |
| enum | `0x1891` | 74 | double | `0x18d3` | 14 | HIGH |
| | | | uint32 | `0x0891` | 11 | HIGH |
| | | | uint64 | `0x08d1` | 4 | HIGH |

> **NOTE —** the `has_idx` in each `FieldEntry` is recovered, but the packing of `has_idx` → (hasbit word offset, bit position) was **not** re-walked. It follows protobuf's standard sequential packing from `has_bits_offset=16`; the hasbit region spans struct `+0x10`..`~+0xb0` for ~1121 presence bits. A reimplementer reading field presence from a serialized env must derive this packing (one disassembly of `TpuCompilationEnvironment::Clear` confirms it). Marked LOW until walked.

---

## Field# → Default: the `FlagImpl+0x48` Union

### Purpose

Every TCE field is the typed mirror of an `absl::Flag` named `FLAGS_<field_name>`. The field's default — the value the materialized env carries before any command-line flag or per-codename overlay is applied — is the flag's registered default. This section locates that default inside the `FlagImpl` object and gives the decode rule per base type.

### `FlagImpl` layout

Each `FLAGS_<name>` is a 0x60-byte `absl::FlagImpl`:

```text
FlagImpl (0x60 bytes)
  +0x00  vtable (0x22040f18)
  +0x08  name (const char*)
  +0x10  type_name (const char*)
  +0x18  filename (const char*)
  +0x20  FlagOps<T> (op dispatch)
  +0x28  HelpGen
  +0x30  packed metadata
  +0x38  lazy sentinel (-1)
  +0x40  default-kind discriminator
  +0x48  DEFAULT-VALUE UNION  ← the literal lives here
```

The discriminator is *implicit in the relocation table*, not just the `+0x40` byte:

- **`kGenFunc`** — if an `R_X86_64_RELATIVE` reloc targets `+0x48`, the union holds a function pointer to `AbslFlagDefaultGenFor<name>::Gen`. That body writes the default into the caller's buffer with a single `mov imm`. **507 fields.**
- **`kOneWord`** — if no reloc targets `+0x48`, the eight bytes *are* the literal value. **614 fields.**

### The `Gen` body decode

A `Gen` body is the smallest possible function: store an immediate, return. Four byte-confirmed examples (decompiled bodies, exact addresses):

```c
AbslFlagDefaultGenForxla_tpu_register_selection_policy::Gen:   // 0x1d723540
    *(uint8*)dst = 6;                       // RegSelectPolicy DISREGARD_RECENTLY_USED

AbslFlagDefaultGenForxla_msa_enable::Gen:                      // 0x1d705500
    *(uint8*)dst = 2;                       // Tristate ENABLED

AbslFlagDefaultGenForxla_tpu_msa_inefficient_use_to_copy_ratio::Gen: // 0x1d721c60
    *(uint32*)dst = 0x3F000000;             // 1056964608 == 0.5f

AbslFlagDefaultGenForrematerialization_algorithm::Gen:        // 0x1d718ee0
    *(uint64*)dst     = 0x7464697765657274; // "treewidt"
    *(uint64*)(dst+8) = 104;                // 'h' + SSO terminator → "treewidth"
    *(uint64*)(dst+16)= 0x0900000000000000; // std::string SSO length byte (9)
```

The instruction skeleton tells the type directly:

| Pattern | Type | Example |
|---|---|---|
| `c6 07 imm8 c3` (`movb`) | bool / 1-byte enum | `register_selection_policy = 6` |
| `c7 07 imm32 c3` (`movl`) | float / int32 / uint32 / 4-byte enum | ratio `0x3F000000 = 0.5f` |
| `48 c7 07 …` / `48 b8 …` (`movabs`) | int64 / uint64 / double | `vliw_fuel = 0x7fffffffffffffff` |
| `c5 f8 57 c0 …` / movabs-SSO | string (SSO / empty) | `"treewidth"` |
| `66 c7 07 0000 c3` / `vxorps` | empty AutoProto / message | oneof-case 0 = AUTO |

For `kOneWord` fields, the eight bytes at `+0x48` are read directly — e.g. `xla_tpu_arf_combiner_threshold_in_bytes` carries inline `0x0000000007800000` (= 125,829,120 = 120 MiB), and `xla_jf_loop_trip_count` carries inline `4`.

### Default census

The 1121 defaults break down by base type as follows. The census is the reimplementer's checklist: reproduce these distributions and the named non-trivial values, and the default env is byte-identical.

| Type | Count | Default distribution | Confidence |
|---|---|---|---|
| bool | 418 | 153 **true**, 265 **false** | HIGH |
| enum | 74 | 67 Tristate (21 ENABLED, 37 AUTO, 9 DISABLED) + 7 wrapper enums | HIGH |
| int64 | 148 | 108 non-{0,−1}; combiner thresholds, BRKGA limits, INT64_MAX fuel knobs | HIGH |
| int32 | 32 | 18 non-{0,−1}; trip counts, send/recv limits | HIGH |
| float | 34 | MSA overlap ratios, megacore margins, oblongness 50.0 | HIGH |
| double | 14 | MSA scaling factors | HIGH |
| string | 37 | 30 empty `""`, 7 non-empty literals | HIGH |
| message | 349 | **all** empty/AUTO instance (oneof-case 0) | HIGH |
| uint32 | 11 | — | HIGH |
| uint64 | 4 | — | HIGH |

> **GOTCHA —** the registered absl flag default is the **byte-authoritative** source for a field's default. Help-string text is *not*. Two fields, `xla_tpu_rwb_fusion` and `xla_tpu_accumulate_into_mrb`, were read `false` from `=value` help-string text by an earlier pass; their `FlagImpl+0x48` inline literal is `01 00 00 00` = **true** in both cases. The help string describes behavior, not the seeded default. When the two disagree, trust the union at `+0x48`.

---

## The Write Path

### `CreateDefaultTpuCompEnv` — the per-field seeding loop

`CreateDefaultTpuCompEnv` @`0x1d73dfa0` is the driver that materializes a fresh default env. It is *not* a static initializer list; it walks the descriptor and reads each field's flag default at runtime. Allocation, loop, and the two post-loop steps are all byte-confirmed:

```c
function CreateDefaultTpuCompEnv(sdm_fdo_config):           // 0x1d73dfa0
    env = operator new(0x15E8)                              // sizeof(TCE) = 5608
    TpuCompilationEnvironment::ctor(env, /*arena=*/0)
    md = GetMetadata(&TpuCompilationEnvironment_globals_)
    fields = md.descriptor.field_array                      // md+64, stride 88 B per field
    for field in fields:                                    // loop while i < md.field_count
        flag = TpuCompEnvReflection::GetFlagForField(field) // CHECK OK @reflection: line 5755
        value = TpuCompEnvReflection::ReadFlag(flag, field) // CHECK OK @line 5758
        if field.options.is_used_at_runtime:                // field+56 → options+125
            runtime_fields.push_back(field)                 // collected for the diff below
        TpuCompEnvReflection::SetEnvField(value, field, env) // CHECK OK @line 5765
    if sdm_fdo_config != null:
        env.flag_bits[25] |= 0x20                           // mark fdo-config present
        env.sparse_dense_matmul_fdo_config (+0x280) = sdm_fdo_config.CopyFrom()
    // Deprecated-flag tripwire:
    diff = MessageDifferencer(report_to_string)
    diff.CompareWithFields(env, GetTpuCompEnvWithDefaultValues(), runtime_fields)
    if diff != empty:
        LOG("[DEPRECATED_XLA_TPU_FLAG_USE] Deprecated TpuCompilationEnvironment "
            "flags were present and not matching their default values:\n" + diff)  // line 5776
    return env
```

The loop is the core mechanism: for each `FieldDescriptor`, `GetFlagForField` resolves the `absl::Flag` via a `FlagFieldMappings` Swiss-table keyed on the descriptor pointer (`_mm_crc32_u64` hash, `vpcmpeqb`/`vpmovmskb` group probe), `ReadFlag` dispatches to a `ReadFlagImpl<T>` template that pulls the flag's default through the flag vtable (`vtable+24` validate, `vtable+56` TypeId check against `FastTypeTag<T>`, `vtable+72` read default), and `SetEnvField` stores it at the field's offset. Each `GetFlagForField` / `ReadFlag` failure is a `CHECK`-fail (fatal), so a build with a flag missing for any field aborts here rather than silently using a wrong default.

> **QUIRK —** the FDO-config copy writes struct offset **`+0x280`** (640) and sets bit `0x20` of the flag byte at `env+25`. This is the one field `CreateDefaultTpuCompEnv` writes *outside* the descriptor loop — it is sourced from the caller's argument, not from a flag default. Every other field is seeded purely from its `FLAGS_<name>` default.

### `GetTpuCompEnvWithDefaultValues` — the cached default singleton

`GetTpuCompEnvWithDefaultValues` @`0x1d73f100` is a lazy `absl::NoDestructor` singleton guarded by a `__cxa_guard`. On first call it materializes the default env (via the same `$_0` lambda the descriptor loop uses), placement-news it into a static `NoDestructor` slot, then runs `SharedDtor` on the stack temporary. Subsequent calls return the cached pointer. It is the reference the two single-field overriders diff against to decide whether a field is "still default".

### `SetFieldFromFlagString` — parse-and-set a single field

`SetFieldFromFlagString` @`0x1d73fcc0` writes one field from a string value, given the `absl::CommandLineFlag` for that field:

```c
function SetFieldFromFlagString(flag, value_str, env):      // 0x1d73fcc0
    field = TpuCompEnvReflection::GetFieldForFlag(flag)      // reverse map; err @line 5905
    value = TpuCompEnvReflection::ParseFlagFromString(flag, field, value_str)
    if value is error:                                       // err @line 5908
        return error
    return TpuCompEnvReflection::SetEnvField(value, field, env)
```

`GetFieldForFlag` @`0x1d74ab20` is the inverse of `GetFlagForField` — the same `FlagFieldMappings` structure, keyed the other way. The parsed value is an 18-alternative `std::variant` (the scalar types plus `RangeSpecProto`, `RepeatedStrings`, `SparseDenseMatmulFdoConfig`, the MSA option messages, `AutoProto`, …); `SetEnvField` @`0x1d752ae0` visits the variant and stores into the field at its offset.

### `OverwriteFieldIfNotDefault` — the conflict-aware overrider

`OverwriteFieldIfNotDefault` @`0x1d73f360` takes a *source* field and a *destination* field and copies source→dest only when safe. It is used to migrate a value from a renamed/deprecated field onto its replacement without clobbering an explicitly-set replacement:

```c
function OverwriteFieldIfNotDefault(src_name, dst_name, env):  // 0x1d73f360
    md       = GetMetadata(&TpuCompilationEnvironment_globals_)
    dst_fd   = md.FindFieldByName(dst_name)   // err "Could not find field … " @line 5851
    src_fd   = md.FindFieldByName(src_name)   // err "Could not find field … " @line 5858
    defaults = GetTpuCompEnvWithDefaultValues()
    src_cur  = GetFieldValueAsString(src_fd, env)       // @line 5869
    src_def  = GetFieldValueAsString(src_fd, defaults)  // @line 5872
    if src_cur == src_def:
        return OK                             // source untouched → nothing to migrate
    dst_cur  = GetFieldValueAsString(dst_fd, env)       // @line 5885
    dst_def  = GetFieldValueAsString(dst_fd, defaults)  // @line 5888
    if dst_cur == dst_def:                    // dest still default → safe to overwrite
        value = GetFieldValue(src_fd, env)              // @line 5881
        return SetEnvField(value, dst_fd, env)
    // both non-default → conflict, keep dest:
    LOG("Both " + src_name + " and " + dst_name +
        " were set to non-default values; keeping the value of " + src_name)  // line 5890
    return OK
```

> **GOTCHA —** the conflict message at line 5890 says "*keeping the value of* `src_name`", but the code path that emits it does **not** write — it leaves the destination at its already-set non-default value. The log text names the source, yet the dest is what survives. A reimplementer copying this string verbatim must not also copy a misread of its semantics: when both are non-default, the **destination** is kept untouched and the source's value is discarded.

---

## Notable Default Values

These are the non-trivial defaults — the "magic numbers" a reimplementer must reproduce. Field numbers are from the [dictionary](tce-field-dictionary-a.md); offsets are from the parse table; values are byte-exact from `FlagImpl+0x48`.

### The 7 wrapper-enum defaults

The 67 Tristate-typed enums default per the [AUTO resolution](autoproto-autoor-resolution.md) split (21 ENABLED, 37 AUTO, 9 DISABLED). The 7 *non-Tristate* wrapper enums carry these specific defaults:

| Field# | Name | Enum | Default | Confidence |
|---|---|---|---|---|
| #31 | `xla_memory_scheduler` | `MemorySchedulerProto.Value` | 0 (DEFAULT) | HIGH |
| #132 | `xla_tpu_verify_or_assign_tiling_before_lowering` | `VerifyOrAssignTilingFlags.Value` | 1 (VERIFY) | HIGH |
| #487 | `xla_tpu_vmac_transform_strategy` | `TpuVmacTransformStrategy.Value` | 0 (NONE) | HIGH |
| #583 | `xla_tpu_sdc_checker_checksum_algo` | `ChecksumAlgoProto.Value` | 0 (DEFAULT) | HIGH |
| #631 | `xla_tpu_register_selection_policy` | `RegSelectPolicyProto.Value` | 6 (DISREGARD_RECENTLY_USED) | CERTAIN |
| #723 | `xla_tpu_precision_tracer_mode` | `PrecisionTracerModeProto.Value` | 0 (NONE) | HIGH |
| #827 | `xla_sc_async_wrapper_fusion_type` | `ScAsyncWrapperFusionType.Value` | 3 (SINGLE_TPU_CUSTOM_CALL) | HIGH |

`#631 = 6` is byte-confirmed against the `Gen` body @`0x1d723540` (`movb $0x6`).

### The 7 non-empty string defaults

The other 30 string fields default to `""`.

| Field# | Name | Default | Confidence |
|---|---|---|---|
| #198 | `xla_jf_hlo_deduplicate_only` | `"true"` | HIGH |
| #209 | `config_criterion` | `"min"` | HIGH |
| #212 | `rematerialization_algorithm` | `"treewidth"` | CERTAIN |
| #393 | `xla_tpu_nested_dot_fusion_supported_custom_ops` | `"PartialReduce"` | HIGH |
| #578 | `xla_tpu_alternate_memory_benefit_scaling_factor_for_large_buffers` | `"SQRT"` | HIGH |
| #656 | `xla_tpu_collect_sflag_wait_stats_filter` | `"all"` | HIGH |
| #739 | `xla_tpu_synthetic_compute_in_sflag_wait_filter` | `"all"` | HIGH |

`#212 = "treewidth"` is byte-confirmed against the `Gen` body @`0x1d718ee0` (movabs `0x7464697765657274` = "treewidt" + 'h').

### Notable integer defaults

| Field#(s) | Name | Default | Confidence |
|---|---|---|---|
| #55/56/57, #184 | arf / ars / agf / crs combiner threshold (bytes) | 125,829,120 (120 MiB) | HIGH |
| #58 | `xla_jf_crs_combiner_threshold_count` | 256 | HIGH |
| #41 | `xla_hlo_scheduling_brkga_generation_limit` | 1200 | HIGH |
| #42 | `xla_hlo_scheduling_brkga_computation_limit` | 3 | HIGH |
| #12/13 | net-router ring limits (cross-module / cross-replica) | 8 / 16 | HIGH |
| #18/19 | cmem max outstanding prefetches / evictions | 40 / 40 | HIGH |
| #40 | `xla_hbm_logging_buffer_size_bytes` | 1,048,576 (1 MiB) | HIGH |
| #74 | `xla_tpu_rematerialization_min_size_in_bytes` | 10,485,760 (10 MiB) | HIGH |
| #107 | `xla_jf_vliw_fuel` | INT64_MAX (unlimited) | HIGH |
| #128 | `xla_tpu_min_elements_for_while_loop_concat_code_motion` | INT64_MAX | HIGH |
| #149 | `xla_tpu_max_concurrent_send_recv` | INT32_MAX (2147483647) | HIGH |
| #151 | `xla_tpu_licm_analysis_allowance` | 100,000 | HIGH |
| #166 | `xla_jf_loop_trip_count` | 4 | CERTAIN |
| #255 | `xla_jf_overlay_compression_threshold` | 2,044,723,200 (`0x79e00000`) | HIGH |

`#166 = 4` and `#255 = 0x79e00000` are byte-confirmed inline literals (no reloc at `+0x48`).

### Notable float / double defaults

The MSA overlap-ratio triples (max / min / pref) repeat across the five memory families. They are the single most reused default pattern; reproduce them exactly per family.

| Family | Fields (max/min/pref) | max | min | pref | Confidence |
|---|---|---|---|---|---|
| jf | #280 / #284 / #285 | 32.0 | 1.0 | 2.0 | HIGH |
| vf | #442 / #446 / #447 | 8.0 | 1.0 | 2.0 | HIGH |
| gf | #540 / #544 / #545 | 8.0 | 1.0 | 2.0 | HIGH |
| cmem | #309 / #313 / #314 | 8.0 | 1.0 | 2.0 | HIGH |
| msa | #790 / #788 / #789 | 8.0 | 1.0 | 2.0 | HIGH |

Other notable scalars: `#592` MSA inefficient-use-to-copy ratio = **0.5** (byte-confirmed `movl 0x3F000000` @`0x1d721c60`), `#459` auto-SPMD memory-budget ratio = 1.1 (f32 `1.10000002`), `#389` megacore-fusion scaling factor = 2.0, `#319` copy-fusion pad/unpad ratio = 300.0, `#364` experimental max padding = 0.125 GiB, `#30` embedding-table oblongness = 50.0, `#176` fusion max vmem = 15.0 MiB.

> **NOTE —** these are the **flag-default baseline** only. The effective per-chip default is `baseline ⊕ overlay`: `ComputeMemorySpaceAssignmentOptions` and the target jump table overwrite the per-family MSA fields per TPU generation (v0/1→jf, v2→cmem, v3→vf, v4/5→gf). What a JAX/PJRT user gets on a given chip is the baseline composed with that overlay. The overlay itself is out of scope here; see the MSA per-codename material referenced from [TpuCompilationEnvironment](tpu-compilation-environment.md).

---

## What Is Not Covered Here

- **The has-bit packing.** `has_idx` per field is recovered, but the `has_idx` → (struct word, bit) map was not re-walked (LOW). Needed to read field *presence* from a serialized env.
- **The per-version overlay values.** This page is the flag-default baseline; the `ComputeMemorySpaceAssignmentOptions` family overrides are owned elsewhere.
- **The field#→name→type schema.** Owned by [TCE Field Dictionary (A)](tce-field-dictionary-a.md) / [(B)](tce-field-dictionary-b.md).
- **The AutoProto message-arm sub-defaults.** A message field defaults to its *empty* instance here; the 12 message-typed AutoProto arms each have their own sub-message defaults, owned by [AutoProto / AutoOr Resolution](autoproto-autoor-resolution.md).

---

## Cross-References

- [TpuCompilationEnvironment](tpu-compilation-environment.md) — the structural overview of the 5608-byte object these offsets and defaults populate
- [TCE Field Dictionary (A)](tce-field-dictionary-a.md) — field# → name → type schema (fields part 1); the schema this page joins offsets and defaults onto
- [TCE Field Dictionary (B)](tce-field-dictionary-b.md) — field# → name → type schema (fields part 2)
- [AutoProto / AutoOr Resolution](autoproto-autoor-resolution.md) — how message/oneof fields resolve their AUTO default arm at use time
- [XLA Flag Atlas](xla-flag-atlas.md) — the `FLAGS_<name>` flag registry that supplies every `+0x48` default
- [Configuration Overview](overview.md) — where the TCE sits in the config/compile-knob subsystem
