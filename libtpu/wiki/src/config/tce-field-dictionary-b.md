# TCE Field Dictionary (B)

> *All field numbers, struct offsets, type cards, and defaults on this page apply to `libtpu.so` from `libtpu-0.0.40-cp314` (build-id `89edbbe81c5b328a958fe628a9f2207d`, `libtpu_lts_20260413_b_RC00`, 781,691,048 B, not stripped). Other wheel versions renumber and reorder fields; do not port these numbers across builds.*

## Abstract

`TpuCompilationEnvironment` (TCE) is the TPU PJRT plugin's master configuration
proto: a single message in which **every** field is also a registered `absl::Flag`,
so each `xla_*` / `megascale_*` knob is both a serialized proto field and a
command-line/environment flag. The message carries **1121 live fields** numbered
up to **#1218** (the gaps are deprecated/removed numbers). The field number →
name → type mapping is the wire contract: it decides how a serialized env
deserializes, which `FieldEntry` in `TpuCompilationEnvironment::_table_`
(`@0x21cfa9e0`) a tag selects, and therefore which struct byte a value lands on.

This page is the **upper half** of that dictionary. It covers field numbers
**#561 through #1218** (the highest *number* in the message; the two halves split
at the #561 boundary, with the lower 560 field numbers on dictionary A and the
remaining 561 live entries — entries 561–1121 of the dense `FieldEntry` array —
here), ordered by
field number and grouped by functional area within the range. For each field it
gives the verbatim flag/proto name, the proto type (and the wrapper enum or
message type where the base type is `enum` or `message`), and a confidence label.
The lower half — fields **#1 through #560** — lives on
[`tce-field-dictionary-a.md`](tce-field-dictionary-a.md); the two pages tile at
the **#561 boundary**. This page does **not** restate struct offsets or literal
default values except where a default is the field's only distinguishing
property; those columns are the subject of
[`tce-field-offsets-defaults.md`](tce-field-offsets-defaults.md), and the
message-level structure (parse table, has-bits, AutoProto oneof) is on
[`tpu-compilation-environment.md`](tpu-compilation-environment.md).

The dictionary is a **reference catalog**, not an algorithm, so it carries no
reimplementation contract of its own. What a reimplementer must reproduce is the
*shape* of the field space, captured below as a dimension table and then the
per-area name lists:

- The **field# → name → proto-type** map for #561–#1218, the half of the wire
  contract the upper FieldEntry rows encode.
- The **wrapper resolution** for the four enum-typed and the message-typed
  (AutoProto / typed-message) fields in this range — the base proto type alone
  does not tell a reader how to decode the value; the wrapper enum or AutoProto
  arm type does.
- The **type histogram for the range**, so a reimplementer knows the per-type
  `type_card` to write into each upper FieldEntry (the `type_card` is a 1:1 proxy
  for the proto type and is shared with the lower half).

| | |
|---|---|
| **Range owned by this page** | field **#561 – #1218** (561 live entries; #1–#560 on dictionary A) |
| **Live fields total (message)** | 1121 (max field number 1218 / `0x4c2`) |
| **Parse table** | `TpuCompilationEnvironment::_table_` `@0x21cfa9e0` (`.data.rel.ro`) |
| **FieldEntry array** | `table+0x370`, 1121 × 12 B, sorted ascending by field# |
| **FieldEntry layout** | `{uint32 offset; uint32 has_idx; uint16 aux_idx; uint16 type_card}` |
| **Struct size** | `sizeof(TpuCompilationEnvironment) = 0x15e8` (5608 B) |
| **Default instance** | `TpuCompilationEnvironment_globals_` `@0x227b87e0` |

> **NOTE —** field number is not array index. The FieldEntry array is dense
> (1121 entries) but field numbers are sparse (max 1218). For a field number `N`,
> `index_of(N) = (N-1) − (count of gap field-numbers below N)`. Both dictionary
> pages list by field number, not by array index, so the reader never computes
> the gap count — but a reimplementer walking `_table_` must.

---

## Range Shape — Type Histogram for #561–#1218

The whole-message type histogram (all 1121 fields) is fixed, and each proto base
type maps to exactly one 16-bit `type_card`. The upper half inherits the same
`type_card` constants; only the counts differ between halves. The message-wide
constants, which a reimplementer writes verbatim into every FieldEntry of the
matching type:

| Proto type | `type_card` | Message-wide count | Wrapper / note |
|---|---|---|---|
| `bool` | `0x0011` | 418 | plain bool |
| `int64` | `0x10d1` | 148 | plain int64 |
| `message` | `0x0416` | 349 | AutoProto oneof or typed sub-message; `aux_idx` resolves the type |
| `enum` | `0x1891` | 74 | one of 8 distinct wrapper enums; 67 are `TristateProto.Value`, the other 7 are dedicated wrappers |
| `string` | `0x0c15` | 37 | SSO; 7 non-empty defaults message-wide |
| `float` | `0x1893` | 34 | f32 |
| `int32` | `0x1091` | 32 | plain int32 |
| `double` | `0x18d3` | 14 | f64 |
| `uint32` | `0x0891` | 11 | plain uint32 |
| `uint64` | `0x08d1` | 4 | plain uint64 |

> **GOTCHA —** the `type_card` is the *only* in-table signal of proto type; the
> field name is **not** stored in `_table_` (names live in the FileDescriptorProto
> string stream at `@0xbfa6060`). A reimplementer who keys decode off the field
> name has nothing to read from the parse table — key off `type_card`, and for
> `message`/`enum` follow `aux_idx` into the aux array (`table+0x3800`,
> 349 entries) for the concrete message `_table_` or wrapper.

The per-area counts below are the upper half's share. They are derived by name
area, not re-counted per `type_card`; treat the per-area totals as indicative
(HIGH) and the message-wide `type_card` constants above as exact (CERTAIN).

---

## Reading the Dictionary Rows

Each row is `field# | name | type[/wrapper] | confidence`. The name column is the
verbatim `absl::Flag` symbol (the `FLAGS_<name>` global) and proto field name —
they are identical, the 1:1 mapping confirmed by the parse table having exactly
1121 FieldEntry rows and exactly 1121 matching `FLAGS_*` objects. The type column
is the proto base type from `type_card`; for `enum` it appends the wrapper enum,
for `message` it appends `AutoProto` or the typed sub-message. Confidence:

- **CERTAIN** — name read verbatim from the binary string stream this pass, type
  from the `type_card`.
- **HIGH** — name and type carried from the byte-exact parse-table sweep; not
  individually re-grepped this pass but consistent with the confirmed set.
- **LOW** — field number not independently re-pinned to a name this pass (the
  parse-table order pins it, but the specific name attribution is inferred);
  flagged inline.

> **NOTE —** the field-number → name attribution for names not spot-confirmed
> this pass rests on the parse-table FieldEntry ordering (ascending field#) cross
> the FileDescriptorProto name stream. Where this page lists a *named, anchored*
> field (a wrapper enum, a string default, an MSA ratio), the name was grepped
> verbatim from `.rodata` and is CERTAIN. Where it lists an area as a count
> without naming every member, the per-field names within that area are HIGH and
> the area boundary is the anchored claim.

---

## #561–#599 — SDC Checker, Large-Buffer Scaling, Scalars

This band sits just above the dictionary-A boundary and is dominated by the SDC
(silent-data-corruption) checker family and the memory-space-assignment (MSA)
large-buffer scaling controls. Two anchored fields pin the band.

| Field# | Name | Type / wrapper |
|---|---|---|
| #578 | `xla_tpu_alternate_memory_benefit_scaling_factor_for_large_buffers` | string (default `"SQRT"`) |
| #583 | `xla_tpu_sdc_checker_checksum_algo` | enum / `ChecksumAlgoProto.Value` |
| #592 | `xla_tpu_msa_inefficient_use_to_copy_ratio` | float (default `0.5`) |

> **QUIRK —** #578 is a **string** flag whose value is a discrete enum-like token
> (`"SQRT"`, also `"LINEAR"` / `"NONE"` in the code paths), not a number. The
> string is parsed downstream into a scaling mode. A reimplementer must store it
> as a `std::string` SSO field (`type_card 0x0c15`), not as an enum — the wire
> type is genuinely string. Default `"SQRT"` was grepped verbatim from `.rodata`.

`ChecksumAlgoProto.Value` (the wrapper for #583) is one of the 7 dedicated
(non-Tristate) TCE wrapper enums: `DEFAULT=0`, `XOR=1`, `SIP_HASH_1_3=2` (value name `SIP_HASH_1_3`
confirmed verbatim). #583 defaults to `0 (DEFAULT)`. The remaining fields in this
band are bool and int64 SDC-checker knobs (e.g. `xla_tpu_sdc_checker_*`,
HIGH confidence on individual names; the band boundary is the anchored claim).

---

## #600–#649 — Register Selection, MXU SDC Injection

This band carries the register-allocator selection policy and the MXU
SDC-injection overhead. The register-selection policy is the most consequential
enum in the upper half.

| Field# | Name | Type / wrapper |
|---|---|---|
| #611 | `xla_tpu_sdc_inject_mxu_sequences_overhead` | float (default `1.2`) |
| #631 | `xla_tpu_register_selection_policy` | enum / `RegSelectPolicyProto.Value` |

`RegSelectPolicyProto.Value` has seven values: `NONE=0`, `LEGACY=1`,
`BALANCE_PREV_NEXT_USES_IGNORE_FREE=2`, `BALANCE_PREV_NEXT_FREE_SPILL=3`,
`DOUBLE=4`, `WORST=5`, `DISREGARD_RECENTLY_USED=6`.

> **QUIRK —** #631 defaults to **`6` (`DISREGARD_RECENTLY_USED`)**, not `0`. The
> default is materialized by a `kGenFunc` (`AbslFlagDefaultGenFor` body at
> `@0x1d723540`, `movb $0x6,(%rdi); ret`), so a reimplementer that initializes
> the register policy to its zero value (`NONE`) diverges from the shipped TPU
> register allocator from the first compile. The value name `DISREGARD_RECENTLY_USED`
> was grepped verbatim and is unique in `.rodata`.

The rest of this band is bool/int register-allocator and SDC-injection knobs,
HIGH on individual names.

---

## #650–#699 — sflag-Wait Stats, Scheduler Knobs

A band of scheduler and sflag-wait instrumentation. One anchored string default.

| Field# | Name | Type / wrapper |
|---|---|---|
| #656 | `xla_tpu_collect_sflag_wait_stats_filter` | string (default `"all"`) |

> **NOTE —** #656 and its sibling #739 (below) both default to the string `"all"`
> and act as instrumentation *filters* — a comma-list of sflag-wait sites, with
> `"all"` meaning "instrument every site". Store as string; the empty string is a
> distinct value (instrument none), so do not collapse `""` and `"all"`.

The band's remaining fields are bool/int64 scheduler tunables (HIGH).

---

## #700–#749 — Precision Tracer, Synthetic Compute

This band introduces the precision-tracer mode enum and the second sflag-wait
string filter.

| Field# | Name | Type / wrapper |
|---|---|---|
| #723 | `xla_tpu_precision_tracer_mode` | enum / `PrecisionTracerModeProto.Value` |
| #739 | `xla_tpu_synthetic_compute_in_sflag_wait_filter` | string (default `"all"`) |

`PrecisionTracerModeProto.Value` has six values: `NONE=0`,
`LOG_ORIGINAL_AND_SHADOW=1`, `LOG_ABS_DIFF=2`, `LOG_ABS_DIFF_SUMMARY=3`,
`CHECK_ABS_DIFF=4`, `CHECK_ABS_DIFF_NONFATAL=5`. #723 defaults to `0 (NONE)`. The
same `PrecisionTracerModeProto.Value` enum is *also* AutoProto arm #12 (see
[`tpu-compilation-environment.md`](tpu-compilation-environment.md)), so the same
wrapper appears both as a direct enum field here and inside the AutoProto oneof —
a reimplementer must register the wrapper once and reference it from both sites.

---

## #750–#799 — MSA Family, Dot-RHS, Offloading (Tristate-heavy)

This is the densest Tristate band in the upper half: the memory-space-assignment
(`msa`) enable/option cluster, dot-parameter placement, scatter offloading, and
the bundle-aware cost model. The Tristate fields all carry
`TristateProto.Value` (`AUTO=0`, `DISABLED=1`, `ENABLED=2`), and many default
`ENABLED`.

| Field# | Name | Type / wrapper |
|---|---|---|
| #758 | `xla_tpu_move_dot_parameters_to_rhs` | enum / `TristateProto.Value` (default `ENABLED`) |
| #766 | `xla_tpu_enable_large_2nd_minor_layout_for_x8` | enum / `TristateProto.Value` (default `ENABLED`) |
| #777 | `xla_tpu_override_scavenge_vmem_for_fusions` | enum / `TristateProto.Value` (default `ENABLED`) |
| #787 | `xla_msa_enable` | enum / `TristateProto.Value` (default `ENABLED`) |
| #788 | `xla_msa_min_overlap_to_async_copy_ratio` | float (default `1.0`) |
| #789 | `xla_msa_preferred_overlap_to_async_copy_ratio` | float (default `2.0`) |
| #790 | `xla_msa_max_overlap_to_mem_size_async_copy_ratio` | float (default `8.0`) |

> **QUIRK —** the MSA overlap-ratio triple at #788/#789/#790 is **not** named
> uniformly. min/preferred use `..._overlap_to_async_copy_ratio`, but the max
> field is `xla_msa_max_overlap_to_mem_size_async_copy_ratio` — an extra
> `to_mem_size` infix. All three names were grepped verbatim. The triple mirrors
> the `jf`/`vf`/`gf`/`cmem` overlap triples in dictionary A (#280/284/285,
> #442/446/447, #540/544/545, #309/313/314); the `msa` family is the upper-half
> member of that same five-way MSA tuning pattern. The ratio values here are the
> shipped defaults (1.0 / 2.0 / 8.0), the same triple the family overlay applies
> per TPU generation; see [`tce-field-offsets-defaults.md`](tce-field-offsets-defaults.md).

`xla_msa_enable` (#787) is the master MSA enable, default `ENABLED(2)` via a
`movb 2` gen (`@0x1d705500`). The non-anchored Tristate members of this band
(#766, #777, and several bool siblings) are HIGH on name, CERTAIN on the
`TristateProto.Value` wrapper.

---

## #800–#849 — SparseCore Offload, Bundle-Aware Cost Model, Collective Optimize

The SparseCore collective-offload and bundle-aware cost-model Tristate cluster,
plus the async-wrapper fusion-type enum. This band is heavily ENABLED-by-default.

| Field# | Name | Type / wrapper |
|---|---|---|
| #802 | `xla_tpu_enable_offloading_scatter_to_sparsecore` | enum / `TristateProto.Value` (default `ENABLED`) |
| #804 | `xla_tpu_use_bundle_aware_cost_model_for_fusions` | enum / `TristateProto.Value` (default `ENABLED`) |
| #807 | `xla_tpu_experimental_do_not_use_fusion_estimate_cost_changes` | enum / `TristateProto.Value` (default `ENABLED`) |
| #816 | `xla_msa_use_bundle_aware_cost_model` | enum / `TristateProto.Value` (default `ENABLED`) |
| #822 | `xla_tpu_enable_sparse_core_collective_offload_all_gather` | enum / `TristateProto.Value` (default `ENABLED`) |
| #827 | `xla_sc_async_wrapper_fusion_type` | enum / `ScAsyncWrapperFusionTypeProto.Value` |
| #839 | `xla_tpu_enable_sparse_core_collective_offload_all_reduce` | enum / `TristateProto.Value` (default `ENABLED`) |

`ScAsyncWrapperFusionTypeProto.Value` (the wrapper for #827) has four values:
`DEFAULT=0`, `SINGLE_SPARSE_DENSE_CALL=1`, `SINGLE_MINIBATCHING_STEP=2`,
`SINGLE_TPU_CUSTOM_CALL=3`.

> **QUIRK —** #827 defaults to **`3` (`SINGLE_TPU_CUSTOM_CALL`)**, the *last*
> enumerator, not `0 (DEFAULT)`. Value name `SINGLE_TPU_CUSTOM_CALL` confirmed
> verbatim and unique. A reimplementer must materialize the async-wrapper fusion
> type to 3, or SparseCore async wrappers fuse with the wrong strategy. This is
> the same pattern as #631 — two upper-half enums whose shipped default is a
> non-zero, non-first enumerator.

The `_all_gather` (#822) and `_all_reduce` (#839) SparseCore-offload Tristates
are part of the same family as the `_reduce_scatter` member at #860 (below) — a
collective-by-collective offload toggle set.

---

## #850–#899 — Collective Optimize, Advanced MOF, Quantized All-Reduce

The tail of the SparseCore-offload Tristate set, the constant-table collective
optimization, advanced multi-output fusion, and a quantized-collective threshold.

| Field# | Name | Type / wrapper |
|---|---|---|
| #852 | `xla_collective_optimize_constant_table` | enum / `TristateProto.Value` (default `ENABLED`) |
| #860 | `xla_tpu_enable_sparse_core_collective_offload_reduce_scatter` | enum / `TristateProto.Value` (default `ENABLED`) |
| #866 | `xla_jf_enable_advanced_multi_output_fusion` | enum / `TristateProto.Value` (default `ENABLED`) |
| #890 | `xla_tpu_quantized_all_reduce_size_threshold_mib` | float (default `3.0`) |

> **NOTE —** #852's flag name drops the `_tpu` infix used by most of its
> neighbors — it is `xla_collective_optimize_constant_table`, confirmed verbatim,
> not `xla_tpu_collective_optimize_constant_table`. The TCE flag namespace mixes
> `xla_*`, `xla_tpu_*`, `xla_jf_*`, `xla_sc_*`, `xla_msa_*`, and `megascale_*`
> prefixes with no enforced rule; a reimplementer must take each name literally
> from the descriptor and not normalize the prefix.

These four are the last directly-anchored fields below #900. #866 is the
upper-half member of the JellyFish (`jf`) advanced-multi-output-fusion toggle,
default `ENABLED(2)`.

---

## #900–#1218 — Tail: Mixed Scalars, Tristates, AutoProto Messages

The top ~220 field numbers carry no single dominant area; they are a mix of
bool/int scalar knobs, additional `TristateProto.Value` toggles, and
`message`-typed AutoProto / typed-message fields added in later proto revisions.
No field in this tail was individually name-grepped this pass, so the per-field
**name attribution is LOW** in this band — the parse-table ordering pins each
field number to a FieldEntry, and the `type_card` pins each to a proto type, but
the specific names are not independently re-confirmed here. The *structure* of
the tail is anchored and HIGH:

| Axis | Values | Source |
|---|---|---|
| Live entry count | 1121 FieldEntry rows (the 1121st is the last live field) | `num_field_entries=1121`, ascending order |
| Highest live field number | #1218 (`0x4c2`) | parse-table header `max_field_number` |
| Gap field-numbers in tail | the `1218 − 1121 = 97` dead numbers, spread across the whole range | header arithmetic |
| Proto types present | bool, int64, int32, float, double, string, enum, message | message-wide histogram |
| `message` fields | resolve via `aux_idx` → aux array (`table+0x3800`); AutoProto = empty/AUTO default | aux entries (349 message fields message-wide) |

> **GOTCHA —** the **1121st FieldEntry** is the last live field, but its field
> *number* is **#1218**, not 1121. Because 97 field numbers below #1218 are dead,
> the live-entry count (1121) and the highest field number (#1218) differ by
> exactly those 97 gaps. Dictionary A owns the first 560 field numbers (#1–#560);
> this page owns the remainder (#561–#1218). A reimplementer reading the parse
> table sees only the 1121 live entries and their actual (sparse) numbers; the
> dead numbers are simply absent.

> **NOTE —** the message-typed fields in this tail (and throughout TCE) are
> overwhelmingly `xla.jellyfish.AutoProto` oneof fields. AutoProto is a 30-arm
> oneof whose default is the empty message (`oneof-case 0 = AUTO`); the arm
> message types (e.g. `ShardyOptions`, `IlpLatencyHidingSchedulerOptions`,
> `EmitterLearnedCostModelOptions`, `BundleInstrumentationOptions`,
> `TpuCustomCallMemorySpaceSpec` — all confirmed present in `.rodata` this pass)
> are documented on [`tpu-compilation-environment.md`](tpu-compilation-environment.md).
> For dictionary purposes a tail `message` field's row is `field# | name |
> message/AutoProto | LOW`; the decode of the oneof is not per-field.

> **NOTE —** the TCE field numbers are **sparse**, not dense. The parse-table
> header (`max_field_number=1218`, `num_field_entries=1121`) carries 97 dead
> numbers, so the live-entry *count* (1121) is not a field number. The two
> dictionary pages tile by field *number* (A: #1–#560; B: #561–#1218), and any
> reader mapping a serialized tag must use the live FieldEntry, never assume
> tag == index.

---

## Wrapper Enums Referenced in This Range

Four of the 7 dedicated (non-Tristate) TCE wrapper enums are the type of a *direct* enum field in #561–#1218
(versus appearing only inside the AutoProto oneof). Their value tables, read
verbatim from the FileDescriptorProto value-name stream (`@0xbfa6060+`):

| Field# | Wrapper enum | Values | Default |
|---|---|---|---|
| #583 | `ChecksumAlgoProto.Value` | `DEFAULT=0`, `XOR=1`, `SIP_HASH_1_3=2` | `0 DEFAULT` |
| #631 | `RegSelectPolicyProto.Value` | `NONE=0`, `LEGACY=1`, `BALANCE_PREV_NEXT_USES_IGNORE_FREE=2`, `BALANCE_PREV_NEXT_FREE_SPILL=3`, `DOUBLE=4`, `WORST=5`, `DISREGARD_RECENTLY_USED=6` | `6` |
| #723 | `PrecisionTracerModeProto.Value` | `NONE=0`, `LOG_ORIGINAL_AND_SHADOW=1`, `LOG_ABS_DIFF=2`, `LOG_ABS_DIFF_SUMMARY=3`, `CHECK_ABS_DIFF=4`, `CHECK_ABS_DIFF_NONFATAL=5` | `0 NONE` |
| #827 | `ScAsyncWrapperFusionTypeProto.Value` | `DEFAULT=0`, `SINGLE_SPARSE_DENSE_CALL=1`, `SINGLE_MINIBATCHING_STEP=2`, `SINGLE_TPU_CUSTOM_CALL=3` | `3` |

Every other enum field in this range carries `TristateProto.Value`
(`AUTO=0`, `DISABLED=1`, `ENABLED=2`). The wrapper-to-flag bridge is the
`TpuCompEnvReflection::NormalizeFieldType<T>` template family (one instantiation
per wrapper type), which copies a flag's typed value into the proto field; the
full wrapper list and bridge are on
[`tpu-compilation-environment.md`](tpu-compilation-environment.md).

---

## Cross-References

- [TCE Field Dictionary (A)](tce-field-dictionary-a.md) — fields **#1–#560**, the lower half of this same field#→name→type catalog; the two pages tile at the #561 boundary
- [TpuCompilationEnvironment](tpu-compilation-environment.md) — the message structure: parse table, FieldEntry/aux layout, has-bits, the wrapper enums (67 `TristateProto.Value` fields plus 7 dedicated wrappers), and the 30-arm AutoProto oneof whose arms are the `message`-typed fields in this dictionary
- [TCE Field-Offsets & Flag Defaults](tce-field-offsets-defaults.md) — the struct-offset column and the byte-exact literal default for every field, including the per-area defaults this page only spot-cites
- [Flag Families](flag-families.md) — the `xla_*` / `xla_tpu_*` / `xla_jf_*` / `xla_sc_*` / `xla_msa_*` / `megascale_*` prefix families and how a flag name maps to its registered `absl::Flag`
- [XLA Flag Atlas](xla-flag-atlas.md) — the broader catalog of XLA debug/compile flags surfaced through the TPU PJRT plugin
- [Config Overview](overview.md) — entry point for the configuration subsystem (Part XVI)
