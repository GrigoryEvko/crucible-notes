# Uniform Register Optimization

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Four passes in the ptxas pipeline collectively manage the conversion of general-purpose register (R) values to uniform registers (UR) on sm_75+ targets. The UR register file is a dedicated 63-entry, 32-bit register bank shared across all threads in a warp: every thread reads the same value from a given UR. By routing warp-uniform computations through the UR file, ptxas reduces R-register pressure (the dominant occupancy limiter), enables the UR-specific ALU datapath, and avoids broadcasting the same value 32 times across the register file.

| | |
|---|---|
| **Phases** | 11, 27, 74, 86 |
| **Phase names** | `ReplaceUniformsWithImm`, `AnalyzeUniformsForSpeculation`, `ConvertToUniformReg`, `InsertPseudoUseDefForConvUR` |
| **Target** | sm_75+ (Turing and later) -- no-op on earlier architectures |
| **Register file** | UR: UR0--UR62 usable, UR63 = URZ (zero register); UP: UP0--UP6, UP7 = UPT |
| **Hardware limit** | 63 uniform GPRs, 7 uniform predicates per thread |
| **Code Object field** | `+99` = UR count; `+856` = UR liveness bitvector |
| **Context flags** | `+1368` bit 1 = has-uniform; `+1376` bit 4 = UR tracking enabled; `+1378` bit 3 = has-UR-regs |
| **Key knobs** | 487 (general optimization gate), 628 (pre-allocation UR promotion), 687 (uniform register mode) |
| **Related passes** | `OriPropagateVaryingFirst` (53), `OriPropagateVaryingSecond` (70), `OptimizeUniformAtomic` (44), `ConvertMemoryToRegisterOrUniform` (`sub_910840`) |

## Background: Uniform vs. Divergent Values

A value is **uniform** (warp-uniform) if every active thread in the warp holds the same value for that register at a given program point. A value is **divergent** if different threads may hold different values.

Sources of uniformity:
- **Kernel parameters.** All threads receive the same parameter values. Parameters loaded from constant memory (`LDC`) with a uniform address are uniform by construction.
- **Constant memory loads.** `LDC` with a uniform base address produces a uniform result.
- **S2R of warp-uniform special registers.** Registers like `SR_CTAID_X/Y/Z`, `SR_GRIDID`, and `SR_SMID` are uniform across the warp. `SR_TID_X/Y/Z` and `SR_LANEID` are divergent.
- **Arithmetic on uniform inputs.** If all source operands are uniform, the result of any pure ALU operation is uniform.
- **Convergent control flow.** A value defined before a divergent branch and used after reconvergence is still uniform if the definition was uniform.

Sources of divergence:
- **Thread identity registers.** `SR_TID_X/Y/Z`, `SR_LANEID` vary per thread.
- **Memory loads from thread-dependent addresses.** `LDG [R_addr]` where `R_addr` is divergent produces a divergent result.
- **Phi merges across divergent branches.** A `MOV.PHI` that merges values from two sides of a divergent branch is divergent even if each incoming value was individually uniform.

ptxas tracks uniformity through two complementary mechanisms: forward "varying" propagation (`OriPropagateVarying`, phases 53 and 70) marks registers as divergent, while the uniform analysis passes (this page) identify which remaining values are safe to move to the UR file.

## UR Hardware ISA

sm_75+ architectures provide a dedicated set of uniform-only SASS instructions that operate on UR/UP registers. These execute on the uniform datapath, which processes one value per warp instead of 32:

| SASS mnemonic | ROT13 in binary | Operation |
|---|---|---|
| `UIADD3` | `HVNQQ3` | Uniform 3-input integer add |
| `UIMAD` | `HVZNQ` | Uniform integer multiply-add |
| `ULOP3` | `HYBC3` | Uniform 3-input logic |
| `UISETP` | `HVFRGC` | Uniform integer set-predicate |
| `USGXT` | `HFTKG` | Uniform sign-extend |
| `UPRMT` | `HCEZG` | Uniform byte permute |
| `UPOPC` | `HCBCP` | Uniform population count |
| `UBREV` | `HOERI` | Uniform bit reverse |
| `UP2UR` | `HC2HE` | Uniform predicate to uniform register |
| `UPLOP3` | `HCYBC3` | Uniform predicate LOP3 |
| `VOTEU` | `IBGRH` | Uniform vote |

Blackwell (sm_100+) extends the uniform ISA with:
- `UFADD`, `UFFMA`, `UFSEL`, `UFSETP` -- uniform floating-point operations
- `UVIADDR` -- uniform virtual address computation
- `UCLEA`, `UCVTA`, `ULEPC` -- uniform address operations
- `UTMAPC`, `UTMALDG`, `UTMAPF`, `UTMAREDG` -- uniform TMA (tensor memory accelerator) operations
- `UBLKPC`, `UBLKRED`, `UBLKPF` -- uniform block operations

The `R2UR` instruction transfers a value from the R file to the UR file; `UR2R` does the reverse. These are the bridge instructions that `ConvertToUniformReg` inserts at file boundaries.

The SASS encoder at `sub_7BC360` (126 callers) handles UR register encoding using the register-variant-B format, distinct from the main register encoder `sub_7BC030`. The decoder `sub_7BD7D0` (4 callers) extracts UR operands with type=4 (uniform register). In the Mercury encoding layer, Major 0x0E (6 variants, `sub_10C0550`) encodes the uniform ALU instructions (UIADD3, ULOP3, etc.).

## Phase 11: ReplaceUniformsWithImm

| | |
|---|---|
| **Phase index** | 11 |
| **Pipeline position** | Stage 1 (Initial Setup), after `EarlyOriSimpleLiveDead` (10), before `OriSanitize` (12) |
| **Category** | Optimization |

### Purpose

Replaces uniform register reads with immediate constants when the value is known at compile time. This is the earliest uniform-related optimization in the pipeline, running before any loop or branch optimization.

### Motivation

Kernel launch parameters are passed through constant memory. After PTX-to-Ori lowering, a kernel parameter access looks like:

```
LDC  R3, c[0x0][0x160]     // load parameter from constant bank
IMAD R4, R3, R5, RZ        // use the parameter
```

If the compiler can prove that the constant bank address contains a known immediate (e.g., from `.param` directives with known offsets), the `LDC` is dead and the use can be folded:

```
IMAD R4, 42, R5, RZ        // parameter replaced with immediate 42
```

This eliminates constant memory traffic and reduces register pressure by one register.

### When It Fires

The pass is most effective for:
- Kernel parameters with known constant offsets
- Shared memory size constants
- Grid/block dimension constants when known at compile time
- Constant expressions that survive PTX-to-Ori lowering as `LDC` loads

The pass is gated by knob 487 (general optimization enablement).

## Phase 27: AnalyzeUniformsForSpeculation

| | |
|---|---|
| **Phase index** | 27 |
| **Pipeline position** | Stage 2 (Early Optimization), after `OriRemoveRedundantBarriers` (26), before `SinkRemat` (28) |
| **Category** | Analysis |

### Purpose

Identifies uniform values that are safe for speculative execution. This analysis feeds subsequent passes that may hoist or speculatively execute instructions -- most immediately `SinkRemat` (phase 28) and `SpeculativeHoistComInsts` (phase 56).

### Speculative Uniformity

A value is "speculatively uniform" if it would be uniform under all possible execution paths, not just the currently taken path. This is a stronger property than simple uniformity: a value that is uniform within one branch arm might not be speculatively safe to hoist above the branch if the other arm would produce a different value or a side effect.

The analysis must be conservative:
- **Memory loads** cannot be speculated unless the address is provably valid on all paths (no faults).
- **Atomic operations** are never speculative candidates.
- **Values defined under divergent control flow** require careful handling -- the analysis must determine whether the definition dominates all paths that could reach the speculation point.

### Pipeline Position Rationale

Phase 27 runs after:
- Loop unrolling (22), which may duplicate uniform definitions
- SSA phi insertion (23), which provides single-definition reaching information
- Software pipelining (24), which may interleave loop iterations
- Barrier removal (26), which may relax synchronization constraints

And before:
- `SinkRemat` (28), which uses the analysis to decide what can be sunk/recomputed
- `GeneralOptimize` (29), which benefits from knowing which values are uniform

## Phase 74: ConvertToUniformReg

| | |
|---|---|
| **Phase index** | 74 |
| **Pipeline position** | Stage 4 (Late Optimization), after `ConvertAllMovPhiToMov` (73), before `LateArchOptimizeFirst` (75) |
| **Category** | Optimization |
| **String reference** | `"ConvertToUniformReg"` at `0x22BCA12` |
| **Related function** | `sub_911030` (10,741 bytes, 56 callees) |

### Purpose

The main UR promotion pass. Converts qualifying R-register values to UR registers, replacing per-thread general-purpose register storage with warp-uniform storage. This is the highest-impact uniform register optimization in the pipeline.

### Pipeline Position Rationale

Phase 74 runs immediately after SSA destruction (`ConvertAllMovPhiToMov`, phase 73). This is deliberate:
- **After SSA destruction**: phi nodes have been converted to plain MOVs, giving the pass a clear view of all definitions and uses without phi-node complications.
- **After varying propagation** (phases 53 and 70): the divergence annotations are complete -- the pass knows which values are proven uniform.
- **After predication** (phase 63): if-conversion has already eliminated short branches, which may have exposed new uniform values.
- **Before register allocation**: UR conversion reduces R-register demand before the fat-point allocator runs (phase 101), directly improving occupancy.
- **Before scheduling**: the scheduler (phases 97+) can exploit UR-specific latency characteristics.

### Conversion Criteria

A value qualifies for R-to-UR conversion when all of the following hold:

1. **Uniformity**: the value is proven warp-uniform -- all threads compute the same result. This is established by the varying propagation passes and the phase 27 analysis.

2. **UR-expressible operation**: the defining instruction has a uniform-datapath equivalent. Not all SASS instructions have UR variants. Operations like `IMAD`, `IADD3`, `LOP3`, `ISETP`, `MOV`, `SEL`, `PRMT`, `SGXT`, `POPC`, and `BREV` have UR counterparts. Complex operations like `FFMA`, `LDG`, `STG`, texture instructions, and atomics do not (until sm_100 added some uniform FP).

3. **UR pressure budget**: the conversion must not exceed the 63-register UR hardware limit. The pass tracks live UR count and aborts conversion for a value if it would push the UR pressure beyond the limit.

4. **All uses accept UR sources**: every consumer of the value must be able to read from the UR file. Some instructions have encoding restrictions that prohibit UR operands in certain source positions.

5. **No cross-warp dependencies**: the value must not participate in cross-warp communication patterns (e.g., shuffle instructions that explicitly exchange values between lanes).

### Algorithm

The pass operates in two main phases:

**Phase A -- Candidate identification.** Walks the instruction list and marks each definition as a UR candidate based on the criteria above. For each candidate, it checks:
- The `vreg+64` register file type is R (type 1 or 2, not already UR type 3)
- The varying propagation flag on the register indicates uniformity (bit 2 of `vreg+49` clear)
- The defining opcode has a UR-equivalent instruction form
- All consumers of this register accept UR sources

**Phase B -- Conversion.** For each approved candidate:
1. Changes the register's file type from R (type 1) to UR (type 3) at `vreg+64`
2. Updates the register's allocator class from class 1 (R) to class 4 (UR) at `vreg+12`
3. Rewrites the defining instruction to use the UR-variant opcode (e.g., `IMAD` becomes `UIMAD`)
4. Inserts `R2UR` bridge instructions where a converted UR value flows into an instruction that requires an R-file source
5. Inserts `UR2R` bridge instructions where an R-file value needs to flow into a converted UR instruction
6. Updates the UR count at Code Object `+99`

### UR Pressure Management

The UR file has only 63 usable registers (UR0--UR62), compared to 254 for the R file. The pass must be conservative about how many values it converts:

- **Greedy allocation with pressure cap**: candidates are evaluated in program order (RPO). Each conversion increments a pressure counter. If the counter reaches the hardware limit, remaining candidates are skipped.
- **Priority by benefit**: conversions that save the most R-register pressure (long live ranges with many uses) are preferred.
- **Retry mechanism**: the scheduling infrastructure at `sub_A0D800` supports a "retry without uniform regs" fallback (controlled by flag `v63`). If scheduling with UR-converted code fails to meet latency targets, the scheduler can request a re-run without UR conversion.

### Interaction with Register Allocation

The UR conversion reduces R-register demand but introduces UR-register demand. The fat-point allocator (phase 101) handles R and UR as separate register classes (class 1 and class 4 respectively), with independent allocation passes. The trade-off:

| | R file | UR file |
|---|---|---|
| Capacity | 254 usable | 62 usable |
| Pressure impact | Reduced by conversion | Increased by conversion |
| Occupancy impact | Positive (fewer R regs = higher occupancy) | Neutral (UR count does not affect warp occupancy on most SMs) |
| Spill cost | Spilled to local memory | Spilled to R file, then to local memory |

The allocator state at `alloc+440` tracks the uniform register promotion flag (controlled by knob 628 and context flag `+1414`). When this flag is set, the pre-allocation pass (`sub_94A020`) enables UR-aware allocation.

## Phase 86: InsertPseudoUseDefForConvUR

| | |
|---|---|
| **Phase index** | 86 |
| **Pipeline position** | Stage 5 (Legalization), after `OriPropagateGmma` (85), before `FixupGmmaSequence` (87) |
| **Category** | Lowering |

### Purpose

Inserts pseudo use/def instructions to maintain correct liveness information for UR-converted registers. After `ConvertToUniformReg` (phase 74) converts values from R to UR, subsequent optimization and legalization passes may invalidate the liveness information. This pass inserts lightweight pseudo-instructions that prevent later passes from incorrectly eliminating UR definitions or extending UR live ranges beyond their intended scope.

### Why Pseudo Instructions Are Needed

The UR conversion in phase 74 changes register file assignments, but does not update all downstream data structures. Between phase 74 and register allocation (phase 101), several passes run:

```
74  ConvertToUniformReg         <-- UR conversion happens here
75  LateArchOptimizeFirst
76  UpdateAfterOptimize
77  AdvancedPhaseLateConvUnSup
78  LateExpansionUnsupportedOps
79  OriHoistInvariantsLate2
80  ExpandJmxComputation
81  LateArchOptimizeSecond
82  AdvancedPhaseBackPropVReg
83  OriBackCopyPropagate
84  OriPerformLiveDeadFourth    <-- DCE could kill "unused" UR defs
85  OriPropagateGmma
86  InsertPseudoUseDefForConvUR <-- pseudo use/def insertion
87  FixupGmmaSequence
    ...
101 AdvancedPhaseAllocReg       <-- register allocation
```

The critical problem: `OriPerformLiveDeadFourth` (phase 84) runs liveness analysis and dead code elimination. If a UR-converted value appears dead (no R-file use remaining because the uses were also converted), DCE would remove it. The pseudo use/def instructions inserted by phase 86 create artificial uses that keep UR definitions alive through DCE.

### Pseudo Instruction Properties

The pseudo use/def instructions:
- Have no hardware encoding -- they are removed before SASS emission
- Carry register operand references that maintain the def-use chain
- Are transparent to instruction scheduling (zero latency, no functional unit)
- Are removed during post-RA cleanup or Mercury encoding

### Convergent Boundary Interaction

The pass also interacts with the convergent boundary enforcement mechanism. The string `"Missing proper convergent boundary around func call annotated with allowConvAlloc"` (from `sub_19D13F0`) indicates that UR-converted values crossing function call boundaries require convergent allocation markers. The `allowConvAlloc` annotation on function calls triggers convergent boundary checking, and `"Multiple functions calls within the allowConvAlloc convergent boundary"` (`sub_19C6400`) warns when a convergent region contains more than one call.

The CONV.ALLOC pseudo-instruction (opcode 286 / `0x11E`) is inserted by `sub_19D7A70` to mark convergent allocation boundaries. This prevents the register allocator from assigning the same physical UR to values that are live across a convergent boundary where the UR might be redefined.

## Varying Propagation (Supporting Analysis)

The `OriPropagateVarying` passes (phases 53 and 70) propagate divergence information forward through the IR. They are not part of the four-pass uniform register group, but provide the critical input data.

**Phase 53 (`OriPropagateVaryingFirst`)** runs after late expansion (55) and before rematerialization. It marks each register as either "uniform" or "varying" (divergent) by propagating divergence from known-divergent sources (thread ID registers, divergent memory loads) through the def-use chain. The propagation is a forward dataflow analysis: if any source operand of an instruction is varying, the destination is varying.

**Phase 70 (`OriPropagateVaryingSecond`)** repeats the analysis after predication (phase 63) and rematerialization (phase 69) may have changed the divergence landscape.

The varying flag is stored in the virtual register descriptor (bit 2 of `vreg+49`). During `ConvertToUniformReg`, only registers marked as non-varying are candidates for UR promotion.

## Uniform Atomic Optimization (Phase 44)

`OptimizeUniformAtomic` (phase 44) is a mid-pipeline optimization that converts thread-uniform atomic operations into warp-level reductions. When all threads in a warp perform the same atomic operation on the same address with the same value, the hardware can coalesce them into a single atomic. This pass detects such patterns and rewrites them using `REDUX` (reduction) or `ATOM.UNIFORM` instruction forms.

## Code Object Uniform Register Tracking

The Code Object maintains several fields related to UR state:

| Offset | Field | Description |
|--------|-------|-------------|
| `+99` | `ur_count` | Number of uniform registers allocated for this function |
| `+832` | Main liveness bitvector | One bit per virtual register (R + UR combined) |
| `+856` | UR liveness bitvector | Separate bitvector for UR/UP registers only |
| `+1368` bit 1 | has-uniform flag | Set when the function uses any UR registers |
| `+1376` bit 4 | UR tracking enabled | Controls whether scheduling tracks UR pressure |
| `+1378` bit 3 | has-UR-regs flag | Secondary flag confirming UR register usage |

The scheduling dependency builder at `sub_A0D800` (39 KB) tracks UR pressure separately. When `+1376` bit 4 is set, the control word computation at `sub_A09850` doubles the register count for uniform operands (`v15 = type==3 ? 2 : 1`) and writes a 9-bit register count to the control word bits [0:8].

The scheduling statistics printer (`sub_A3A7E0`) reports texture binding mode as "UR-bound" when textures are accessed via uniform-register-based descriptors:
```
# [inst=142] [texInst=0] [tepid=0] [rregs=24]
```

### Disallowed Uniform Register Diagnostic

The function `sub_A465F0` (`CodeObject::buildCodeObjectHeader`, 2.6 KB binary) checks whether UR registers were used despite being disallowed. The diagnostic:

```
"Uniform registers were disallowed, but the compiler required (%d) uniform
 registers for correct code generation."
```

This fires on pre-sm_75 targets where the UR file does not exist, or when a CLI option explicitly disables UR usage. Knob 687 controls the uniform register mode.

## SM Architecture Availability

| SM range | UR support | UR ALU instructions | Uniform FP |
|---|---|---|---|
| sm_30 -- sm_72 | None | None | None |
| sm_75 -- sm_89 | UR0--UR62, UP0--UP6 | UIADD3, UIMAD, ULOP3, UISETP, UMOV, UPRMT, USGXT, UPOPC, UBREV | None |
| sm_90 -- sm_90a | UR0--UR62, UP0--UP6 | Full integer uniform ALU | None (LDCU requires `-forcetext -sso`) |
| sm_100+ | UR0--UR62, UP0--UP6 | Full integer + FP uniform ALU | UFADD, UFFMA, UFSEL, UFSETP, UVIADDR |

The `LDCU` (Load Constant Uniform) instruction is gated by architecture capability. The validation at `sub_B28400` (345 bytes) checks:

```
"SM does not support LDCU. On SM90 -knob EmitLDCU is only supported when
 options '-forcetext' and '-sso out.sass' are provided."
```

This check queries vtable+1336 for the LDCU capability.

## ConvertMemoryToRegisterOrUniform

The function `sub_910840` (`ConvertMemoryToRegisterOrUniform`, gated by knob 487) is a pre-allocation optimization that promotes stack-resident variables to registers, with the option of promoting to UR when the variable is proven uniform. It is not one of the four numbered phases but works closely with them.

| | |
|---|---|
| **Entry** | `sub_910840` (2,100 bytes) |
| **Core** | `sub_911030` (10,741 bytes, 56 callees) |
| **Liveness builder** | `sub_905B50` (5,407 bytes) |
| **Promotion transform** | `sub_90FBA0` (~4,000 bytes) |
| **Gate knob** | 487 |
| **String** | `"ConvertMemoryToRegisterOrUniform"` at `0x910897` |

The entry function checks knob 487 for enablement (via vtable+152 dispatch), builds def-use chains via `sub_905B50`, then calls `sub_90FBA0` for the actual promotion.

The `sub_911030` core function (10.7 KB) handles the "OrUniform" part -- it iterates through the variable list, checks variable properties (address space, type), and decides whether to promote to R or UR. The decision process involves:
1. Checking the register's `vreg+49` flags byte (bit 2 = uniform marker from `sub_907870`)
2. Evaluating whether the variable's address space permits UR promotion
3. Confirming that the defining and using instructions have UR-compatible forms
4. Verifying UR pressure headroom

The per-register-class property accessors at `sub_900C50`--`sub_9013F0` (6 nearly identical 391-byte functions, 2 callers each) provide the class-indexed lookups for the promotion decision.

## Key Functions

| Address | Size | Function | Description |
|---------|------|----------|-------------|
| `sub_910840` | 2.1 KB | `ConvertMemoryToRegisterOrUniform` | Promotes stack variables to R or UR registers (knob 487 gated) |
| `sub_911030` | 10.7 KB | Core UR promotion logic | Iterates variables, decides R vs UR promotion based on uniformity |
| `sub_905B50` | 5.4 KB | Liveness builder for promotion | Builds def-use chains for promotion analysis |
| `sub_90FBA0` | ~4 KB | Promotion transform | Applies the actual memory-to-register transformation |
| `sub_8FEAC0` | 2.1 KB | Per-BB pressure analyzer | Walks instruction list, decodes operand types, updates pressure via vtable+1824; called from `sub_910840` |
| `sub_A465F0` | 2.6 KB | `CodeObject::buildCodeObjectHeader` | Writes UR count into code object, checks disallowed-UR diagnostic |
| `sub_B28E90` | small | `isUReg` | Predicate: is operand a uniform register? |
| `sub_19D13F0` | 4.3 KB | Convergent boundary checker | Validates `allowConvAlloc` boundaries around function calls |
| `sub_19C6400` | 330 B | Per-instruction convergent classifier | Callback: warns on opcode 159 within convergent boundary |
| `sub_19D7A70` | 3.3 KB | CONV.ALLOC marker insertion | Inserts opcode `0x11E` pseudo-instructions at convergent boundaries |
| `sub_A0D800` | 39 KB | Scheduling dependency builder | Builds per-block dependency graph; tracks UR pressure via `+856` bitvector |
| `sub_A09850` | ~2 KB | Control word computation | Doubles count for uniform operands: `type==3 ? 2 : 1` |
| `sub_B28400` | 345 B | LDCU validator | Checks SM support for Load Constant Uniform |
| `sub_7BC360` | ~1 KB | UR register encoder | Encodes UR operands in SASS instruction words (126 callers) |
| `sub_7BD7D0` | ~1 KB | UR register decoder | Decodes UR operands from SASS instruction words (type=4) |
| `sub_94A020` | ~3.5 KB | Pre-allocation setup | Sets `alloc+440` UR promotion flag from knob 628 + context flag `+1414` |
| `sub_900C50` | 391 B | Register class property accessor | Per-class property lookup (one of 6 identical functions for GP, predicate, UR, etc.) |

## Related Pages

- [Register Model](../ir/registers.md) -- UR file, register descriptor layout, allocator classes
- [Ori IR Overview](../ir/overview.md) -- instruction format, partial SSA window
- [Pass Inventory](index.md) -- complete 159-phase table
- [Liveness Analysis](liveness.md) -- bitvector infrastructure used by UR liveness tracking
- [Rematerialization](rematerialization.md) -- phases 28, 54, 69 (interact with speculation analysis)
- [Predication](predication.md) -- phase 63, changes divergence landscape before UR conversion
- [Register Allocator](../regalloc/overview.md) -- 7-class allocator handling R and UR independently
- [GMMA Pipeline](gmma-pipeline.md) -- phases 85, 87 (adjacent to InsertPseudoUseDefForConvUR)
- [GPU ABI](../regalloc/abi.md) -- convergent allocation, allowConvAlloc enforcement
- [Scheduler Architecture](../scheduling/overview.md) -- UR pressure tracking in scheduling
