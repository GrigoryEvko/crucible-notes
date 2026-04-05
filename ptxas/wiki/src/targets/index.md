# SM Architecture Map

ptxas validates the `--gpu-name` target against three sorted lookup tables, constructs a profile object with family metadata and a `CUDA_ARCH` macro value, then populates seven parallel dispatch tables that drive capability checks, code generation factory selection, performance modeling, and occupancy calculation throughout the compiler. The default target is `sm_75` (Turing). Every downstream decision -- instruction legality, encoder selection, register file geometry, scheduling latencies -- routes through the profile object built here.

| | |
|---|---|
| **SM validation** | `sub_6765E0` (54KB, profile object construction) |
| **Capability dispatch** | `sub_607DB0` (14KB, 7 parallel hash maps) |
| **Default target** | `sub_6784B0` -- returns `sm_75` when `--gpu-name` is omitted |
| **Validation tables** | 3 bsearch arrays: base (32 entries at `unk_1D16220`), `f` (6 entries at `unk_1D16160`), `a` (7 entries at `unk_1D161C0`) |
| **Per-SM accessors** | `sub_609XXX` cluster (24 functions, ~1.2KB each) |
| **Per-SM intrinsic init** | `sub_60AXXX` cluster (12 functions, ~1KB each) |
| **Profile lookup** | `sub_608D70` (384 bytes, dispatcher registered via `sub_42BEC0`) |

**Per-SM Deep Dives:**

- [Turing & Ampere (SM 75--88)](turing-ampere.md) -- Baseline feature set, codegen factory 24577/28673
- [Ada & Hopper (SM 89--90a)](ada-hopper.md) -- WGMMA, cluster operations, codegen factory 32768
- [Blackwell (SM 100--121)](blackwell.md) -- tcgen05, arch/family gating, codegen factory 36864
- [TCGen05 -- 5th Gen Tensor Cores](tcgen05.md) -- Blackwell tensor core ISA detail

## Complete SM Table

23 active SM base targets ship in ptxas v13.0.88. Each base target optionally has `a` (accelerated) and/or `f` (feature-reduced) sub-variants. The `CUDA_ARCH` column shows the value the `-D__CUDA_ARCH__` macro expands to.

| SM | `__CUDA_ARCH` | Family | Product | Codegen Factory | Status | Deep Dive |
|---|---|---|---|---|---|---|
| `sm_75` | 750 | Turing | TU10x (RTX 20xx) | 24577 | Production | [turing-ampere](turing-ampere.md) |
| `sm_80` | 800 | Ampere | A100 | 28673 | Production | [turing-ampere](turing-ampere.md) |
| `sm_86` | 860 | Ampere | A40/A10/RTX 30xx | 28673 | Production | [turing-ampere](turing-ampere.md) |
| `sm_87` | 870 | Ampere | Orin (Jetson) | 28673 | Production | [turing-ampere](turing-ampere.md) |
| `sm_88` | 880 | Ampere | -- | 28673 | Production | [turing-ampere](turing-ampere.md) |
| `sm_89` | 890 | Ada Lovelace | AD10x (RTX 40xx) / L40S | 28673 | Production | [ada-hopper](ada-hopper.md) |
| `sm_90` / `sm_90a` | 900 | Hopper | H100 / H200 | 32768 | Production | [ada-hopper](ada-hopper.md) |
| `sm_100` / `sm_100a` / `sm_100f` | 1000 | Blackwell | B200 (datacenter) | 36864 | Production | [blackwell](blackwell.md) |
| `sm_103` / `sm_103a` / `sm_103f` | 1030 | Blackwell Ultra | GB300 (datacenter) | 36864 | Production | [blackwell](blackwell.md) |
| `sm_110` / `sm_110a` / `sm_110f` | 1100 | Jetson Thor | Thor SoC (auto/robotics) | 36864 | Production | [blackwell](blackwell.md) |
| `sm_120` / `sm_120a` / `sm_120f` | 1200 | Blackwell (sm120) | RTX 50xx / RTX Pro | 36864 | Production | [blackwell](blackwell.md) |
| `sm_121` / `sm_121a` / `sm_121f` | 1210 | Blackwell (sm120) | DGX Spark | 36864 | Production | [blackwell](blackwell.md) |

The family name stored in the profile object (from `sub_6765E0`) uses NVIDIA's internal naming: `"Turing"`, `"Ampere"`, `"Hopper"`, `"Blackwell"`. Ada Lovelace (sm_89) is stored as Ampere-derived internally despite being a distinct microarchitecture. sm_120/121 use `"Blackwell"` internally despite being a different consumer microarchitecture from sm_100 datacenter Blackwell.

### Suffix Semantics

ptxas uses three suffix modes to control forward compatibility. The distinction is critical: it determines which SASS binary a cubin can execute on.

| Suffix | Meaning | Forward Compatibility | Validation Table |
|---|---|---|---|
| (none) | Base feature set | Full forward-compat across generations | `unk_1D16220` (32 entries) |
| `a` (accelerated) | Architecture-locked, advanced features | **No** forward compat -- locked to specific silicon | `unk_1D161C0` (7 entries) |
| `f` (feature-reduced) | Same-family forward compat only | Forward-compat within family, not across | `unk_1D16160` (6 entries) |

The base variant (no suffix) produces SASS that runs on the named architecture and all later ones: `sm_80` code runs on sm_86, sm_89, sm_90, sm_100, etc. The `a` suffix locks the binary to exact silicon: `sm_90a` code runs only on H100/H200 hardware and will not execute on Blackwell. The `f` suffix allows forward compatibility within the same family: `sm_100f` code runs on sm_100 and sm_103 (both Blackwell datacenter) but not on sm_120 (different family).

**Compilation rules from help text:**

- `sm_90a` PTX must be compiled to `sm_90a` SASS (no cross-arch compilation)
- `sm_100f` PTX can compile to `sm_100f` or `sm_103f` SASS (same family)
- `sm_100a` PTX must compile to `sm_100a` SASS only
- Base `sm_100` PTX compiles to any `sm_100+` SASS

### Sub-Variant Expansion

| Base | `a` Variant | `f` Variant | `CUDA_ARCH` (a) | `CUDA_ARCH` (f) |
|---|---|---|---|---|
| `sm_90` | `sm_90a` | -- | `90a0` | -- |
| `sm_100` | `sm_100a` | `sm_100f` | `100a0` | `100f0` |
| `sm_103` | `sm_103a` | `sm_103f` | `103a0` | `103f0` |
| `sm_110` | `sm_110a` | `sm_110f` | `110a0` | `110f0` |
| `sm_120` | `sm_120a` | `sm_120f` | `120a0` | `120f0` |
| `sm_121` | `sm_121a` | `sm_121f` | `121a0` | `121f0` |

sm_75 through sm_89 have no `a` or `f` variants. sm_90 has only the `a` variant (no `f`). All Blackwell-era targets (sm_100+) have both `a` and `f`.

## SM Validation Tables

Target name validation uses three sorted arrays searched via `bsearch()`. The CLI parser extracts the SM string from `--gpu-name`, strips any suffix, and searches the appropriate table.

### Base Table -- `unk_1D16220` (32 entries)

Contains all valid base SM names without suffix. Includes legacy architectures no longer supported for active compilation but retained for validation:

```
sm_20, sm_21, sm_30, sm_32, sm_35, sm_37,     // Fermi/Kepler (legacy)
sm_50, sm_52, sm_53,                           // Maxwell (legacy)
sm_60, sm_61, sm_62,                           // Pascal (legacy)
sm_70, sm_72, sm_73,                           // Volta (legacy)
sm_75,                                         // Turing (active)
sm_80, sm_86, sm_87, sm_88, sm_89,             // Ampere/Ada (active)
sm_90,                                         // Hopper (active)
sm_100, sm_103, sm_110, sm_120, sm_121         // Blackwell (active)
```

### Accelerated Table -- `unk_1D161C0` (7 entries)

```
sm_90a, sm_100a, sm_103a, sm_110a, sm_120a, sm_121a
```

One Hopper entry, five Blackwell entries.

### Feature-Reduced Table -- `unk_1D16160` (6 entries)

```
sm_100f, sm_103f, sm_110f, sm_120f, sm_121f
```

No Hopper entry (sm_90 has no `f` variant). All Blackwell-era.

## Architecture Registration -- `sub_6765E0`

This 54KB function constructs profile objects for every SM version. Each profile contains:

| Field | Content | Example (sm_90) |
|---|---|---|
| SM name | `"sm_90"` | `"sm_90"` |
| Compute name | `"compute_90"` | `"compute_90"` |
| Family name | `"Hopper"` | `"Hopper"` |
| `CUDA_ARCH` macro | Decimal integer | `900` |
| LTO name | `"lto_90"` | `"lto_90"` |
| `isaClass` | Architecture class ID | -- |

The function registers each profile into three hash maps indexed by `sm_XX`, `compute_XX`, and `lto_XX` strings. This allows lookup by any of the three naming conventions used in different contexts (CLI, PTX `.target` directive, LTO linking).

**Family assignment from `sub_6765E0`:**

| SM Range | Family String | Notes |
|---|---|---|
| sm_75 | `"Turing"` | Single entry |
| sm_80, sm_86, sm_87, sm_88 | `"Ampere"` | Includes sm_88 (undocumented Ada variant) |
| sm_89 | `"Ampere"` | Ada Lovelace stored as Ampere-derived internally |
| sm_90/90a | `"Hopper"` | Single silicon, two feature levels |
| sm_100/100a/100f | `"Blackwell"` | Datacenter B200 |
| sm_103/103a/103f | `"Blackwell"` | Blackwell Ultra (GB300) |
| sm_110/110a/110f | `"Blackwell"` | Jetson Thor -- same family string despite different product |
| sm_120/120a/120f | `"Blackwell"` | Consumer/enterprise (RTX 50xx) -- different uarch, same string |
| sm_121/121a/121f | `"Blackwell"` | DGX Spark |

All sm_100 through sm_121 share the `"Blackwell"` family string internally, even though sm_110 (Jetson Thor) and sm_120 (consumer RTX 50xx) are distinct microarchitectures. The compiler distinguishes them through the capability dispatch tables, not through family name.

## Capability Dispatch -- `sub_607DB0`

The capability dispatch initializer builds 7 parallel hash maps at initialization time, protected by a once-guard (`byte_29FE1D8`). Each map indexes `sm_XX` / `compute_XX` strings to per-architecture values or handler functions. Error recovery uses `setjmp`/`longjmp`.

| Map | Global | Purpose | Value Type |
|---|---|---|---|
| 1 | `qword_29FE1D0` | Handler A (primary codegen) | Function pointer |
| 2 | `qword_29FE1C8` | Handler B (secondary codegen) | Function pointer |
| 3 | `qword_29FE1C0` | Intrinsic table initializer | Function pointer |
| 4 | `qword_29FE1B8` | Capability flags | Byte value |
| 5 | `qword_29FE1B0` | Profile registration | Registered via `sub_42BEC0` |
| 6 | `qword_29FE1A8` | Perf-stats / occupancy handler E | Function pointer |
| 7 | `qword_29FE1A0` | Perf-stats / occupancy handler F | Function pointer |

### Handler Function Assignments

Each SM version registers its own handler functions into these maps. Functions within the same suffix group (e.g., sm_100/100a/100f) share **all** handlers -- they are the same silicon with different feature exposure.

**Map 1 -- Handler A (per SM):**

| SM | Handler A | SM | Handler A |
|---|---|---|---|
| sm_75 | `sub_609B70` | sm_100 | `sub_609C30` |
| sm_80 | `sub_609CC0` | sm_110 | `sub_609F30` |
| sm_86 | `sub_609D50` | sm_103 | `sub_608F20` |
| sm_87 | `sub_609F00` | sm_120 | `sub_609E40` |
| sm_88 | `sub_609E70` | sm_121 | `sub_609ED0` |
| sm_89 | `sub_609E10` | | |
| sm_90 | `sub_609DB0` | | |

**Map 2 -- Handler B (per SM):**

| SM | Handler B | SM | Handler B |
|---|---|---|---|
| sm_75 | `sub_609B40` | sm_100 | `sub_609BD0` |
| sm_80 | `sub_609C90` | sm_110 | `sub_608F50` |
| sm_86 | `sub_609D80` | sm_103 | `sub_609D20` |
| sm_87 | `sub_609DE0` | sm_120 | `sub_609C60` |
| sm_88 | `sub_609EA0` | sm_121 | `sub_609BA0` |
| sm_89 | `sub_609CF0` | | |
| sm_90 | `sub_609C00` | | |

**Map 3 -- Intrinsic table initializer (per SM):**

| SM | Initializer | SM | Initializer |
|---|---|---|---|
| sm_75 | `sub_60A2E0` | sm_100 | `sub_60A910` |
| sm_80 | `sub_60A3E0` | sm_110 | `sub_60AA20` |
| sm_86 | `sub_60AC30` | sm_103 | `sub_60A700` |
| sm_87 | `sub_60AD30` | sm_120 | `sub_608DF0` |
| sm_88 | `sub_60AB30` | sm_121 | `sub_60A4E0` |
| sm_89 | `sub_60A810` | | |
| sm_90 | `sub_60A5F0` | | |

### Shared Handler Groups

Sub-variants within a base SM share all handler functions, confirming they are identical silicon:

| Group | Members | Shared Handlers |
|---|---|---|
| Hopper | sm_90, sm_90a | All 7 maps |
| Blackwell DC | sm_100, sm_100a, sm_100f | All 7 maps |
| Blackwell Ultra | sm_103, sm_103a, sm_103f | All 7 maps |
| Jetson Thor | sm_110, sm_110a, sm_110f | All 7 maps |
| Consumer | sm_120, sm_120a, sm_120f | All 7 maps |
| DGX Spark | sm_121, sm_121a, sm_121f | All 7 maps |

## Codegen Factory Values

The profile object stores an encoded architecture identifier at a known offset (visible as field `+348` on the profile pointer chain, e.g., `*(_QWORD *)(a1+1584)+348`). This value is compared throughout the compiler to gate features:

| Codegen Factory | SM Range | SASS ISA Generation |
|---|---|---|
| 24577 | sm_75 | Turing (SM 7.5) |
| 28673 | sm_80 -- sm_89 | Ampere / Ada (SM 8.x) |
| 32768 | sm_90 | Hopper (SM 9.0) |
| 36864 | sm_100 -- sm_121 | Blackwell (SM 10.x -- 12.x) |

These values appear in feature-gating checks. For example, FMA/DFMA combining in the peephole optimizer checks `profile[+372] > 28673` to require sm_70+ capability. The exact encoding formula is `(isa_generation << 12) | variant`, where the high bits identify the SASS instruction set generation.

Related encoded values seen in the binary:

```
12288  = sm_30 (Kepler)       // 3 << 12
16385  = sm_50 (Maxwell)      // 4 << 12 | 1
20481  = sm_50 alt (Maxwell)  // 5 << 12 | 1
24576  = sm_60 (Pascal)       // 6 << 12
24577  = sm_75 (Turing)       // 6 << 12 | 1
28673  = sm_80 (Ampere)       // 7 << 12 | 1
28674-28677 = sm_86/87/88/89  // 7 << 12 | 2..5
32768  = sm_90 (Hopper)       // 8 << 12
36864  = sm_100 (Blackwell)   // 9 << 12
36865-36869 = sm_103..121     // 9 << 12 | 1..5
```

## SM Version Encoding

The raw SM version number stored in profile objects and code object headers uses a packed integer format. This is the value at `v4[93]` in the code object builder (`sub_A465F0`):

| Encoded Value | SM Target | Code Object Version | Max Threads/CTA |
|---|---|---|---|
| 12288 | sm_30 | `0x70007` | 96 |
| 20481 | sm_50 | `0xC000C` | 176 |
| 24576 | sm_60 | -- | -- |
| 28673 | sm_80 | -- | -- |
| 36864 | sm_90 | `0x100010` | 240 |

The code object builder (`sub_A465F0` at `0xA465F0`) maps these encoded SM versions to ELF code object version fields and thread-per-CTA limits. The magic number `0x16375564E` is written at offset 0 of every code object header, with the SM version at offset +8.

## Per-SM Capability Accessors -- `sub_609XXX`

The 24 functions in the `sub_609XXX` cluster (range `0x609280`--`0x609F60`, ~1.2KB each) are the per-SM-version capability accessor functions. They are registered into Maps 1 and 2 of the dispatch tables and return architecture-specific values: register file sizes, feature flags, warp geometry, shared memory limits, and similar hardware parameters.

These are the functions that downstream code calls (through the dispatch table) to answer questions like "how many registers does this SM have?" or "is feature X available on this target?"

## Profile Layering

Three levels of SM profile information cooperate:

```
Level 1: sub_607DB0                    // Capability dispatch (7 hash maps)
    |                                  //   -> feature flags, handler functions
    v
Level 2: sub_6765E0                    // Profile objects (name, family, CUDA_ARCH, lto)
    |                                  //   -> identity metadata, isaClass
    v
Level 3: sub_609XXX / sub_60AXXX       // Per-SM accessor functions
                                       //   -> concrete hardware parameter values
```

Level 1 provides the dispatch infrastructure. Level 2 provides identity metadata for diagnostics and linking. Level 3 provides the actual numeric values that drive register allocation, scheduling, and instruction legality.

## Generation-Specific Features

### Turing (sm_75)

sm_75 is the default architecture for ptxas v13.0.88, returned by `sub_6784B0` when no `--gpu-name` is specified. Codegen factory value: 24577.

- Base tensor core (WMMA m16n16k16 f16/f32, m32n8k16, m8n32k16)
- Integer MMA (IMMA int8/int4), binary MMA (BMMA b1)
- Base warp-level operations (shfl.sync, vote.sync, match.sync, redux.sync)
- Named barrier support (bar 0-15 with arrive/red/sync variants)

### Ampere (sm_80 -- sm_88)

Codegen factory value: 28673. Shared with Ada Lovelace (sm_89).

- Extended tensor core: TF32, BF16, FP64 MMA shapes
- `cp.async` for asynchronous shared memory copies
- L2 cache hints on atomic operations
- `createpolicy` instructions for cache management
- 14 additional intrinsics (`__cuda_sm80_*`: bf16/tf32/s4/s8/b1 MMA, createpolicy)

### Ada Lovelace (sm_89)

Codegen factory value: 28673 (same as Ampere). Stored as `"Ampere"` internally despite being a distinct Ada Lovelace microarchitecture.

- Same codegen path as Ampere; differentiated through capability flags, not codegen factory
- 39 additional MMA intrinsics (`__cuda_sm_8x_mma_*`)

### Hopper (sm_90 / sm_90a)

Codegen factory value: 32768. `sm_90a` is architecture-locked (H100/H200 only).

- **WGMMA** (warpgroup MMA async): `wgmma.mma_async`, `wgmma.fence`, `wgmma.commit_group`, `wgmma.wait_group`
- **Cluster operations**: `barrier.cluster.arrive/wait`, distributed shared memory
- **setmaxnreg**: Dynamic register allocation limit
- Cluster special registers: `%clusterid`, `%cluster_ctaid`, `%cluster_ctarank`, etc.
- 38 sub-byte MMA intrinsics (`__cuda_sm_9x_mma_sub_byte_internal_*`: s4/u4 sparse)

### Blackwell Datacenter (sm_100, sm_103)

Codegen factory value: 36864. Both `a` and `f` sub-variants available.

- **tcgen05**: 5th-generation tensor core ISA (alloc, dealloc, ld, st, commit, cp, shift, mma) -- `a`/`f` sub-variants only
- **tcgen05 guardrails**: 8 debug validation functions (phase validity, column allocation, bounds checking)
- Extended MMA: 10 Blackwell-specific hmma/imma + bit MMA intrinsics (`__cuda_sm_10x_*`)
- 11 tcgen05 guardrail trap intrinsics (`__cuda_sm10x_tcgen05_guardrail_trap_*`)
- 18 sm_1xx bulk copy intrinsics (`__cuda_sm1xx_*`: cp.async.bulk.tensor 1D-5D tile/im2col)

### Jetson Thor (sm_110)

Codegen factory value: 36864 (same as sm_100). Originally sm_101 before rename. Automotive/robotics SoC.

- Retains full tcgen05/TMEM hardware on `a`/`f` sub-variants
- Same Blackwell datacenter feature set for tensor operations
- Differentiated through capability flags for SoC-specific constraints

### Blackwell Consumer (sm_120, sm_121)

Codegen factory value: 36864 (same as sm_100). Architecturally a distinct consumer microarchitecture despite sharing the `"Blackwell"` family string.

- **No tcgen05**: The entire tcgen05 ISA is absent on sm_120/121 -- gated by SM version checks
- Tensor core falls back to HMMA/IMMA/WGMMA inherited from sm_70--sm_90 path
- sm_120 = RTX 50xx consumer / RTX Blackwell Pro (enterprise)
- sm_121 = DGX Spark

## Diagnostic Strings

| String | Context | Function |
|---|---|---|
| `"Turing"` | Family name in profile object | `sub_6765E0` |
| `"Ampere"` | Family name in profile object | `sub_6765E0` |
| `"Hopper"` | Family name in profile object | `sub_6765E0` |
| `"Blackwell"` | Family name in profile object | `sub_6765E0` |
| `"isaClass"` | Architecture class reference on profile | `sub_6765E0` |
| `"sm_%d"` | SM name formatting | Multiple |
| `"compute_%d"` | Compute name formatting | `sub_6765E0` |
| `"lto_%d"` | LTO name formatting | `sub_6765E0` |

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_607DB0` | 14KB | SM capability dispatch -- builds 7 hash maps | 99% |
| `sub_608D70` | 384B | Profile lookup dispatcher | 80% |
| `sub_608DF0` | ~1KB | sm_120 intrinsic table initializer | 85% |
| `sub_608F20` | ~1.2KB | sm_103 handler A (capability accessor) | 90% |
| `sub_608F50` | ~1.2KB | sm_110 handler B (capability accessor) | 90% |
| `sub_609280`--`sub_609F60` | ~1.2KB each | 24 per-SM capability accessor functions (Maps 1+2) | 90% |
| `sub_609F60` | 2.8KB | `lds128convert` option handler (`"always"`, `"nonconst"`, `"never"`) | 90% |
| `sub_60A2E0`--`sub_60AD30` | ~1KB each | 12 per-SM intrinsic table initializers (Map 3) | 85% |
| `sub_60B040` | 4.5KB | Stress test options (`"stress-maxrregcount"`, etc.) | 85% |
| `sub_6765E0` | 54KB | SM profile object construction (family, CUDA_ARCH, lto) | 95% |
| `sub_6784B0` | -- | Default architecture -- returns sm_75 | 99% |
| `sub_A465F0` | 14KB | Code object header builder (SM version -> ELF fields) | 88% |

## Profile Object Layout (1936 bytes)

Every SM's intrinsic table initializer (Map 3 handler) calls `sub_917990` to allocate a 1,936-byte profile object that carries target-specific parameters throughout the compiler. This is the compilation unit's target descriptor -- the single structure that downstream code reads to answer "what hardware am I compiling for?"

### Construction Sequence

```
1. sub_71BDE0(1936, a1)     heap allocate 1936 bytes
2. sub_C1B7A0(profile)      zero-fill + structural defaults (8 SSE blocks, 5 scalars)
3. sub_917990(a3)           overlay: codegen factory default, tail constants
4. sub_60AXXX(a1,a2,a3,a4)  per-SM: codegen factory, shmem base, capability flags
```

### Key Fields -- Explicitly Initialized

These fields receive non-zero values during construction. Offsets are byte offsets from the profile object base pointer. Type column: D=DWORD(4B), Q=QWORD(8B), O=OWORD(16B), B=BYTE.

| Offset | Type | Default | Set By | Semantic Name | Confidence |
|---|---|---|---|---|---|
| +0 | Q | 0 | sub_C1B7A0 | `object_base` -- zeroed, likely vtable/class pointer | 75% |
| +112 | Q | `0x500000000` | sub_C1B7A0 | `packed_config` -- stores DWORD 5 at +112, DWORD 0 at +116 | 85% |
| +120 | D | 5 | sub_C1B7A0 | `opt_level_default` -- initial optimization level or block dimension | 85% |
| +132 | Q | `0xFFFFFFFF` | sub_C1B7A0 | `max_register_limit` -- -1 sentinel = "no limit" | 85% |
| +340 | D | 1 | sub_C1B7A0 | `enable_flag_A` -- default-enabled capability | 85% |
| +344 | D | 0x100000 | per-SM init | `shared_memory_config_base` -- 1 MB for all SM 75+ targets | 95% |
| +348 | D | per-SM | per-SM init | `codegen_factory` -- ISA generation encoding `(gen << 12) \| variant` | 99% |
| +424 | Q | `0x100000000` | sub_C1B7A0 | `packed_enable` -- stores DWORD 1 at +424, DWORD 0 at +428 | 90% |
| +428 | D | 0 (cond.) | per-SM init | `conditional_feature_flag` -- sm_90+ only: set to 0 when `*(a2+355)` is true | 85% |
| +432 | D | computed | per-SM init | `module_base_address` -- `callback() - 0x6FFFFE64` or -1 if disabled | 95% |
| +588 | D | 0 | sub_917990 | `cleared_field` -- explicitly re-zeroed; used in 10+ consumer functions | 90% |
| +708 | D | 1 | per-SM init | `enable_flag_D` -- universally set to 1 by all per-SM initializers | 85% |
| +944 | D | 4 | sub_C1B7A0 | `pipeline_depth` -- possibly barrier count or pipeline stage limit | 85% |
| +1200 | Q | `"NVIDIA"` | sub_43A400 | `vendor_string_ptr` -- pointer to vendor identification string | 95% |
| +1208 | Q | (pointer) | sub_43A400 | `associated_data_ptr` -- assigned from callback result | 90% |
| +1216 | D | 1 | sub_43A400 | `vendor_flag` -- set to 1 during ELF builder initialization | 85% |
| +1385 | B | 0 (bits) | runtime | `scheduling_feature_flags` -- bitfield, 21+ consumer sites | 99% |
| +1536 | Q | 1832 | sub_C1B7A0 | `dynamic_region_offset` -- points to tail SSE constant region start | 90% |
| +1552 | Q | 0 | runtime | `pipeline_phase_counter` -- values 16-19 checked in scoreboard guards | 95% |
| +1584 | Q | `nullsub_856` | sub_C1B7A0 | `sm_backend_vtable_ptr` -- THE central polymorphic pointer; initialized to null stub | 99% |
| +1684 | D | CLI value | per-SM init | `cli_option_value` -- `*(a1+108)` passthrough from compiler driver | 90% |
| +1840 | D | 1 | per-SM init | `elf_section_data` -- initially 1 (enable), later overwritten with ptr | 85% |
| +1880 | Q | 1 | per-SM init | `barrier_tracking_ptr` -- initially 1, later pointer to scoreboard data | 95% |
| +1892 | D | 2 | sub_917990 | `tail_mode_value` -- possibly versioning or encoding mode indicator | 85% |
| +1912 | D | 0 (cond.) | per-SM init | `conditional_clear` -- cleared when `*(a2+233)` is true (debug mode) | 85% |
| +1928 | D | 1 | per-SM init | `output_config_value` -- compilation output configuration | 85% |

### SSE Constant Blocks

10 blocks of 16 bytes each are loaded from `.rodata` segments via `_mm_load_si128`. These likely contain per-register-class sizing parameters, pipeline configuration constants, or default opcode table pointers. Exact values require `.rodata` dump.

| Offset | Source | Set By |
|---|---|---|
| +184 | `xmmword_20206F0` | sub_C1B7A0 |
| +280 | `xmmword_2027950` | sub_C1B7A0 |
| +312 | `xmmword_2027600` | sub_C1B7A0 |
| +680 | `xmmword_2027620` | sub_C1B7A0 |
| +696 | `xmmword_22B4ED0` | sub_C1B7A0 |
| +740 | `xmmword_22B4EE0` | sub_C1B7A0 |
| +788 | `xmmword_22B4EF0` | sub_C1B7A0 |
| +816 | `xmmword_22B4F00` | sub_C1B7A0 |
| +1832 | `xmmword_21DEBA0` | sub_917990 |
| +1908 | `xmmword_21DEBB0` | sub_917990 |

### Scheduling Feature Flags (+1385 bitfield)

The byte at offset +1385 is the most heavily accessed bitfield on the profile object (21+ consumer sites in 15+ decompiled functions). Each bit gates a scheduling or codegen behavior.

| Bit | Mask | Meaning | Evidence |
|---|---|---|---|
| 0 | `0x01` | Function has sync barriers | `sub_792CD0` sets/clears; `sub_75F680`, `sub_75F580` check |
| 1 | `0x02` | (unknown) | -- |
| 2 | `0x04` | Extended barrier model | `sub_796D60` checks jointly with `+1382 & 0x20` |
| 3 | `0x08` | Scoreboard tracking enabled | `sub_925670`, `sub_925510`, `sub_9253C0` check jointly with `+1880` and `+1552` |
| 4 | `0x10` | (unknown) | -- |
| 5 | `0x20` | Scheduling feature flag | `sub_793220`, `sub_A36360` (scoreboard encoder) check |
| 6 | `0x40` | Temporary analysis flag | `sub_752E40` sets; `sub_77F0D0` clears |
| 7 | `0x80` | Preserved across resets | `sub_7F7DC0`: `*(a1+1385) &= 0x80` (all others cleared) |

### Per-SM Initializer Differences

All 12 per-SM initializers (one per SM family) are structurally identical. Only two fields differ between families.

| Field | sm_75--sm_88 | sm_89 | sm_90 | sm_100--sm_121 |
|---|---|---|---|---|
| +348 `codegen_factory` | 24577--28676 | 28677 | 0x8000 (32768) | 36864--36869 |
| +428 `conditional_feature_flag` | not written | not written | written (if `*(a2+355)`) | written (if `*(a2+355)`) |

All other fields (+344, +432, +588, +708, +1684, +1840, +1880, +1912, +1928) are set identically across all SM families.

### Critical Distinction: Profile Object vs SM Backend

The profile object (1936 bytes, this layout) is the **compilation unit's target descriptor**, stored somewhere in the compilation context. The pointer at `context+1584` (`sm_backend_vtable_ptr`) points to a **separate polymorphic SM backend object** -- not to another profile object. Fields accessed as `*(*(ctx+1584) + N)` are on the SM backend, not on this profile object.

Commonly accessed SM backend fields (NOT on the 1936-byte profile):

| SM Backend Offset | Semantic | Consumer Count |
|---|---|---|
| +12 | `arch_class_id` (4=Maxwell, 10=Grace, 11=NVLink-Volta+) | 3+ |
| +372 | `codegen_factory` (THE feature-gating value, 37+ sites) | 37+ |
| +1037 | `hw_capability_flags` bitfield (SFU precision, barriers) | 20+ |
| +1312 | vtable dispatch for predicate marking capability | 2+ |
| +1320 | vtable function slot for optimization dispatch | 2+ |
| +1417 | Grace/ARM architecture flag (bit 7) | 1+ |
| +1418 | NVLink-capable flag (bit 0) | 1+ |

The profile's own `+348` stores the codegen factory value at construction time. The SM backend's `+372` is where all downstream code reads it. These are the same numeric value stored in two different objects.

### Object Region Map

```
Offset Range   Size    Content
-----------    ----    -------
[0..111]       112B    Object header, vtable chain, zeroed bulk
[112..159]     48B     Config scalars (opt level, register limit)
[160..343]     184B    Zeroed + 3 SSE constant blocks (184, 280, 312)
[344..587]     244B    Target identity (shmem base, codegen factory, enable flags)
[588..879]     292B    Capability fields, 4 SSE constant blocks (680, 696, 740, 788)
[880..1063]    184B    Scheduling config, barrier config (+944=4 default)
[1064..1279]   216B    Extended config, vendor metadata (+1200="NVIDIA")
[1280..1535]   256B    Architecture feature flags (+1385 bitfield, +1417/+1418)
[1536..1663]   128B    Dynamic region pointer, phase counter, sm_backend ptr (+1584)
[1664..1831]   168B    CLI passthrough, ELF section data, barrier tracking ptr
[1832..1935]   104B    Tail region: 2 SSE constant blocks, mode value (+1892=2)
```

## Cross-References

- [Turing & Ampere (SM 75--88)](turing-ampere.md) -- Detailed feature flags for sm_75 through sm_89
- [Ada & Hopper (SM 89--90a)](ada-hopper.md) -- WGMMA, cluster operations, sm_90a arch-lock
- [Blackwell (SM 100--121)](blackwell.md) -- tcgen05, arch-conditional vs family-conditional gating
- [TCGen05 -- 5th Gen Tensor Cores](tcgen05.md) -- tcgen05 instruction set detail
- [Intrinsic Table (608 Entries)](../intrinsics/index.md) -- Master intrinsic catalog with per-SM generation ranges
- [Tensor Core Intrinsics](../intrinsics/tensor.md) -- WMMA/MMA/tcgen05 intrinsic lowering
- [Latency Model & HW Profiles](../scheduling/latency-model.md) -- Per-SM scheduling parameters
- [SASS Instruction Encoding](../codegen/encoding.md) -- Codegen factory -> encoder selection
- [Mercury Encoder](../codegen/mercury.md) -- SM-dependent SASS encoding
- [CLI Options](../config/cli-options.md) -- `--gpu-name` parsing and default target
- [Data Structures](../ir/data-structures.md) -- context+1584 polymorphic pointer documentation
