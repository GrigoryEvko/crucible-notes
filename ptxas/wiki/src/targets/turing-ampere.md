# Turing & Ampere (SM 75–88)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

SM 75 through SM 88 span two microarchitecture generations that ptxas treats as a contiguous feature band. sm_75 (Turing) is the **default target** for ptxas v13.0.88 — when `--gpu-name` is omitted, `sub_6784B0` returns `sm_75`. The Ampere targets (sm_80, sm_86, sm_87, sm_88) share generation-7 SASS encoding and add incremental tensor core and async-copy capabilities. sm_89 (Ada Lovelace) is architecturally Ampere-derived internally but is covered in [Ada & Hopper](ada-hopper.md) because it bridges to sm_90 features.

| | |
|---|---|
| **SM targets** | sm_75, sm_80, sm_86, sm_87, sm_88 (+ sm_82 validation-only) |
| **Codegen factory range** | 24577–28676 |
| **ISA generation** | 6 (Turing), 7 (Ampere) |
| **Encoding format** | 128-bit per-instruction control word |
| **Scheduler profile** | 7 warps, 208 dispatch slots |
| **Family strings** | `"Turing"` (sm_75), `"Ampere"` (sm_80–88) |
| **Sub-variants** | None (no `a` or `f` suffixes) |
| **Profile object size** | 1,936 bytes (allocated by `sub_917990`) |

## SM Version Table

| SM | Product | Family | `__CUDA_ARCH__` | Codegen Factory | Hex | Variant |
|---|---|---|---|---|---|---|
| `sm_75` | TU10x (RTX 20xx, Quadro RTX) | Turing | 750 | 24577 | `0x6001` | 1 (gen 6) |
| `sm_80` | GA100 (A100, A30) | Ampere | 800 | 28673 | `0x7001` | 1 (gen 7) |
| `sm_86` | GA10x (A40, A10, RTX 30xx) | Ampere | 860 | 28674 | `0x7002` | 2 |
| `sm_87` | GA10B (Jetson Orin) | Ampere | 870 | 28675 | `0x7003` | 3 |
| `sm_88` | — (undocumented) | Ampere | 880 | 28676 | `0x7004` | 4 |

**Codegen factory encoding:** `(isa_generation << 12) | sub_variant`. Turing is generation 6; Ampere is generation 7. The sub-variant distinguishes silicon cut within a generation. sm_75 and Pascal sm_60 share generation 6 (sm_60 = 24576 = `0x6000`), differentiated by sub-variant 0 vs 1.

**sm_88 note:** Registered in ptxas with `CUDA_ARCH=880` and codegen factory 28676, but no public product ships on this SM. It may represent an unreleased Ampere derivative or internal test target.

## SM 82 — Internal Ampere Target

sm_82 is an undocumented internal Ampere target present in the base validation table (`unk_1D16220`, entry [20]) but **not registered** in the profile constructor `sub_6765E0`. It has no capability dispatch handler, no profile object, and no handler functions in any of the 7 hash maps. It exists in ptxas solely as a validation table entry and as the SASS opcode generation boundary.

| | |
|---|---|
| **Validation table entry** | `{82, 6, 2}` — sm_82, PTX 6.2 |
| **PTX ISA requirement** | 6.2 (anomalously low — see below) |
| **Profile object** | None — not registered in `sub_6765E0` |
| **Capability handlers** | None — not registered in `sub_607DB0` |
| **SASS opcode role** | `SM82_FIRST` (index 172) through `SM82_LAST` (index 193) |

### PTX 6.2 Anomaly

sm_82 requires PTX ISA version 6.2, which is **lower** than both its neighbors:

| SM | PTX ISA | CUDA Toolkit |
|---|---|---|
| sm_75 | 6.3 | CUDA 10.0 |
| sm_80 | 7.0 | CUDA 11.0 |
| **sm_82** | **6.2** | — |
| sm_86 | 7.1 | CUDA 11.1 |

PTX 6.2 corresponds to CUDA 10.1 (Turing era). This backward version number strongly suggests sm_82 was created as an early Ampere development target — a PTX-level placeholder added before the Ampere PTX ISA (7.0) was defined. The validation table entry was never removed, but no profile object was ever created for it.

### SASS Opcode Boundary Role

sm_82's primary significance in ptxas is as the opcode generation boundary for Ampere SASS instructions. The opcode hierarchy uses SM-number-based range labels:

```text
SM82_FIRST  = index 172   (first Ampere-era SASS opcode)
SM82_LAST   = index 193   (last opcode in the sm_82 range)
```

These 22 opcode slots (indices 172–193) cover the core Ampere SASS additions:

| Opcodes | Category |
|---|---|
| GATHER, GENMETADATA, SPMETADATA | Sparse MMA infrastructure |
| BMMA_88128, BMMA_168128, BMMA_168256 | Binary tensor core MMA shapes |
| DMMA | FP64 tensor core MMA (re-introduced at index 215 for Hopper) |
| HMMA_SP_1688, HFMA2_MMA, HMNMX2 | FP16 sparse/packed operations |
| IMMA_88, IMMA_SP_88, IMMA_16816, IMMA_16832, IMMA_SP_16832 | Integer tensor core MMA shapes |
| ARRIVES, LDGDEPBAR, LDGSTS | Async copy and barrier infrastructure |
| REDUX | Warp-wide reduction |
| CLMAD | Carry-less multiply-add (GF(2) arithmetic) |

The name `SM82_FIRST`/`SM82_LAST` is used as the boundary label even though these instructions are available on sm_80+ (any codegen factory >= 28673). The "82" in the label refers to the internal target used during Ampere development, not to a minimum SM requirement for the opcodes themselves.

### Why sm_82 Matters

sm_82 is a ghost target: it occupies a validation table slot and lends its name to an opcode range, but cannot be compiled for. Passing `--gpu-name sm_82` to ptxas would pass the initial validation check (bsearch succeeds in the base table) but fail during profile construction because `sub_6765E0` has no case for SM 82. The practical consequence is that sm_82 is a naming artifact preserved from Ampere development, not a usable compilation target.

## Profile Object Construction

Each SM's intrinsic table initializer (Map 3 handler) calls `sub_917990` to allocate a 1,936-byte profile object, then populates architecture-specific fields.

**Source:** decompiled `sub_60A2E0` (sm_75), `sub_60A3E0` (sm_80), `sub_60AC30` (sm_86), `sub_60AD30` (sm_87), `sub_60AB30` (sm_88).

All five initializers are structurally identical. The only field that differs between them is offset `+348` (codegen factory). Common initialization:

```c
v6 = sub_917990(a3);              // allocate 1936-byte profile object
*(_DWORD *)(v6 + 344) = 0x100000; // shared memory config base (1 MB)
*(_DWORD *)(v6 + 348) = FACTORY;  // codegen factory (per-SM)
v7[147] = 0;                      // cleared
v7[421] = cli_option_value;       // from a1+108
v7[460] = 1;                      // enable flag
v7[482] = 1;                      // enable flag
v7[470] = 1;                      // enable flag
v7[177] = 1;                      // enable flag
```

The `sub_917990` base constructor sets the default codegen factory to `0x2000` (8192), which every per-SM initializer immediately overwrites. Key base constructor fields:

| Offset | Default | Content |
|---|---|---|
| +348 | 0x2000 | Codegen factory (overridden) |
| +588 | 0 | Cleared |
| +1892 | 2 | Mode/config value |
| +1832–1848 | xmmword | SSE-loaded constant block |
| +1908–1924 | xmmword | SSE-loaded constant block |

## Handler Dispatch

ptxas registers per-SM handler functions into 7 parallel hash maps via `sub_607DB0`. For sm_75–88, all handlers are thin wrappers around shared codegen infrastructure.

### Map 1 (Handler A) and Map 2 (Handler B)

| SM | Handler A | Handler B |
|---|---|---|
| sm_75 | `sub_609B70` | `sub_609B40` |
| sm_80 | `sub_609CC0` | `sub_609C90` |
| sm_86 | `sub_609D50` | `sub_609D80` |
| sm_87 | `sub_609F00` | `sub_609DE0` |
| sm_88 | `sub_609E70` | `sub_609EA0` |

Every Handler A function is identical:

```c
bool handler_A(int64_t a1, int64_t a2) {
    *(int32_t*)(a2 + 100) = read_option(a2, "cpf_optx");
    return sub_663C30(a2, 0) != 0;  // a2=0: sets *(a1+96) = 1
}
```

Every Handler B function is identical except the second argument:

```c
bool handler_B(int64_t a1, int64_t a2) {
    *(int32_t*)(a2 + 100) = read_option(a2, "cpf_optx");
    return sub_663C30(a2, 1) != 0;  // a2=1: skips the flag set
}
```

The `"cpf_optx"` option controls OptiX IR compilation mode. `sub_663C30` is the core codegen-factory-aware driver that delegates to `sub_662920` (ELF section iteration, 26KB) and `sub_7FBB70` (actual compilation pass). The only behavioral difference between Handler A and Handler B: when called as Handler A (arg=0), `sub_663C30` sets `*(a1 + 96) = 1` before processing — likely a "primary pass" flag.

### Map 3 (Intrinsic Table Initializer)

| SM | Initializer | Notes |
|---|---|---|
| sm_75 | `sub_60A2E0` | Factory 24577 |
| sm_80 | `sub_60A3E0` | Factory 28673 |
| sm_86 | `sub_60AC30` | Factory 28674 |
| sm_87 | `sub_60AD30` | Factory 28675 |
| sm_88 | `sub_60AB30` | Factory 28676 |

### Maps 6–7 (Performance / Occupancy)

| Map | sm_75 Handler | Purpose |
|---|---|---|
| 6 | `sub_608D50` | Perf-stats / occupancy handler E |
| 7 | `sub_6096E0` | Perf-stats / occupancy handler F |

These handlers return per-SM occupancy parameters used by the driver API's occupancy calculator. Other Ampere SMs have their own handlers registered into the same maps (addresses not fully traced in sweep data).

## Scheduler Profile

`sub_8E4400` (`InitHWProfile_Warp`) uses the codegen factory value to select scheduling parameters. The dispatch is a linear threshold cascade:

| Factory Range | Warps | Dispatch Slots | Architecture Class |
|---|---|---|---|
| <= 20479 | 4 | 96 | Kepler (sm_30) |
| <= 24575 | 6 | 176 | Pascal (sm_60) |
| <= 28672 | 7 | 192 | Volta (sm_70) |
| **<= 32767** | **7** | **208** | **Turing / Ampere (sm_75–88)** |
| <= 36863 | 8 | 224 | Hopper (sm_90) |
| > 36863 | 16 | 240 | Blackwell (sm_100+) |

All SM 75–88 targets fall into the 7-warp / 208-slot bucket. After the warp count, a secondary switch maps specific codegen factory values to **sub-architecture variants** (stored at `a1+26`):

| Codegen Factory | Variant | SM |
|---|---|---|
| 24576, 32768, 36864 | 0 | sm_60, sm_90, sm_100 (base) |
| 8193, 20481, **28674** | 2 | sm_30, sm_50, **sm_86** |
| **28675**, 36867 | 3 | **sm_87**, sm_103 |
| **28676**, 36868 | 4 | **sm_88**, **sm_120** |
| 28677, 36869 | 5 | sm_89, sm_121 |

sm_75 (24577) and sm_80 (28673) are absent from the variant table and fall through to the default variant (0 or 1). This means sm_75 and sm_80 use the baseline latency model, while sm_86–88 get tuned sub-architecture parameters. **Note:** gen-9 code `0x9004` (36868) is **sm_120** (chip ordinal 4), *not* sm_110 — `sub_8E4400` matches `$0x9004`→sm_120 and `$0x9005`→sm_121 on distinct branches. sm_110 carries code `0x9001` (36865, ordinal 1, shared with the phantom sm_101 slot) and is handled on the low-code path with sm_80, not in this gen-9 variant-4 row.

## HW Latency Tables

Each SM has a dedicated latency table function containing per-opcode pipeline stage assignments, stall cycle costs, and functional unit mappings. These are called from the scheduling infrastructure to drive instruction ordering.

| Function | Size | SM | Notes |
|---|---|---|---|
| `sub_8E7720` | 3.5 KB | sm_75 | Turing baseline |
| `sub_8E7940` | 2.9 KB | sm_80 (base) | Ampere shared layer |
| `sub_8E7B40` | 3.3 KB | sm_80 | Ampere full table |
| `sub_8E7D80` | 4.4 KB | sm_86 | Largest in Ampere family |
| `sub_8E8070` | 3.5 KB | sm_87 | Orin tuning |
| `sub_8E8280` | 3.1 KB | sm_89 | Ada Lovelace |

sm_80 uniquely has **two** latency tables: a "base" table (`sub_8E7940`, 2.9KB) and a full table (`sub_8E7B40`, 3.3KB), suggesting a layered lookup where the base provides defaults and the full table overrides specific entries. sm_86's table is the largest at 4.4KB, likely because RTX 30xx consumer GPUs have different pipeline characteristics from A100 datacenter parts.

No separate table entry was found for sm_88 in the sweep data. It may share sm_86 or sm_87's latency profile, or be registered through a path not captured in the sweep.

## SASS Instruction Encoding

SM 75–88 all use the **128-bit per-instruction encoding format** introduced with Turing. This replaced the Volta/Pascal scheme where scheduling control was packed into a separate 64-bit control header shared by 3 instructions.

### Control Word Layout

Each 128-bit SASS instruction carries a 23-bit control word encoding scheduling decisions. The control word is generated by `sub_A36360` (52KB, the control-word / scoreboard encoder).

```text
bits [0:3]   stall count       (4 bits, max 15 cycles)
bit  [4]     yield flag        (1 bit, warp scheduler hint)
bits [5:7]   write barrier idx (3 bits, selects 1 of 6 barriers)
bits [8:13]  read barrier mask (6 bits, one per barrier)
bits [14:19] wait barrier mask (6 bits, one per barrier)
bits [20:25] reuse flags       (6 bits, per source operand)
```

### Instruction Word Structure

```text
128-bit instruction word:
  bits [0:3]     = 0x2       (format code: 128-bit)
  bits [4:6]     = slot      (scheduling group slot)
  bits [8:16]    = MAJOR     (9-bit major opcode, 0x00-0x171)
  bits [17:24]   = MINOR     (8-bit minor opcode / variant)
  bits [25:31]   = SUBOP     (7-bit sub-opcode / format ID)
  bits [48+]     = MODIFIERS (format-dependent modifier fields)
  bits [132:134] = 0x0       (extended opcode flag, at offset 0x84)
```

The 3-level opcode hierarchy (major/minor/subop) allows up to 102 major opcodes with 48 sub-operations each. Maximum observed variant value: 0x2F (47 decimal).

## Scoreboard / Dependency Barriers

SM 75–88 provide **6 hardware dependency barriers** per warp. The scoreboard tracker is managed by `sub_8E4920` (BuildScoreboardEntries, 6.9KB) and encoded by `sub_A36360`.

| Resource | Width | Range | Notes |
|---|---|---|---|
| Write barrier index | 3 bits | 0–5 active, 6–7 reserved | Assigns instruction to a barrier |
| Read barrier mask | 6 bits | 1 bit per barrier | Indicates which barriers to check before read |
| Wait barrier mask | 6 bits | 1 bit per barrier | Indicates which barriers to wait on |

The scoreboard tracker allocates 952 bytes per function when bit 4 of the flag byte at offset `+1385` is set. An additional 856-byte bitset is allocated when bit 8 is also set (for barrier register tracking in writeback mode).

The scoreboard infrastructure is shared across all SM 75–88 targets. The barrier count (6) is constant for this entire range. sm_90 (Hopper) potentially increases this, and sm_100+ (Blackwell) changes the barrier model further.

## Intrinsic Table

Intrinsic availability is cumulative. Each generation adds to the previous.

### sm_75 Baseline (IDs 0x89–0x1FA, 370 intrinsics)

sm_75 inherits the full sm_70 (Volta) intrinsic set labeled `__cuda_sm70_*`:

| Category | Intrinsics | PTX Operations |
|---|---|---|
| Named barriers | barrier_arrive/red/sync (0–15) | `bar.arrive`, `bar.red.{and,or,popc}`, `bar.sync` |
| Warp shuffle | shflsync_bfly/down/idx/up | `shfl.sync.{bfly,down,idx,up}` |
| Warp vote | votesync_all/any/ballot/uni | `vote.sync.{all,any,ballot,uni}` |
| Warp match | matchsync_all/any_b32/b64 | `match.sync.{all,any}.b{32,64}` |
| Warp sync | warpsync | `bar.warp.sync` |
| Redux | reduxsync_* (IDs 0x01–0x11) | `redux.sync.{and,or,xor,min,max,add}` |
| WMMA | m16n16k16, m32n8k16, m8n32k16 | `wmma.{load,store,mma}` |

WMMA intrinsics cover all combinations of:
- Shapes: m16n16k16, m32n8k16, m8n32k16
- Operations: load_a, load_b, load_c, store_d, mma
- Layouts: row/col combinations
- Types: f16, f32, with/without satfinite
- Address spaces: generic, global, shared

### sm_80 Additions (IDs 0x1FB–0x22F, 53 intrinsics)

sm_80 adds two intrinsic groups:

**14 `__cuda_sm80_*` intrinsics (IDs 0x1FB–0x208):**

| Intrinsic | PTX Operation | Notes |
|---|---|---|
| createpolicy_range | `createpolicy.range` | L2 cache persistence control |
| createpolicy_fractional | `createpolicy.fractional` | L2 cache fraction control |
| createpolicy_cvt | Cache policy conversion | |
| mma_bf16_* | `mma.sync` with `.bf16` | BF16 tensor core MMA |
| mma_tf32_* | `mma.sync` with `.tf32` | TF32 tensor core MMA |
| mma_s4_* | `mma.sync` with `.s4` | INT4 tensor core MMA |
| mma_s8_* | `mma.sync` with `.s8` | INT8 tensor core MMA |
| mma_b1_* | `mma.sync` with `.b1` | Binary tensor core MMA |

**39 `__cuda_sm_8x_mma_*` intrinsics (IDs 0x209–0x22F):**

Extended MMA shapes and sparse variants for the 2nd/3rd generation tensor core.

### Intrinsic Gate Mechanism

The per-SM intrinsic table initializer (Map 3 handler) controls which intrinsics are available. sm_75 registers only the sm_70 intrinsic block. sm_80+ additionally registers the sm_80 and sm_8x blocks. The gate is not a runtime check — it is a registration-time decision: if the intrinsic's handler function is not registered for a given SM, the PTX parser emits an error when it encounters a call to that intrinsic.

## Peephole Optimizer Gates

The SASS-level peephole optimizer (`sub_83EF00`, 4,858 lines decompiled) applies pattern-matching transformations to the instruction stream. Several transformations are gated by codegen factory thresholds.

### FMA/DFMA Combining

```c
// sub_83EF00, case 0x50 (opcode 80 = FMA/DFMA)
if (*(_QWORD *)(*(_QWORD *)(a1 + 1584) + 372) > 28673) {
    // FMA combining enabled: sm_86+ (codegen factory 28674+)
    // Look for CVT -> FMA patterns and combine
}
```

**Gate:** `codegen_factory > 28673`. This means:
- sm_75 (24577): FMA combining **disabled**
- sm_80 (28673): FMA combining **disabled** (equality fails the `>` check)
- sm_86 (28674): FMA combining **enabled**
- sm_87 (28675): FMA combining **enabled**
- sm_88 (28676): FMA combining **enabled**

This is a significant compiler optimization difference between A100 (sm_80) and RTX 30xx (sm_86). A100 code does not get FMA/DFMA combining, possibly because A100's pipeline already has hardware FMA fusion or because the combining transformation is not profitable on GA100 silicon.

### Master Gate

All peephole passes check capability 487 ("enable peephole") and capability 350 (per-instruction gate) before applying any transformation. These capabilities are controlled by optimization level and user options, not by SM target.

## BB Initialization Flags

The basic block initializer `sub_6E8EB0` sets architecture-specific flags using a secondary SM version encoding (distinct from the codegen factory):

| SM Version (secondary) | Hex | Flags Set | Architecture |
|---|---|---|---|
| 20480 | 0x5000 | bits 1, 8 | sm_80 encoding space |
| 20484 | 0x5004 | bits 16, 64 | sm_84 encoding space |

This secondary encoding uses `(gen << 12)` with generation 5 for Ampere in the BB init context. The specific bit flags control opcode descriptor table population — each BB gets a set of 40+ `(opcode_id, encoding_word)` pairs that define which SASS instructions are legal in that basic block.

## Feature Comparison

| Feature | sm_75 | sm_80 | sm_86 | sm_87 | sm_88 |
|---|---|---|---|---|---|
| ISA generation | 6 | 7 | 7 | 7 | 7 |
| Codegen factory | 24577 | 28673 | 28674 | 28675 | 28676 |
| WMMA (1st gen TC) | Yes | Yes | Yes | Yes | Yes |
| MMA bf16/tf32 (2nd gen TC) | — | Yes | Yes | Yes | Yes |
| MMA s4/s8/b1 extended | — | Yes | Yes | Yes | Yes |
| `createpolicy` (L2 cache) | — | Yes | Yes | Yes | Yes |
| `cp.async` (async copy) | — | Yes | Yes | Yes | Yes |
| sm_8x MMA intrinsics (39) | — | Yes | Yes | Yes | Yes |
| FMA/DFMA peephole combining | — | — | Yes | Yes | Yes |
| Scheduler: warps / slots | 7/208 | 7/208 | 7/208 | 7/208 | 7/208 |
| Sub-arch variant | default | default | 2 | 3 | 4 |
| Separate HW latency table | Yes | Yes (2) | Yes | Yes | ? |
| Family string | Turing | Ampere | Ampere | Ampere | Ampere |

## Hardware Resource Geometry

Per-SM hardware resource limits used by ptxas for register allocation, occupancy calculations, and scheduling decisions. Extracted from `sub_8688F0` (universal baseline), `sub_8E4400` (scheduler partition geometry), and `sub_ABF250` (occupancy calculator). See [targets/index.md — Per-SM Resource Geometry Table](index.md#per-sm-resource-geometry-table) for the complete table across all architectures.

| SM | Regs/SM | Max Regs/Thread | Max Threads/CTA | Warps/SM | Max CTAs/SM | Sched Partitions | Dispatch Slots | Configurable Shared Memory | Conf |
|---|---|---|---|---|---|---|---|---|---|
| `sm_75` | 65,536 | 255 | 1,024 | 32 | 16 | 7 / 208 | 208 | 32 / 48 / 64 KB | 90% |
| `sm_80` | 65,536 | 255 | 2,048 | 64 | 32 | 7 / 208 | 208 | 48 / 100 / 132 / 164 KB | 90% |
| `sm_86` | 65,536 | 255 | 1,536 | 48 | 16 | 7 / 208 | 208 | 48 / 100 KB | 90% |
| `sm_87` | 65,536 | 255 | 1,536 | 48 | 16 | 7 / 208 | 208 | 48 / 100 / 164 KB | 90% |
| `sm_88` | 65,536 | 255 | 1,536 | 48 | 16 | 7 / 208 | 208 | (same as sm\_86) | 85% |

**Column definitions:**
- **Regs/SM**: Total 32-bit registers per streaming multiprocessor. 65,536 universally for sm\_75+.
- **Max Regs/Thread**: Maximum registers a single thread can use. 255 universally (`sub_8688F0` offset +612).
- **Max Threads/CTA**: Maximum threads per cooperative thread array (block).
- **Warps/SM**: Total concurrent warps per SM. Determines peak occupancy.
- **Max CTAs/SM**: Maximum concurrent CTAs per SM.
- **Sched Partitions / Dispatch Slots**: From `sub_8E4400` offset +18 (packed DWORD) and offset +22 (WORD).
- **Configurable Shared Memory**: Valid shared memory sizes per CTA, selected by `cudaFuncSetAttribute`.

All Turing/Ampere targets share the 7-partition / 208-slot scheduling geometry. The major resource difference is sm\_80 (A100 datacenter) with 2,048 max threads and 64 warps vs. the consumer/embedded parts (sm\_86–88) with 1,536 max threads and 48 warps. sm\_75 (Turing) is the most constrained with 1,024 max threads and 32 warps.

## Codegen Factory Gating Patterns

Throughout ptxas, the codegen factory value at profile offset `+348` (or equivalently `*(*(QWORD*)(a1+1584)+372)` from the function context) is used to gate features. Common patterns for the Turing/Ampere range:

```c
// Pattern 1: Generation check (factory >> 12)
int gen = codegen_factory >> 12;
if (gen >= 7) { /* Ampere+ feature */ }

// Pattern 2: Exact threshold
if (codegen_factory > 28673) { /* sm_86+ only */ }
if (codegen_factory >= 28673) { /* sm_80+ */ }

// Pattern 3: Range check
if (codegen_factory >= 24577 && codegen_factory < 32768) {
    /* Turing/Ampere only, not Hopper+ */
}
```

The `>> 12` shift extracts the ISA generation, allowing coarse checks (Turing = 6, Ampere = 7). Fine-grained checks compare against specific factory values to distinguish sm_80 from sm_86, etc.

## Function Map

| Address | Size | Identity | SM | Confidence |
|---|---|---|---|---|
| `sub_609B70` | ~48B | Handler A (cpf_optx + compile) | sm_75 | 99% |
| `sub_609B40` | ~48B | Handler B (cpf_optx + compile) | sm_75 | 99% |
| `sub_60A2E0` | ~300B | Intrinsic table initializer | sm_75 | 95% |
| `sub_609CC0` | ~48B | Handler A | sm_80 | 99% |
| `sub_609C90` | ~48B | Handler B | sm_80 | 99% |
| `sub_60A3E0` | ~300B | Intrinsic table initializer | sm_80 | 95% |
| `sub_609D50` | ~48B | Handler A | sm_86 | 99% |
| `sub_609D80` | ~48B | Handler B | sm_86 | 99% |
| `sub_60AC30` | ~300B | Intrinsic table initializer | sm_86 | 95% |
| `sub_609F00` | ~48B | Handler A | sm_87 | 99% |
| `sub_609DE0` | ~48B | Handler B | sm_87 | 99% |
| `sub_60AD30` | ~300B | Intrinsic table initializer | sm_87 | 95% |
| `sub_609E70` | ~48B | Handler A | sm_88 | 99% |
| `sub_609EA0` | ~48B | Handler B | sm_88 | 99% |
| `sub_60AB30` | ~300B | Intrinsic table initializer | sm_88 | 95% |
| `sub_608D50` | ~200B | Perf/occupancy handler E | sm_75 | 85% |
| `sub_6096E0` | ~200B | Perf/occupancy handler F | sm_75 | 85% |
| `sub_663C30` | ~400B | Core codegen driver (shared) | all | 90% |
| `sub_662920` | 26KB | ELF section iteration (shared) | all | 85% |
| `sub_917990` | ~300B | Profile object constructor | all | 90% |
| `sub_8E7720` | 3.5KB | HW latency table | sm_75 | 90% |
| `sub_8E7940` | 2.9KB | HW latency table (base layer) | sm_80 | 90% |
| `sub_8E7B40` | 3.3KB | HW latency table (full) | sm_80 | 90% |
| `sub_8E7D80` | 4.4KB | HW latency table | sm_86 | 90% |
| `sub_8E8070` | 3.5KB | HW latency table | sm_87 | 90% |
| `sub_8E4400` | 3.3KB | InitHWProfile_Warp (shared) | all | 90% |
| `sub_83EF00` | ~100KB | Primary peephole optimizer | all | 85% |
| `sub_A36360` | 52KB | Control word / scoreboard encoder | all | 85% |
| `sub_8E4920` | 6.9KB | BuildScoreboardEntries | all | 85% |

## Cross-References

- [SM Architecture Map](index.md) — Overview of all 23 SM targets and the 3-level profile system
- [Ada & Hopper (SM 89–90a)](ada-hopper.md) — sm_89 (Ada) shares Ampere codegen factory range but bridges to Hopper features
- [Blackwell (SM 100–121)](blackwell.md) — Next-generation targets with codegen factory 36864+
- [Intrinsic Table (608 Entries)](../intrinsics/index.md) — Full intrinsic catalog with per-SM generation ranges
- [SASS Instruction Encoding](../codegen/encoding.md) — 128-bit encoding format, bitfield packer, opcode hierarchy
- [Peephole Optimization](../codegen/peephole.md) — FMA combining and other post-scheduling SASS transforms
- [3-Phase Scheduler Architecture](../scheduling/overview.md) — Scheduler infrastructure that consumes HW latency tables
- [CLI Options](../config/cli-options.md) — `--gpu-name` parsing, `sm_75` default
