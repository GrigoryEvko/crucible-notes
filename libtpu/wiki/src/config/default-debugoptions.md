# Default DebugOptions

> *All addresses, struct offsets, and field-number mappings on this page apply to `libtpu` 0.0.40 (cp314 manylinux build, `libtpu_lts_20260413_b_RC00`, `libtpu.so` build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions reorder the field-offset layout and will differ.*

## Abstract

`xla::DefaultDebugOptionsIgnoringFlags()` at `0x1e66a860` is the single function that constructs the *baseline* `xla.DebugOptions` message every TPU compilation starts from. It runs before any flag, environment override, or PJRT `CompileOptions.debug_options` merge. The name is literal: it builds the in-binary defaults *ignoring* the flag layer, so its body is the authoritative seed for the 290-field message. In upstream OpenXLA the equivalent is `DefaultDebugOptionsIgnoringFlags()` in `xla/debug_options_flags.cc`; this is the TPU plugin's compiled copy of it.

Proto3 carries no descriptor-level defaults — every scalar's wire default is the zero value (`false` / `0` / `""` / enum-0). The *effective* default is whatever this constructor stores on top of that zero state. The body therefore divides cleanly into two populations: the fields it **explicitly writes** (the non-zero seeds documented here), and the ~218 fields it **never touches**, which keep the proto-zero default established by the `DebugOptions::DebugOptions(this, 0)` base-construction call on entry (line 60). This page owns that explicit-write → value map. It does **not** own the field schema — field number, name, and type live on [`debugoptions-proto.md`](debugoptions-proto.md). It does not own the flag overrides ([`xla-flag-atlas.md`](xla-flag-atlas.md)) nor the AUTO/`-1`-sentinel resolution ([`autoproto-autoor-resolution.md`](autoproto-autoor-resolution.md)).

The decompiled body is a flat sequence of raw struct stores — `*((_DWORD *)this + N) = value`, `*((_BYTE *)this + N) = 0/1`, `… |= mask`, and packed QWORD writes that pack several adjacent fields into one 8-byte move. Mapping a store back to a proto field requires matching its `(offset, has-bit)` pair against the individual `set_xla_*` accessors, which is how every CONFIRMED row below is anchored. The `|= mask` writes are proto2 has-bit ORs (presence flags in the message's `_has_bits_` words at offsets 8–55); they record *that* a field was set without changing its value, and are not themselves defaults.

For reimplementation, the contract is:

- The **base-zero invariant**: any field absent from this constructor is left at its proto3 zero — the constructor never clears a non-zero base, it only seeds.
- The **explicit-seed table**: the field → value pairs the body writes, recovered by `(offset, has-bit)` → accessor matching.
- The **packing convention**: how one QWORD store (`0x1E0000000A`) seeds two adjacent int fields (low DWORD = field N, high DWORD = field N+1), and how float fields ride the upper half of a packed QWORD.
- The **repeated-enum seeds**: two `RepeatedField<int>` are appended element-by-element (`xla_gpu_enable_command_buffer` and a second command-buffer list), not stored as scalars.

| | |
|---|---|
| **Function** | `xla::DefaultDebugOptionsIgnoringFlags()` |
| **Address** | `0x1e66a860` (~610 decompiled lines) |
| **Base ctor** | `DebugOptions::DebugOptions(this, 0)` at line 60 — zeroes all 290 fields first |
| **Message** | `xla.DebugOptions` (290 live fields, descriptor index 403) |
| **Explicit scalar/string seeds** | ~70 stores (this page's table) |
| **Repeated-enum seeds** | 2 lists (offset 160 ×6 elems, offset 288 ×3 elems) |
| **Map seeds** | `xla_gpu_analytical_latency_estimator_options` (6 string→string pairs) |
| **Untouched (proto-zero) fields** | ~218 — left `false`/`0`/`""`/enum-0 |
| **Caller of record** | `AllocateFlags()` `0x1e6b8020`, then the PJRT compile path |

---

## How a Default Is Recovered

### The store-to-field mapping problem

The decompiler renders the body against the raw `DebugOptions` object — it has no proto field names, only byte offsets into the C++ message layout. A line such as

```c
*((_DWORD *)this + 142) = 4;          // 0x1e66a860 + body; offset 568
*((_BYTE *)this + 24) |= 0x20u;       // has-bit OR
```

means "store the 32-bit value 4 at byte offset 568, then set has-bit `0x20` in the `_has_bits_` word at offset 24." To name the field, match that exact `(value-offset, has-bit-offset, has-bit-mask)` triple against the generated `set_xla_*(…)` accessor, each of which writes precisely one field's offset and ORs precisely one has-bit. The accessor for `xla_gpu_autotune_level` (`0x1e66b860`) writes `*((_DWORD *)this + 142)` and ORs `*((_BYTE *)this + 24) |= 0x20` — an exact match. Therefore offset 568 is field 123, and its default is **4**.

Every CONFIRMED row below was anchored this way: the offset/has-bit pair was cross-checked against the field's `set_*` accessor. Rows where only the offset is known (no accessor cross-check performed within the grep budget) are marked HIGH; semantically-inferred packed-pair members are marked HIGH or MEDIUM as noted.

### The packing convention

The compiler coalesces adjacent stores into wide moves. Three patterns recur:

```c
*(_QWORD *)((char *)this + 844) = 0x1E0000000ALL;   // two int32 fields
//   offset 844 (low DWORD)  = 0x0000000A = 10  -> field 327
//   offset 848 (high DWORD) = 0x0000001E = 30  -> field 328
*((_QWORD *)this + 84) = 0x3F8CCCCD00000000LL;       // a float in the upper half
//   offset 672 (low DWORD)  = 0          -> adjacent int/bool group
//   offset 676 (high DWORD) = 0x3F8CCCCD = 1.1f -> a float field
*(_QWORD *)((char *)this + 92) = 0x101010100000003LL; // int + four bools
//   offset 92  (low DWORD)  = 3          -> field 31 (backend_optimization_level)
//   offsets 96,97,98,99     = 01 01 01 01 -> four adjacent bool fields = true
```

> **GOTCHA —** a single QWORD store is not one field. `0x101010100000003` at offset 92 seeds *five* proto fields at once: the int32 `xla_backend_optimization_level` (low DWORD = 3) plus four byte-wide bools at 96–99, each set to 1. A reimplementation that treats the QWORD as one 64-bit field value gets every one of the five wrong. Decode packed stores byte-lane by byte-lane against the layout.

> **QUIRK —** `&stru_800000`, `&unk_1E00007`, `&stru_1000100` in the decompiled body are *not* pointers. IDA mislabels the wide immediates `0x800000`, `0x1E00007`, `0x1000100` as data symbols because their numeric value collides with a section/struct address. They are integer/packed-pair literals: `0x800000` = 8 388 608 (8 MiB), `0x1000100` = a packed `{int=256, …}` pair.

---

## The Explicit-Seed Table

Values the constructor writes on top of the zero base. Offset is the byte offset into the `DebugOptions` C++ object; field# is the proto field on [`debugoptions-proto.md`](debugoptions-proto.md). "Confidence" reflects whether the `(offset, has-bit)` pair was matched to a named accessor (CONFIRMED) or only located by offset / inferred from packing (HIGH/MEDIUM).

### Confirmed scalar seeds (accessor-anchored)

| Field# | Name | Type | Offset | Default |
|---|---|---|---|---|
| 31 | `xla_backend_optimization_level` | int32 | 92 | **3** |
| 123 | `xla_gpu_autotune_level` | int32 | 568 | **4** |
| 142 | `xla_multiheap_size_constraint_per_heap` | int32 | 584 | **-1** |
| 228 | `xla_gpu_redzone_padding_bytes` | int64 | 680 | **8388608** (8 MiB) |
| 237 | `xla_gpu_collective_permute_decomposer_threshold` | int64 | 688 | **0x7FFFFFFFFFFFFFFF** (INT64_MAX) |
| 311 | `xla_cmd_buffer_trace_cache_size` | int64 | 816 | **16** |
| 327 | `xla_gpu_executable_warn_stuck_timeout_seconds` | int32 | 844 | **10** |
| 328 | `xla_gpu_executable_terminate_timeout_seconds` | int32 | 848 | **30** |

The last two are the canonical packed pair: line 548 `*(_QWORD *)((char *)this + 844) = 0x1E0000000A` writes both with one move (`0x0A`=10 in the low lane at 844, `0x1E`=30 in the high lane at 848). Both offsets are accessor-confirmed.

### High-confidence scalar seeds (offset-located)

These stores are unambiguous in the body but their `set_*` accessors were not individually cross-checked within the grep budget; the offset and value are byte-exact, the field assignment is by layout adjacency to confirmed neighbours.

| Offset | Store (body line) | Decoded value | Type inferred |
|---|---|---|---|
| 568 (field 123) | `*((_DWORD*)this+142)=4` | 4 | int32 (autotune_level) |
| 576 (field 132) | `*((_DWORD*)this+144)=-1` | -1 | int32 (`xla_dump_max_hlo_modules`) |
| 644 | `*((_DWORD*)this+161)=5` | 5 | int32 |
| 712 | `*((_QWORD*)this+89)=15` | 15 | int64 |
| 724 | `*((_DWORD*)this+181)=95` | 95 | int32 |
| 728 | `*((_QWORD*)this+91)=100000` | 100000 | int64 |
| 768 | `*((_QWORD*)this+96)=100` | 100 | int64 |
| 792 | `*((_QWORD*)this+99)=16` | 16 | int64 |
| 812 | `*((_DWORD*)this+203)=256` | 256 | int32 |
| 836 | `*((_DWORD*)this+209)=32` | 32 | int32 |
| 956 | `*((_DWORD*)this+239)=2` | 2 | enum/int32 |
| 976 | `*((_DWORD*)this+244)=40` | 40 | int32 |
| 984 | `*((_DWORD*)this+246)=20` | 20 | int32 |
| 1008 | `*((_DWORD*)this+252)=1800` | 1800 | int32 (timeout s) |
| 1024 | `*((_QWORD*)this+128)=0x400000` | 4194304 (4 MiB) | int64 |
| 1040 | `*((_QWORD*)this+130)=2048` | 2048 | int64 |

> **NOTE —** several of these are recognizable XLA defaults independent of the symbol match: 256 (`xla_gpu_memory_limit_slop_factor`-class), 1800 s (a 30-minute terminate timeout), 4 MiB / 8 MiB (collective-combine/redzone byte thresholds), `INT64_MAX` (a "no threshold" sentinel). The values are byte-exact; only the precise field name carries the HIGH (not CONFIRMED) caveat.

### Packed-pair int/enum seeds

| Body line | Store | Low lane (offset → value) | High lane (offset → value) |
|---|---|---|---|
| 548 | `+844 = 0x1E0000000A` | 844 → 10 (field 327) | 848 → 30 (field 328) |
| 553 | `+936 = 0x2800000014` | 936 → 20 (0x14) | 940 → 40 (0x28) |
| 63 | `+92 = 0x101010100000003` | 92 → 3 (field 31) | 96–99 → bool 1,1,1,1 |

### Float seeds (upper-half packing)

Two `float` fields exist in DebugOptions (`xla_gpu_auto_spmd_partitioning_memory_budget_ratio` field 225, `xla_gpu_autotune_gemm_rtol` field 316). Both are seeded via the upper half of a packed QWORD:

| Body line | Store | Float lane | Value |
|---|---|---|---|
| 491 | `*((_QWORD*)this+84)=0x3F8CCCCD00000000` | offset 676 = `0x3F8CCCCD` | **1.1f** |
| 543 | `*((_QWORD*)this+103)=0x53DCCCCCD` | offset 824 = `0x3DCCCCCD` | **0.1f** |

`0x3F8CCCCD` decodes to IEEE-754 single `1.1000000238…` and `0x3DCCCCCD` to `0.1000000015…` — the canonical `1.1` and `0.1` float literals. The `+103` store also carries `0x5` in the lane above the float, seeding an adjacent small int.

### String seeds (`ArenaStringPtr::Set`)

| Body line | Offset | Value | Likely field |
|---|---|---|---|
| 69 | 80 | `"./cuda_sdk_lib"` (len 14) | `xla_gpu_cuda_data_dir` (61) |
| 551 | 520 | `"inf"` (len 3) | a GPU string-typed timeout/mode field |

All other `ArenaStringPtr::Set(... &nptr, 0)` calls (offsets 408, 432, 464, 472, 480, 488, 496) set the **empty string** — they are explicit no-op seeds that re-establish `""`, matching the proto-zero default. They are listed for completeness but add no non-zero default.

> **QUIRK —** `"./cuda_sdk_lib"` and `"inf"` are GPU/host-platform strings baked into the *shared* upstream default even though no TPU code reads `xla_gpu_cuda_data_dir`. This is direct evidence that libtpu compiles the whole OpenXLA `DefaultDebugOptionsIgnoringFlags` body unchanged, GPU defaults included, rather than a TPU-pruned variant — consistent with the proto carrying all 183 `xla_gpu_*` fields. See [`overview.md`](overview.md).

---

## Boolean Seeds

The body writes ~40 individual `*((_BYTE *)this + N) = 0/1` stores plus four bools packed in the offset-92 QWORD. Listing 40 raw `(offset, 0|1)` rows would be the byte-dump anti-pattern; the structure is what matters:

```text
bool stores in DefaultDebugOptionsIgnoringFlags (line / offset / value):
  set to 1 (true):
    91, 620, 631, 929, 1055, 910, 895, 989, 588, 622, 665, 621, 742,
    894, 671, 637, 930, 806, 743, 878, 933, 909, 1007, 1006, 932, 964,
    1053, 567, 567(+1)   and the 4 packed bools at 96-99
  set to 0 (false, explicit):
    555, 100, 629, 563, 596, 575, 589, 782, 546, 966, 907, 547, 697,
    859, 965, 103, 736, 809, 991, 554, 739, 961, 934, 811, 885, 630,
    636, 1013, 905, 783, 1035, 664, 877, 698, 857, 740, 1014, 833,
    856, 1034, 858, 879, 884, 931, 892, 904, 928, 1012, 911, 988, 967,
    950, 1015, 982, 980, 1004, 1032, 1033
```

> **NOTE —** the explicit `= 0` bool stores are redundant against the zero base (line 60 already cleared them) — the compiler emits them because the source assigns `set_field(false)` unconditionally. They do not change the effective default; they only force the has-bit so the field serializes as "explicitly set to false" rather than "absent." That presence distinction is the front-end's "the default touched this" signal (see the proto3-optional presence model on [`debugoptions-proto.md`](debugoptions-proto.md)).

> **GOTCHA —** because ~60 bools are explicitly written `false` *with* their has-bit set, a wire-level `DebugOptions` emitted from these defaults is **not** sparse — it carries presence bits for every field the constructor names, not just the non-zero ones. A reimplementation that only serializes non-zero fields will produce a byte-different (though semantically equal) message and break any has-bit-sensitive consumer.

The named `true`-by-default bools were not all accessor-matched within the grep budget; the count and offset list are byte-exact, individual field names are HIGH-confidence by layout. The reliably-named `true` defaults (confirmed by adjacency to accessor-anchored neighbours) include the four bools packed at offsets 96–99 alongside `xla_backend_optimization_level`.

---

## Repeated-Enum Seeds — Command Buffer Lists

Two `RepeatedField<int>` members are populated element-by-element with `GrowNoAnnotate` + index store, not as scalar writes. Both are `CommandBufferCmdType` enum lists (the enum: `INVALID=0, FUSION=1, CUBLAS=2, CUDNN=3, COLLECTIVES=4, CONDITIONAL=5, WHILE=6, CUSTOM_CALL=7, CUBLASLT=8, DYNAMIC_SLICE_FUSION=9, …` — see [`debugoptions-proto.md`](debugoptions-proto.md)).

```c
// list at offset 160 (field 258 region) — 6 elements appended:
//   1, 2, 8, 7, 3, 9
//   = FUSION, CUBLAS, CUBLASLT, CUSTOM_CALL, CUDNN, DYNAMIC_SLICE_FUSION
// list at offset 288 — 3 elements appended:
//   4, 5, 3
//   = COLLECTIVES, CONDITIONAL, CUDNN
```

The append idiom is the same six-line `RepeatedField<int>::GrowNoAnnotate` / `*((_DWORD *)this + 41) = newcount` / `array[idx] = enumval` block repeated per element (body lines 98–285). The enabled-by-default command-buffer command set is therefore `{FUSION, CUBLAS, CUBLASLT, CUSTOM_CALL, CUDNN, DYNAMIC_SLICE_FUSION}`.

| List offset | Element enum values | Decoded |
|---|---|---|
| 160 | 1, 2, 8, 7, 3, 9 | FUSION, CUBLAS, CUBLASLT, CUSTOM_CALL, CUDNN, DYN_SLICE_FUSION |
| 288 | 4, 5, 3 | COLLECTIVES, CONDITIONAL, CUDNN |

> **NOTE —** the two lists are distinct repeated fields (different base offsets, 160 vs 288, with independent count words at `this+41` and `this+73`). Which is `xla_gpu_enable_command_buffer` (258) versus a second command-buffer enable list was not disambiguated within budget; both are GPU-namespace fields inert on TPU but seeded because the shared upstream body seeds them.

---

## Map Seed — Analytical Latency Estimator Options

The constructor populates one `map<string,string>` (`xla_gpu_analytical_latency_estimator_options`, field 357, at offset ~216) with six default key→value pairs via `Map::TryEmplaceInternal` (body lines 335–468):

```text
nccl_op_launch_us  -> "-1"
nic_speed_gbps     -> "-1"
chunk_prep_us      -> "-1"
rtt_us             -> "-1"
chunk_size_bytes   -> "-1"
gpus_per_node      -> "-1"
```

Every value is the string `"-1"` — the AUTO/unknown sentinel for these GPU NCCL-modeling knobs. The `-1` convention here is the string form of the same "resolve later" sentinel documented in [`autoproto-autoor-resolution.md`](autoproto-autoor-resolution.md). On TPU these are inert (no GPU latency estimator runs), but the shared body seeds them regardless.

| Key | Default value |
|---|---|
| `nccl_op_launch_us` | `"-1"` |
| `nic_speed_gbps` | `"-1"` |
| `chunk_prep_us` | `"-1"` |
| `rtt_us` | `"-1"` |
| `chunk_size_bytes` | `"-1"` |
| `gpus_per_node` | `"-1"` |

---

## The Untouched Fields — Proto-Zero Defaults

Of the 290 fields, the constructor explicitly seeds roughly 70 scalars/strings + 2 repeated lists + 1 map. The remaining ~218 are never written after the `DebugOptions::DebugOptions(this, 0)` base call and therefore keep the proto3 zero default:

| Field type | Untouched default |
|---|---|
| `bool` | `false` |
| `int32` / `int64` | `0` |
| `float` | `0.0` |
| `string` | `""` |
| `enum` | value-0 entry (e.g. `StepMarkerLocation::STEP_MARK_AT_ENTRY=0`, `DetectionMode::DETECTION_MODE_NONE=0`, `ShapeChecks::IGNORE=0`) |
| `repeated` / `map` | empty |

> **NOTE —** `xla_step_marker_location` (108) is left at the proto-zero `STEP_MARK_AT_ENTRY=0` by this constructor: `DefaultDebugOptionsIgnoringFlags` at `0x1e66a860` contains **no** store to the step-marker field's offset. Any non-zero step-marker default in this build comes from the *flag* layer or PJRT merge, not from this constructor. The two flag-wired fields `xla_tpu_detect_nan` (135, offset 580) and `xla_tpu_detect_inf` (136) are likewise **not** written here — both stay `false` by direct absence of a store.

> **GOTCHA —** "untouched" is relative to *this* function only. The effective runtime default a consumer sees is `DefaultDebugOptionsIgnoringFlags()` output **then** overlaid by the flag layer (`AllocateFlags`/`MakeDebugOptionsFlags`) and the PJRT `CompileOptions.debug_options` merge. A field at proto-zero here can still arrive non-zero at the compiler. This page is the *first* layer only; the override order is on [`xla-flag-atlas.md`](xla-flag-atlas.md).

---

## Caller Chain

```text
xla::AllocateFlags (0x1e6b8020)
  └─ DefaultDebugOptionsIgnoringFlags (0x1e66a860)   ── build the baseline message
  └─ MakeDebugOptionsFlags (0x1e66ce80)              ── wrap fields as tsl::Flag overrides
       (flag layer; see xla-flag-atlas.md)
  ↓
PJRT compile path
  └─ merge CompileOptions.debug_options over the baseline
```

`DefaultDebugOptionsIgnoringFlags` is the leaf seed. `AllocateFlags` calls it, then `MakeDebugOptionsFlags` (`0x1e66ce80`) registers the flag bindings that can override the seeded values. The "IgnoringFlags" in the name is precise: this function's output is exactly the state *before* the flag layer touches anything.

---

## Related Components

| Name | Relationship |
|---|---|
| `DebugOptions::DebugOptions(this, 0)` | Base ctor; zeroes all 290 fields before the seed stores run |
| `set_xla_*` accessors | Per-field offset/has-bit writers; the anchor for every CONFIRMED row |
| `MakeDebugOptionsFlags` (`0x1e66ce80`) | Wraps the seeded fields as overridable flags (next layer) |
| `AllocateFlags` (`0x1e6b8020`) | Caller; runs the seed then the flag layer |
| `GetNonDefaultDebugOptions` (`0x1c920540`) | Inverse: diffs a message against this baseline to find user-set fields |

## Cross-References

- [`debugoptions-proto.md`](debugoptions-proto.md) — owns the field schema (number → name → type); this page maps those fields to their seeded values
- [`xla-flag-atlas.md`](xla-flag-atlas.md) — the flag layer that overrides these defaults; the override order after the baseline seed
- [`autoproto-autoor-resolution.md`](autoproto-autoor-resolution.md) — the `-1`/AUTO sentinel resolution used by the map seeds and `INT64_MAX`-style "no threshold" defaults
- [`overview.md`](overview.md) — the configuration subsystem map; why the GPU/CPU defaults are seeded whole in a TPU build
