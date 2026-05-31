# Blackwell (SM 100--121)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas v13.0.88 handles five Blackwell-era base targets -- sm_100, sm_103, sm_110, sm_120, sm_121 -- spanning datacenter, automotive, consumer, and DGX product lines. All share generation 9 (upper nibble `0x9000` of the codegen factory) and the `"Blackwell"` family string internally, but each uses a distinct sub-variant: sm_100=36864 (`0x9000`), sm_103=36867 (`0x9003`), sm_110=36868 (`0x9004`), sm_120=sm_121=36869 (`0x9005`). The defining Blackwell feature is **Capsule Mercury** (capmerc) as the default binary output format, automatically enabled for SM numbers exceeding 99. The datacenter variants (sm_100, sm_103, sm_110) support **tcgen05** (5th-generation tensor cores with dedicated tensor memory); the consumer variants (sm_120, sm_121) do not.

| | |
|---|---|
| **SM targets** | sm_100, sm_103, sm_110, sm_120, sm_121 (+ `a` and `f` sub-variants each) |
| **Codegen factory range** | 36864--36869 (`0x9000`--`0x9005`, generation 9) |
| **Family string** | `"Blackwell"` (all five targets) |
| **Default binary format** | Capsule Mercury (capmerc) -- auto-enabled for SM > 99 |
| **SASS encoding** | 128-bit per instruction (Mercury-encoded) |
| **Warp geometry** | 16 warps, 240 dispatch slots (shared with Hopper sm_90) |
| **Sub-variants per SM** | 3: base, `a` (accelerated), `f` (feature-reduced) |
| **Profile constructor** | `sub_6765E0` (54KB) |
| **Capability dispatch** | `sub_607DB0` (7 hash maps, once-guarded) |

## SM Version Table

| SM | Product | `__CUDA_ARCH__` | Codegen Factory | Hex | Variant |
|---|---|---|---|---|---|
| `sm_100` | B100 / B200 (datacenter) | 1000 | 36864 | `0x9000` | 0 (gen 9 base) |
| `sm_103` | GB300 (Blackwell Ultra) | 1030 | 36867 | `0x9003` | 3 |
| `sm_110` | Jetson Thor SoC | 1100 | 36868 | `0x9004` | 4 |
| `sm_120` | RTX 50xx / RTX Pro | 1200 | 36869 | `0x9005` | 5 |
| `sm_121` | DGX Spark | 1210 | 36869 | `0x9005` | 5 |

**Codegen factory encoding:** `(9 << 12) | sub_variant`. sm_100 is variant 0 (generation base). sm_103 is variant 3. sm_110 is variant 4. sm_120 and sm_121 appear to share variant 5 in the scheduling sub-architecture table at `sub_8E4400`.

**Unreleased SM numbers referenced in the binary:** The SASS formatter `sub_583190` (rsqrt) checks for SM codes 102, 103, 107, 124, 130 in architecture-specific dispatch paths, suggesting internal/future variants beyond the five publicly exposed targets.

## Sub-Variant System

Every Blackwell SM has three sub-variants. The base and `a`/`f` variants within an SM share **all 7 dispatch table handler functions** -- they are identical silicon with different compatibility metadata and feature exposure.

### Profile Object Fields

The profile constructor `sub_6765E0` builds profile objects for each sub-variant with these fields:

| SM | Base | `a` Variant | `f` Variant |
|---|---|---|---|
| sm_100 | `sm_100` / `compute_100` / `lto_100` | `sm_100a` / `compute_100a` / `lto_100a` | `sm_100f` / `compute_100f` / `lto_100f` |
| sm_103 | `sm_103` / `compute_103` / `lto_103` | `sm_103a` / `compute_103a` / `lto_103a` | `sm_103f` / `compute_103f` / `lto_103f` |
| sm_110 | `sm_110` / `compute_110` / `lto_110` | `sm_110a` / `compute_110a` / `lto_110a` | `sm_110f` / `compute_110f` / `lto_110f` |
| sm_120 | `sm_120` / `compute_120` / `lto_120` | `sm_120a` / `compute_120a` / `lto_120a` | `sm_120f` / `compute_120f` / `lto_120f` |
| sm_121 | `sm_121` / `compute_121` / `lto_121` | `sm_121a` / `compute_121a` / `lto_121a` | `sm_121f` / `compute_121f` / `lto_121f` |

### CUDA_ARCH Macro Values

| Sub-Variant | sm_100 | sm_103 | sm_110 | sm_120 | sm_121 |
|---|---|---|---|---|---|
| Base | `-D__CUDA_ARCH__=1000` | `=1030` | `=1100` | `=1200` | `=1210` |
| Accelerated | `=100a0` | `=103a0` | `=110a0` | `=120a0` | `=121a0` |
| Feature-reduced | `=100f0` | `=103f0` | `=110f0` | `=120f0` | `=121f0` |

### Suffix Bit Flags in Profile Objects

From the decompiled profile constructor, suffixed variants set specific byte flags:

| Suffix | Flag Position | Evidence |
|---|---|---|
| `a` (accelerated) | `profile[4] = 1` | `v79->m128i_i8[4] = 1; *(_BYTE *)(v82 + 4) = 1;` |
| `f` (feature-reduced) | `profile[5] = 1` (on all 3 objects: sm, compute, lto) | `v88->m128i_i8[5] = 1; *(_BYTE *)(v91 + 5) = 1; v94[5] = 1;` |

The `a` flag is set on the SM and compute profile objects only. The `f` flag is set on all three (sm, compute, lto), reflecting the fact that `f`-compiled code must retain its feature-reduced metadata through linking.

### isaClass Inheritance

Sub-variants reference their base SM's isaClass rather than defining a new one:

- `sm_100a` and `sm_100f` reference `"(profile_sm_100)->isaClass"`
- `sm_103a` and `sm_103f` reference `"(profile_sm_103)->isaClass"`
- `sm_120a` and `sm_120f` reference `"(profile_sm_120)->isaClass"`
- `sm_121a` and `sm_121f` reference `"(profile_sm_121)->isaClass"`

This confirms that sub-variants share the instruction set architecture class with their base. The `a`/`f` distinction is purely in compatibility metadata, not in the ISA or codegen.

## Capability Dispatch

`sub_607DB0` registers handler functions into 7 parallel hash maps. All sub-variants of a given SM register the **same** function pointers.

### Handler Assignments (Maps 1--3)

| SM | Handler A (Map 1) | Handler B (Map 2) | Intrinsic Init (Map 3) |
|---|---|---|---|
| sm_100 / 100a / 100f | `sub_609C30` | `sub_609BD0` | `sub_60A910` |
| sm_103 / 103a / 103f | `sub_608F20` | `sub_609D20` | `sub_60A700` |
| sm_110 / 110a / 110f | `sub_609F30` | `sub_608F50` | `sub_60AA20` |
| sm_120 / 120a / 120f | `sub_609E40` | `sub_609C60` | `sub_608DF0` |
| sm_121 / 121a / 121f | `sub_609ED0` | `sub_609BA0` | `sub_60A4E0` |

### Performance / Occupancy Handlers (Maps 6--7)

| SM | Handler E (Map 6) | Handler F (Map 7) |
|---|---|---|
| sm_100 / 100a / 100f | `sub_609080` | `sub_6098A0` |
| sm_103 / 103a / 103f | `sub_609020` | `sub_6091A0` |
| sm_110 / 110a / 110f | `sub_609000` | `sub_609280` |
| sm_120 / 120a / 120f | `sub_608FE0` | `sub_609520` |
| sm_121 / 121a / 121f | `sub_609040` | `sub_6097C0` |

Every Blackwell SM has **unique** handler functions in all 7 maps. This contrasts with Hopper where sm_90 and sm_90a share all handlers. Each Blackwell SM variant is architecturally distinct enough to warrant separate capability accessors, performance models, and intrinsic tables.

## Warp Geometry

The warp geometry initializer at `sub_8E4400` uses the codegen factory value to select dispatch parameters. All Blackwell targets (codegen factory > 36863) fall into the maximum bucket:

```text
encoded >  36863  -> 16 warps, 240 dispatch slots
```

This is identical to Hopper (sm_90). The 16-warp / 240-slot geometry supports Blackwell's warpgroup execution model (4 warps per warpgroup, 4 warpgroups per SM partition).

### Sub-Architecture Variant Table

The secondary variant assignment at `sub_8E4400` maps codegen factory values to sub-architecture indices:

| Codegen Factory | Variant | SM |
|---|---|---|
| 36864 | 0 | sm_100 (base) |
| 36867 | 3 | sm_103 |
| 36868 | 4 | sm_110 |
| 36869 | 5 | sm_120, sm_121 |

These variant indices select different entries within the per-SM latency tables, allowing the scheduler to use silicon-specific pipeline timing.

## Hardware Resource Geometry

Per-SM hardware resource limits used by ptxas for register allocation, occupancy calculations, and scheduling decisions. Extracted from `sub_8688F0` (universal baseline), `sub_8E4400` (scheduler partition geometry), and `sub_ABF250` (occupancy calculator). See [targets/index.md -- Per-SM Resource Geometry Table](index.md#per-sm-resource-geometry-table) for the complete table across all architectures.

| SM | Regs/SM | Max Regs/Thread | Max Threads/CTA | Warps/SM | Max CTAs/SM | Sched Partitions | Dispatch Slots | Configurable Shared Memory | Conf |
|---|---|---|---|---|---|---|---|---|---|
| `sm_100` | 65,536 | 255 | 1,024 | 64 | 32 | 16 / 240 | 240 | 48 / 100 / 132 / 164 / 228 KB | 90% |
| `sm_103` | 65,536 | 255 | 1,024 | 64 | 32 | 16 / 240 | 240 | (same as sm\_100) | 88% |
| `sm_110` | 65,536 | 255 | 1,024 | 64 | 32 | 16 / 240 | 240 | (same as sm\_100) | 85% |
| `sm_120` | 65,536 | 255 | 1,024 | 64 | 32 | 16 / 240 | 240 | 48 / 100 / 132 / 164 / 228 KB | 88% |
| `sm_121` | 65,536 | 255 | 1,024 | 64 | 32 | 16 / 240 | 240 | (same as sm\_120) | 85% |

**Column definitions:**
- **Regs/SM**: Total 32-bit registers per streaming multiprocessor. 65,536 universally for sm\_75+.
- **Max Regs/Thread**: Maximum registers a single thread can use. 255 universally (`sub_8688F0` offset +612).
- **Max Threads/CTA**: Maximum threads per cooperative thread array (block).
- **Warps/SM**: Total concurrent warps per SM. Determines peak occupancy.
- **Max CTAs/SM**: Maximum concurrent CTAs per SM.
- **Sched Partitions / Dispatch Slots**: From `sub_8E4400` offset +18 (packed DWORD) and offset +22 (WORD).
- **Configurable Shared Memory**: Valid shared memory sizes per CTA, selected by `cudaFuncSetAttribute`.

All Blackwell targets share the 16-partition / 240-slot geometry (identical to Hopper sm\_90). The `a` and `f` sub-variants within each SM share the same geometry -- differentiation is in compatibility metadata and feature exposure, not in resource limits. The primary distinction across Blackwell SMs is in the latency tables and tcgen05 availability, not in the scheduling partition structure.

## Capsule Mercury (capmerc) -- Default Output Format

Capsule Mercury is automatically enabled for all Blackwell targets. When the SM architecture number exceeds 99, ptxas sets the capmerc flag at offset+81 in the compilation context. This applies to sm_100, sm_103, sm_110, sm_120, and sm_121 uniformly.

### Three Output Modes

| Mode | String | Default For | SM Range |
|---|---|---|---|
| `mercury` | `"mercury"` | sm_75 -- sm_90 | Turing through Hopper |
| `capmerc` | `"capmerc"` | sm_100 -- sm_121 | All Blackwell |
| `sass` | `"sass"` | None (explicit only) | Any |

### Capsule Mercury vs Mercury

Both modes use the same Mercury encoder pipeline (phases 117--122). The capmerc distinction is at the ELF emission level:

- **Mercury** produces a fully-resolved SASS binary in `.text.<funcname>` sections
- **Capsule Mercury** wraps Mercury-encoded instructions in `.nv.capmerc<funcname>` sections with a 328-byte capsule descriptor, plus `.nv.merc.*` debug/metadata sections

The capsule descriptor (constructed by `sub_1C9C300`, 24KB) contains the Mercury instruction stream, relocation metadata (`R_MERCURY_*` types), KNOBS compilation configuration snapshot, and function-level metadata (register counts, barriers, shared memory usage).

### Opportunistic Finalization

Capsule Mercury enables deferred finalization -- compiling once for one SM and reconstituting SASS for a different SM at link or load time.

| Level | Name | Behavior | Example |
|---|---|---|---|
| 0 | default | Standard finalization (compile-target only) | -- |
| 1 | none | No finalization; output stays as capmerc | -- |
| 2 | intra-family | Finalize within same SM family | sm_100 -> sm_103 |
| 3 | intra+inter | Finalize across SM families | sm_100 -> sm_120 |

The compatibility checker `sub_60F290` determines whether a capmerc binary compiled for SM X can be finalized for SM Y. On success, ptxas emits: `"applied for off-target %u -> %u finalization"`.

### Self-Check Mechanism

The `--self-check` CLI option performs roundtrip verification:

1. Generate capmerc output (Mercury encoding + metadata)
2. Reconstitute SASS from the capmerc data
3. Compare section-by-section; report error codes 17 (content mismatch), 18 (count mismatch), 19 (metadata mismatch)

The reconstituted SASS can be dumped with `--out-sass` for debugging self-check failures.

## TCGen05 -- 5th Generation Tensor Cores

TCGen05 is the defining hardware feature of Blackwell datacenter parts. It introduces **tensor memory (TMEM)** as a dedicated register-like storage directly connected to the tensor core, eliminating the shared-memory bottleneck of previous WGMMA designs.

### SM Availability

| SM | tcgen05 Available | Notes |
|---|---|---|
| sm_100 / 100a / 100f | Yes | Full datacenter tcgen05 |
| sm_103 / 103a / 103f | Yes | Blackwell Ultra -- same tcgen05 ISA |
| sm_110 / 110a / 110f | Yes | Jetson Thor -- full tcgen05 hardware |
| sm_120 / 120a / 120f | **No** | Consumer -- no TMEM, no tcgen05 |
| sm_121 / 121a / 121f | **No** | DGX Spark -- no TMEM, no tcgen05 |

The tcgen05 ISA is gated by SM version checks (visible as `sub_70FA00(*, 29)` capability queries). sm_120 and sm_121 fall back to inherited HMMA/IMMA/WGMMA tensor core paths.

### PTX Instructions

Registered in the opcode dispatch table at `sub_5D4190`:

| PTX Instruction | Codegen Handler | Formatter | Size |
|---|---|---|---|
| `tcgen05.alloc` | `sub_569180` | `sub_526370` | 1287B |
| `tcgen05.relinquish_alloc_permit` | `sub_526370` | -- | -- |
| `tcgen05.dealloc` | `sub_58C7F0` | `sub_574050` | 2130B |
| `tcgen05.ld` | `sub_574050` | `sub_578DB0` | 2466B |
| `tcgen05.ld.red` | `sub_578DB0` | -- | -- |
| `tcgen05.st` | `sub_571FE0` | `sub_56C190` | 1842B |
| `tcgen05.commit` | `sub_56C190` | `sub_5427F0` | 1575B |
| `tcgen05.cp` | `sub_5427F0` | `sub_4F1A90` | 903B |
| `tcgen05.shift` | `sub_4F1A90` | `sub_58FA20` | 4604B |
| `tcgen05.mma` | `sub_5BBC30` (90KB) | -- | -- |
| `tcgen05.mma.ws` | `sub_58FA20` | `sub_4DA720` | 343B |

The `tcgen05.mma` codegen handler at `sub_5BBC30` is **90KB** -- the largest single-instruction handler in ptxas -- reflecting the complexity of 5th-gen tensor core MMA with tensor memory operands, scale factors, sparsity, and accumulator management.

### TCGen05 Guardrail Functions

Eight debug/validation functions provide runtime instrumentation for tensor memory operations when compiled with `--g-tensor-memory-access-check` (or `-g-tmem-access-check`):

| Guardrail | Formatter | Size | Validation |
|---|---|---|---|
| `_tcgen05.guardrails.is_phase_valid` | `sub_4DA720` | 775B | Phase lifecycle |
| `_tcgen05.guardrails.are_columns_allocated` | `sub_4DDE70` | 599B | Column allocation |
| `_tcgen05.guardrails.is_current_warp_valid_owner` | `sub_4DBF20` | 791B | Warp ownership |
| `_tcgen05.guardrails.in_physical_bounds` | `sub_4DB050` | 439B | Memory bounds |
| `_tcgen05.guardrails.allocation_granularity` | `sub_4F0960` | 839B | Allocation alignment |
| `_tcgen05.guardrails.datapath_alignment` | `sub_4DD580` | 735B | Data path checks |
| `_tcgen05.guardrails.sp_consistency_across_idesc_mod` | `sub_500FA0` | 970B | Sparse descriptor |
| `_tcgen05.guardrails.check_sparse_usage` | `sub_4DDB80` | 743B | Sparsity validation |

The bounds checker (`sub_70E0E0`, 296 lines decompiled) generates inline PTX code to validate tensor memory column counts, extracting bitfield positions from tcgen05 descriptors (e.g., `and.b32 %s, 0x7E0000, %s; shr.u32 %s, %s, 17; mul.lo.u32 %s, %s, 8`).

### TCGen05 Intrinsics

| ID Range | Count | Category |
|---|---|---|
| 0x20--0x2A | 11 | `__cuda_sm10x_tcgen05_guardrail_trap_*` (trap on validation failure) |
| 0x230--0x239 | 10 | `__cuda_sm_10x_*` (hmma/imma mdata + bit MMA) |

Additional tcgen05 helper intrinsics observed in decompiled code:

- `__cuda_sm_100_tcgen05_ld_red_immhalfSplitOff` -- load-reduce with immediate half-split offset
- `__cuda_sm_100_tcgen05_ld_immhalfSplitOff` -- load with immediate half-split offset
- `__cuda_sm_100_tcgen05_st_immhalfSplitOff` -- store with immediate half-split offset
- `__cuda_sm_100_tcgen05_ld_red_funcRetArr` -- load-reduce returning array
- `__cuda_sm_100_tcgen05_ld_funcRetArr` -- load returning array

These helpers (decompiled in `sub_70D910` and `sub_70DDB0`) generate inline PTX for complex tensor memory access patterns including array returns via `ld.param.b32` sequences.

### Bulk Copy Intrinsics (sm_1xx)

18 intrinsics in the `__cuda_sm1xx_*` namespace cover `cp.async.bulk.tensor` 1D--5D in tile and im2col modes. These extend the Hopper TMA infrastructure with Blackwell-specific enhancements.

## SM 100 / SM 100a / SM 100f -- Blackwell Datacenter

sm_100 is the reference Blackwell architecture. Codegen factory 36864 (`0x9000`), CUDA_ARCH 1000.

### Products

B100, B200 (datacenter GPU), paired as GB200 NVL72 superchips.

### Key Features

- **TCGen05**: Full 5th-gen tensor core with TMEM (alloc, dealloc, ld, st, commit, cp, shift, mma, mma.ws)
- **Capsule Mercury**: Default output format (auto-enabled for SM > 99)
- **WGMMA inherited**: Warpgroup MMA from Hopper carries forward
- **Cluster operations**: Thread-block clusters, distributed shared memory (from Hopper)
- **setmaxnreg**: Dynamic register allocation (from Hopper)
- **Uniform register ALU**: UFADD, UFFMA, UFSEL, UFSETP, UVIADDR (Blackwell uniform register ISA additions)

### Handler Functions

| Map | Function | Role |
|---|---|---|
| Handler A | `sub_609C30` | Primary codegen capability accessor |
| Handler B | `sub_609BD0` | Secondary codegen capability accessor |
| Intrinsic init | `sub_60A910` | Intrinsic table population |
| Perf/occupancy E | `sub_609080` | Performance statistics |
| Perf/occupancy F | `sub_6098A0` | Occupancy calculator |

### HW Latency Table

`sub_8E8A90` (3.0KB) -- the base Blackwell latency table. Two-part structure: a 3.0KB base table for standard instructions plus a ~949-byte TCGEN05 supplement covering tensor core scheduling classes 745--772+.

### Profile Object

From `sub_6765E0`:

```text
SM name:       "sm_100"
Compute name:  "compute_100"
LTO name:      "lto_100"
Family:        "Blackwell"
CUDA_ARCH:     "-D__CUDA_ARCH__=1000"
```

The profile constructor stores `dword_29FE2C4 = 100` after constructing all sm_100 sub-variants, likely recording the current highest-registered base SM number.

## SM 103 / SM 103a / SM 103f -- Blackwell Ultra

sm_103 is Blackwell Ultra, targeting the GB300 NVL72 platform. Codegen factory 36867 (`0x9003`), CUDA_ARCH 1030.

### Products

GB300 (datacenter, Blackwell Ultra). Incremental silicon revision over sm_100.

### Differentiation from sm_100

The SASS formatter `sub_583190` (rsqrt instruction) explicitly checks for `"sm_103"` and applies a Blackwell Ultra-specific operand layout. The formatter dispatch first tests raw SM codes (102, 103, 107, 130, 124), then uses `check_target_sm(instr, 0, "sm_103")` for string-based validation.

From sweep data on the SM103-specific encoding path:
- Sets encoding flags XOR 0x10, XOR 0x40 for sm_103-specific instruction variants
- New operand layout for transcendental instructions (rsqrt, likely also rcp/sqrt)

sm_103 has a separate 618-byte supplementary latency table -- the smallest in the binary -- suggesting minimal scheduling parameter changes from sm_100.

### Handler Functions

All unique from sm_100:

| Map | Function |
|---|---|
| Handler A | `sub_608F20` |
| Handler B | `sub_609D20` |
| Intrinsic init | `sub_60A700` |
| Perf/occupancy E | `sub_609020` |
| Perf/occupancy F | `sub_6091A0` |

### Profile Object

```text
SM name:       "sm_103"
Compute name:  "compute_103"
LTO name:      "lto_103"
Family:        "Blackwell"
CUDA_ARCH:     "-D__CUDA_ARCH__=1030"
```

## SM 110 / SM 110a / SM 110f -- Jetson Thor

sm_110 targets the Jetson Thor SoC for automotive and robotics applications. Codegen factory 36868 (`0x9004`), CUDA_ARCH 1100.

### Products

Jetson Thor (automotive-grade SoC with integrated GPU). Originally internally designated sm_101 before rename.

### sm_101 Legacy Alias

sm_101 (with variants sm_101a and sm_101f) was the original internal name for Jetson Thor. It was renamed to sm_110 in a later CUDA release, but all three validation table entries are retained for backward compatibility:

| Table | Entry | PTX ISA | Purpose |
|---|---|---|---|
| Base (`unk_1D16220`) | `{101, 8, 6}` | 8.6 | Accepts `--gpu-name sm_101` in existing PTX files |
| Accelerated (`unk_1D161C0`) | `{101, 8, 6}` | 8.6 | Accepts `sm_101a` |
| Feature-reduced (`unk_1D16160`) | `{101, 8, 8}` | 8.8 | Accepts `sm_101f` |

The validation tables use `bsearch()` (`sub_484B70` comparator), so both sm_101 and sm_110 are independently findable. However, `sub_6765E0` (the profile constructor) registers only sm_110 / sm_110a / sm_110f -- there is no profile object for sm_101. After passing validation, sm_101 must resolve to the sm_110 profile through an internal aliasing path (likely in `sub_4B1080`, the target directive parser).

The PTX ISA version difference is notable: sm_101 requires PTX 8.6 (same as sm_100), while sm_110 requires PTX 9.0. This reflects the timeline -- sm_101 was named when the Jetson Thor target was first added alongside sm_100, before the sm_110 numbering and PTX 9.0 specification existed.

### Key Characteristics

- **Full tcgen05 hardware**: Retains datacenter-class tensor memory and tensor core features
- **Same WGMMA/cluster support**: Inherits Hopper-era warpgroup and cluster operations
- **SoC-specific constraints**: Differentiated from sm_100 through capability flags, not through missing features -- the capability accessor functions (`sub_609F30`, `sub_608F50`) return SoC-appropriate resource limits

### Handler Functions

| Map | Function |
|---|---|
| Handler A | `sub_609F30` |
| Handler B | `sub_608F50` |
| Intrinsic init | `sub_60AA20` |
| Perf/occupancy E | `sub_609000` |
| Perf/occupancy F | `sub_609280` |

### Profile Object

```text
SM name:       "sm_110"
Compute name:  "compute_110"
LTO name:      "lto_110"
Family:        "Blackwell"
CUDA_ARCH:     "-D__CUDA_ARCH__=1100"
```

Note: sm_110 uses different `xmmword` constants for profile fields `[5]`--`[7]` compared to sm_100, visible in the profile constructor where `v98[5] = xmmword_2027D70; v98[6] = v103 (from v209); v98[7] = v104 (from v207)`. This encodes different hardware resource parameters.

## SM 120 / SM 120a / SM 120f -- Blackwell Consumer

sm_120 targets consumer and enterprise workstation GPUs. Codegen factory 36869 (`0x9005`), CUDA_ARCH 1200.

### Products

RTX 5090, RTX 5080, RTX 5070 Ti, RTX 5070, RTX 5060 (consumer). RTX Blackwell Pro (enterprise workstation).

### The tcgen05 Gap

sm_120 is architecturally distinct from sm_100 despite sharing the `"Blackwell"` family string. The critical difference: **no tcgen05 support**. The entire tensor memory subsystem (alloc, dealloc, ld, st, commit, cp, shift, mma) is absent. Tensor operations fall back to:

- HMMA/IMMA inherited from sm_70--sm_89 (direct MMA path)
- WGMMA inherited from sm_90 (warpgroup async MMA)

This is gated by SM version checks in the capability accessor functions. The intrinsic table initializer (`sub_608DF0`) does not register tcgen05 intrinsic handlers for sm_120.

### HW Latency Table

sm_120 has a distinct latency model, split into two parts:

| Function | Size | Content |
|---|---|---|
| `sub_8E9000` | 2.9KB | Base consumer Blackwell table |
| `sub_8E92E0` | 5.5KB | Extended table (largest individual table in binary) |

The 5.5KB extended table is larger than any other individual latency table, suggesting that sm_120's consumer pipeline has significantly different scheduling characteristics from datacenter sm_100. The consumer pipeline likely has different functional unit counts, memory latencies, and tensor core throughput profiles.

### Handler Functions

| Map | Function |
|---|---|
| Handler A | `sub_609E40` |
| Handler B | `sub_609C60` |
| Intrinsic init | `sub_608DF0` |
| Perf/occupancy E | `sub_608FE0` |
| Perf/occupancy F | `sub_609520` |

### Profile Object

```text
SM name:       "sm_120"
Compute name:  "compute_120"
LTO name:      "lto_120"
Family:        "Blackwell"
CUDA_ARCH:     "-D__CUDA_ARCH__=1200"
```

sm_120 uses a third distinct set of `xmmword` constants for profile fields, including `xmmword_2027DC0` at field `[6]`, confirming different hardware resource parameters from both sm_100 and sm_110.

## SM 121 / SM 121a / SM 121f -- DGX Spark

sm_121 targets the DGX Spark desktop AI workstation. Codegen factory 36869 (`0x9005`), CUDA_ARCH 1210.

### Products

NVIDIA DGX Spark (desktop AI workstation with Grace CPU + Blackwell GPU).

### Relationship to sm_120

sm_121 shares the same codegen factory sub-variant (5) as sm_120 in the scheduling sub-architecture table, and inherits sm_120's `xmmword` profile constants. This suggests sm_121 is a binned or slightly modified sm_120 die, similar to how sm_86 relates to sm_80 in the Ampere generation.

Like sm_120, sm_121 has **no tcgen05 support** -- tensor operations use the HMMA/IMMA/WGMMA path.

### Handler Functions

All unique from sm_120:

| Map | Function |
|---|---|
| Handler A | `sub_609ED0` |
| Handler B | `sub_609BA0` |
| Intrinsic init | `sub_60A4E0` |
| Perf/occupancy E | `sub_609040` |
| Perf/occupancy F | `sub_6097C0` |

### Profile Object

```text
SM name:       "sm_121"
Compute name:  "compute_121"
LTO name:      "lto_121"
Family:        "Blackwell"
CUDA_ARCH:     "-D__CUDA_ARCH__=1210"
```

## Blackwell Uniform Register ISA

Blackwell extends the uniform register (UR) ISA introduced in Turing/Ampere with dedicated uniform ALU instructions:

| SASS Instruction | Operation | Notes |
|---|---|---|
| `UFADD` | Uniform floating-point add | New in Blackwell |
| `UFFMA` | Uniform fused multiply-add | New in Blackwell |
| `UFSEL` | Uniform floating-point select | New in Blackwell |
| `UFSETP` | Uniform FP set-predicate | New in Blackwell |
| `UVIADDR` | Uniform integer-to-address | New in Blackwell |

These instructions execute on the uniform datapath (UDP, functional unit index 9), allowing floating-point uniform computations to stay in the UR file without round-tripping through the R file. Mercury encoding assigns major opcode `0x0E` with 6 variants (`sub_10C0550`) for uniform ALU.

## Architecture Version Threshold Checks

All Blackwell targets share codegen factory 36864+ (`0x9000`+). The binary uses these thresholds to gate Blackwell-specific features:

| Check Pattern | Threshold | Meaning |
|---|---|---|
| `encoded > 36863` | > sm_90 extended | Blackwell warp geometry (16 warps, 240 slots) |
| `codegen_factory >= 36864` | >= sm_100 | Blackwell generation features |
| `codegen_factory == 36864` | sm_100 exactly | sm_100-specific paths |
| `SM_number > 99` | sm_100+ | Capsule Mercury auto-enable |
| `sub_70FA00(*, 29)` | -- | tcgen05 capability query (SM-specific) |

The tcgen05 gating is not a simple threshold -- it uses a per-SM capability query (`sub_70FA00` with argument 29) that returns true for sm_100/103/110 and false for sm_120/121.

### BB Initialization (Secondary Encoding)

The basic block initializer `sub_6E8EB0` uses a secondary encoding space for Blackwell:

| Secondary Encoding | SM | Flags |
|---|---|---|
| 20480 (`0x5000`) | sm_100 | Instruction set flags for datacenter |
| 20484 (`0x5004`) | sm_103 | XOR 0x10, XOR 0x40 for Ultra variants |

This secondary encoding uses generation 5 in the BB init context (`5 << 12`), separate from the primary codegen factory's generation 9.

## Scheduling Profile Differences

Blackwell targets share the 16-warp / 240-slot geometry with Hopper but have distinct latency tables:

| SM | Latency Table | Size | Structure |
|---|---|---|---|
| sm_100 | `sub_8E8A90` | 3.0KB | Base + 949B TCGEN05 supplement |
| sm_103 | (supplementary) | 618B | Smallest table -- minimal delta from sm_100 |
| sm_110 | (shared with sm_100 or dedicated) | -- | Not separately identified in sweep |
| sm_120 | `sub_8E9000` + `sub_8E92E0` | 2.9KB + 5.5KB | Two-part consumer table |
| sm_121 | (likely shares sm_120's table) | -- | Same sub-variant index as sm_120 |
| Universal | `sub_8E97B0` | 8.8KB | Fallback / universal |

The 5.5KB extended table for sm_120 is the **largest individual latency table** in the binary, reflecting the consumer microarchitecture's distinct pipeline design. The sm_100 table uses a supplement mechanism for TCGEN05-specific scheduling classes that the consumer sm_120 table does not need (since sm_120 lacks tcgen05).

### Scheduling Class Assignments

From the opcode-to-scheduling-class mapper `sub_89FBA0` (85KB), Blackwell-era opcodes use high-numbered scheduling classes:

| Class Range | Category | Architecture |
|---|---|---|
| 700--772+ | Mercury/Blackwell tensor ops | sm_100+ with tcgen05 |
| 745 | WGMMA primary | sm_90+ (Hopper carry-forward) |
| 744 | WGMMA variant | sm_90+ |
| 765--767 | BGMMA/QMMA (Blackwell-specific MMA types) | sm_100+ |
| 759 | HMMA/BMMA tensor core | sm_100+ |
| 757, 761 | Narrow/wide DP tensor | sm_100+ |
| 600, 604 | Tensor fence / tensor sync | sm_90+ |

## Intrinsic Table

Blackwell intrinsic availability is cumulative -- all sm_70, sm_80, sm_8x, and sm_9x intrinsics carry forward. Blackwell adds two new intrinsic groups:

### sm_10x Intrinsics (21 entries)

| ID Range | Count | Namespace | Category |
|---|---|---|---|
| 0x20--0x2A | 11 | `__cuda_sm10x_tcgen05_guardrail_trap_*` | Trap on guardrail validation failure |
| 0x230--0x239 | 10 | `__cuda_sm_10x_*` | hmma/imma mdata + bit MMA (Blackwell-specific shapes) |

### sm_1xx Intrinsics (18 entries)

| Namespace | Count | Category |
|---|---|---|
| `__cuda_sm1xx_*` | 18 | cp.async.bulk.tensor 1D--5D tile/im2col (extended bulk copy) |

### OCG (On-Chip Generated) Intrinsics

The OCG builtin name table at `sub_6C9EB0` (13KB) contains the master list of Blackwell+ runtime-generated intrinsic names:

```text
cp_async_bulk, cp_red_async_bulk, cp_async_tensor,
cp_async_prefetch_tensor, fence_view_async,
viaddmax, viaddmin, viadd, vimax, vimin, vimax3, vimin3,
write_async, tcbar, mmareadshma, tcmma,
tcshift, tcatomsws, tcldsws, tcstsws,
gdesc, breuse, bkeep, virtcount,
memclear, acqshminit, sparsify, spfactor2to4,
2x64dp128bitlw02lw13, 2x64dp128bitlw01lw23, 4x32dp128bit,
16dp32bitt0t15, 16dp32bitt16t31,
selfcast, broadcast, findandset, align
```

These names represent the Blackwell SASS operations exposed through the OCG intrinsic interface, covering tensor core scheduling (`tcbar`, `tcmma`, `tcshift`), sparse operations (`sparsify`, `spfactor2to4`), integer min/max variants (`viaddmax`, `viaddmin`), and async memory operations.

## New PTX Instructions (Blackwell-Specific)

Beyond tcgen05, Blackwell introduces or extends several instruction families visible in the opcode dispatch:

| Instruction | Category | Evidence |
|---|---|---|
| `tcgen05.*` (11 instructions) | Tensor memory ops | `sub_5D4190` registration |
| `fence_view_async` | Memory ordering | OCG builtin table |
| `write_async` | Async writes | OCG builtin table |
| `viaddmax` / `viaddmin` | Integer add-with-max/min | OCG builtin table |
| BGMMA / QMMA | Block/quantized MMA | Scheduling classes 765--767 |

## CLI Options -- Tensor Memory Checks

Two CLI options control tcgen05 guardrail instrumentation:

| Option | Short Form | Default | Description |
|---|---|---|---|
| `--g-tensor-memory-access-check` | `-g-tmem-access-check` | Enabled with `-g` | Enable tensor memory access checks for tcgen05 operations |
| `--gno-tensor-memory-access-check` | `-gno-tmem-access-check` | false | Disable checks (overrides the above) |

When enabled, the compiler inserts inline validation code (the 8 guardrail functions) around tcgen05 operations. These emit trap instructions if tensor memory invariants are violated at runtime -- useful for debugging TMEM allocation errors, bounds violations, and ownership conflicts.

## Feature Comparison

| Feature | sm_100 | sm_103 | sm_110 | sm_120 | sm_121 |
|---|---|---|---|---|---|
| Codegen factory | 36864 | 36867 | 36868 | 36869 | 36869 |
| Sub-arch variant | 0 | 3 | 4 | 5 | 5 |
| Family string | Blackwell | Blackwell | Blackwell | Blackwell | Blackwell |
| Capsule Mercury default | Yes | Yes | Yes | Yes | Yes |
| tcgen05 (tensor memory) | Yes | Yes | Yes | **No** | **No** |
| WGMMA (from Hopper) | Yes | Yes | Yes | Yes | Yes |
| Cluster operations | Yes | Yes | Yes | Yes | Yes |
| setmaxnreg | Yes | Yes | Yes | Yes | Yes |
| Uniform FP ALU (UFADD etc.) | Yes | Yes | Yes | Yes | Yes |
| BGMMA/QMMA | Yes | Yes | Yes | ? | ? |
| Guardrail instrumentation | Yes | Yes | Yes | N/A | N/A |
| HW latency table | 3.0KB + 949B | 618B (supp.) | -- | 2.9KB + 5.5KB | (shared w/ sm_120) |
| `a` sub-variant | Yes | Yes | Yes | Yes | Yes |
| `f` sub-variant | Yes | Yes | Yes | Yes | Yes |
| Products | B100/B200 | GB300 | Jetson Thor | RTX 50xx | DGX Spark |

## SASS Instruction Encoding

Blackwell continues the 128-bit per-instruction format introduced in Turing. The Mercury encoder handles the SM100+ instruction set through a dedicated encoding subsystem spanning approximately 851KB in the address range `0xDFC000`--`0x107B000`.

The encoding subsystem covers 16 instruction format groups with full Blackwell ISA support including:

- Standard ALU/FPU/memory operations (inherited from earlier architectures)
- TCGEN05 tensor memory operations (new encoding classes)
- BGMMA/QMMA block-scale and quantized MMA variants
- Extended bulk copy operations (UBLKCP variants)
- Sparse tensor operations

## Function Map

| Address | Size | Identity | SM | Confidence |
|---|---|---|---|---|
| `sub_607DB0` | 14KB | Capability dispatch (all Blackwell registrations) | all | 99% |
| `sub_608DF0` | ~1KB | Intrinsic table initializer | sm_120 | 85% |
| `sub_608F20` | ~1.2KB | Handler A (capability accessor) | sm_103 | 90% |
| `sub_608F50` | ~1.2KB | Handler B (capability accessor) | sm_110 | 90% |
| `sub_609000` | ~200B | Perf/occupancy E | sm_110 | 85% |
| `sub_609020` | ~200B | Perf/occupancy E | sm_103 | 85% |
| `sub_609040` | ~200B | Perf/occupancy E | sm_121 | 85% |
| `sub_609080` | ~200B | Perf/occupancy E | sm_100 | 85% |
| `sub_6091A0` | ~200B | Perf/occupancy F | sm_103 | 85% |
| `sub_609280` | ~200B | Perf/occupancy F | sm_110 | 85% |
| `sub_609520` | ~200B | Perf/occupancy F | sm_120 | 85% |
| `sub_6097C0` | ~200B | Perf/occupancy F | sm_121 | 85% |
| `sub_6098A0` | ~200B | Perf/occupancy F | sm_100 | 85% |
| `sub_609BA0` | ~48B | Handler B | sm_121 | 99% |
| `sub_609BD0` | ~48B | Handler B | sm_100 | 99% |
| `sub_609C30` | ~48B | Handler A | sm_100 | 99% |
| `sub_609C60` | ~48B | Handler B | sm_120 | 99% |
| `sub_609D20` | ~48B | Handler B | sm_103 | 99% |
| `sub_609E40` | ~48B | Handler A | sm_120 | 99% |
| `sub_609ED0` | ~48B | Handler A | sm_121 | 99% |
| `sub_609F30` | ~48B | Handler A | sm_110 | 99% |
| `sub_60A4E0` | ~1KB | Intrinsic table initializer | sm_121 | 85% |
| `sub_60A700` | ~1KB | Intrinsic table initializer | sm_103 | 85% |
| `sub_60A910` | ~1KB | Intrinsic table initializer | sm_100 | 85% |
| `sub_60AA20` | ~1KB | Intrinsic table initializer | sm_110 | 85% |
| `sub_60F290` | est. | Off-target capmerc compatibility checker | all | 75% |
| `sub_612DE0` | 47KB | Kernel finalizer / ELF builder | all | 80% |
| `sub_6765E0` | 54KB | Profile constructor (Blackwell entries at lines 600--1330) | all | 95% |
| `sub_703AB0` | 10KB | CLI option parser (capmerc/mercury/sass) | all | 90% |
| `sub_70D910` | 24 lines | tcgen05 immhalfSplitOff helper | sm_100 | 90% |
| `sub_70DDB0` | 47 lines | tcgen05 funcRetArr helper | sm_100 | 90% |
| `sub_70E0E0` | 296 lines | tcgen05 guardrail bounds checker | sm_100 | 90% |
| `sub_8E4400` | 3.3KB | Warp geometry initializer (Blackwell = 16 warps, 240 slots) | all | 90% |
| `sub_8E8A90` | 3.0KB | HW latency table (Blackwell datacenter) | sm_100 | 85% |
| `sub_8E9000` | 2.9KB | HW latency table (consumer base) | sm_120 | 85% |
| `sub_8E92E0` | 5.5KB | HW latency table (consumer extended) | sm_120 | 85% |
| `sub_8E97B0` | 8.8KB | Universal fallback latency table | all | 85% |
| `sub_89FBA0` | 85KB | Opcode-to-scheduling-class mapper | all | 90% |
| `sub_5BBC30` | 90KB | tcgen05.mma codegen handler | sm_100 | 98% |
| `sub_5D4190` | -- | Opcode dispatch table (tcgen05 registrations) | all | 99% |
| `sub_1C9C300` | 24KB | Capsule Mercury section processor | all | 85% |
| `sub_1C9B110` | 23KB | Mercury capsule builder | all | 85% |

## Cross-References

- [SM Architecture Map](index.md) -- Validation tables, codegen factory encoding, suffix semantics
- [Turing & Ampere (SM 75--88)](turing-ampere.md) -- Baseline features that Blackwell inherits
- [Ada & Hopper (SM 89--90a)](ada-hopper.md) -- WGMMA, clusters, TMA -- carried forward to Blackwell
- [TCGen05 -- 5th Gen Tensor Cores](tcgen05.md) -- Detailed tcgen05 ISA, TMEM model, instruction encoding
- [Capsule Mercury & Finalization](../codegen/capmerc.md) -- Capmerc format, ELF structure, finalization levels
- [Mercury Encoder](../codegen/mercury.md) -- Shared encoding pipeline for all Mercury/capmerc output
- [Intrinsic Table (608 Entries)](../intrinsics/index.md) -- Full intrinsic catalog with sm_10x and sm_1xx ranges
- [Latency Model & HW Profiles](../scheduling/latency-model.md) -- Per-SM scheduling parameters and functional units
- [Uniform Register Optimization](../passes/uniform-regs.md) -- UR-file passes, Blackwell uniform ALU additions
- [CLI Options](../config/cli-options.md) -- `--gpu-name`, `--binary-kind`, tensor memory check flags
- [SASS Instruction Encoding](../codegen/encoding.md) -- 128-bit format, Mercury encoder selection
