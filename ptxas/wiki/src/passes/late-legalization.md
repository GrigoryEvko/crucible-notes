# Late Expansion & Legalization

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas pipeline contains six legalization passes spread across the 159-phase sequence. Their collective job is to replace Ori IR operations that the target SM cannot execute natively with equivalent sequences of legal instructions. "Unsupported ops" means exactly this: operations that exist in the PTX ISA or internal Ori representation but have no single-instruction mapping on the compilation target. The replacement may be an inline expansion (a sequence of simpler instructions), a call to a libdevice helper function, or an SM-specific intrinsic sequence.

The six passes run at deliberately different pipeline positions because each intervening group of optimization passes can expose new unsupported operations or create new legalization opportunities.

| | |
|---|---|
| **Passes covered** | 6 (phases 5, 45, 55, 78, 93, 137) |
| **Category** | Lowering |
| **Backend dispatch** | Architecture-specific via two backend objects at `context+0x630` and `context+0x640` |
| **Libdevice functions** | 607 call targets (`sub_5D1660`) + 473 inline templates (`sub_5D7430`) = 1,080 total `__cuda_*` entries; legality table at `0x22FEE00` (236 ops x 128 SM configs) |
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

**Two-level dispatch mechanism.** The execute function (`sub_C60A20`, 40 bytes) implements a two-level dispatch through the outer backend at `context+0x640`:

```
backend = *(ctx + 0x640)            // outer backend object
vtable  = *backend                   // read vtable pointer
fn      = vtable[0]                  // slot 0: ConvertUnsupportedOps handler
if fn != sub_661280:                 // non-default? (library/OptiX override installed)
    return fn(backend)               // call override directly
inner   = *(backend + 0x10)          // unwrap to inner SM backend object
return (*(inner))[0x78](inner)       // tail call inner vtable offset +0x78 (120)
```

The outer backend at `ctx+0x640` wraps the SM backend at `ctx+0x630`. In the default (standalone ptxas) path, vtable slot 0 points to `sub_661280` and the dispatch falls through to the inner object's vtable at offset `+0x78`. The inner object provides the SM-specific legalization implementation, which varies by SM generation:

| SM Backend | Vtable | SM Targets | Object Size |
|---|---|---|---|
| `sub_A99A30` | `off_21B4A50` | sm_50 (Maxwell), sm_60 (Pascal) | 1712B |
| `sub_A99A30` | `off_21D82B0` | sm_70 (Volta) | 1912B |
| `sub_ACDE20` | `off_21B2D30` | sm_80 (Ampere) | 1928B |
| `sub_662220` | `off_21C0C68` | sm_89 (Ada) | 1992B |
| `sub_662220` | `off_21D6860` | sm_90+ (Hopper/Blackwell) | 1992B |

**Library-mode and OptiX-mode overrides.** When ptxas operates as a library (invoked via the `nvptxcompiler` API) or compiles OptiX IR (controlled by the `"cpf_optx"` option), the compilation context constructor replaces the outer backend's vtable slot 0 with a custom handler. This intercepts ConvertUnsupportedOps before it reaches the SM backend, allowing the host tool to suppress certain legalizations (e.g., keeping operations in a form the host runtime can handle) or inject additional ones (e.g., OptiX-specific address space lowering). When the override is installed, the comparison against `sub_661280` fails and the override is called directly -- the inner SM backend vtable at `+0x78` is never reached unless the override explicitly delegates to it.

**Flag effect.** After execution, the secondary vtable method at offset `[40]` (`sub_C5F5D0`) sets bit 0 of `context+1378`, signaling to downstream passes that early legalization has completed. Passes like `OriCreateMacroInsts` (phase 8) check this flag to know whether certain patterns have already been lowered.

**What gets legalized early.** Operations that cannot survive optimization in their original form. The legality decision is driven by a 60,416-entry lookup table at VA `0x22FEE00` (241,664 bytes). Each entry pair encodes `(op_key, action)` where `op_key` packs the Ori opcode and type/modifier bits into a 16-bit value, and `action` is either a function pointer to the expansion handler (values in the `0x118xxxx` range) or the flag `0x08000000` meaning "unconditionally illegal, requires special validation." Of the 60,416 entries, 19,086 (31.6%) are nonzero -- the rest represent operations that are legal on all targets without conversion. Concrete categories:

- **Integer division/remainder** (`div.s64`, `rem.u64`, `div.s16`, `rem.u16`): no single-instruction SASS encoding on any SM. Always expanded via `__cuda_sm20_div_*` / `__cuda_sm20_rem_*` library functions.
- **FP64 atomics** (`atom.add.f64`): native hardware only on sm_60+; expanded on sm_50.
- **DP2A/DP4A** (`dp2a`, `dp4a`): native on sm_61+; emulated via `__cuda_sm62_dp2a` / `__cuda_sm62_dp4a` on sm_62 (Xavier).
- **Unsupported address spaces**: operations referencing state spaces not available on the target (e.g., `.shared::cluster` on pre-sm_90).
- **Modifier combinations without encoding**: certain rounding-mode + type combinations that the SASS ISA cannot represent (e.g., some FP16 rounding modes on sm_50/sm_60).
- **Pre-Volta barrier ops**: `barrier.arrive`, `barrier.red.*` with explicit barrier ID and thread count require emulation via the 393 `__cuda_sm70_barrier_*` library functions on sm_50/sm_60.
- **TCGen05 guardrails** (sm_100+): bounds check, alignment, and allocation granularity traps inserted as `__cuda_sm10x_tcgen05_guardrail_trap_*` calls.

### Phase 45 -- MidExpansion

```
Factory index:  51
Vtable:         off_22BDDC0
execute():      sub_C5EFB0  (thunk -> context+0x630 vtable+0xB0)
isNoOp():       sub_C5EFD0  (returns 0 -- always runs)
Field side-effect: context+1552 = 3 (via AdvPhAfterMidExpansion gate, binary index 52)
Pipeline:       After ExpandMbarrier (42), ForwardProgress (43), OptimizeUniformAtomic (44)
                Before GeneralOptimizeMid2 (46)
```

MidExpansion runs after the CTA/mbarrier/barrier expansion passes (phases 42-44) and before the second mid-level GeneralOptimize bundle (phase 46). It handles target-dependent expansions that must occur after barrier-related lowering but before the mid-level optimization cleanup. Unlike ConvertUnsupportedOps (phase 5, which uses the two-level outer backend at `ctx+0x640`), MidExpansion dispatches through the SM backend at `ctx+0x630` without library/OptiX interception.

**Dispatch mechanism.** The execute body (`sub_C5EFB0`, 13 bytes) is a direct tail-call thunk:

```
backend = *(ctx + 0x630)            // SM backend object (no outer wrapper)
vtable  = *backend                   // read vtable pointer
jmp     vtable[0xB0]                 // tail call offset +0xB0 (176)
```

There is no default-check-and-unwrap pattern here. The SM backend provides the handler directly, and no override mechanism exists for library or OptiX mode. The implementation varies by SM generation, because each SM backend constructor installs a different vtable (see the SM Backend table under Phase 5 above), and each vtable has a different function pointer at offset `+0xB0`. On older architectures (sm_50/sm_60) the handler at `+0xB0` is typically a no-op or near-trivial, since most operations needing mid-pipeline expansion did not exist pre-Volta. The slot becomes substantive on sm_70+ where barrier-adjacent operations, cache-policy creation (`__cuda_sm80_*` library functions on sm_80+), and async copy lowering require mid-pipeline treatment.

**Why this pipeline position.** MidExpansion must follow ExpandMbarrier (phase 42) because barrier pseudo-instructions must be lowered before any further legalization touches the same basic blocks. It must precede GvnCse (phase 49) and OriReassociateAndCommon (phase 50) because the expanded sequences benefit from value numbering and reassociation -- expanding earlier would expose these sequences to fewer optimization passes.

**Pipeline progress marker.** The `context+1552 = 3` write is performed by `AdvancedPhaseAfterMidExpansion` (wiki phase 134, binary index 52), a Type C gate phase that executes the secondary vtable method `sub_C5EF80` immediately after MidExpansion returns. This is not inside MidExpansion::execute itself. Downstream passes read this value: `sub_752CF0` checks `*(ctx+1552) <= 3` and `sub_A11060` checks `*(ctx+1552) > 4` to gate cross-block rematerialization second-pass behavior.

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

**What gets expanded here:** This is the pass where most math library calls are introduced. Operations like `div.rn.f64`, `sqrt.rn.f32`, `rcp.rd.f64` that were kept as single Ori instructions through optimization are now replaced with Newton-Raphson sequences or calls to the 607-function libdevice library. The SM20 library functions (division, square root, reciprocal, bit-field extract/insert) and SM70 functions (WMMA matrix operations, barrier reductions) are the primary candidates.

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

**Implementation -- iterative expand-check-repeat loop.** The SM backend vtable at `+0x178` dispatches to `sub_7917F0` (400 bytes), which is the same convergence driver used by OriBranchOpt (phase 15). The function executes three phases:

*Prerequisite gate chain.* Four conditions must all pass before any work begins:

1. `context+1382` bit 2 must be set (CFG validity flag -- cleared when the CFG is invalidated by an earlier pass, set by CFG rebuild).
2. Knob 214 must be **clear**. The capability dispatch at `context+1664` vtable+72 is compared against the default `sub_6614A0`; if default, the function reads the fast-path flag at `backend_inner+15408` directly, otherwise calls with argument 214. Knob 214 is a disable switch -- when set, the entire pass is skipped.
3. Knob 487 must be **set** (general optimization enablement). Checked via capability dispatch vtable+152 with the same default-vs-override pattern, argument 487.
4. The function table pointer at `*(context+0) + 1056` must be null (first-time initialization guard). If nonzero, the setup phase is skipped -- the pass was already initialized by a prior invocation (phase 15 shares this infrastructure).

*Setup (one-time initialization).* When the function table is null, four setup calls execute in sequence:

```
sub_785E20(ctx, 0)          // CFG rebuild: recompute RPO, predecessor lists
sub_781F80(ctx, 1)          // Block preparation: 8335B, builds per-block metadata
sub_7E6090(ctx, 0, 0, 0, 0) // Pattern scanner: 2614B, populates match table
sub_7E6AD0(ctx, 0, ...)     // Chain setup: 33B, links pattern entries
```

*Per-block convergence loop.* The outer loop walks blocks 1 through `context+520` (block count) in RPO order, indexing through the RPO permutation array at `context+512` into the block table at `context+296`:

```
for i = 1 to block_count:
    block = block_table[ rpo_index[i] ]

    loop:                                          // inner convergence
        matched = sub_753600(&work, block)         // pattern match attempt
        if not matched:
            break

        if not knob_enabled(464):                  // convergence gate
            break

        any_changed = matched
        sub_753B50(&work)                          // apply CFG rewrite
        // re-examine same block for cascading patterns

if any_changed:
    sub_785E20(ctx, 0)                             // post-pass CFG rebuild
```

The inner loop is the convergence mechanism. `sub_753600` (1351 bytes) attempts to match a transformable pattern in the current block's terminator and its successors. When it succeeds, `sub_753B50` (598 bytes) applies the CFG rewrite -- cloning instructions, redirecting edges, and updating block successor lists via `sub_931920`, `sub_932E80`, `sub_749090`, `sub_749290`, `sub_91E310`, and `sub_9253C0`. After rewriting, control returns to `sub_753600` on the same block, catching cascading opportunities: expanding one pattern may leave a redundant unconditional branch or expose a new pattern in the rewritten block.

**Knob 464 as convergence gate.** Knob 464 (`MergeEquivalentConditionalFlowBudget`, type `OKT_BDGT`) is checked on every iteration of the inner loop. When disabled, each block gets at most one match-and-rewrite -- no convergence. When enabled (the default), the loop runs until `sub_753600` returns zero, meaning no further patterns exist. There is no explicit iteration cap: convergence relies on each rewrite strictly reducing the pattern count in the block. In practice, cascading depth rarely exceeds 2-3 levels for typical PTX code.

**Post-pass CFG rebuild.** The `any_changed` flag (register `r12` in the binary, variable `v4` in the decompile) latches to true on the first successful match and is never reset. If any block was rewritten, `sub_785E20(ctx, 0)` runs after all blocks are processed, recomputing RPO order, predecessor lists, and dominance information for subsequent passes.

**Shared infrastructure with OriBranchOpt.** Phase 78 and phase 15 call the identical `sub_7917F0` function. The behavioral difference comes from the SM backend vtable: phase 15 is dispatched through a different call site (not via `+0x178`), but both land in the same code. The setup functions, pattern matcher, and rewriter are shared -- only the gating conditions (context+1414 bit 2 for phase 78, vs. always-on for phase 15) differ.

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

**Dispatch.** The thunk at `sub_C5E790` dispatches through the SM backend (`ctx+0x630`) at vtable offset `+0xD8` (216). The implementation installed there (`sub_C60B30`, 62 bytes) performs a secondary dispatch through the outer backend (`ctx+0x640`):

```
outer   = *(ctx + 0x640)              // outer backend object
vtable  = *outer                       // read vtable pointer
fn      = vtable[12]                   // slot 12 (offset +0x60)
if fn != sub_661310:                   // non-default? (SM-specific override installed)
    return fn(outer)                   // call override -- single unified handler
inner   = *(outer + 0x10)             // unwrap to inner SM backend object
call inner_vtable[+0x118](inner)      // step 1: instruction expansion (offset 280)
jmp  inner_vtable[+0xC10](inner)      // step 2: operand legalization (offset 3088)
```

**Two-step default path.** When no override is installed at vtable slot 12, the default `sub_661310` splits the work into two sequential calls on the inner object:

1. **Instruction expansion** (inner vtable `+0x118` / 280) -- scans for Ori instructions that the GMMA/register-attribute passes introduced and that lack direct SASS encodings. Replaces them with equivalent legal sequences, following the same per-instruction dispatch pattern as Phase 78 but restricted to the newly-introduced operations.
2. **Operand legalization** (inner vtable `+0xC10` / 3088) -- ensures every operand of the newly-expanded instructions is in a hardware-encodable form: correct register class, immediate width, or absent-operand sentinel. This is the same operand materializer infrastructure as `sub_13AF3D0` (the 164-case dispatcher), invoked on the subset of instructions that Phase 93 touched.

Architectures that override slot 12 with a custom function replace both steps with a single unified handler. This is the path taken by newer SM backends (sm_90+) where the post-GMMA legalization rules are complex enough to warrant a monolithic implementation rather than the generic two-step split.

### Phase 137 -- LateExpansionUnsupportedOpsMid

```
Factory index:  93
Vtable:         off_22BE450
execute():      sub_C607E0  (thunk -> context+0x630 vtable+0x180)
isNoOp():       sub_C5EA00  (returns 0 -- always runs)
Default check:  compares vtable+0x180 against nullsub_183 (sub_7D6D50) -- if default, no-op
Pipeline:       After LateMergeEquivalentConditionalFlow (136), before OriSplitHighPressureLiveRanges (138)
```

The final legalization catch, positioned between the two conditional flow merge passes (133, 136) and the last-resort live range splitter (138). The merge passes can combine basic blocks in ways that create new instruction sequences containing unsupported operations.

**Conditional execution.** Unlike the other five legalization passes, this one has a soft no-op mechanism built into the execute thunk itself (`sub_C607E0`, 30 bytes). The thunk reads vtable slot `+0x180` (384), loads the function pointer, and compares it against `nullsub_183` (`sub_7D6D50` -- a 2-byte `rep ret`). If the pointer matches the default, the thunk returns immediately via `rep ret` without entering the handler. If the backend has installed a non-default function pointer, the thunk calls it.

**SM target activation.** The default vtable at `ctx+0x630` has `nullsub_183` in slot `+0x180`, so the pass is a no-op on all architectures that do not override it. In practice, only Hopper (sm_90, sm_90a) and all Blackwell targets (sm_100, sm_103, sm_110, sm_120, sm_121) install a non-default handler. Pre-Hopper architectures (sm_50 through sm_89) retain the nullsub default and skip Phase 137 entirely.

**Why Hopper+ needs this.** On sm_90+, the conditional flow merge passes (phases 133, 136) can create instruction patterns involving WGMMA accumulator registers, TMA descriptor operations, or barrier-based synchronization that were individually legal before merging but become unsupported in the merged block context. Additionally, Blackwell's TCGen05 tensor memory operations and bulk copy sequences can produce merge artifacts that require re-legalization. Pre-Hopper architectures lack these instruction classes, so the merge passes cannot produce unsupported operations.

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

The legalization passes replace unsupported operations with calls to a library of 607 predefined helper functions plus 473 force-inlined templates, for a combined pool of 1,080 `__cuda_*` entries embedded in the ptxas binary. These are not external libraries -- they are PTX function bodies compiled into the binary image and linked into the output at need.

The two tiers serve different purposes. The 607 `.weak .func` entries are call targets: legalization replaces an unsupported Ori instruction with a `CALL` to the named function, and the function body is emitted once per compilation unit. The 473 `.FORCE_INLINE` entries are templates whose bodies are spliced directly into the caller's instruction stream, avoiding call overhead for performance-critical sequences (WMMA load/store variants, warp shuffle/vote, MMA shuffle helpers).

**Tier 1 -- Call targets (`sub_5D1660`, 607 registrations).** Copies a 9,728-byte pre-built table from `unk_1D4D940` (608 x 16B slots, ID 0 = null sentinel), creates a hash map at `context+1064`, and registers 607 names with contiguous IDs `0x01`--`0x25F`.

**Tier 2 -- Inline templates (`sub_5D7430`, 473 registrations).** Builds a second hash map at `context+824` with 1,079 entries (607 tier-1 names re-registered plus 473 additional WMMA/MMA generation-specific variants). The extra 473 entries cover sm72 integer WMMA (114), sm7x sub-byte/bit WMMA (229), sm8x tf32/bf16/f64 WMMA (80), additional sm70 inline helpers (40), sm10x tcgen05 inline variants (9), and one sm80 inline entry.

### Library Function Categories (Tier 1 -- 607 Call Targets)

| SM Prefix | Count | ID Range | Operations |
|---|---|---|---|
| `__cuda_reduxsync_` | 17 | `0x01`--`0x11` | Redux-sync reductions (and/or/xor for b32, add/max/min for s32/u32/f32 with NaN/abs variants) |
| `__cuda_sanitizer_` | 7 | `0x12`--`0x18` | Memory sanitizer checks (malloc/free/generic/global/local/shared/readmetadata) |
| `__cuda_scalar_video_emulation_` | 7 | `0x19`--`0x1F` | Video instruction emulation (operand extract, sign extend, saturate, merge) |
| `__cuda_sm10x_` | 11 | `0x20`--`0x2A` | TCGen05 guardrail traps (bounds check, alignment, allocation), create_mask helper |
| `__cuda_sm1xx_` | 18 | `0x2B`--`0x3C` | Bulk copy (unicast/multicast), async bulk tensor copy (1D-5D tile/im2col, unicast/multicast) |
| `__cuda_sm20_` | 70 | `0x3D`--`0x82` | IEEE math: div/rcp/sqrt/dsqrt/drsqrt (all rounding modes + slowpaths), bfe/bfi 64-bit, integer div/rem (s16/s64/u16/u64) |
| `__cuda_sm3x_` | 4 | `0x83`--`0x86` | FP32 division with FTZ variants (Kepler-specific refinement paths) |
| `__cuda_sm62_` | 2 | `0x87`--`0x88` | DP2A, DP4A dot-product accumulate (pre-Volta emulation) |
| `__cuda_sm70_` | 393 | `0x89`--`0x211` | Barrier ops (arrive/red/sync/wait x 16 IDs x count variants), WMMA (204 shape/type/layout combos), warp shuffle/vote/match sync |
| `__cuda_sm80_` | 3 | `0x212`--`0x214` | Cache policy creation (fractional, fractional_encode, range_encode) |
| `__cuda_sm_10x_` | 10 | `0x215`--`0x21E` | Blackwell hmma/imma mdata + bit MMA (and/xor m8n8k128/m16n8k128/m16n8k256) |
| `__cuda_sm_8x_` | 14 | `0x21F`--`0x22C` | Direct MMA operations (f16/f32 accum, 4 layout combos) + mma_shfl helpers |
| `__cuda_sm_9x_` | 51 | `0x22D`--`0x25F` | Hopper sub-byte + bit MMA: s4/u4 dense/sparse m16n8k32/k64/k128, bit xor variants |

### Instruction Legality Table

The per-SM legalization rules are encoded in a 241,664-byte static table at `0x22FEE00`--`0x2339E00`. The table is a 3D array indexed as `legality[operation][sm_entry][field]`:

| Dimension | Size | Description |
|---|---|---|
| Operation | 236 | Internal legalization operation ID (maps to Ori opcode + type/modifier combination) |
| SM entry | 128 | SM configuration index (covers all SM variants including sub-variants like sm_70a/b/c/f) |
| Field | 2 | Two u32 dwords per entry |

**Entry encoding.** Each entry's dword 0 uses one of four value classes:

| Value Class | Bit Pattern | Meaning | Count |
|---|---|---|---|
| Zero | `0x00000000` | Operation is natively supported -- no legalization needed | 41,330 |
| Small descriptor | `< 0x10000` | Packed recipe: `(expansion_class << 8) \| sub_variant` -- selects an inline expansion sequence | 9,721 |
| Code pointer | `>= 0x10000`, no flag | Address of an expansion handler function in `.text` (7,037 unique targets) | 9,053 |
| Illegal flag | `0x08000000` | Operation is illegal on this SM with no available expansion -- assembler must error | 259 |

Of the 236 operations, 233 have at least one non-zero SM entry (3 are universally legal). Only 4 operations require legalization on all 128 SM configurations; the remaining 229 are partially SM-dependent. The `0x08000000` illegal-flag entries concentrate in just 4 SM rows, corresponding to SM configurations with restricted instruction sets (debug/validation targets).

### SM-Dependent Legalization Examples

The core design principle: what is "unsupported" depends entirely on the target SM. An operation legal on one architecture may require library expansion on another.

**Integer division/remainder.** PTX `div.s64` and `rem.u64` have no single SASS instruction on any SM. They are always expanded to multi-instruction sequences via `__cuda_sm20_div_s64`, `__cuda_sm20_rem_u64`, etc. These are "sm20" functions because the expansion has been the same since Fermi.

**FP32 division with rounding.** `div.rn.f32` on Turing (sm_75) uses a hardware-assisted Newton-Raphson (`MUFU.RCP` + refinement). On Kepler (sm_3x, no longer shipped but the code path remains), different refinement sequences are needed, using `__cuda_sm3x_div_rn_ftz_f32` and its slowpath variant.

**Barrier operations.** On Volta+ (sm_70), `barrier.arrive` with a specific barrier ID and thread count is a single SASS instruction (`BAR.ARV`). On pre-Volta targets, these must be emulated with the 393 `__cuda_sm70_barrier_*` library functions that implement the semantic equivalent using older synchronization primitives.

**WMMA/Tensor Core.** Warp-level matrix multiply-accumulate (`wmma.*`) on sm_70 has dedicated hardware instructions (HMMA). The 204 `__cuda_sm70_wmma_*` call-target variants cover the combinatorial explosion of shapes (m16n16k16, m8n32k16, m32n8k16), types (f16, bf16, tf32, s8, u8, s4, u4, b1), layouts (row/col), and accumulator types. An additional 463 force-inlined WMMA templates (sm72 integer, sm7x sub-byte/bit, sm8x tf32/bf16/f64) are spliced directly at the call site for later GPU generations.

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
| `sub_C5E790` | ~16B | LateExpansionUnsupportedOps2 execute thunk (-> `ctx+0x630` vtable+0xD8) |
| `sub_C60B30` | 62B | LateExpansionUnsupportedOps2 implementation at vtable+0xD8 (two-step dispatch) |
| `sub_661310` | 18B | Default handler for Phase 93 outer backend slot 12 (calls inner +0x118, +0xC10) |
| `nullsub_183` | 2B | Default (no-op) handler for Phase 137 vtable+0x180 (`sub_7D6D50`, `rep ret`) |
| `sub_C5F8A0` | ~30B | SetAfterLegalization execute |
| `sub_7DDB50` | 232B | Optimization level gate (knob 499 check) |
| `sub_7917F0` | ~400B | LateExpansionUnsupportedOps core implementation |
| `sub_9059B0` | ~500B | LateExpansion core implementation (with expansion loop) |
| `sub_5D1660` | ~46KB | Libdevice function table initializer (607 call targets + null sentinel = 608 slots) |
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

The switch routes to seven categories of legalization logic, totalling 164 case labels over 100 distinct opcodes. The complete dispatch map follows.

**Category A -- Direct operand materialization (73 cases).** Each case calls `sub_13A6280` (general materializer) and/or `sub_13A7410` (try-inline-first) on a fixed set of operand slots. The slot indices are hardcoded per opcode. The most common patterns:

| Pattern | Cases (representative) | Operand recipe |
|---|---|---|
| mat(src0) | 44,45,66,80,135,161,205 | `sub_13A6280(ctx, instr, 1, ...)` |
| mat(src0)+try(src1) | 42,53,55 | `sub_13A6280(...,1,...); sub_13A7410(...,2,0,...)` |
| mat(src0,src1,src2) | 73,74,198 | `sub_13A6280` on slots 1,2,3 in sequence |
| mat(src0)+try(src1)+mat(src2) | 6 (FMA) | `sub_13A6280(...,3,...); sub_13A7410(...,4,1,...); sub_13A6280(...,5,...)`; then sentinel-test slots 6,7 for optional predicate via `sub_13A45E0` |
| mat(src0)+try(src1)+imm(src2) | 82,166,196 | `sub_13A6280(...,1,...); sub_13A7410(...,2,flag,...); sub_13A6F90(...,3,flag,...)` where `flag` = (src is uniform register class 6) |
| mat(src0)+mat(src1)+pred-check | 119,283 | `sub_13A6280` then sentinel-test last operand for `vtable+2864` predicate handler |

Case 6 additionally checks whether src1 is in uniform register class 6 and calls `sub_13A75D0` (uniform-to-GPR conversion) before materialization. Cases 2/3/4/5/7 compute a variable operand count from the instruction modifier word (slots at offsets 16/24) before dispatching to a shared suffix at `LABEL_125` / `LABEL_119`.

**Category B -- Variable-length operand scanning (5 cases).** Case 16 (store) scans up to 15 operand slots, testing each against the `0x70000000` sentinel to find where active operands end. After materializing each source, it delegates the address operand range to `vtable+2328` (or `vtable+2600` if the vtable slot is the default `sub_13A6110`). Cases 109, 284, 288, 329 use similar counted loops: case 109 walks operand slots backward using a callee-descriptor bitmap; case 284 loops `operand_count - 4` times calling `sub_13A6AE0`; case 288 loops `(modifier & 7) + 1` times; case 329 reads the destination count from the modifier word then delegates the tail to `vtable+2328`.

**Category C -- Architecture-specific vtable delegation (24 cases).**

| Vtable slot | Cases | Likely instruction class |
|---|---|---|
| `+2816` | 70, 243, 245-247, 254-255, 257-259, 262 | Tensor core / WGMMA / TMA (11 cases) |
| `+2768` | 10, 11, 151, 152, 290, 291 | Texture/surface (6 cases) |
| `+2328` | 8 (call), 31 | Indirect call / computed-goto |
| `+2640` | 112, 110-111, 114-115, 117 | Conversion/cast family (6 cases, fallthrough from Category A type-check) |
| `+2616` | 102 | Integer divide (with FP-type-check gate to LABEL_750) |
| `+2624` | 108 | Special register read |
| `+2672` | 94 | Uniform load (default = `sub_13AEA10` inline handler) |
| `+2680` | 20 | Conditional load (`sub_A8CBE0` = no-op sentinel) |
| `+2688` | 21 | Conditional store (`sub_A8CBF0` = no-op sentinel) |
| `+2704` | 280-281 (inner loop) | Bulk-copy per-pair dispatch |
| `+2720` | 18 | Branch-with-address legalization pre-hook |
| `+2744` | 22, 77, 83, 297, 352 | Passthrough with optional vtable override |
| `+2752` | 50 | Shared-memory fence |
| `+2760` | 309 | Arch-specific post-RA fixup |
| `+2856` | 286-287 | TMA descriptor legalization |
| `+2896` | 280-281 (prefix) | Bulk-copy modifier pre-hook |
| `+3168` | 288 (suffix) | Store-to-address-space override |

**Category D -- Opcode rewriting (3 cases).** Case 137 rewrites the opcode field: to `0x82` (130, CMOV) when the type is FP and the modifier has destination flags, or to `0x109` (265, MOV-from-special-register) when the source is in register class 4 (special). Case 137 also attempts FP16 immediate folding: if the source is a 16-bit immediate fitting the SASS encoding width (10-bit or 7-bit mantissa), it emits an `FMOV_TINY` (opcode `0x10E` = 270) instruction instead. Cases 36 and 32 also rewrite: case 36 may convert to opcode `0x29` (41, PRMT) when source is in class 4/5; case 32 may convert opcode to `0x0B`/`0x0C` (11/12) based on FP type width.

**Category E -- Conditional materialization (15 cases).** These inspect the modifier word, data type, or register class before deciding which operands to legalize. Case 43 checks `(modifier >> 4) & 0xF` and `modifier & 0xF` to select between materializing slot 1 only, slot 2 only, or both. Case 118 reads `modifier & 3`: value 0 means materialize slot 1; value 1 means slots 1+2. Cases 88/89 compute variable slot indices from signed operand values (shifted by `>> 31`) to handle packed-pair FP operations. Case 29/95/96/190 check whether the last operand is in register class 6 (uniform), 9, 4, or 5 before choosing between `sub_13A6AE0` (special-class) and `sub_13A6280` (general).

**Category F -- Complex multi-phase legalization (8 cases).** Cases 223/228/234/238 (load/store with addressing modes) compute a source-count from the modifier word (`(modifier >> 19) & 0xF + (modifier >> 4) & 3`), materialize that many sources in a counted loop, then dispatch the address portion to `vtable+2840`/`vtable+2848` depending on the address-space type code (4 or 5). Case 280-281 iterates over source-pair operands in steps of 2, calling `vtable+2704` per pair, then performs a suffix that reorders source/address operands and adjusts modifier bit-fields for the encoding. Case 125 and 211 have similarly deep logic with intermediate register-class queries and possible instruction splitting.

**Category G -- Passthrough / no-op (22 cases + default).** Cases 24, 34, 209, 213, 214 jump directly to the exit. Cases 38, 59, 106, 180, 182, 192, 194, 215, 221 exit unless `SM_version > 0x4FFF` (SM80+), in which case they materialize slot 1. Cases 44, 45, 135, 161 always materialize slot 1 (so are Category A in practice but share the same `LABEL_325` target). The `default` case exits immediately.

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
