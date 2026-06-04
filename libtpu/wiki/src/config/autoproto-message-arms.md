# AutoProto Message-Arms

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

The `xla::jellyfish::AutoProto` oneof has 30 arms. The scalar arms (`bool`, `int64`, `int32`, `double`, …) carry a primitive value and are resolved by the present-bit packing on [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md). This page owns the *other* dozen — the **message-typed arms**, where the value an `AUTO` knob resolves to is not a number but an entire sub-message: `IlpLatencyHidingSchedulerOptions`, `ShardyOptions`, `EmitterLearnedCostModelOptions`, `BundleInstrumentationOptions`, `TpuCustomCallMemorySpaceSpec`, the five repeated-enum option sets, and the two repeated-primitive containers. Each arm is a real protobuf message with its own `TcParseTableBase` (`_table_`), its own default instance (`_globals_`), and its own constructor.

Two facts make these arms worth a page of their own. First, **every message-arm default instance is all proto-zero**, byte-confirmed: each `<Msg>_globals_` reads zero at every `_table_`-derived field offset, and each `<Msg>(Arena*)` constructor writes only zero immediates (the analogue of the scalar `AUTO`→`0x000` rule, but for a whole struct). There is no `DefaultDebugOptions`-style non-zero default writer anywhere in the twelve. So an unset (`AUTO`) message knob materializes a genuinely empty config — every inner `bool=false`, `int/double=0`, `enum=first-value(0)`, repeated field empty, sub-message absent. Second, two arms break that rule at the *consumer*, not in the proto: `SparseCoreAssertLevel` resolves `AUTO` to `assert_level::prod() = {values:[ALWAYS]}` and `TpuCustomCallMemorySpaceSpec` resolves `AUTO` to a `MsaReservationPolicy` sized from `Target::VmemSizeBytes`. The proto default is empty; the *effective* shipping default is not.

The reference frame is protobuf reflection. The reader who has built a `TextFormat` parser knows the shape: a `TcParseTableBase` header, a `FieldEntry` array sorted by field number, a `type_card` per field, and the `Reflection::Set*`/`Add*` families. The libtpu twist is the *ingest entry* (`Message::AbslParseFlagImpl`, the `text:`/`serialized:`/`base64:` prefix dispatch a JAX flag value runs through) and the per-arm `ParseFlag` flavor (pure-TextFormat vs custom comma-list vs preset/level-list). The page is structured as: the message-arm inventory and the shared `_table_`/`type_card` layout; the per-arm field dictionary and its all-zero default; the `TpuCustomCallMemorySpaceSpec` oneof (the one arm with an internal oneof); the `text:`-form SET path (`AbslParseFlagImpl` → `TextFormat` → `Reflection::Add*`); and the two consumer-side defaults that override the empty proto.

For reimplementation, the contract is:

- **The message-arm inventory and layout** — the twelve arm types, each with a `_table_` (`FieldEntry` array, `type_card` per field) and an all-zero `_globals_` default instance; the `type_card` encoding extended with the repeated cardinality bit and the oneof bit `0x20`.
- **The per-arm field dictionary and its empty default** — every inner field's number/name/proto-type/struct-offset/`type_card`, and the proof (ctor disassembly) that the default is proto-zero for all twelve.
- **The `text:`-form SET path** — `Message::AbslParseFlagImpl` format dispatch, `TextFormat::Parser::ParseFromString` against the freshly-`Clear()`'d empty instance, and the `is_repeated` (FieldDescriptor bit `0x20`) → `Reflection::Add*` vs `Set*` split that lands each value; plus the three per-arm `ParseFlag` ingest flavors and the two consumer-side `AUTO` overrides.

| | |
|---|---|
| **Arm count** | 12 message-typed arms (of the 30-arm `AutoProto` oneof) |
| **Shared layout** | each `<Msg>::_table_` = `TcParseTableBase` + 12-byte `FieldEntry{u32 off; u32 has_idx; u16 aux_idx; u16 type_card}`, sorted by field# |
| **Default rule** | every `<Msg>_globals_` all proto-zero; ctors write zero-only (no non-zero default writer) |
| **`type_card` (message)** | singular message `0x0416`; oneof-member message `0x0436` (`= 0x0416 \| 0x20`) |
| **Deepest tree** | `EmitterLearnedCostModelOptions` — 9 fields incl. an 8-field `LearnedCostModelClientOptions` sub-message (sibling-file descriptor) |
| **Internal oneof** | `TpuCustomCallMemorySpaceSpec.policy` — `{msa_reservation_policy, hbm_policy}` mutually exclusive |
| **SET entry** | `proto2::Message::AbslParseFlagImpl @ 0x20ef2120` — `text:`/`serialized:`/`base64:` dispatch |
| **text: ingest** | `TextFormat::Parser::ParseFromString @ 0x20efd420` → `ConsumeFieldValue @ 0x20f07a60` (is_repeated bit `0x20` → `Add*` vs `Set*`) |
| **Consumer overrides** | `assert_level::prod() @ 0x1db1d8a0` → `[ALWAYS]`; `ResolveMemorySpaceSpec @ 0x11036320` → MSA sized to default scoped VMEM |
| **Descriptors** | `tpu_compilation_environment.proto` FDP `@ 0xbfa6060` (11 arms); `emitter_learned_cost_model_options.proto` FDP `@ 0xbfc7bc0` + `learned_cost_model_client_options.proto` FDP `@ 0xbfc8160` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The Message-Arm Inventory

### Purpose

Twelve of the thirty `AutoProto` oneof arms are message-typed (`type_card` family `0x04xx`), not scalar. When a TCE knob backed by one of these arms is left at `AUTO` (`oneof_case_ == 0`, see [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md)), the consumer accessor (Idiom E on the resolution page) constructs the arm's *empty default instance* — `IlpLatencyHidingSchedulerOptions(arena)`, `ShardyOptions(arena)`, and so on. The value of an unset message knob is therefore whatever that empty instance contains. This section fixes the shared layout; §2 gives each arm's field dictionary and proves the default is empty.

### The shared `_table_` layout

Each message arm is a generated protobuf message with the standard `TcParseTableBase` header and a `FieldEntry` array — the same layout the TCE master table and the `AutoProto` oneof itself use (the third independent byte-confirmation that the `TcParser` layout is consistent across this proto family).

```text
<Msg>::_table_   (TcParseTableBase, in .data.rel.ro)
  +0x00  u16  has_bits_offset       ── byte offset of the _has_bits_ word in the message
  +0x04  u32  max_field_number
  +0x10  u16  field_entries_offset  ── byte offset of the FieldEntry array within this table
  +0x14  u16  num_field_entries
  +0x16  u16  num_aux_entries       ── count of aux sub-table pointers (for message fields)
  +0x18  u32  aux_offset            ── byte offset of the aux pointer array within this table

FieldEntry  (12 bytes each, sorted ascending by field number)
  +0x00  u32  offset      ── the field's C++ struct offset within the message
  +0x04  u32  has_idx     ── has-bit index (128..) for singular; oneof _case_ word offset for oneof members
  +0x08  u16  aux_idx     ── index into the aux table (message fields → nested <Sub>::_table_)
  +0x0a  u16  type_card   ── proto type + cardinality (table below)
```

The `type_card` encoding extends the scalar table from [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) with the repeated cardinality bit (low 3 bits: singular `=1`, repeated `=2`) and, for oneof members, the `0x20` oneof bit:

```text
singular : bool 0x0011  int32 0x1091  int64 0x10d1  uint64 0x08d1  double 0x18d3
           float 0x18b3  string 0x0c15  enum 0x1891  message 0x0416
repeated : enum 0x18a2   int64 0x10e2  string 0x0d25
oneof    : message 0x0436   (= singular message 0x0416 | 0x20)   — TpuCustomCallMemorySpaceSpec only
```

### Inventory

The twelve message arms, with their parse table, default instance, and constructor. The flag-name column is the TCE knob(s) backed by each arm (cross-join from [registry-mediated-flags.md](registry-mediated-flags.md) and [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md)); a single message type can back several flags.

| Arm | `_table_` | `_globals_` | ctor | Fields | TCE flag(s) |
|---|---|---|---|---|---|
| `IlpLatencyHidingSchedulerOptions` | `0x21cfa308` | `0x223c8790` | `0x1db24d00` | 6 | `xla_tpu_ilp_latency_hiding_scheduler_options` |
| `CostModelFlagOptions` | `0x21cfa170` | `0x223c87e8` | `0x1db23d40` | 2 (repeated enum) | `xla_msa_cost_model_options`, `xla_tpu_fusion_cost_model_options`, `xla_tpu_latency_hiding_scheduler_cost_model_options` |
| `SparseCoreOffloadingOptions` | `0x21cfa110` | `0x223c85d8` | `0x1db23780` | 1 (repeated enum) | `xla_tpu_sparse_core_offloading_options` |
| `ShardyOptions` | `0x21cfa260` | `0x223c8650` | `0x1db24940` | 3 | `xla_shardy_options` |
| `EmitterLearnedCostModelOptions` | `0x21cff9a8` | `0x223c9710` | `0x1db63f20` | 9 (+ 8-field sub-msg) | `xla_tpu_emitter_learned_cost_model_options` |
| `AccumulatorTransformations` | `0x21cf9c30` | `0x223c88d8` | `0x1db20fe0` | 1 (repeated enum) | `xla_tpu_accumulator_transformations` |
| `SparseCoreAssertLevel` | `0x21cfa550` | `0x223c8608` | `0x1db252e0` | 1 (repeated enum) | `xla_sc_assert_level` |
| `BundleInstrumentationOptions` | `0x21cfa5b0` | `0x223c8860` | `0x1db258a0` | 3 | `xla_tpu_bundle_instrumentation_options` |
| `TpuCustomCallMemorySpaceSpec` | `0x21cfa708` | `0x223c8920` | `0x1db25fa0` | 2 (oneof, +nested) | `xla_tpu_tpu_custom_call_memory_space_spec` |
| `BufferContentsSanitizerConfig` | `0x21cf9f58` | `0x223c8890` | `0x1db23040` | 2 | TCE field #623 (arm 10 per the oneof map) |
| `RepeatedStrings` | `0x21cf9d18` | — | — | 1 (repeated string) | `xla_explicit_disable_passes` (#900), `xla_explicit_enable_passes` (#901), `xla_tpu_enable_mosaic_emitters`, `xla_tpu_block_summary_split_specs` |
| `RepeatedIntegers` | `0x21cf9da0` | — | — | 1 (repeated int64) | `xla_tpu_distributed_hash_moduli`, `xla_tpu_reserved_sparse_cores` |

> **NOTE —** a thirteenth `AutoOr<message>` type, `CostModelLoggingOptions` (`_globals_ @ 0x223c87c8`, ctor `@ 0x1db24600`, 2 bools both default `false`), is *not* one of the 30 TCE oneof arms — it is the non-TCE `xla_tpu_impure_cost_model_logging_options` flag, decoded here only for completeness. Its non-TCE resolver/consumer is not traced (LOW).

> **GOTCHA —** the parent `AutoProto::_table_` (`@ 0x21cfa788`) and the per-arm tables sit adjacent in `.data.rel.ro` because the protos share a translation unit. Cross-reference an arm by its symbol (`<Msg>::_table_`), never by eyeballing the address — the arm tables, the `AutoProto` table, and the 1121-field TCE master table (`@ 0x21cfa9e0`) are interleaved in the same band.

---

## 2. Per-Arm Field Dictionaries and the Empty Default

### Purpose

For each arm, the inner field dictionary (number / name / proto-type / struct-offset / `type_card`) plus the byte-confirmed default value of each field. Two independent byte sources were cross-validated field-for-field with **zero disagreements**: the generated C++ `<Msg>::_table_` `FieldEntry` array (gives offsets and `type_card`s) and the carved `FileDescriptorProto` (gives the field numbers, names, and proto-types). The `_table_` is the binary struct layout; the FDP is the human field names; together they are the complete dictionary.

The default-value column is uniform across all twelve arms: **AUTO ⇒ empty default-instance ⇒ proto-zero per field**. Each `<Msg>_globals_` was read at its `_table_` offsets (every byte zero) and each `<Msg>(Arena*)` was disassembled to confirm zero-only stores.

### The all-zero default proof

`IlpLatencyHidingSchedulerOptions(Arena*) @ 0x1db24d00` is the canonical shape — arena into metadata, vtable, then a single vectorized zero of the field region:

```c
function IlpLatencyHidingSchedulerOptions(this, Arena*):   // 0x1db24d00
    this[+0x08] = arena                  // InternalMetadata tagged ptr
    this[+0x00] = &vtable+0x10
    *(ymm*)(this + 0x10) = 0             // vxorps/vmovups — zero 32 B (has-bits + fields)
    *(u32*)(this + 0x2f) = 0             // zero the field tail
    return                               // NO non-zero immediate store anywhere
```

No `movb $imm`/`movl $imm` with a non-zero immediate appears in any of the twelve constructors or `Clear()` bodies — unlike `DefaultDebugOptions` (see [default-debugoptions.md](default-debugoptions.md)), which writes ~165 non-zero immediates. The repeated-field arms load a 16-byte init constant from `@ 0xa2cc520` (`00*8, f0 ff ff ff, 00*4` — the shared empty-message / `RepeatedField` init), also pure zero. `EmitterLearnedCostModelOptions(Arena*) @ 0x1db63f20` additionally points its two string fields at `proto2::internal::fixed_address_empty_string` (`= ""`), still the zero default.

> **GOTCHA —** the `f0 ff ff ff` / `d8 ff ff ff` bytes visible in a `<Msg>_globals_` hexdump are *not* field values. They are the `MessageLite` internal `_cached_size_` / `RepeatedField`-rep / oneof-init sentinels living *before* the field-data region (which starts at `+0x18` for the `has_bits_offset=16` arms). A reimplementer reading defaults straight from `_globals_` must start at the `_table_`-derived field offset, never at `+0x10`.

### Scalar / mixed arms

```text
§ IlpLatencyHidingSchedulerOptions   _table_ 0x21cfa308 · sizeof 0x38 · 6 fields · all default 0/false
  #1 enable_ilp_latency_hiding_scheduler  bool    off=0x30 has=131 tc=0x0011   false
  #2 max_solver_deterministic_time        double  off=0x18 has=128 tc=0x18d3   0.0
  #3 computation_size_threshold           int64   off=0x20 has=129 tc=0x10d1   0
  #4 use_ilp_schedule_sequence            bool    off=0x31 has=132 tc=0x0011   false
  #5 also_minimize_total_lifetime         bool    off=0x32 has=133 tc=0x0011   false
  #6 min_compute_latency                  uint64  off=0x28 has=130 tc=0x08d1   0

§ ShardyOptions                      _table_ 0x21cfa260 · 3 fields · all false
  #1 enable_explicit_collectives           bool  off=0x18 has=128 tc=0x0011   false
  #2 dedup_functions_fully                 bool  off=0x19 has=129 tc=0x0011   false
  #3 enable_native_non_flat_support        bool  off=0x1a has=130 tc=0x0011   false

§ BundleInstrumentationOptions       _table_ 0x21cfa5b0 · 3 fields · all 0/false
  #1 trace_best_effort_frequency           int64 off=0x18 has=128 tc=0x10d1   0
  #2 trace_guaranteed_frequency            int64 off=0x20 has=129 tc=0x10d1   0
  #3 trace_branches                        bool  off=0x28 has=130 tc=0x0011   false

§ BufferContentsSanitizerConfig      _table_ 0x21cf9f58 · 2 fields
  #1 cores_to_sanitize    enum[REPEATED] off=0x18 has=128 tc=0x18a2   [] (empty)
  #2 sanitizer_mode       enum           off=0x2c has=129 tc=0x1891   0 (DEFAULT)
  ENUM CoreToSanitize: INVALID=0, TC=1, SC_SCS=2, SC_TILE=3
  ENUM SanitizerMode: DEFAULT=0, LOCAL_ONLY=1, CROSS_CORE_ONLY=2
```

### Repeated-enum / repeated-primitive arms

These arms' single (or first) field is a repeated enum. The default is the **empty list** — no feature/op/level selected — *not* "value 0 is selected", even though each enum's value 0 (`ALL` / `FEATURE_UNSPECIFIED` / `NONE` / `INVALID`) exists. The empty-list default is the only AUTO answer for `CostModelFlagOptions`, `SparseCoreOffloadingOptions`, `AccumulatorTransformations`, `BufferContentsSanitizerConfig.cores_to_sanitize`, and `RepeatedStrings`/`RepeatedIntegers`; `SparseCoreAssertLevel` is the exception whose *consumer* injects a non-empty default (§5).

```text
§ CostModelFlagOptions               _table_ 0x21cfa170 · 2 fields · both [] (empty)
  #1 ops_to_use_bundle_aware_cost_model  enum[REP] off=0x18 has=128 tc=0x18a2
  #2 ops_to_use_codegen_windows          enum[REP] off=0x30 has=129 tc=0x18a2
  ENUM OpType: ALL=0, OUTPUT_FUSION=1, CONV_LOWERABLE=2, LOOP_FUSION=3

§ SparseCoreOffloadingOptions        _table_ 0x21cfa110 · 1 field · [] (empty)
  #1 features  enum[REP] off=0x18 has=128 tc=0x18a2
  ENUM OffloadFeature: FEATURE_UNSPECIFIED=0, OP_TRIGONOMETRY=1, OP_SELECT_BF16=2,
    SHAPE_1D_PADDING=3, STANDALONE_OP_DATA_FORMAT=4, FUSION=5, LEM_DATA_FORMAT=6,
    EXPERIMENTAL_FEATURES=7

§ AccumulatorTransformations          _table_ 0x21cf9c30 · 1 field · [] (empty)
  #1 values  enum[REP] off=0x18 has=128 tc=0x18a2  → .xla.jellyfish.AccumulatorTransformation.Value
  ENUM Value: NONE=0, MULTIPLY_ADD_FULLBANDWIDTH=1, MULTIPLY_ADD_HALFBANDWIDTH=2,
    CUMULATIVE_SUM=3, CUMULATIVE_MAX=4, MAX_ABS=5, MULTIPLY_ADD_FULLBANDWIDTH_V2=6

§ SparseCoreAssertLevel               _table_ 0x21cfa550 · 1 field · [] (empty)*  (*see §5)
  #1 values  enum[REP] off=0x18 has=128 tc=0x18a2  → .xla.jellyfish.SparseCoreAssertLevel.Value
  ENUM Value: NONE=0, ALWAYS=1, BOUNDS=2, CSRS=3, CHECKSUMS=4, SYNC_FLAGS=5, STREAMS=6,
    DMA=7, ALL_TO_ALL=8, RADIX_SORT=9, OVERLAYS=10, RUN_IDS=11, VECTOR_LOADS=12,
    VECTOR_STORES=13, SCALAR_LOADS=14, SCALAR_STORES=15, MASKS=16, CONTINUATIONS=17

§ RepeatedStrings   _table_ 0x21cf9d18 · 1 field #1 values string[REP] off=0x18 tc=0x0d25 · [] (empty)
§ RepeatedIntegers  _table_ 0x21cf9da0 · 1 field #1 values int64 [REP] off=0x18 tc=0x10e2 · [] (empty)
```

> **QUIRK —** the empty-list default means a repeated-enum arm at AUTO selects *nothing*, but the enum's value `0` is a real, meaningful first value (`CostModelFlagOptions.OpType.ALL=0` would mean "all ops" *if present*). A reimplementation that materializes a singleton `[0]` instead of `[]` silently enables a feature the binary leaves off. The default is the absence of the field, not its first value.

### The deepest tree — `EmitterLearnedCostModelOptions`

This arm's descriptor lives in a *sibling* proto file (`emitter_learned_cost_model_options.proto` FDP `@ 0xbfc7bc0`), not in `tpu_compilation_environment.proto`, and pulls in `learned_cost_model_client_options.proto` (FDP `@ 0xbfc8160`) for its #2 sub-message. An AUTO learned-cost-model knob is a 2-level-empty default: 9 outer fields zero, including an empty 8-field `LearnedCostModelClientOptions` whose own enums all default to `*_UNSPECIFIED=0`.

```text
§ EmitterLearnedCostModelOptions     _table_ 0x21cff9a8 · 9 fields · all default 0/false/""/empty
  #1 enable_learned_cost_model           bool    off=0x38 has=132 tc=0x0011   false
  #2 learned_cost_model_client_options   message off=0x28 has=130 tc=0x0416   absent
                                         → .xla.jellyfish.LearnedCostModelClientOptions
  #3 max_num_considered_windows          int64   off=0x30 has=131 tc=0x10d1   0
  #4 dump_fusion_data_proto              bool    off=0x39 has=133 tc=0x0011   false
  #5 db_path                             string  off=0x18 has=128 tc=0x0c15   "" (empty)
  #6 db_query_type                       enum    off=0x3c has=134 tc=0x1891   0 (DB_QUERY_TYPE_NONE)
  #7 cost_model_mode                     enum    off=0x40 has=135 tc=0x1891   0 (..._MODE_INVALID)
  #8 ml_output_validation_strategy       enum    off=0x44 has=136 tc=0x1891   0 (..._STRATEGY_NONE)
  #9 dump_fusion_data_proto_dir          string  off=0x20 has=129 tc=0x0c15   "" (empty)
  ENUM LearnedCostModelMode: ..._INVALID=0, ..._ONLY_ML_PREDICTION=1, ..._ONLY_DB=2,
    ..._DB_WITH_FALLBACK_TO_ML_PREDICTION=3, ..._ONLY_DATA_COLLECTION=4
  ENUM DbQueryType: DB_QUERY_TYPE_NONE=0, DB_QUERY_TYPE_REPLAY_PREDICITIONS=1, DB_QUERY_TYPE_GROUND_TRUTH=2
  ENUM MLOutputValidationStrategy: ..._NONE=0, ..._NEVER_TRUST=1, ..._ALWAYS_TRUST=2, ..._NO_NEGATIVE_CYCLES=3

  SUB-MESSAGE LearnedCostModelClientOptions (FDP @0xbfc8160; 8 fields; all empty/0):
    #1 embedding_service_type                  enum (ServiceType)
    #2 remote_embedding_server_address         string
    #3 remote_embedding_model_name             string
    #4 inflight_rpc_monitoring_interval_milliseconds  int32
    #5 local_embedding_model_path              string
    #6 embedding_cache_path                    string
    #7 fusion_data_proto_generation_options     message (FusionDataProtoGenerationOptions: 2 bools)
    #8 max_batch_size                          int32
    ENUM ServiceType: SERVICE_TYPE_UNSPECIFIED=0, SERVICE_TYPE_LOCAL=1, SERVICE_TYPE_REMOTE=2
```

The enum value-sets are decoded here (the structured knobs that drive the learned cost model); they are cross-linked from the cost-model documentation. The two enum-default semantics worth flagging: `cost_model_mode=0` is `LEARNED_COST_MODEL_MODE_INVALID` (an explicit *invalid*, not a benign default), and the field is only meaningful once `enable_learned_cost_model` is `true` — which it is not by default.

---

## 3. The `TpuCustomCallMemorySpaceSpec` Oneof

### Purpose

`TpuCustomCallMemorySpaceSpec` is the one message arm with an *internal* oneof. Its two message fields — `msa_reservation_policy` and `hbm_policy` — are members of a single oneof named `policy`, so they are mutually exclusive: exactly one (or neither) is set. This is the AutoProto-oneof mechanism (off `0x10`, case at `+0x1c`, oneof bit `0x20`) recursing one level — a miniature 2-arm oneof built on the same `TcParser` machinery as the 30-arm `AutoProto` oneof itself.

### Layout

```text
TpuCustomCallMemorySpaceSpec object   (sizeof 0x20)
  +0x00  vtable ptr (→ vtable+0x10 @0x21cf9740)
  +0x08  Arena / InternalMetadata tagged ptr
  +0x10  oneof union (8 B): MsaReservationPolicy* | HbmPolicy*  (whichever arm is active)
  +0x18  unused has-bits word (ctor zeros it)
  +0x1c  oneof _case_ : 0 = unset, 1 = msa_reservation_policy, 2 = hbm_policy
```

```text
TpuCustomCallMemorySpaceSpec::_table_ @ 0x21cfa708
  has_bits_offset=0x18, max_field=2, num_aux=2, aux@table+0x68
  field#1 msa_reservation_policy  off=0x10  case_off=0x1c  aux=0  tc=0x0436 (oneof-msg)
            → aux[0] MsaReservationPolicy::_table_ @ 0x21cfa658
  field#2 hbm_policy              off=0x10  case_off=0x1c  aux=1  tc=0x0436 (oneof-msg)
            → aux[1] HbmPolicy::_table_ @ 0x21cfa6b8

Nested MsaReservationPolicy  _table_ @ 0x21cfa658  (_globals_ @ 0x223c8538)
  #1 msa_reservation_size_bytes  uint64  off=0x18  tc=0x08d1   default 0
Nested HbmPolicy             _table_ @ 0x21cfa6b8  (_globals_ @ 0x223c8558)  0 fields — marker message
```

### Three byte sources confirm the oneof

Both fields sit at struct off `0x10` (a single 8-byte union pointer) with `has_idx=0x1c` (the oneof `_case_` *word offset*, not a has-bit — a has-bit would be `128+`) and `type_card=0x0436` (the singular-message `0x0416` with the `TcParser` oneof bit `0x20` set). The `0x0436` value is cross-confirmed against the `AutoProto` master oneof, whose every arm uses the oneof family (`bool 0x0031`, `int64 0x10f1`, `string 0x0c35`, `message 0x0436` — each `= singular | 0x20`).

The generated methods prove single-arm semantics — each switches on the case word at `+0x1c` and touches exactly one arm:

```c
function clear_policy(this):                 // 0x1db25f00
    switch this->_case_ {                     // mov 0x1c(%rdi),%eax
      case 2: ~HbmPolicy; free(0x18 bytes)
      case 1: ~MsaReservationPolicy; free(0x20 bytes)
    }
    this->_case_ = 0                          // movl $0,0x1c

function _InternalSerialize(this, ...):      // 0x1db263c0
    switch this->_case_ {                     // mov 0x1c,%edi
      case 1: WriteMessage(field 1, msa @ rep+0x14)
      case 2: WriteMessage(field 2, hbm @ rep+0x10)
    }                                         // exactly one message field emitted, never both
```

`Clear() @ 0x1db26300` and `ByteSizeLong() @ 0x1db26420` mirror the same case switch; the ctor `@ 0x1db25fa0` does `movq $0,0x18(%rdi)` (zeroes the unused has-bits word and the case discriminator → case 0 = unset). The carved FDP (`@ 0xbfa6060`) confirms the oneof at the schema level: `oneof_decl[0].name="policy"`; both fields carry `oneof_index=0`. The `.rodata` strings `"policy"`, `"msa_reservation_policy"`, `"hbm_policy"`, `"msa_reservation_size_bytes"` corroborate.

> **QUIRK —** the shared `off=0x10`/`case=0x1c` fingerprint is exactly the `AutoProto` oneof's own layout (see [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md)). A reimplementer who treats `msa_reservation_policy` and `hbm_policy` as two independent `optional` sub-messages (both potentially present) will mis-size the struct (two pointers instead of one union word) and mis-serialize (two fields where the binary emits one). It is a oneof, not two optionals.

---

## 4. The `text:`-Form Message-Arm SET Path

### Purpose

The default of a message arm is empty (§2). This section is the *write* direction: how a JAX flag value — `--<flag>=text:<field>: <VAL>` and friends — mutates that empty default-instance into a populated config. The path is generic protobuf reflection ingest with a libtpu-specific entry (`AbslParseFlagImpl`) and three per-arm `ParseFlag` flavors.

### Entry Point

```text
AutoOr<Msg>::ParseFlag(value)                       ── per-arm, e.g. 0x1d747b00 (Ilp), 0x1d744f80 (CostModel)
  ├─ bcmp value vs "auto" → AUTO (empty default, present byte clear)
  └─ else construct Msg(arena) and run the arm's MESSAGE PARSER:
       Message::AbslParseFlagImpl @ 0x20ef2120       ── generic: Clear(), then format dispatch
         ├─ "text:"       → TextFormat::Parser::ParseFromString @ 0x20efd420
         │                    └─ ConsumeField @ 0x20f037e0 → ConsumeFieldValue @ 0x20f07a60
         │                         └─ is_repeated? Reflection::Add* : Reflection::Set*
         ├─ "serialized:"  → MessageLite::ParseFromString @ 0x21057460
         └─ "base64:"      → Base64Unescape @ 0x2116df80 then ParseFromString
```

### The flag-value parse (`AbslParseFlagImpl`)

`proto2::Message::AbslParseFlagImpl @ 0x20ef2120` is the generic message-flag parser. It first `Clear()`s the message (via vtable `*0x10`), then dispatches on the format prefix:

```c
function Message::AbslParseFlagImpl(this, value, &error):   // 0x20ef2120
    (*this->vtable[0x10])(this)                  // Clear() — start from the empty default
    if value[0] == ':': delimited = 1            // leading ':' = delimited-format specifier
    memchr(value, ':')                           // locate the format prefix
    split format-options on ',' (ByChar Splitter @ 0xe6d1240)
    switch prefix:                               // in-body immediates: "text"=0x74786574, "serialized", "base64"
      "text"       → TextFormat::Parser ctor @ 0x20efde40; ParseFromString @ 0x20efd420
      "serialized" → MessageLite::ParseFromString @ 0x21057460
      "base64"     → Base64Unescape @ 0x2116df80; then ParseFromString
      default      → text                         // no prefix = text
    // field-name collision: "Prefix `%s:` ... ambiguous with message fields" (Descriptor::FindFieldByName @ 0x20e57900)
    //                       "Invalid format `%s`."
```

The decompile confirms each branch: the `ParseFromString` serialized path, the `"Invalid base64 input."` error, the `TextFormat::Parser` ctor + `ParseFromString`, and the ambiguous-prefix error string (`"Prefix \`%s:\` used is ambiguous with message fields. ... use \`:text:\` as a prefix."`). The default (no prefix) is text.

### How a `text:` value becomes a field (the `Add*` vs `Set*` split)

Inside the TextFormat parser, the repeated-vs-singular decision per consumed value is `proto2::FieldDescriptor::is_repeated`, encoded as bit `0x20` of `FieldDescriptor+0x1`. The decompile of `ConsumeFieldValue @ 0x20f07a60` shows the test verbatim — `(*((_BYTE *)FieldDescriptor + 1) & 0x20) != 0` selecting the `Add*` branch over the `Set*` branch, per CPP type:

```c
function ConsumeFieldValue(this, msg, reflection, field):   // 0x20f07a60
    // ... parse one value of the field's CPP type ...
    if (field[1] & 0x20) != 0:                  // FieldDescriptor::is_repeated
        Reflection::Add<T>(reflection, msg, field, value)   // APPEND to RepeatedField at arm +0x18
    else:
        Reflection::Set<T>(reflection, msg, field, value)   // overwrite the singular slot
```

The decoded `Add*` / `Set*` families:

| CPP type | Repeated → | Singular → |
|---|---|---|
| double | `AddDouble` | `SetDouble` |
| float | `AddFloat` | `SetFloat` |
| int32 | `AddInt32 @ 0x20ecf940` | `SetInt32` |
| int64 | `AddInt64 @ 0x20ed03c0` | `SetInt64` |
| bool | `AddBool @ 0x20ed3900` | `SetBool` |
| string | `AddString` | `SetString` |
| enum | `AddEnum @ 0x20ed8860` (after `FindValueByName`/`AddEnumValue`) | `SetEnum @ 0x20ed8480` |

For an enum element the parser runs `ConsumeIdentifier` → `EnumDescriptor::FindValueByName`; a numeric token goes through `ConsumeSignedInteger` → `FindValueByNumber`, and a number with no matching descriptor entry is admitted via `AddEnumValue @ 0x20f06...` only when `!FieldDescriptor::legacy_enum_field_treated_as_closed` (the closed-enum guard). Each `Add*` appends one element to the `RepeatedField` living at the arm struct's `+0x18` — the offset §2 records for all five repeated arms. So `--xla_tpu_sparse_core_offloading_options=text:features: FUSION features: LEM_DATA_FORMAT` turns the AUTO-empty `SparseCoreOffloadingOptions` into a 2-element set.

The repeated short-list `field: [A, B, C]` is supported: `ConsumeField @ 0x20f037e0` has the `cmpb $0x5b` (`'['`) open-bracket path that loops `ConsumeFieldValue` over a comma-separated list, in addition to the per-occurrence `field: A field: B` form. Both append element-by-element; this is the standard proto TextFormat repeated grammar, not a TPU custom syntax.

> **GOTCHA —** the closed-enum tolerance for an unknown enum *name* in a repeated arm was not byte-traced to its error string (the `legacy_enum_field_treated_as_closed` branch is present but the error-vs-silent-skip behavior is unconfirmed — LOW). A reimplementer should not assume a mistyped `features:` name is rejected; it may drop to the `UnknownFieldSet`.

### Three per-arm ingest flavors

The arm's `AutoOr<Msg>::ParseFlag` decides what grammar the user spells. Three flavors were decoded from each arm's callee set:

| Flavor | Arms (`ParseFlag` VA) | Grammar |
|---|---|---|
| **A — pure TextFormat** (`AbslParseFlagImpl` only) | `CostModelFlagOptions` (`0x1d744f80`), `SparseCoreOffloadingOptions` (`0x1d745d80`), `IlpLatencyHidingSchedulerOptions` (`0x1d747b00`), `ShardyOptions` (`0x1d746e20`), `EmitterLearnedCostModelOptions` (`0x1d745680`), `BundleInstrumentationOptions` (`0x1d749ce0`), `TpuCustomCallMemorySpaceSpec` (`0x1d74a3c0`) | `--<flag>=text:<field>: <VAL>` (or `serialized:`/`base64:`) |
| **B — custom comma-list** (`AutoOrTypeTraits<T>::Parse`, + TextFormat fallback) | `RepeatedStrings` (Parse `0x1d746720`: `ByChar::Find` split → `RepeatedPtrFieldBase::Add<string>` per token), `RepeatedIntegers` (Parse `0x1d7446e0`: split → `safe_strto64_base @ 0x21173e20` → append int64), `AccumulatorTransformations` (Parse `0x1d748f40`: split → `ParseNamedEnum` → append enum) | `--<flag>=A,B,C` *or* `text:values: A` |
| **C — preset / level comma-list** (`assert_level::Parse @ 0x1db1e3e0`) | `SparseCoreAssertLevel` (`ParseFlag 0x1d7495e0`) — `ByChar(',')` split, each token via `StringToAssertLevel @ 0x1db1e8a0` (preset aliases) or `StringToEnum @ 0x1db1ec60` (individual level names) | `--xla_sc_assert_level=prod` (preset) *or* `=bounds,csrs,checksums` (level list) |

The flavor-B arms each *also* retain `AbslParseFlagImpl` as a fallback, so they accept both the comma-list short form and the full `text:` form.

> **NOTE —** the `EmitterLearnedCostModelOptions` *nested* sub-message ingest (the 2-level `text:learned_cost_model_client_options { embedding_service_type: SERVICE_TYPE_REMOTE … }` block) was not separately byte-walked; it uses the same generic `AbslParseFlagImpl` → `ConsumeFieldMessage` recursion (INFERRED-by-pattern — HIGH). The serialized:/base64: repeated-element wire encoding (packed vs unpacked) is likewise not traced (LOW); only the `text:` append path is byte-confirmed.

---

## 5. Consumer-Side `AUTO` Overrides

### Purpose

§2 proves the *proto* default of every message arm is empty. Two arms have a code-side default *on top of* the empty proto: the consumer accessor materializes the empty instance, then unconditionally substitutes a non-empty value when the AutoOr is AUTO/absent. For these two the *effective* shipping default is not the proto-empty value — a reimplementer who stops at the proto will get the wrong behavior.

### `assert_level::prod()` — the `SparseCoreAssertLevel` default

`GetSparseCoreAssertLevel @ 0x1d6b9ac0` (env+`0xb78` = `2936`) reads the `AutoProto*`, null-falls-back to `AutoProto_globals_ @ 0x223c8968` (case 0 ⇒ AUTO), resolves `AutoOr<SparseCoreAssertLevel>::FromProtoOrDie`, then *unconditionally* calls `assert_level::prod()` and uses it whenever the AutoOr is AUTO/absent:

```c
function GetSparseCoreAssertLevel(out, env):                // 0x1d6b9ac0
    p = env[+0xb78]                                          // AutoProto* (xla_sc_assert_level)
    if !p: p = &AutoProto_globals_                           // 0x223c8968 — AUTO
    AutoOr<SparseCoreAssertLevel>::FromProtoOrDie(autoor, p) // stack AutoOr
    chosen = assert_level::prod()                            // 0x1db1d8a0 — UNCONDITIONAL
    if autoor.present == 1:                                  // user supplied a value
        chosen = autoor.value
    SparseCoreAssertLevel(out, arena=0, chosen)             // copy chosen into the sret
```

`prod() @ 0x1db1d8a0` builds the empty `SparseCoreAssertLevel(arena=0)` then appends exactly one value, `1` (= `Value.ALWAYS`), to the repeated `values` field via `RepeatedField<int>::GrowNoAnnotate` + store. The decompile confirms the single `RepeatedField<int>` append and the `*((_BYTE*)this+16) |= 1u` presence set. So **the effective shipping default of an unset `xla_sc_assert_level` is `{values:[ALWAYS]}`** — the cheap always-on bounds/safety asserts — not the empty proto of §2.

The preset family `prod()` is part of (decoded for contrast; each is `SparseCoreAssertLevel(arena=0)` + a fixed appended-value list, numbers per the §2 `Value` enum):

```text
prod()                = [1]                  (ALWAYS)                       CLI keyword "prod"
san_lite()            = [5,6,7,8,11]          (SYNC_FLAGS,STREAMS,DMA,ALL_TO_ALL,RUN_IDS)   "san-lite"
vector_loads_stores() = [12,13]               (VECTOR_LOADS,VECTOR_STORES)  "vector-loads-stores"
all_loads_stores()    = [12,13,14,15]         (VECTOR/SCALAR LOADS+STORES)  "all-loads-stores"
san()                 = [1..15,17]            (ALWAYS..SCALAR_STORES + CONTINUATIONS; skips 16=MASKS)   "san"
```

> **QUIRK —** `SparseCoreAssertLevel` is the *only* repeated arm with a code fallback. `EnableAnyAccumulatorTransformation @ 0x1d6b98e0` / `EnableAccumulatorTransformation @ 0x1d6b9660` resolve the empty `AccumulatorTransformations` default and membership-test it with no injected default — so for `AccumulatorTransformations` the effective default *stays* the proto-empty list. Do not generalize the `prod()` override across the repeated arms; it is one arm. (Whether the `CostModelFlagOptions`/`SparseCoreOffloadingOptions` consumers inject a code default after the empty proto was not exhaustively swept — LOW.)

### `ResolveMemorySpaceSpec` — the `TpuCustomCallMemorySpaceSpec` default

The custom-call memory-space spec has its own consumer-side AUTO default. `TpuCustomCallMemorySpacePolicy::ResolveMemorySpaceSpec @ 0x11036320`, on AUTO (the AutoOr arm not present), `clear_policy()`s, then `Arena::DefaultConstruct<MsaReservationPolicy>` and fills `msa_reservation_size_bytes` from `Target::VmemSizeBytes @ 0x1d615e00` / `scoped_memory_util::DefaultScopedVmemBytes @ 0x1c864e40`:

```c
function ResolveMemorySpaceSpec(target, module, autoor):    // 0x11036320
    if autoor is AUTO (not present):
        spec.clear_policy()                                  // 0x1db25f00
        msa = Arena::DefaultConstruct<MsaReservationPolicy>(arena)
        msa.msa_reservation_size_bytes =
            scoped_memory_util::DefaultScopedVmemBytes(target, module, Target::VmemSizeBytes(target))
    else:
        switch autoor.value.policy_case { 1: MSA; 2: HBM }   // dispatch the user oneof arm
```

So the AUTO policy is an MSA reservation sized to the default scoped VMEM — empty proto, code-materialized policy. The decompile confirms `clear_policy`, `DefaultConstruct<...MsaReservationPolicy>`, `Target::VmemSizeBytes`, and `DefaultScopedVmemBytes` in the AUTO branch. Whether `ResolveMemorySpaceSpec` is the *only* spec consumer is unconfirmed (LOW).

---

## Related Components

| Component | Relationship |
|---|---|
| `AutoProto::_table_ @ 0x21cfa788` | the parent 30-arm oneof; the 12 message arms are its `0x04xx` `type_card` members |
| `AutoProto_globals_ @ 0x223c8968` | the all-AUTO default instance the message-arm getters fall back to (case 0) |
| `Message::AbslParseFlagImpl @ 0x20ef2120` | the generic message-flag parser — `text:`/`serialized:`/`base64:` dispatch |
| `TextFormat::Parser::ParseFromString @ 0x20efd420` · `ConsumeFieldValue @ 0x20f07a60` | the `text:` ingest; the `is_repeated` bit `0x20` → `Add*`/`Set*` split |
| `assert_level::prod() @ 0x1db1d8a0` · `GetSparseCoreAssertLevel @ 0x1d6b9ac0` | the `{values:[ALWAYS]}` consumer default for `xla_sc_assert_level` |
| `ResolveMemorySpaceSpec @ 0x11036320` | the MSA-sized-to-VMEM consumer default for `TpuCustomCallMemorySpaceSpec` |
| `tpu_compilation_environment.proto` FDP `@ 0xbfa6060` | the carved descriptor naming 11 of the 12 arms and the `policy` oneof |

## Cross-References

- [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) — the resolution model; owns the *scalar* `AutoOr<T>` arms (this page owns the *message-typed* arms and their Idiom-E default)
- [autoor-parse-grammar.md](autoor-parse-grammar.md) — the scalar `auto`/`enabled`/`disabled`/literal token grammar that the message-arm `text:` ingest sits beside
- [autoor-unparse.md](autoor-unparse.md) — the reverse `AbslUnparseFlag<AutoOr<T>>` text direction
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the TCE proto that hosts the `AutoProto*` fields these arms back, and the field#→offset oracle
- [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md) — the byte-exact field#→offset→default map; the env offsets cited per consumer here
- [registry-mediated-flags.md](registry-mediated-flags.md) — the flag registry that binds each message-arm flag name to its TCE field
- [default-debugoptions.md](default-debugoptions.md) — the contrast: a default instance *with* ~165 non-zero default writers, unlike the all-zero message arms here
- [overview.md](overview.md) — the three-layer config pipeline that the AutoProto knobs sit in
