# Late Expansion & Legalization

The ptxas pipeline contains six legalization passes spread across the 159-phase sequence. Their collective job is to replace Ori IR operations that the target SM cannot execute natively with equivalent sequences of legal instructions. "Unsupported ops" means exactly this: operations that exist in the PTX ISA or internal Ori representation but have no single-instruction mapping on the compilation target. The replacement may be an inline expansion (a sequence of simpler instructions), a call to a libdevice helper function, or an SM-specific intrinsic sequence.

The six passes run at deliberately different pipeline positions because each intervening group of optimization passes can expose new unsupported operations or create new legalization opportunities.

| | |
|---|---|
| **Passes covered** | 6 (phases 5, 45, 55, 78, 93, 137) |
| **Category** | Lowering |
| **Backend dispatch** | Architecture-specific via two backend objects at `context+0x630` and `context+0x640` |
| **Libdevice functions** | 608 helper functions registered at `sub_5D1660` (9,728-byte table from `unk_1D4D940`) |
| **Legalization flag** | `SetAfterLegalization` (phase 95) marks the point past which no unsupported ops should remain |
| **Update pass** | `UpdateAfterConvertUnsupportedOps` (phase 132, factory 8) rebuilds IR metadata after late expansion |
| **Knob gates** | Knob 499 (ConvertUnsupportedOps, LateExpansionUnsupportedOps), knob 487 (LateExpansion, SetAfterLegalization, LateExpansionUnsupportedOps), knob 214 / 464 (LateExpansionUnsupportedOps inner loop) |

## Why Six Passes

A monolithic legalize-everything pass early in the pipeline would cripple optimization. Many optimizations (CSE, LICM, strength reduction, predication) work on high-level operation semantics. If `div.rn.f64` were expanded into a 30-instruction Newton-Raphson sequence at phase 5, loop-invariant code motion at phase 35 would see 30 independent instructions instead of one hoistable division. Conversely, some unsupported operations only appear after optimization passes transform the IR: predication (phase 63) can create new predicated ops that need legalization, GMMA fixup (phase 87) can introduce new WGMMA-related sequences, and conditional flow merging (phases 133/136) can expose operations that were previously dead.

The six passes form a progressive legalization strategy:

| Phase | Name | Pipeline Position | Purpose |
|---|---|---|---|
| 5 | `ConvertUnsupportedOps` | Before optimization (stage 1) | Early legalization of obviously unsupported ops; preserves optimization opportunities for everything else |
| 45 | `MidExpansion` | After early/mid optimization (stage 3) | Target-dependent expansion after loop unrolling, strength reduction, and GVN have run |
| 55 | `LateExpansion` | After high-level optimizations (stage 4) | Expansion of ops that optimization passes should see in unexpanded form |
| 78 | `LateExpansionUnsupportedOps` | After all optimization (stage 5) | Catches remaining unsupported ops after predication, rematerialization, and uniform conversion |
| 93 | `LateExpansionUnsupportedOps2` | After GMMA/attr passes (stage 5) | Second catch -- handles ops exposed by GMMA propagation, GMMA fixup, and register attribute setting |
| 137 | `LateExpansionUnsupportedOpsMid` | After late merge (stage 10) | Final catch between the two conditional flow merge passes |

## Architecture Backend Dispatch

None of the six passes contain legalization logic directly. Each is a thin dispatcher that forwards to a virtual method on one of two architecture backend objects stored in the compilation context. The backend objects are constructed per-SM-target and provide the actual SM-specific legalization implementations.

**Two backend objects:**

| Context Offset | Used By | Role |
|---|---|---|
| `context+0x640` | ConvertUnsupportedOps, LateExpansion | Outer backend -- wraps an inner object at `+0x10`, provides two-level dispatch |
| `context+0x630` | MidExpansion, LateExpansionUnsupportedOps, LateExpansionUnsupportedOps2, LateExpansionUnsupportedOpsMid, SetAfterLegalization | SM backend -- single-level dispatch through vtable |

The two-level dispatch through `context+0x640` allows the outer backend to override the entire legalization strategy (by replacing vtable slot 0), while the inner object provides the SM-specific implementation when the outer backend delegates. This separation exists because ConvertUnsupportedOps and LateExpansion may need to coordinate with higher-level compilation modes (e.g., library compilation, OptiX IR) that wrap the SM backend.

### Backend Vtable Slots

The SM backend at `context+0x630` dispatches legalization through these vtable offsets:

| Vtable Offset | Decimal | Called By |
|---|---|---|
| `+0xB0` | 176 | MidExpansion |
| `+0xD8` | 216 | LateExpansionUnsupportedOps2 |
| `+0x108` | 264 | SetAfterLegalization |
| `+0x178` | 376 | LateExpansionUnsupportedOps |
| `+0x180` | 384 | LateExpansionUnsupportedOpsMid |

The outer backend at `context+0x640` dispatches:

| Vtable Offset | Decimal | Called By |
|---|---|---|
| `+0x00` | 0 | ConvertUnsupportedOps (type check -- compared against `sub_661280`) |
| `+0x78` | 120 | ConvertUnsupportedOps (delegated to inner object) |
| `+0x58` | 88 | LateExpansion (type check -- compared against `sub_6612E0`) |
| inner `+0xE0` | 224 | LateExpansion (delegated to inner object) |

## Pass Details

### Phase 5 -- ConvertUnsupportedOps

```
Factory index:  5
Vtable:         off_22BD690
execute():      sub_C60A20  (thunk -> context+0x640 dispatch)
isNoOp():       sub_C5F610  (returns 0 -- always runs)
Flag side-effect: sets context+1378 bit 0 (isConvertUnsupportedDone)
Knob gate:      499 (checked via sub_7DDB50)
Pipeline:       Bracketed by AdvancedPhaseBeforeConvUnSup (4) and AdvancedPhaseAfterConvUnSup (7)
```

This is the earliest legalization pass, running at phase 5 before any optimization. It converts operations that are clearly illegal on the target SM into equivalent sequences. The pass always runs (isNoOp = false) and is unconditional -- every compilation executes it.

**Dispatch mechanism.** The execute function (`sub_C60A20`) reads the backend at `context+0x640`, checks whether vtable slot 0 is the default implementation (`sub_661280`), and either calls the overridden method directly or unwraps to the inner object at `backend+0x10` and calls vtable offset `+0x78` (120). This two-level indirection allows library-mode and OptiX-mode compilation to inject custom legalization logic.

**Flag effect.** After execution, the pass sets bit 0 of `context+1378`, signaling to downstream passes that early legalization has completed. Passes like `OriCreateMacroInsts` (phase 8) check this flag to know whether certain patterns have already been lowered.

**What gets legalized early:** Operations that cannot survive optimization in their original form. Examples include operations that reference address spaces not supported on the target, certain modifier combinations that have no encoding, and PTX instructions that are syntactically valid but architecturally illegal (e.g., `atom.add.f64` on targets without native FP64 atomics).

### Phase 45 -- MidExpansion

```
Factory index:  51
Vtable:         off_22BDDC0
execute():      sub_C5EFB0  (thunk -> context+0x630 vtable+0xB0)
isNoOp():       sub_C5EFD0  (returns 0 -- always runs)
Field side-effect: sets context+1552 = 3
Pipeline:       After ExpandMbarrier (42), ForwardProgress (43), OptimizeUniformAtomic (44)
                Before GeneralOptimizeMid2 (46)
```

MidExpansion runs after the CTA/mbarrier/barrier expansion passes and before the second mid-level GeneralOptimize bundle. It handles target-dependent expansions that must occur after barrier-related lowering but before the mid-level optimization cleanup.

**Dispatch.** Dispatches directly through the SM backend vtable at offset `+0xB0` (176). No two-level indirection -- the SM backend provides the implementation directly.

**Side effect.** Sets `context+1552` to 3. This field is the pipeline progress counter (not exclusively a legalization counter -- see Context Fields below) and is read by subsequent passes to determine which pipeline stages have completed. The value 3 indicates "mid-expansion complete."

### Phase 55 -- LateExpansion

```
Factory index:  63
Vtable:         off_22BDFA0
execute():      sub_C60AA0  (thunk -> context+0x640 dispatch)
isNoOp():       sub_C5EE20  (returns 0 -- always runs)
Field side-effect: sets context+1552 = 7 (via inner dispatch)
Pipeline:       After OriDoRematEarly (54), before SpeculativeHoistComInsts (56)
                Followed by GeneralOptimizeLate (58)
```

LateExpansion is the primary post-optimization legalization pass. It runs after all high-level optimizations (loop unrolling, strength reduction, GVN-CSE, reassociation, predication setup) have completed, expanding operations that were deliberately kept in high-level form for those passes.

**Dispatch.** Uses the outer backend at `context+0x640`. Checks vtable slot `+0x58` (88) against the default (`sub_6612E0`). If overridden, calls the override. Otherwise, calls the inner object's vtable at `+0xE0` (224) and then sets `context+1552 = 7`, advancing the pipeline progress counter.

**What gets expanded here:** This is the pass where most math library calls are introduced. Operations like `div.rn.f64`, `sqrt.rn.f32`, `rcp.rd.f64` that were kept as single Ori instructions through optimization are now replaced with Newton-Raphson sequences or calls to the 608-function libdevice library. The SM20 library functions (division, square root, reciprocal, bit-field extract/insert) and SM70 functions (WMMA matrix operations, barrier reductions) are the primary candidates.

**Optimization interaction.** GeneralOptimizeLate (phase 58) runs immediately after, cleaning up the expanded sequences with copy propagation, constant folding, and dead code elimination. This is why expansion happens here rather than later -- the expanded code benefits from one more optimization round.

### Phase 78 -- LateExpansionUnsupportedOps

```
Factory index:  90
Vtable:         off_22BE3D8
execute():      sub_C5EA50  (thunk -> context+0x630 vtable+0x178)
isNoOp():       sub_C5EA70  (returns 0 -- always runs)
Knob gate:      499 (via sub_7DDB50), plus flag check: context+1414 bit 2
Pipeline:       After AdvancedPhaseLateConvUnSup (77), before OriHoistInvariantsLate2 (79)
```

The first of three "late unsupported ops" catches. It runs after all optimizations have completed (phases 13-76) and catches operations that optimization passes themselves introduced or exposed.

**Gating.** This pass has the most complex gating of the six. In addition to the standard knob 499 check (via `sub_7DDB50`), it also checks bit 2 of `context+1414`. If the bit is clear, the pass is skipped even though isNoOp returns false. This allows the backend to dynamically disable the pass when no unsupported ops were detected during earlier compilation phases.

**Implementation.** When active, calls `sub_7917F0` which:
1. Checks `context+1382` bit 2 (another prerequisite flag)
2. Checks knob 214 (via the capability dispatch at `context+1664`)
3. If the function table at `context+0 + 1056` is not yet initialized, calls the expansion setup functions (`sub_785E20`, `sub_781F80`, `sub_7E6090`, `sub_7E6AD0`)
4. Iterates over basic blocks, applying per-instruction legalization with convergence check (knob 464 gates the inner loop)

This iterative structure -- expand, check if more work needed, repeat -- handles cascading expansions where expanding one operation exposes another unsupported operation.

### Phase 93 -- LateExpansionUnsupportedOps2

```
Factory index:  109
Vtable:         off_22BE6D0
execute():      sub_C5E790  (thunk -> context+0x630 vtable+0xD8)
isNoOp():       sub_C5E7B0  (returns 0 -- always runs)
Pipeline:       After AdvancedPhaseAfterSetRegAttr (92), before FinalInspectionPass (94)
```

The second late catch, positioned after the GMMA/WGMMA passes (85-87), register attribute setting (90), and texture dependency analysis (91). These intervening passes can introduce new operations that need legalization:

- **GMMA propagation** (phase 85) may introduce WGMMA accumulator movement operations
- **GMMA sequence fixup** (phase 87) may insert hardware ordering instructions
- **Register attribute setting** (phase 90) may expose operations that become illegal once register classes are assigned

**Dispatch.** Uses the SM backend vtable at offset `+0xD8` (216). The dispatch is architecture-dependent: the execute function reads vtable slot 12 (`backend[12]`), compares against a default implementation (`sub_661310`), and either calls the override or falls through to a two-step sequence that calls methods at offsets 280 and 3088 on an inner object.

### Phase 137 -- LateExpansionUnsupportedOpsMid

```
Factory index:  93
Vtable:         off_22BE450
execute():      sub_C607E0  (thunk -> context+0x630 vtable+0x180)
isNoOp():       sub_C5EA00  (returns 0 -- always runs)
Default check:  compares vtable+0x180 against sub_7D6D50 -- if default, entire pass is no-op
Pipeline:       After LateMergeEquivalentConditionalFlow (136), before OriSplitHighPressureLiveRanges (138)
```

The final legalization catch, positioned between the two conditional flow merge passes (133, 136) and the last-resort live range splitter (138). The merge passes can combine basic blocks in ways that create new instruction sequences containing unsupported operations.

**Conditional execution.** Unlike the other five passes, this one has a soft no-op mechanism: the execute function reads vtable slot `+0x180` (384) and compares the function pointer against the default implementation (`sub_7D6D50`). If the backend has not overridden this slot, the pass returns immediately without doing any work. This means the pass is truly active only on SM targets that define a `LateExpansionUnsupportedOpsMid` handler -- typically newer architectures (Hopper/Blackwell) that have more complex merge and expansion interactions.

## Supporting Passes

### Phase 95 -- SetAfterLegalization

```
Factory index:  111
Vtable:         off_22BE720
execute():      sub_C5F8A0
isNoOp():       sub_C5E9C0  (returns 0 -- always runs)
Pipeline:       After FinalInspectionPass (94), before ReportBeforeScheduling (96)
```

Not a legalization pass per se. It marks the compilation context as post-legalization by calling the SM backend's vtable at offset `+0x108` (264). This sets the `legalization_complete` flag that downstream passes (scheduling, register allocation, encoding) check to assert that no unsupported operations remain. The pass is gated by optimization level: `sub_7DDB50` returns the current optimization level, and the dispatch only fires at `-O2` and above.

### Phase 132 -- UpdateAfterConvertUnsupportedOps

```
Factory index:  8
Vtable:         off_22BD708
execute():      sub_C5F570  (rep ret -- NOP)
isNoOp():       sub_C5F590  (returns 1 -- skipped by default)
Pipeline:       First pass in Stage 10
```

A placeholder update pass that rebuilds IR metadata after late unsupported-op conversion. Its `execute()` is a NOP (`rep ret`) and `isNoOp()` returns 1 (true), so it is skipped by default. Architecture backends can override the vtable to activate it when late expansion produces structural changes requiring metadata rebuild.

## Libdevice Function Library

The legalization passes replace unsupported operations with calls to a library of 608 predefined helper functions. These are not external libraries -- they are PTX function bodies embedded in the ptxas binary itself, compiled and linked into the output at need.

The function table is initialized by `sub_5D1660`, which copies a 9,728-byte pre-built table from `unk_1D4D940` and registers 608 function names in a hash map for lookup.

### Library Function Categories

| SM Prefix | Count | Operations |
|---|---|---|
| `__cuda_sm20_` | 70 | Division (f32/f64, all rounding modes), reciprocal (f32/f64, all rounding modes), square root (f32/f64), double-precision reciprocal sqrt, bit-field extract/insert 64-bit, integer division/remainder (s16/s64/u16/u64) |
| `__cuda_sm3x_` | 4 | FP32 division with FTZ variants (Kepler-specific paths) |
| `__cuda_sm62_` | 2 | DP2A, DP4A dot-product accumulate (pre-Volta emulation) |
| `__cuda_sm70_` | 397 | Barrier operations (arrive/red/wait with 0-15 barrier IDs and count variants), WMMA matrix operations (204 variants for different shapes/types), warp shuffle sync, warp vote sync, match sync |
| `__cuda_sm80_` | 3 | Cache policy creation (fractional, range encode) |
| `__cuda_sm1xx_` | 18 | Bulk copy (unicast/multicast), async bulk tensor copy (1D-5D tile/im2col, unicast/multicast) |
| `__cuda_sm10x_` | 16 | TCGen05 guardrail traps (bounds check, alignment, allocation), tcgen05 MMA operations, mask creation |
| `__cuda_scalar_video_emulation_` | 7 | Video instruction emulation (operand extract, sign extend, saturate, merge) |
| `__cuda_reduxsync_` | 18 | Redux-sync reductions (and/or/xor for b32, add/max/min for s32/u32/f32 with NaN/abs variants) |
| `__cuda_sanitizer_` | 6 | Memory sanitizer checks (malloc/free/generic/global/local/shared/metadata) |
| Other | ~67 | Miscellaneous: dummy entries, user-function stubs, device synchronize |

### SM-Dependent Legalization Examples

The core design principle: what is "unsupported" depends entirely on the target SM. An operation legal on one architecture may require library expansion on another.

**Integer division/remainder.** PTX `div.s64` and `rem.u64` have no single SASS instruction on any SM. They are always expanded to multi-instruction sequences via `__cuda_sm20_div_s64`, `__cuda_sm20_rem_u64`, etc. These are "sm20" functions because the expansion has been the same since Fermi.

**FP32 division with rounding.** `div.rn.f32` on Turing (sm_75) uses a hardware-assisted Newton-Raphson (`MUFU.RCP` + refinement). On Kepler (sm_3x, no longer shipped but the code path remains), different refinement sequences are needed, using `__cuda_sm3x_div_rn_ftz_f32` and its slowpath variant.

**Barrier operations.** On Volta+ (sm_70), `barrier.arrive` with a specific barrier ID and thread count is a single SASS instruction (`BAR.ARV`). On pre-Volta targets, these must be emulated with the 397 `__cuda_sm70_barrier_*` library functions that implement the semantic equivalent using older synchronization primitives.

**WMMA/Tensor Core.** Warp-level matrix multiply-accumulate (`wmma.*`) on sm_70 has dedicated hardware instructions (HMMA). The 204 `__cuda_sm70_wmma_*` variants cover the combinatorial explosion of shapes (m16n16k16, m8n32k16, m32n8k16), types (f16, bf16, tf32, s8, u8, s4, u4, b1), layouts (row/col), and accumulator types.

**DP2A/DP4A.** The integer dot-product-accumulate instructions have native hardware support starting at sm_61. On sm_62 (Xavier), they use `__cuda_sm62_dp2a` and `__cuda_sm62_dp4a` emulation routines.

**Bulk tensor copy (Blackwell).** The `cp.async.bulk.tensor` family on sm_100+ (Blackwell) supports 1D through 5D tile and im2col access patterns, with unicast and multicast variants. These 18 `__cuda_sm1xx_cp_async_bulk_tensor_*` functions provide the expansion for targets where hardware support is partial or absent.

**TCGen05 guardrails (Blackwell).** The 5th-generation tensor core operations (sm_100+) include runtime guardrail traps -- bounds checking, alignment validation, allocation granularity checks -- implemented as `__cuda_sm10x_tcgen05_guardrail_trap_*` functions inserted during legalization.

## Context Fields

The legalization passes interact with several fields on the compilation context:

| Offset | Type | Description |
|---|---|---|
| `+0x630` | `void*` | SM backend object (main legalization dispatch target) |
| `+0x640` | `void*` | Outer backend object (wraps SM backend, used by ConvertUnsupportedOps and LateExpansion) |
| `+1378` | `byte` | Bit 0: ConvertUnsupportedOps has run |
| `+1382` | `byte` | Bit 2: prerequisite flag for LateExpansionUnsupportedOps |
| `+1414` | `byte` | Bit 2: enable flag for LateExpansionUnsupportedOps |
| `+1552` | `int32` | Pipeline progress counter -- written by multiple passes across legalization, optimization, and post-RA stages (see value table below) |
| `+1664` | `void*` | Capability dispatch object (knob/option queries) |

The pipeline progress counter at `context+1552` provides a monotonically increasing value that downstream passes can check to determine which pipeline stages have completed. Despite being documented previously as a "legalization stage counter," it is written by passes outside the legalization family (rematerialization, backward copy propagation, architecture-specific peephole, post-RA finalization):

| Value | Writer | Phase | Function |
|---|---|---|---|
| 0 | Context constructor | -- | `sub_7F7DC0` |
| 3 | MidExpansion | 45 | `sub_C5EF80` |
| 4 | OriDoRematEarly | 54 | `sub_C5EF30` |
| 7 | LateExpansion | 55 | `sub_6612E0` |
| 8 | Peephole/ISel refinement (arch-specific) | varies | `sub_849C60` |
| 9 | OriBackCopyPropagate | 83 | `sub_C5EB80` |
| 10 | PostRAFinalizer (arch-specific) | varies | `sub_88E9D0` |
| 12 | SetAfterLegalization | 95 | `sub_C5E980` |

Downstream passes compare against these thresholds: `sub_A11060` checks `> 4` to enable cross-block rematerialization; `sub_752CF0` checks `<= 3`; `sub_766520` checks `<= 11`; `sub_781F80` checks `<= 12`; `sub_78B8D0` checks `> 18`.

## Pipeline Position Summary

```
Phase 0-4:   Initial setup, FP16 promotion, CFG analysis
Phase 5:     ConvertUnsupportedOps          <-- LEGALIZATION #1
Phase 6-44:  Optimization passes (branch, loop, strength reduction, GVN, barrier expansion)
Phase 45:    MidExpansion                    <-- LEGALIZATION #2
Phase 46-54: Mid/late optimization (GVN-CSE, reassociation, predication setup, remat)
Phase 55:    LateExpansion                   <-- LEGALIZATION #3
Phase 56-77: Late optimization (predication, commoning, LICM, remat, sync, phi destruction, uniform)
Phase 78:    LateExpansionUnsupportedOps     <-- LEGALIZATION #4
Phase 79-92: Post-opt (LICM, arch opt, back copy prop, GMMA, reg attrs)
Phase 93:    LateExpansionUnsupportedOps2    <-- LEGALIZATION #5
Phase 94:    FinalInspectionPass
Phase 95:    SetAfterLegalization (marks legalization complete)
Phase 96-136: Scheduling, RA, Mercury, post-RA, late merge
Phase 137:   LateExpansionUnsupportedOpsMid  <-- LEGALIZATION #6
Phase 138:   OriSplitHighPressureLiveRanges
```

## Key Functions

| Address | Size | Role |
|---|---|---|
| `sub_C60A20` | ~40B | ConvertUnsupportedOps execute dispatcher |
| `sub_C5EFB0` | ~16B | MidExpansion execute dispatcher |
| `sub_C60AA0` | ~50B | LateExpansion execute dispatcher |
| `sub_C5EA50` | ~16B | LateExpansionUnsupportedOps execute dispatcher |
| `sub_C607E0` | ~30B | LateExpansionUnsupportedOpsMid execute dispatcher |
| `sub_C5E790` | ~16B | LateExpansionUnsupportedOps2 execute dispatcher |
| `sub_C5F8A0` | ~30B | SetAfterLegalization execute |
| `sub_7DDB50` | 232B | Optimization level gate (knob 499 check) |
| `sub_7917F0` | ~400B | LateExpansionUnsupportedOps core implementation |
| `sub_9059B0` | ~500B | LateExpansion core implementation (with expansion loop) |
| `sub_5D1660` | ~8KB | Libdevice function table initializer (608 entries) |
| `sub_785E20` | -- | Expansion setup (function table initialization) |
| `sub_781F80` | -- | Expansion setup (mode configuration) |
| `sub_7E6090` | -- | Instruction expansion driver |
| `sub_7E6AD0` | -- | Instruction expansion driver (secondary) |
| `sub_753600` | -- | Per-instruction legalization check |
| `sub_753B50` | -- | Retry/convergence loop for iterative expansion |
| `sub_13AF3D0` | 26,795B | Operand legalization dispatcher -- 164-case switch on opcode, called from `sub_A29220` |
| `sub_13A6280` | 1,289B | General operand materializer -- ensures operand is in legal register (called 83x) |
| `sub_13A6AE0` | ~250B | Special-class operand materializer -- handles condition code and predicate classes |
| `sub_13A7410` | ~50B | Try-inline-then-materialize wrapper -- checks `sub_822750` before falling back |
| `sub_13A6F90` | ~40B | Arch-immediate materializer -- like `sub_13A7410` without pre-check |
| `sub_13A45E0` | -- | Predicate operand materializer |
| `sub_13A75D0` | -- | Uniform register conversion (class 6 to class 3) |
| `sub_A29220` | -- | Pass driver that calls `sub_13AF3D0` per instruction |
| `sub_13ADB90` | 3,353B | Extended operand legalization variant (arch-specific override, vtable-dispatched) |

## Operand Legalization Dispatcher

The SASS encoding backend cannot encode arbitrary operand forms. Before an instruction reaches the per-instruction encoder, every operand must be in a form the hardware encoding supports: a register in the correct class, an immediate that fits the bit-field width, or an absent-operand sentinel. The operand legalization dispatcher (`sub_13AF3D0`, 26,795 bytes) enforces these constraints. It is called once per instruction from the pass driver `sub_A29220` and runs after ISel but before the SASS encoders.

### Dispatcher Structure

The function reads the instruction opcode from field `+72`, masks off the predication flags (bits 12-13, mask `& 0xCFFF`), and enters a switch with **164 case labels** covering Ori IR opcodes 0 through 352. Each case implements the legalization recipe for one opcode or a group of opcodes with identical operand layouts.

Before the switch, a pre-pass handles predicated instructions. If bit 12 of the opcode is set (indicating a predicate guard is present), the function first checks backend vtable slot `+3232` for a custom handler. If none exists or it declines, `sub_13A6AE0` is called on the predicate guard operand (at position `operand_count - 2`) to ensure it is in a legal register.

The switch routes to five categories of legalization logic:

**Direct operand materialization.** The majority of cases call `sub_13A6280` on each operand that might need conversion. Example for a 3-source FMA (case 6):

```
sub_13A6280(context, instruction, 3, insert_point, ...)  // src0
sub_13A7410(backend, instruction, 4, 1, insert_point, ...) // src1 (try inline first)
sub_13A6280(context, instruction, 5, insert_point, ...)  // src2
// then check optional predicate operands 6,7 via sentinel test
```

**Variable-length operand scanning.** Case 16 (store) scans up to 15 operand slots, testing each against the `0x70000000` sentinel to find where active operands end before legalizing each one.

**Architecture-specific delegation.** Cases 70, 243, 245-247, 254-255, 257-259, 262 delegate entirely to `vtable+2816`. Cases 280-281 delegate to `vtable+2328` with adjusted operand counts. These are SM-specific instructions (tensor core, WGMMA, bulk copy) where operand constraints vary by architecture.

**Opcode rewriting.** Case 137 (MOV) rewrites the opcode field itself: to `0x82` (130) for conditional MOV, or to `0x109` (265) for MOV-from-special-register when the source is in register class 4.

**Passthrough.** Cases 22, 24, 34, 38, 44, 45, 59, 73, 74, 77, 83, 106, 135, 161, 180, 182, 192, 194, 198, 209, 213-215, 221, 297, 352 and the `default` case require no operand legalization and exit immediately.

### The 0x70000000 Null-Operand Sentinel

Each operand occupies an 8-byte slot in the instruction. The lower 4 bytes encode the operand value and type:

| Bits | Field | Values |
|---|---|---|
| `[30:28]` | Type | 1=register, 2=signed immediate, 3=unsigned immediate, 5=predicate, **7=null** |
| `[23:0]` | Payload | Register index or immediate value |
| `[31]` | Negate | 1=operand is negated |
| `+7` (byte) | Flags | Bit 0: uniform/constant bank reference |

The sentinel value `0x70000000` encodes type 7 ("null") with zero payload and no negation. It marks operand slots that are architecturally absent -- optional predicate guards not specified, trailing source operands of variable-width instructions, or unused operand positions in instructions with fewer sources than the maximum slot count.

The dispatcher tests for the sentinel with:

```c
if ( ((*((_DWORD *)instr + offset) ^ 0x70000000) & 0x70000000) != 0 )
    // operand is PRESENT -- legalize it
```

The XOR produces zero in bits `[30:28]` only when they are exactly `0b111` (type 7). The AND isolates those bits. If the result is zero, the operand is null and legalization is skipped. If non-zero, the operand is present and must be processed.

The function contains **59 references** to `0x70000000`. The heaviest user is case 16 (store), which chains 14 successive sentinel tests (at instruction offsets `+84` through `+196`) to determine the store's vector width -- effectively implementing `for each slot: if sentinel, stop; else legalize`.

### Operand Materialization Helpers

The dispatcher calls six helper functions depending on the operand class:

| Function | Calls | Role |
|---|---|---|
| `sub_13A6280` | 83 | **General materializer.** The core function. Checks if the operand can remain as-is (register in a legal class, or inline immediate that fits). If not, creates a MOV instruction via `sub_92E800` to load the value into a fresh register, inserts it before the current instruction, and replaces the operand slot with a register reference (`0x10000000 \| reg_index`). Short-circuits immediately for uniform registers (class 6). Uses `sub_7DBC80` to test inline-immediate feasibility and `sub_91D150`/`sub_91D160` for constant pool operations. |
| `sub_13A7410` | 15 | **Try-inline-then-materialize.** Checks `sub_822750` first ("can this immediate be encoded inline for this arch?"). If yes, keeps the immediate. If no, tries `sub_822990`/`sub_8229D0` for extended encoding paths. Falls back to `sub_13A6280` only if all inline attempts fail. |
| `sub_13A6AE0` | 15 | **Special-class materializer.** Handles operands in non-standard register classes. For class 5 (predicate): returns immediately. For class 2 (condition code): creates a MOV with opcode `0x108`. For immediates: calls `sub_91D150` for constant pool lookup and replaces the operand. Used on predicate guard operands and instructions with condition-code sources. |
| `sub_13A6F90` | 7 | **Arch-immediate materializer.** Like `sub_13A7410` but skips the `sub_822750` pre-check. Used for operands where inline encoding is known to be architecture-dependent (texture coordinates, barrier IDs). |
| `sub_13A45E0` | 5 | **Predicate materializer.** Handles materialization of optional predicate operand slots, called exclusively after a sentinel test confirms the operand is present. |
| `sub_13A75D0` | 1 | **Uniform register conversion.** Called once (case 6, FMA) to handle uniform register class 6 operands that need conversion to general-purpose class 3. |

### Materialization Flow (sub_13A6280 Detail)

The general materializer at `sub_13A6280` (1,289 bytes) implements this decision tree for a single operand:

1. **Uniform register early exit.** If the operand is a register (type 1) in class 6 (uniform), return immediately -- uniform registers are always legal in the encoding.

2. **Inline immediate check.** If the operand is an immediate (type 2/3), call `sub_7DBC80` to test whether the value fits in the instruction's immediate field. If it fits and passes the floating-point validity check (`vtable+1504`) and architecture encoding check (`vtable+3248`), keep the immediate as-is.

3. **Register reclassification.** If the operand is a register in class 3 (general-purpose), query the architecture via `vtable+1240` and `vtable+904` to determine if the register should be reclassified to uniform class 6 (for data types with width <= 3 register slots).

4. **Data-type conversion.** For boolean (`sub_7D66E0`) or floating-point (`sub_7D6780`) operand types, call `vtable+904` to map the data type to the appropriate register class.

5. **Materialization.** Call `sub_92E800` to create a MOV instruction (opcode 0x82 = 130) that loads the constant/immediate into a new register. Insert it at the insertion point. Replace the operand slot: lower word becomes `0x10000000 | new_reg_index` (type 1 = register), upper word is cleared to `& 0xFEC00000`.

6. **Insertion point update.** If the insertion point `a4` currently points to the instruction being legalized, advance it to the newly inserted MOV so subsequent materializations are ordered correctly.

### Opcode Groups and Legalization Recipes

| Opcodes | Instruction Class | Operands Legalized | Notes |
|---|---|---|---|
| 2-7 | Arithmetic (ADD/MUL/FMA) | dst, src0, src1 [, src2] | FMA (6) has optional predicate slots checked via sentinel |
| 8 | LD (load) | Variable based on addressing mode | Operand count read from `+80` |
| 10-11, 151-152, 290-291 | Compare/select | src0, src1 | Standard 2-source legalization |
| 16 | ST (store) | 1-15 data operands | Sentinel-scanned variable width |
| 32 | ATOM (atomic) | dst, addr, data | Specialized register conversion |
| 36 | TEX (texture) | coords + handle | Texture handle materialization |
| 42, 53, 55 | Shift/logic | src0 + try-inline src1 | `sub_13A6280` + `sub_13A7410` |
| 51 | PRMT (permute) | src0, control, src1 | `sub_13A6F90` for arch-dependent control operand |
| 61 | Branch-conditional | Nested switch on modifier bits | 6 sub-cases for different branch forms |
| 70, 243-262 | Tensor/WGMMA/bulk | Delegated to vtable+2816 | Architecture-specific |
| 82, 166, 196 | FP convert | src + try-inline | `sub_13A6280` + `sub_13A7410` + optional `sub_13A6F90` |
| 88-89 | ATOMS/ATOMG | Loop over sources | Per-source legalization with count |
| 110-121 | Wide arithmetic | src0, src1, src2 | 3 consecutive `sub_13A6280` calls |
| 137 | MOV | Opcode rewrite | Rewrites to 0x82 or 0x109 based on register class |
| 230-232 | LD/ST extended | src + inline + arch | `sub_13A6280` + `sub_13A7410` + `sub_13A6F90` |
| 270-289 | Control flow / misc | Variable | Several sub-groups with different patterns |
| 280-281 | Multi-source | Delegated to vtable+2328 | Operand count adjusted by -4 |

### Architecture Override Points

The dispatcher provides three escape hatches for architecture-specific behavior:

| Vtable Offset | Decimal | Opcodes | Purpose |
|---|---|---|---|
| `+2816` | 0xB00 | 70, 243, 245-247, 254-255, 257-259, 262 | Full delegation for SM-specific instructions |
| `+2328` | 0x918 | 280-281 (+ other cases) | Multi-source instructions with adjusted operand counts |
| `+3232` | 0xCA0 | Pre-switch (predicated instructions) | Custom predicate guard handling |

The `vtable+2816` handler receives `(backend, instruction, insert_point, pass_context, mode_flag)` and is expected to perform complete operand legalization for the instruction. The `vtable+2328` handler receives an adjusted operand count (`total - 4`), suggesting these instructions have 4 fixed operands plus a variable source list.

### Relationship to Legalization Passes

The operand legalization dispatcher operates at a different abstraction level than the six legalization passes described above. The legalization passes (phases 5-137) operate on the **Ori IR**, replacing unsupported operations with sequences of supported ones. The operand legalization dispatcher operates on **individual operands within already-legal instructions**, ensuring each operand is in a form the SASS encoder can bit-pack into machine code.

The dispatcher runs as part of the SASS encoding pipeline (called from `sub_A29220`), well after all six Ori-level legalization passes have completed. It is invoked per-instruction during the encoding walk, not as a standalone pass.

```
Ori legalization passes (phases 5-137)
  Replace unsupported OPERATIONS with legal sequences
         |
         v
SASS operand legalization (sub_13AF3D0, during encoding)
  Ensure each OPERAND of a legal instruction is encodable
         |
         v
SASS per-instruction encoders (522 functions)
  Pack operands into binary instruction word
```

## Cross-References

- [Pass Inventory & Ordering](index.md) -- Complete 159-phase table with legalization passes highlighted
- [Phase Manager Infrastructure](phase-manager.md) -- Phase factory, vtable layout, dispatch loop
- [SM Architecture Map](../targets/index.md) -- Per-SM capability tables driving legalization decisions
- [GeneralOptimize Bundles](general-optimize.md) -- Cleanup passes that run after expansion (phases 46, 58)
- [GMMA/WGMMA Pipeline](gmma-pipeline.md) -- Phases 85, 87 that create work for LateExpansionUnsupportedOps2
- [Synchronization & Barriers](sync-barriers.md) -- Barrier expansion (phase 42) that feeds MidExpansion
- [Mercury Encoder](../codegen/mercury.md) -- Post-legalization encoding (must see only legal ops)
- [Optimization Levels](../config/opt-levels.md) -- SetAfterLegalization gating by -O level
- [Knobs System](../config/knobs.md) -- Knobs 214, 464, 487, 499 controlling legalization
- [SASS Encoding Format](../codegen/encoding.md) -- Per-instruction SASS encoders that consume legalized operands
- [Instruction Representation](../ir/instructions.md) -- Ori IR operand layout (8-byte slots, type/payload encoding)
