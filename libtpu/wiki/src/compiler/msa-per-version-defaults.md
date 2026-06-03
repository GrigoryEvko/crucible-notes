# MSA Per-Version Defaults

> *All addresses, offsets, and flag values on this page apply to `libtpu` 0.0.40 (`libtpu-0.0.40-cp314`), `libtpu.so` build-id `89edbbe81c5b328a958fe628a9f2207d`. Other wheels — and any v5e/v6e-targeted build — may differ.*

## Abstract

Memory-Space Assignment (MSA) — the TPU compiler's Phase-7 alternate-memory placement pass (see [msa-overview.md](msa-overview.md)) — is not driven by a single hard-coded option struct. Its tuning knobs (overlap-to-async-copy ratios, outstanding-copy caps, repack/retry counts, the inefficient-use skip threshold, cross-program-prefetch enables, and the alternate-memory byte budget) are resolved **per TPU version** from a layered set of `absl` command-line flags. Each `tpu_version` is mapped to one of five *codename families* — `jf` (Jellyfish, v0/v1), `cmem` (the CMEM tier, v2/Pufferfish), `vf` (Viperfish, v3), `gf` (Ghostlite, v4/v5) — plus a global `xla_msa_*` fallback applied last. The effective value of any knob is therefore the cross product of *(version → family selection)* × *(family flag default)*.

A reimplementer's natural assumption — stated in the task that generated this page — is that the per-version numbers live in a `*_compilation_environment.binarypb` overlay shipped alongside the chip-config resources. **They do not.** No such overlay resource exists in this build; the filewrapper table-of-contents and `.rodata` carry only `chip_parts`, `chip_configs`, bootloaders, accuracy tables, route caches, ICI-resiliency, CSR/error blobs, and one timezone blob. The literal defaults instead live in the `absl::flags_internal::Flag<T>` objects in `.data`, and the runtime materializes them into a default `TpuCompilationEnvironment` proto at first use. Because both `memory_space_assignment.proto` and `tpu_compilation_environment.proto` are **proto3** in this build, neither descriptor stores `default_value` strings — which is exactly why the editions/text-format printer path shows no defaults.

This page owns the **byte-exact per-family numeric table** and the **two-stage resolution path**: (1) `GetTpuCompEnvWithDefaultValues()` walks the descriptor and copies each `absl` flag's current default into the matching field; (2) `ComputeMemorySpaceAssignmentOptions()` selects, per version, the family-specific flag with the global `xla_msa_*` flag as fallback, via `OverwriteFieldIfNotDefault`. The version→family choice is a 6-entry jump table inside `IsMemorySpaceAssignmentEnabled`.

For reimplementation, the contract is:

- The **version→family jump table** (`tpu_version` at `Target+0x398`, cases 0..5 → `jf`/`cmem`/`vf`/`gf`), and the `OverwriteFieldIfNotDefault(family_flag, msa_fallback)` resolution rule.
- The **literal per-family defaults**: the three overlap ratios (min/preferred uniform, max split 32×-jf vs 8×-rest), the outstanding-copy caps (4-jf vs 40-rest), repacks=4, retries=2, inefficient-use=0.5, cross-program-prefetch=1.
- The fact that the **alternate-memory byte budget is not a flag** — the byte-cap flags all default to `-1` ("derive from `Target`"), so `Options.max_size_in_bytes` comes from `chip_parts`, not a literal.
- The `absl::flags_internal::Flag<T>` object layout (`+0x48` default-value union: `kGenFunc` for floats/bools, `kOneWord` inline literal for the int caps).

| | |
|---|---|
| **Version→family gate** | `xla::jellyfish::IsMemorySpaceAssignmentEnabled` @ `0x12fc1280` |
| **Version jump table** | `0xae09ac8` — 6 entries (v0/1→jf, v2→cmem, v3→vf, v4/5→gf) |
| **Per-version selector** | `xla::jellyfish::ComputeMemorySpaceAssignmentOptions` @ `0x12fc1440` (55 `OverwriteFieldIfNotDefault` calls) |
| **Default materializer** | `xla::jellyfish::GetTpuCompEnvWithDefaultValues` @ `0x1d73f100` |
| **Resolution primitive** | `xla::jellyfish::OverwriteFieldIfNotDefault(family_flag, fallback, env&)` @ `0x1d73f360` |
| **`tpu_version` field** | `Target + 0x398` (`int32`) |
| **Default-value source** | `absl::flags_internal::Flag<T>` object `+0x48` (union: `kGenFunc` / `kOneWord`) |
| **Env binarypb overlay** | **None in this build** (no `*_compilation_environment.binarypb`) |

---

## Codename Families and the Version Gate

### Purpose

The first decision MSA makes is whether it runs at all, and — by the same code path — which *family* of flags governs it. `IsMemorySpaceAssignmentEnabled` reads `tpu_version` and jump-tables it to a family-specific `*_memory_space_assignment` enable flag, with `xla_msa_enable` as the global fallback. The same family selection (jf / cmem / vf / gf) then governs every numeric knob in `ComputeMemorySpaceAssignmentOptions`.

### Family Prefixes

| Prefix | Codename | `tpu_version` | Role |
|---|---|---|---|
| `jf` | Jellyfish | 0, 1 | Oldest family; the only one with non-default ratio/cap values |
| `cmem` | CMEM tier | 2 (Pufferfish) | Selected for v2; carries the inverse `min_async_copy_to_overlap_ratio` twin |
| `vf` | Viperfish | 3 | |
| `gf` | Ghostlite | 4, 5 | |
| `msa` | global fallback | — | `xla_msa_*` flags applied last by `OverwriteFieldIfNotDefault` |
| `zf` | forward stub | — (not gated) | `xla_zf_vmem_max_outstanding_*` exist but `zf` is absent from the v0..v5 table |

> **QUIRK —** `zf` is a real flag prefix in this binary (`xla_zf_vmem_max_outstanding_prefetches` HelpGen @ `0x1d72ef40`, `_evictions` @ `0x1d72f000`, both defaulting to 40) but it is **not** in the 0..5 version gate and has no overlap-ratio / repack / retry variants. It is a forward-family stub not yet wired into any `tpu_version`. A reimplementer should carry the flag names but must not route any current version to it.

### Algorithm

The gate switches on `*(Target + 0x398)` (decompiler names it `a2 + 920` = `0x398`). Each case calls `OverwriteFieldIfNotDefault` with the global `xla_msa_enable` flag first and the family-specific `*_memory_space_assignment` flag as the per-version override; the resolved value lands in a local `TpuCompilationEnvironment` and is read back at `LABEL_11`.

```c
function IsMemorySpaceAssignmentEnabled(out, Target, env_view, HloModule):  // 0x12fc1280
    if IsPassDisabled("memory-space-assignment", env_view):     // 0x12fc1280 entry guard
        out.enabled = false; return                             //   pass explicitly disabled

    TpuCompilationEnvironment local;                            // built from defaults
    switch (*(int32*)(Target + 0x398)):                         // tpu_version
        case 0: case 1:                                         // jf — Jellyfish
            OverwriteFieldIfNotDefault("xla_msa_enable",
                "xla_jf_vmem_memory_space_assignment", local)
        case 2:                                                 // cmem — Pufferfish
            OverwriteFieldIfNotDefault("xla_msa_enable",
                "xla_tpu_cmem_memory_space_assignment", local)
        case 3:                                                 // vf — Viperfish
            OverwriteFieldIfNotDefault("xla_msa_enable",
                "xla_vf_vmem_memory_space_assignment", local)
        case 4: case 5:                                         // gf — Ghostlite
            OverwriteFieldIfNotDefault("xla_msa_enable",
                "xla_gf_vmem_memory_space_assignment", local)
        default:                                                // unknown version
            // LABEL_11: fall through to the Tristate read below

    // LABEL_11 — read the resolved enable:
    if local.msa_enable_tristate != UNSET:                      // local field v11
        out.enabled = (local.msa_enable_tristate == ENABLED)    //   Tristate==2
    else:
        out.enabled = (*(int32*)(Target + 0x398) >= 2)          //   default: on for v>=2
```

> **NOTE —** the resolution primitive is named `OverwriteFieldIfNotDefault(global, override, env)`: it writes the global default into the field, then overwrites it with the family-specific flag's value *only if that flag is non-default*. The decompiled call passes the global (`xla_msa_enable`, length 14) as arg 1 and the family flag (lengths 35 for `xla_jf/vf/gf_vmem_memory_space_assignment`, 36 for `xla_tpu_cmem_memory_space_assignment`) as the override — matching the flag-name `.rodata` lengths byte-for-byte. The source file recorded on the `AddSourceLocationImpl` error path is `platforms/xla/service/jellyfish/memory_space_assignment_util.cc`.

### The enable defaults

All five `*_memory_space_assignment` family flags default to **1 (enabled)**. The global `xla_msa_enable` is a **Tristate** whose default-gen (`AbslFlagDefaultGenForxla_msa_enable::Gen` @ `0x1d705500`) writes a single byte **2 = ENABLED**. So MSA is on by default for every gated version; the `default:` fallthrough additionally enables it for any unknown `tpu_version >= 2`.

| Flag | Type | Default | Confidence |
|---|---|---|---|
| `xla_jf_vmem_memory_space_assignment` | bool | **1** | CERTAIN |
| `xla_vf_vmem_memory_space_assignment` | bool | **1** | CERTAIN |
| `xla_gf_vmem_memory_space_assignment` | bool | **1** | CERTAIN |
| `xla_tpu_cmem_memory_space_assignment` | bool | **1** | CERTAIN |
| `xla_msa_enable` (global) | Tristate | **2 (ENABLED)** | CERTAIN — byte-exact @ `0x1d705500` |

---

## Per-Family Numeric Defaults

These are the literal values a reimplementer must reproduce. Every float is materialized byte-exact from its default-gen body (`mov dword[rdi], imm32; ret`); every int cap is the inline `kOneWord` literal at `Flag<T>+0x48`. The Confidence column marks how each was verified.

### Overlap-to-async-copy ratios

The three floats feed the `PrefetchIntervalPicker` constructor (P-3-220 #5, `@0x1dcd6b60`): `picker+0x80` ← min, `picker+0x84` ← preferred, `picker+0x88` ← a product derived from preferred × max. `min` seeds the latest legal prefetch time (`use_time − ⌈min × async_copy_elapsed⌉`); `max` bounds how far ahead of the use a prefetch may be hoisted.

| Option | jf | vf | gf | cmem | msa | Confidence |
|---|---:|---:|---:|---:|---:|---|
| `min_overlap_to_async_copy_ratio` | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | CERTAIN |
| `preferred_overlap_to_async_copy_ratio` | 2.0 | 2.0 | 2.0 | 2.0 | 2.0 | CERTAIN |
| `max_overlap_to_mem_size_async_copy_ratio` | **32.0** | 8.0 | 8.0 | 8.0 | 8.0 | CERTAIN |

> **QUIRK —** only the **max** ratio differs by family, and only Jellyfish stands apart: on `jf` a buffer may be prefetched up to **32×** its own async-copy elapsed time ahead of the use; on every later family only **8×**. `min` (1.0) and `preferred` (2.0) are uniform across all families. Byte evidence: `jf` max default-gen @ `0x1d72cae0` writes `0x42000000` (=32.0f); `gf` max @ `0x1d72dfa0` and `vf` max @ `0x1d72d4a0` both write `0x41000000` (=8.0f); `jf` min @ `0x1d72cc80` writes `0x3F800000` (=1.0f); `jf` preferred @ `0x1d72cd60` writes `0x40000000` (=2.0f). `cmem` additionally carries an inverse twin `xla_tpu_cmem_min_async_copy_to_overlap_ratio` = 1.0.

### Outstanding-copy caps and repack/retry counts

The outstanding caps are the ceiling enforced by the `AsynchronousCopyResource` time-bucket model: a candidate copy that would exceed the cap returns `FailOutOfAsyncCopies` (status `0x10`). These are stored as `kOneWord` inline literals (no default-gen function), confirmed by the family-paired flag names referenced inside `ComputeMemorySpaceAssignmentOptions`.

| Option | jf | vf | gf | zf | cmem | msa | Confidence |
|---|---:|---:|---:|---:|---:|---:|---|
| `max_outstanding_prefetches` | **4** | 40 | 40 | 40 | 40 | 40 | HIGH |
| `max_outstanding_evictions` | **4** | 40 | 40 | 40 | 40 | 40 | HIGH |
| `max_repacks` | 4 | 4 | 4 | — | 4 | 4 | HIGH |
| `max_retries` | 2 | 2 | 2 | — | 2 | 2 | HIGH |

> **QUIRK —** the outstanding-copy budget splits Jellyfish from everyone else by **10×**: 4 concurrent prefetches/evictions on `jf` versus 40 on every later family (and `zf`). `max_repacks` (4) and `max_retries` (2) are uniform across all families — the int caps are inline `kOneWord` literals at `Flag<T>+0x48`, not default-gen functions, so they do not appear as `Gen` symbols in the decompile; they are confirmed by name through the family-paired `OverwriteFieldIfNotDefault` argument pairs (`xla_{jf,vf,gf,tpu_cmem,msa}_…_max_outstanding_prefetches`, `…_evictions`, `…_max_repacks`, `…_max_retries`) all present in `ComputeMemorySpaceAssignmentOptions` @ `0x12fc1440`.

### Cross-program prefetch

Cross-program prefetch is the only knob besides `max_overlap` whose *enable* differs by family. The cap itself is global (1), but the per-family `*_vmem_enable_cross_program_prefetch` bool is **0 on Jellyfish, 1 on vf/gf**.

| Flag | jf | vf | gf | cmem | msa | Confidence |
|---|---:|---:|---:|---:|---:|---|
| `<family>_vmem_enable_cross_program_prefetch` | **0** | 1 | 1 | — | — | HIGH |
| `<family>_vmem_enable_cross_program_prefetch_freeing` | — | 1 | 1 | — | 1 | HIGH |
| `<family>_vmem_enable_while_redundant_eviction_elimination` | 1 | 1 | 1 | 1 | 1 | HIGH |
| `<family>_vmem_default_cross_program_prefetch_heuristic` | 0 | 0 | 0 | — | 0 | HIGH |

`msa_enable_cross_program_prefetch_freeing` default-gen @ `0x1d705e00` writes byte **1**; the `jf`/`vf`/`gf` `enable_cross_program_prefetch` and `_freeing` flags exist (HelpGen symbols present, e.g. `jf` enable @ `0x1d72c7e0`, `vf` enable @ `0x1d72d4c0`, `gf` enable @ `0x1d72dfc0`) but use `kOneWord`/inline defaults rather than `Gen` bodies, so the per-family 0/1 split is HIGH (not byte-exact from a `Gen`).

The global shared bools beneath the family flags:

| Global flag | Default |
|---|---:|
| `xla_tpu_enable_cross_program_prefetch_freeing` | 1 |
| `xla_enable_cross_program_prefetch` | 1 |
| `xla_default_cross_program_prefetch_heuristic` | 0 |

---

## Singleton and Global Defaults

Several MSA knobs have no per-family variant — a single global flag governs all versions. The byte-cap flags are the important ones: they all default to **-1**, which means "no override; derive the budget from `Target`."

### Float and integer globals

| Flag | Type | Default | Confidence |
|---|---|---:|---|
| `xla_tpu_msa_inefficient_use_to_copy_ratio` | float | **0.5** | CERTAIN — `0x3F000000` @ `0x1d721c60` |
| `xla_msa_max_cross_program_prefetches` | int64 | 1 | HIGH |
| `xla_max_cross_program_prefetches` | int | 1 | HIGH |
| `xla_tpu_llo_compilation_max_retries` | int32 | 15 | HIGH |
| `xla_tpu_scoped_vmem_limit_kib` | int64 | **-1** (none) | HIGH |
| `xla_tpu_prefetch_interval_picker_size_override` | int | **-1** (none) | HIGH |
| `xla_vf_max_vmem_used_by_memory_space_assignment` | int64 | **-1** (none) | HIGH |
| `xla_gf_max_vmem_used_by_memory_space_assignment` | int64 | **-1** (none) | HIGH |
| `xla_tpu_max_cmem_used_by_memory_space_assignment` | int64 | **-1** (none) | HIGH |
| `xla_tpu_sliced_prefetch_max_slices` | int32 | `0xFFFFFFFF` (unset) | HIGH |
| `xla_tpu_sliced_prefetch_min_bytes` | int64 | -1 (unset) | HIGH |
| `xla_tpu_sliced_prefetch_preferred_slice_size` | int64 | -1 (unset) | HIGH |
| `xla_tpu_auto_spmd_partitioning_memory_budget_ratio` | float | 1.1 | HIGH — `@0x1d70db60` |

`xla_tpu_msa_inefficient_use_to_copy_ratio` (0.5) is the alt-mem skip threshold: if the ratio of in-alt-mem idle time to copy time exceeds 0.5, the buffer is **not** placed in alternate memory. Verified byte-exact: default-gen @ `0x1d721c60` writes `0x3F000000` = 0.5f.

> **GOTCHA —** the alternate-memory byte budget (`Options.max_size_in_bytes`) is **not** read from any flag. Every `*_max_vmem/cmem_used_by_memory_space_assignment` flag and `xla_tpu_scoped_vmem_limit_kib` default to `-1`, signalling "derive from `Target`." The budget is the VMEM hardware size (64 MiB = 67,108,864 B on v7 — see [tpu-chip-config.md](../targets/tpu-chip-config.md)) minus any scoped reservation, filled from [chip-parts-binarypb.md](../targets/chip-parts-binarypb.md), not from a flag literal. A reimplementation that looks for a "MSA budget" flag will find only `-1` and must fall back to the chip config.

### `copies_limit_for_sync_mem_op_conversion` is computed, not a flag

This bound is **not** a top-level flag. It is a derived field of the MSA `Options` struct (TextFormat key `extend_async_copies_limit_for_sync_mem_op_conversion:`), computed inside `ComputeMemorySpaceAssignmentOptions` from the outstanding-copy caps and the two sync-replacement enables (`xla_msa_enable_sync_copy_replacement`, `xla_msa_enable_sync_slice_replacement`, both AutoProto default = unset/AUTO). There is no per-version literal for it.

> **NOTE —** the basic-block-level formula for `copies_limit_for_sync_mem_op_conversion` was not traced. It is a function of the outstanding caps (4 or 40) plus the two sync-replacement enables; since those enables default to AUTO/unset, the sync→async conversion is effectively gated off unless overridden. (Not traced — admitted gap.)

### Boolean / Tristate global enables

| Flag | Type | Default | Confidence |
|---|---|---:|---|
| `xla_msa_allocate_scoped_memory_at_same_offset` | bool | 1 | HIGH |
| `xla_tpu_allocate_scoped_vmem_at_same_offset` | bool | 1 | HIGH |
| `xla_tpu_allocate_scoped_cmem_at_same_offset` | bool | 0 | HIGH |
| `xla_msa_use_bundle_aware_cost_model` | Tristate | 2 (ENABLED) | HIGH — `@0x1d72f260` |
| `xla_msa_cross_program_prefetch_permissive_mode` | bool | 0 | HIGH |
| `xla_msa_experimental_use_telamalloc` | Tristate | 0 (AUTO) | HIGH |
| `xla_tpu_vmem_use_telamalloc` / `cmem_use_telamalloc` | bool | 0 / 0 | HIGH |
| `xla_tpu_msa_use_minimalloc` / `use_tinymalloc` | bool | 0 / 0 | HIGH |
| `xla_tpu_msa_reduce_scoped_vmem_limit` | bool | 0 | HIGH |
| `xla_msa_enable_sync_copy_replacement` | AutoProto | 0 (AUTO) | HIGH |
| `xla_msa_enable_sync_slice_replacement` | AutoProto | 0 (AUTO) | HIGH |
| `xla_msa_enable_window_prefetch` | AutoProto | 0 (AUTO) | HIGH |
| `xla_vf_allow_split_vmem` | AutoProto | 0 (AUTO) | HIGH |
| `xla_msa_expanded_scoped_alternate_memory_mode` | AutoProto | 0 (UNDEFINED) | HIGH |

> **GOTCHA —** the AutoProto-wrapped knobs (`sync_copy`/`sync_slice` replacement, `window_prefetch`, `expanded_scoped_alternate_memory_mode`, `allow_split_vmem`) all default to the empty/AUTO instance (0). "AUTO" means "no opinion at the flag layer" — a specific `Target` may force them ON in per-version C++ that was not traced here. Do not read AUTO=0 as "feature disabled"; read it as "decision deferred."

---

## How the Defaults Are Stored and Materialized

### The `Flag<T>` object layout

Each MSA knob is an `ABSL_FLAG` whose object is a 0x60-byte `absl::flags_internal::Flag<T>` (FlagImpl) in `.data`. The default lives at offset `+0x48`:

```text
absl::flags_internal::Flag<T>   (0x60 bytes, .data)
  +0x00  vtable (FlagImpl, 0x22040f18)
  +0x08  const char* name        → .rodata flag-name string
  +0x10  const char* type_name   → "int64_t" / "float" / "bool" / …
  +0x18  const char* filename    → tpu_compilation_environment.cc
  +0x20  FlagOps<T>              → e.g. FlagOps<long> @ 0xe8cd240
  +0x28  HelpGen function        → AbslFlagHelpGenFor<flag>::NonConst
  +0x30  packed metadata word    (data_guard / value-kind bits)
  +0x38  lazy value sentinel     (-1 / 0xff before first access)
  +0x40  default-kind word       (0)
  +0x48  DEFAULT-VALUE UNION:
           kGenFunc  → AbslFlagDefaultGenFor<flag>::Gen
                       body = `mov dword[rdi], imm32; ret` (float)
                            or `mov byte[rdi], imm8; ret`  (bool/enum)
           kOneWord  → inline int64 literal (the int caps: 4 / 40 / 2 / -1 / 0xFFFFFFFF)
```

> **QUIRK —** floats and bools use a **`Gen` function** (a tiny `mov imm; ret` body) at `+0x48`; the integer caps use a **`kOneWord` inline literal**. This is why `max_outstanding_prefetches` and `max_repacks` have **no** `Gen` symbol in the decompile — their default is a raw immediate in the `Flag<T>` object, recovered by reading `+0x48` directly, not by disassembling a `Gen` body. A reimplementer scanning for `AbslFlagDefaultGenFor*` will find every float ratio but none of the int caps.

### Two-stage resolution

```text
STAGE 1 — materialize defaults (once, lazily)
  GetTpuCompEnvWithDefaultValues()                @ 0x1d73f100
    └─ $_0::operator()()                          @ 0x1d73f1a0   per-field loop
         for each FieldDescriptor in TpuCompilationEnvironment:
           flag = TpuCompEnvReflection::GetFlagForField(field)   @ 0x1d74ad40
           SetFieldFromFlagString(flag, default, env)            @ 0x1d73fcc0
    └─ result: default TpuCompilationEnvironment proto
                (singleton @ 0x22803928, sizeof 0x15e8, guard @ 0x2257ec08)

STAGE 2 — per-version selection (per compile)
  ComputeMemorySpaceAssignmentOptions(Target&, AliasInfo*, HloModule&)  @ 0x12fc1440
    for each MSA Option:                          (55 calls total)
      OverwriteFieldIfNotDefault(                 @ 0x1d73f360
        "xla_<family>_vmem_<opt>",   // family chosen by jump table @ 0xae09ac8
        "xla_msa_<opt>",             // global fallback
        env)
```

`GetTpuCompEnvWithDefaultValues` (Stage 1) iterates every `FieldDescriptor`, resolves the matching `CommandLineFlag` via reflection, and copies the flag's current default into the field. The result is the default `TpuCompilationEnvironment` proto — **the default proto equals the `absl` flag defaults**, not a hard-coded table and not a binarypb overlay. The process-new-env hook `ProcessNewTpuCompilationEnvironment` @ `0x1d742c80` (registered via `RegisterProcessNewEnvFn`) wires these defaults into the live `CompilationEnvironments`.

`ComputeMemorySpaceAssignmentOptions` (Stage 2) is where per-version selection happens: for each Option it calls `OverwriteFieldIfNotDefault(family_flag, msa_fallback, env)`, with the family chosen by the version jump table at `0xae09ac8`. The decompile shows all five family prefixes paired against the `xla_msa_*` global for the overlap ratios and the int caps — 55 `OverwriteFieldIfNotDefault` calls in total.

> **NOTE —** there is **no env binarypb overlay** in this build. The defaults are `absl` flag defaults, full stop. A later v5e/v6e-targeted wheel might ship a literal `TpuCompilationEnvironment` binarypb; this 0.0.40 wheel does not (filewrapper TOC carries only chip_parts/chip_configs/bootloaders/accuracy/route-cache/ICI/CSR-error/tz). See [tpu-compilation-environment.md](../config/tpu-compilation-environment.md).

---

## Worked Chain — `max_overlap` on Ghostlite (v5)

End-to-end, for the single knob `max_overlap_to_mem_size_async_copy_ratio` on a v5 (Ghostlite) target:

```c
// 1. version → family
tpu_version = *(int32*)(Target + 0x398);              // = 5
family = jump_table_0xae09ac8[5];                     // → gf (Ghostlite)

// 2. per-version selection
OverwriteFieldIfNotDefault(
    "xla_gf_vmem_max_overlap_to_mem_size_async_copy_ratio",   // family flag
    "xla_msa_max_overlap_to_mem_size_async_copy_ratio",       // global fallback
    env);                                             // inside ComputeMSAOptions @ 0x12fc1440

// 3. the family flag's literal default
AbslFlagDefaultGenForxla_gf_vmem_max_overlap_…::Gen   // @ 0x1d72dfa0
    *(uint32*)rdi = 0x41000000;                       // = float 8.0
// (had this been Jellyfish, jf gen @ 0x1d72cae0 = 0x42000000 = 32.0)

// 4. consumed by the prefetch window
PrefetchIntervalPicker.ctor(...)                      // @ 0x1dcd6b60 (P-3-220 #5)
    picker[0x80] = min(1.0);
    picker[0x84] = preferred(2.0);
    picker[0x88] = derived(preferred × max=8.0);
    // Begin: latest_prefetch_time = use_time − ⌈min(1.0) × async_copy_elapsed⌉
```

So on Ghostlite a buffer may be prefetched at most ~8× its own copy time ahead of the use; the picker sweeps candidate start times between that bound and the use. On Jellyfish the same knob resolves to 32×, the only family with a non-default `max` ratio.

---

## MSA Config Proto Schema

The upstream XLA `memory_space_assignment.proto` is embedded in `protodesc_cold` (FileDescriptorProto @ VA `0xbfe06e0`, 2,939 B, **proto3**). The relevant message shapes (decoded byte-exact):

```text
message SlicedPrefetchOptions {
  uint32 max_slices = 1;  uint64 min_bytes = 2;
  bool   fail_on_non_alignment_boundary_slice_proposal = 3;
  uint32 all_slice_time_permutations_threshold = 4;
  uint64 preferred_slice_size = 5;
}
message MemoryBoundLoopOptimizerOptions {
  bool  enabled = 1;  float desired_copy_ratio = 2;
  bool  allow_unsatisfied_fully_pipelined_prefetch = 3;
  float min_num_iterations = 4;          // FLOAT
}
enum ExpandedScopedAlternateMemoryMode.Value { UNDEFINED=0; DISABLED=1; ENABLED=2; }
```

> **GOTCHA —** because both `memory_space_assignment.proto` (@ `0xbfe06e0`) and `tpu_compilation_environment.proto` (@ `0xbfa6060`, 137,692 B, 1,121 fields) are **proto3**, neither descriptor carries `default_value` strings. The numeric defaults are not in the descriptor — they are in the `absl` flag layer (above). MSA flag fields in the TCE proto are tagged `MEMORY_SPACE_ASSIGNMENT (101)` via the custom field-option extension `#535801365` (`TpuCompEnvFieldOptions{is_used_at_runtime=1, tags=101}`). A reimplementer who parses the descriptor expecting per-field defaults will find none and must read the flag objects instead.

The `MemoryBoundLoopOptimizerOptions` defaults (`desired_copy_ratio`, `min_num_iterations`) are an *embedded message*, not scalar flags — their defaults live in the message default-instance and were **not** decoded here (`xla_tpu_memory_bound_loop_optimizer_options`, TCE field 568; ctor @ `0x1e6c5120`). (Not traced — admitted gap.)

---

## Evidence Anchors

| Symbol / Anchor | VA | Role | Confidence |
|---|---|---|---|
| `IsMemorySpaceAssignmentEnabled` | `0x12fc1280` | Version gate + family select | CERTAIN |
| `ComputeMemorySpaceAssignmentOptions` | `0x12fc1440` | Per-version selection (55 Overwrite calls) | CERTAIN |
| version jump table | `0xae09ac8` | 6 entries: v0/1→jf, v2→cmem, v3→vf, v4/5→gf | HIGH |
| `GetTpuCompEnvWithDefaultValues` | `0x1d73f100` | Default-proto materializer | HIGH |
| `…::$_0::operator()` | `0x1d73f1a0` | Per-field copy loop | HIGH |
| default `TpuCompilationEnvironment` | `0x22803928` | singleton (sizeof 0x15e8), guard `0x2257ec08` | HIGH |
| `TpuCompEnvReflection::GetFlagForField` | `0x1d74ad40` | field → CommandLineFlag | HIGH |
| `SetFieldFromFlagString` | `0x1d73fcc0` | copy flag default into field | HIGH |
| `ProcessNewTpuCompilationEnvironment` | `0x1d742c80` | process-new-env hook | HIGH |
| `OverwriteFieldIfNotDefault` | `0x1d73f360` | resolution primitive | CERTAIN |
| `jf max_overlap` Gen (`0x42000000`=32.0) | `0x1d72cae0` | byte-exact float | CERTAIN |
| `gf max_overlap` Gen (`0x41000000`=8.0) | `0x1d72dfa0` | byte-exact float | CERTAIN |
| `vf max_overlap` Gen (`0x41000000`=8.0) | `0x1d72d4a0` | byte-exact float | CERTAIN |
| `jf min_overlap` Gen (`0x3F800000`=1.0) | `0x1d72cc80` | byte-exact float | CERTAIN |
| `jf preferred_overlap` Gen (`0x40000000`=2.0) | `0x1d72cd60` | byte-exact float | CERTAIN |
| `inefficient_use_to_copy_ratio` Gen (`0x3F000000`=0.5) | `0x1d721c60` | byte-exact float | CERTAIN |
| `xla_msa_enable` Gen (byte 2) | `0x1d705500` | byte-exact Tristate | CERTAIN |
| `msa_enable_cross_program_prefetch_freeing` Gen (byte 1) | `0x1d705e00` | byte-exact bool | CERTAIN |
| `msa_use_bundle_aware_cost_model` Gen (byte 2) | `0x1d72f260` | byte-exact Tristate | HIGH |
| `zf max_outstanding_prefetches` (=40) | `0x1d72ef40` | forward-stub flag (not gated) | HIGH |
| `memory_space_assignment.proto` FDP | `0xbfe06e0` | proto3, 2,939 B | HIGH |
| `tpu_compilation_environment.proto` FDP | `0xbfa6060` | proto3, 137,692 B, 1,121 fields | HIGH |

---

## Open Items

These were not traced and remain LOW/unknown:

- **`copies_limit_for_sync_mem_op_conversion` formula** — computed inside `ComputeMemorySpaceAssignmentOptions` from the outstanding caps + sync-replacement enables; the basic-block-level derivation was not recovered.
- **`MemoryBoundLoopOptimizerOptions` message defaults** — `desired_copy_ratio` / `min_num_iterations` live in the embedded-message default-instance (TCE field 568), not decoded here.
- **`zf` activation path** — the family's outstanding caps exist (=40) but it is absent from the v0..v5 jump table; the version (or `Target`-level override) that routes to it is unknown.
- **Pufferfish (v2) overlap/repack/retry** — v2 routes to the `cmem` tier (min=1.0/pref=2.0/max=8.0, caps 40, repacks=4, retries=2); whether v2 *also* consults a vmem family via a `Target`-level override was not re-derived.
- **Per-`Target` overrides of AUTO knobs** — any per-version C++ that forces a sync-replacement / window-prefetch / split-vmem knob ON for a specific `Target` was not traced.

---

## Cross-References

- [msa-overview.md](msa-overview.md) — Phase 7 memory-space-assignment pass; this page supplies its per-version numeric configuration.
- [msa-allocate-segment.md](msa-allocate-segment.md) — the allocation body these defaults parameterize (outstanding caps, repacks/retries).
- [msa-reservation-hbm-policy.md](msa-reservation-hbm-policy.md) — the reservation/HBM policy that, together with the chip-config budget, sets `max_size_in_bytes`.
- [compile-phases.md](compile-phases.md) — Phase-7 placement; `RunMemorySpaceAssignment` @ `0x12fc3080`.
- [tpu-compilation-environment.md](../config/tpu-compilation-environment.md) — the 1,121-field TCE proto whose MSA fields these flags populate (no binarypb overlay).
- [tce-field-offsets-defaults.md](../config/tce-field-offsets-defaults.md) — field#→offset map and the ABSL-flag default mechanism shared by all TCE fields.
- [tpu-chip-config.md](../targets/tpu-chip-config.md) — the per-version chip config that supplies the VMEM byte budget (`max_size_in_bytes`), since the MSA byte-cap flags default to `-1`.
- [chip-parts-binarypb.md](../targets/chip-parts-binarypb.md) — the `chip_parts` resource the budget is derived from.
- [tpu-version-codename-matrix.md](../targets/tpu-version-codename-matrix.md) — `tpu_version` ↔ codename mapping behind the jf/cmem/vf/gf family selection.
