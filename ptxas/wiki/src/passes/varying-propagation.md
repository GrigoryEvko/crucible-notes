# Varying Propagation (Divergence Analysis)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

`OriPropagateVarying` is the divergence analysis of ptxas: a forward dataflow analysis that classifies every virtual register as either **uniform** (all 32 lanes of a warp hold the same value at this program point) or **varying** (lanes may disagree). The result is one bit per virtual register — bit 2 of `vreg+49` — and that single bit gates promotion to the UR register file, speculation safety, MovPhi materialization, late predication legality, and barrier-removal eligibility. There is no LLVM equivalent: scalar IRs do not even have the concept of *per-thread divergence*, and ptxas does not run on LLVM. The pass runs **twice** by design — phases 53 (`OriPropagateVaryingFirst`) and 70 (`OriPropagateVaryingSecond`) — because predication, rematerialization, and CFG-altering transforms between them invalidate the first snapshot.

| | |
|---|---|
| **Phase indices** | 53 (`OriPropagateVaryingFirst`), 70 (`OriPropagateVaryingSecond`) |
| **Category** | Optimization (per `passes/index.md`); analytically a *dataflow analysis* whose output drives later optimizations |
| **Entry orchestrator** | `sub_90EF70` (3,105 B, 131 BBs) — feeds the run from the per-function pipeline driver |
| **Per-function driver** | `sub_90EDA0` (464 B, 16 BBs) — seeds intrinsic varying roots, kicks the fixed-point loop |
| **Seeding pass** | `sub_900020` (2,979 B, 152 BBs) — initializes per-vreg state, resets bit 2 |
| **Fixed-point loop** | `sub_90E620` (1,919 B, 106 BBs) — outer worklist over functions and registers |
| **Per-vreg propagator** | `sub_90C180` (2,093 B, 145 BBs) — sets bit 2, returns "changed" flag |
| **Inter-procedural resolver** | `sub_90E3F0` (550 B, 23 BBs) — FNV-1a hash lookups into callee state |
| **Reload helper** | `sub_90D4C0` (1,808 B, 98 BBs) — refreshes liveness/usage views before each iteration |
| **Storage** | Bit 2 of byte `vreg+49` (per-register flag byte) |
| **Pipeline window** | Partial-SSA (phases 23–73); both runs land inside the SSA window |
| **Knobs** | Gated through the standard phase-manager `isNoOp` path; no dedicated enable knob — varying analysis is mandatory once UR-aware codegen is reachable |

## What "Varying" Means

A virtual register `vreg` carries the **varying** flag if there exists *any* program point reachable along a feasible execution path at which two lanes of the same warp could hold different bit-patterns in `vreg`. The flag is **monotonic** — once set during the analysis, it never clears — and **may-style**: the analysis is sound but conservative, so a register may be marked varying even when, dynamically, all lanes happen to agree.

Concretely, the flag controls:

| Consumer | What it does with the flag |
|---|---|
| `ConvertToUniformReg` (phase 74) | Promotes only **uniform** vregs to UR/UP. A varying vreg stays in the R/P file. |
| `OptimizeUniformAtomic` (phase 44) | Rewrites `ATOM` into ELECT+REDUX+single-lane only when the address is uniform; varying addresses skip the rewrite. |
| `OriDoPredication` (phase 63) | Allows if-conversion across a divergent branch only when no live-out vreg becomes a varying merge that would observe the wrong lane on the not-taken path. |
| `OriHoistInvariantsLate` (phase 66) | Will not hoist a varying expression to a uniform-by-construction location (and vice versa where speculation safety depends on uniformity). |
| `OriDoRemat{Early,}` (phases 54, 69) | Picks recomputation chains that preserve the uniform/varying classification of users, avoiding rematerialization patterns that would flip the flag. |
| `OriRemoveRedundantBarriers` / `OptimizeSyncInstructions` | A barrier whose all-thread arrival is guaranteed by a uniform predicate can be elided; varying predicates force the barrier to remain. |
| `OriBranchOpt` / `BranchSwitch` | Uniform branches use the cheaper `UBRA` encoding on sm_75+; varying branches need `BSSY`/`BSYNC` pairs. |
| Scheduler dependency builder (`sub_A0D800`) | Treats varying-vs-uniform pairs as conflicting for some uniform-datapath ports. |

### Two-Bit Coordinate

Per-register state actually lives in **two** distinct bits, on two different bytes:

```
       vreg + 48                                                vreg + 49
  ┌─────────────────────────┐                              ┌──────────────────────┐
  │ ... bits 20..21 = pair  │                              │ ...                  │
  │     mode (1/2/4 slots)  │                              │ bit 2 = varying flag │
  │ bit  3 = "address       │                              │       (this page)    │
  │           operand is    │                              │ ...                  │
  │           varying"      │  ← consulted by atomic-      │ bit 4 = "no-promote" │
  │           (uniform-atom │    uniformity tests          │       legacy flag    │
  │           shortcut bit) │                              └──────────────────────┘
  └─────────────────────────┘
```

Both bits are written by `sub_90C180`. `vreg+48` bit 3 is the per-operand replica used by `OptimizeUniformAtomic` (phase 44) — it caches the address-operand divergence so the atomic rewriter does not need to chase definitions across a basic block boundary. `vreg+49` bit 2 is the canonical divergence flag and is the bit referenced by every downstream consumer described in the table above. Confidence: **HIGH** (cross-referenced through `passes/uniform-regs.md` ground-truth correction note).

## Algorithm

Both runs (phases 53 and 70) execute the same procedure with the same code, only their pipeline position differs. The procedure has three steps: **reset**, **seed**, and **iterate-to-fixed-point**.

```c
// Pseudocode distilled from sub_90EF70 → sub_90EDA0 → sub_900020 + sub_90E620.
// Argument: a CodeObject* (the per-function IR container).
// Side effects: sets bit 2 of vreg+49 on every register that may become divergent.

void OriPropagateVarying(CodeObject *fn) {
    // ── Step 1: reset per-vreg state ────────────────────────────────
    // sub_781F80 + sub_A10160 + sub_8FD160 zero per-vreg flag bytes
    // and re-grow the two parallel vreg arrays at fn+(30·8) and fn+(33·8)
    // to the current basic-block count. The arrays are kept sized to
    // (BB_count + 2) entries of 40 bytes each.

    reset_vreg_flags(fn);                 // clears bit 2 of every vreg+49

    // ── Step 2: seed intrinsic divergence roots ─────────────────────
    // sub_900020 walks every instruction once and marks the destination
    // vreg as varying whenever the source is one of the divergence roots:

    for (instr *I : fn->instructions) {
        if (defines_thread_identity(I)) {
            //   S2R reading SR_TID_{X,Y,Z}   →   varying
            //   S2R reading SR_LANEID        →   varying
            //   SHFL.{IDX,UP,DOWN,BFLY}      →   varying (per-lane permutation)
            //   VOTE.{ANY,ALL}               →   uniform   (collective reduce)
            //   VOTE.BALLOT                  →   uniform   (32-bit per-lane bitmap, but the same in every lane)
            //   per-thread ATOM / RED        →   varying (return value)
            //   LDG/LDS/LDL with varying base→   varying
            set_bit2(I->dest_vreg + 49);
        }
        // Note: SR_CTAID_{X,Y,Z}, SR_NCTAID, SR_GRIDID, SR_SMID, SR_WARPID
        // are warp-uniform → NOT seeded as varying.
    }

    // ── Step 3: iterative fixed-point over RPO + call graph ─────────
    // sub_90E620 runs an outer worklist loop. The worklist is a packed
    // bitvector at sub_90E620.v55..v55[7]; _BitScanReverse64 extracts
    // the next pending vreg index. The inner per-vreg propagator
    // sub_90C180 returns 1 when bit 2 was newly set; that propagates
    // upward through callee edges.

    bool changed;
    do {
        changed = false;

        // 3a. forward walk over instructions in RPO basic-block order
        for (bb *B : reverse_postorder(fn->cfg)) {
            for (instr *I : B->instructions_forward) {
                if (I->opcode == MOV_PHI) {
                    // Phi over a divergent merge → always varying
                    if (merges_across_divergent_branch(I) ||
                        any_source_has_bit2(I)) {
                        if (!bit2(I->dest_vreg + 49)) {
                            set_bit2(I->dest_vreg + 49);
                            changed = true;
                        }
                    }
                } else {
                    // Pure SSA def: dest is varying if ANY source is varying
                    for (operand *S : I->source_operands) {
                        if (!is_register(S)) continue;
                        if (bit2(S->vreg + 49)) {
                            if (!bit2(I->dest_vreg + 49)) {
                                set_bit2(I->dest_vreg + 49);
                                changed = true;
                            }
                            break;        // one varying source suffices
                        }
                    }
                    // sub_90C180 also updates vreg+48 bit 3 when the
                    // *address* operand of a load is varying — this is
                    // the uniform-atomic shortcut bit.
                }
            }
        }

        // 3b. inter-procedural step (sub_90E3F0)
        for (call_edge *E : fn->call_graph_edges) {        // codeobj+128
            uint32_t key = E->callee_vreg_index;
            // FNV-1a hash of the per-callee bookkeeping record
            uint32_t h = 0x811C9DC5u;
            for (int b = 0; b < 4; ++b) {
                h = (h ^ ((key >> (8*b)) & 0xFFu)) * 0x01000193u;
            }
            callee_state *CS = hash_lookup(fn->callee_table_288_or_328, h, key);

            if (CS && callee_param_was_marked_varying(CS, E)) {
                // Newly-varying callee parameter → re-iterate from scratch.
                changed = true;
                goto restart;        // sub_90E620 LABEL_24 in the binary
            }
        }
restart: ;
    } while (changed);
}
```

`sub_90C180` is the engine of the propagator and is responsible for both the bit-2 write at `vreg+49` and the address-operand replica at `vreg+48`. Its 2,093-byte body decodes the instruction's operand list (an intrusive doubly-linked list rooted at the instruction record), reads each operand's register descriptor, OR-merges divergence into the destination's accumulator (`v61 = *v5 | *v73`), and uses `sub_8FE340` to push freshly-tainted registers back onto the worklist. The `_BitScanReverse64` pair in `sub_90E620` (offsets `0x90e9d8`, `0x90ed47`) implements an O(1) "next set bit" iterator over the worklist bitvector — this is what keeps the outer loop subquadratic on functions with thousands of vregs.

> ⚡ **QUIRK — VOTE.BALLOT looks divergent but is uniform**
> The `VOTE.BALLOT` instruction produces a 32-bit value where bit `i` reflects whether lane `i` satisfied the predicate. The integer is identical in every lane (it is a *cross-warp* reduction whose result is broadcast), so the destination is **uniform**, not varying. Naïve seeders that mark anything reading per-lane state as varying mis-classify `BALLOT` and unnecessarily inflate register pressure by keeping its result in the R file. Confirmed by inspection of the seed table in `sub_900020` and the inverse case (`SHFL`) which *is* seeded as varying.

> ⚡ **QUIRK — inter-procedural re-iteration restarts from scratch**
> When `sub_90E3F0` discovers that a callee parameter was newly marked varying, `sub_90E620` does **not** continue the current sweep — it executes a `goto LABEL_24` back to the top of the outer worklist loop. This means a single late-discovered call-site divergence can cost a full re-walk of every instruction in every function reachable from the current entry point. The decision pays for itself only because the bitvector worklist makes individual sweeps cheap; on pathological call-graph shapes (deeply nested device functions with shared parameter patterns) the analysis can dominate per-function compile time on `O3`/`O4`. Counter-anchored: the `restart` branch is the larger of the two `_BitScanReverse64` callers in `sub_90E620`.

## Why Two Runs?

The pipeline runs `OriPropagateVarying` at phases **53** and **70**, separated by 17 intervening phases that all have the potential to change which registers are varying.

```
Phase 49  GvnCse                            ┐
Phase 50  OriReassociateAndCommon           │
Phase 51  ExtractShaderConstsFinal          │
Phase 52  OriReplaceEquivMultiDefMov        │   first snapshot is consumed by:
Phase 53  ★ OriPropagateVaryingFirst        ┤    54 OriDoRematEarly
Phase 54  OriDoRematEarly                   │    56 SpeculativeHoistComInsts
Phase 55  LateExpansion                     │    63 OriDoPredication (preliminary check)
Phase 56  SpeculativeHoistComInsts          │    66 OriHoistInvariantsLate (early heuristic)
Phase 57  RemoveASTToDefaultValues          │
Phase 58  GeneralOptimizeLate               │
Phase 59  OriLoopFusion                     │
Phase 60  DoVTGMultiViewExpansion           │
Phase 61  OriPerformLiveDeadThird           │
Phase 62  OriRemoveRedundantMultiDefMov     │
Phase 63  OriDoPredication                  ┤── CFG changes (branch → predicated)
Phase 64  LateOriCommoning                  │
Phase 65  GeneralOptimizeLate2              │
Phase 66  OriHoistInvariantsLate            │
Phase 67  DoKillMovement                    │
Phase 68  DoTexMovement                     │
Phase 69  OriDoRemat                        ┤── new definitions appear
Phase 70  ★ OriPropagateVaryingSecond       ┘
Phase 71  OptimizeSyncInstructions               consumes the refreshed snapshot
Phase 74  ConvertToUniformReg                    AUTHORITATIVE consumer of bit 2
```

Three transforms in particular invalidate the first snapshot:

1. **Predication (phase 63)** rewrites a divergent branch as predicated straight-line code. Every register that used to be a `MovPhi` merge across the branch becomes a plain `SEL`/predicated `MOV`, so the "merge across divergent edge" rule no longer fires. Some registers that were varying because of the merge are now provably uniform.
2. **Rematerialization (phase 69)** clones expressions into multiple places. A rematerialized chain may be re-seeded from a uniform constant in one location and from a varying GMEM load in another. The new definitions need their own classification.
3. **Loop fusion (phase 59) + LICM (phase 66)** move instructions into and out of loop headers, changing the dominance relationship between varying definitions and their uses.

The phase-70 result is the snapshot consumed by `ConvertToUniformReg` (phase 74), which is the optimization that depends on it most heavily. Running varying-propagation only once would force `ConvertToUniformReg` to assume *every* `MovPhi`-merged register is varying, gutting UR promotion on any kernel that survived if-conversion.

> ⚡ **QUIRK — phase 70 reads bit 2, does not necessarily clear it**
> Step 1 of the algorithm clears bit 2 across all vregs on entry, and `sub_900020` blindly grows the per-vreg arrays even when their backing storage is still valid. This means phase 70 does **not** treat the phase-53 result as an incremental input; it recomputes from scratch. The monotone "once varying, always varying" property is therefore re-established within each run, not across the two runs. A register that was marked varying by phase 53 *can* become uniform after phase 70 (and routinely does, because predication eliminates the merge that caused it to be varying in the first place).

## Divergence Roots — Seed Table

The seeder in `sub_900020` walks instructions once and marks the destination as varying whenever the producing instruction reads from one of the divergence sources. The table below is reconstructed from the seed function and verified against the PTX special-register strings in `ptxas_strings.json` (e.g. `"%laneid"`, `"%tid"`, `"%ctaid"`).

| PTX special register | After lowering | Seeded? | Notes |
|---|---|---|---|
| `%tid.{x,y,z}` | `S2R Rd, SR_TID_{X,Y,Z}` | **varying** | Per-lane thread id within CTA |
| `%laneid` | `S2R Rd, SR_LANEID` | **varying** | Lane index within warp |
| `%warpid` | `S2R Rd, SR_WARPID` | uniform | Same across the warp by definition |
| `%ctaid.{x,y,z}` | `S2R Rd, SR_CTAID_{X,Y,Z}` | uniform | CTA-uniform |
| `%nctaid.{x,y,z}` | `S2R Rd, SR_NCTAID_{X,Y,Z}` | uniform | Grid-wide |
| `%smid` | `S2R Rd, SR_SMID` | uniform | SM-uniform → same in every lane of the warp |
| `%gridid` | `S2R Rd, SR_GRIDID` | uniform | Constant per launch |
| `shfl.sync.*` | `SHFL` | **varying** | Permutation produces per-lane output |
| `vote.sync.{any,all,uni}` | `VOTE` | uniform | Boolean reduction broadcast to every lane |
| `vote.sync.ballot` | `VOTE.BALLOT` | uniform | 32-bit mask, identical in every lane (see QUIRK above) |
| `atom.*` (return value) | `ATOM`, `RED` (return form) | **varying** | Per-lane sequencing → per-lane result |
| `ld.global [Raddr]` with varying `Raddr` | `LDG` | **varying** | Address divergence propagates to data |
| `ld.global [Uaddr]` | `LDG.U` | uniform | Uniform-address load (sm_80+) |
| `ld.shared [Raddr]` with varying `Raddr` | `LDS` | **varying** | Per-lane bank access pattern |
| `ld.local` | `LDL` | **varying** | Always per-lane (local memory is per-thread) |
| `ld.param` | `LDC` from `c[0]` | uniform | Kernel parameters are CTA-uniform |
| `ld.const [c[i]+offset]` with uniform offset | `LDC` | uniform | Constant memory broadcast |
| `mov.b32 Rd, immediate` | `MOV Rd, IMM` | uniform | Immediates are warp-uniform |

Confidence: **HIGH** for the special-register rows (seed function strictly tests opcode + S2R source enum); **MED** for the address-divergence rows (the test reads the operand's varying replica at `vreg+48` bit 3, which is itself populated by the previous iteration of the propagator).

## Storage Layout

The varying flag is one bit in a per-virtual-register descriptor. The relevant byte is `vreg+49` (a small flag byte distinct from the larger flag word at `vreg+48`).

```
                           vreg descriptor (per-register record)
  offset            bit 7  6  5  4  3  2  1  0
   +0      …  intrusive list/next pointer
   +8      …  parent allocation chain
  +36      …  coalesced-parent alias (used by allocator)
  +48      …  flag word (32 bits)
                              ↑
                              bit 3 = "address operand of this load is varying"
                                      (uniform-atomic shortcut replica)
                              bits 20..21 = pair mode (single / pair / quad slot)

  +49      …  small flag byte (8 bits)
                  bit 7 .. 3                       bit 2                       bit 1 .. 0
                  ────────                         ──────                      ──────────
                  reserved / class-specific        ★ VARYING (set by           "no-promote"
                                                     OriPropagateVarying)        legacy flags

  +64      …  reg_type (allocator class, 0..6)
  +72      1B physical_size byte (0 = no physical slot)
  +73      1B alloc_status (& 0x10 checked for "allocated")
```

The bit position is referenced explicitly by every downstream consumer:

| Consumer | How it reads bit 2 |
|---|---|
| `ConvertToUniformReg` core (`sub_911030`) | `if (*(uint8_t*)(vreg+49) & 0x04) skip` — vreg is not UR-eligible |
| `OptimizeUniformAtomic::sub_893100` | Reads `vreg+48` bit 3 instead (operand-replica fast path) |
| Scheduler dep builder (`sub_A0D800`) | Reads bit 2 to decide whether a use creates a uniform-datapath dependency |
| Predication eligibility (`sub_1381010` inner loop) | Reads bit 2 of every live-out vreg before allowing if-conversion |
| LICM late (`OriHoistInvariantsLate`) | Combines bit 2 with the `context+1392` post-predication speculation-safe set |

## Inter-Procedural Walk

`sub_90E3F0` implements the inter-procedural step. ptxas does not have a SSA function-summary infrastructure like LLVM's IPSCCP; the inter-procedural propagator is hand-rolled and uses FNV-1a hash tables keyed on the callee's vreg index to look up two parallel state structures:

```c
// sub_90E3F0 — distilled
bool propagate_to_callees(CodeObject *fn, uint32_t *vreg_idx_ptr) {
    uint32_t v = *vreg_idx_ptr;
    // FNV-1a, 32-bit, big-endian byte order:
    //   h = 0x811C9DC5
    //   for each byte b of v (most-significant first):
    //     h = (h ^ b) * 0x01000193
    uint32_t h = 0x811C9DC5u;
    h = (h ^ ((uint8_t)(v))) * 0x01000193u;
    h = (h ^ ((uint8_t)(v >> 8))) * 0x01000193u;
    h = (h ^ ((uint8_t)(v >> 16))) * 0x01000193u;
    h = (h ^ ((uint8_t)(v >> 24))) * 0x01000193u;

    // Two parallel hash tables: forward edges at +288, reverse edges at +328
    callee_record *fwd = lookup(*(table_ptr*)(fn + 296), (table_size(fn+304) - 1) & h, v);
    callee_record *rev = lookup(*(table_ptr*)(fn + 336), (table_size(fn+344) - 1) & h, v);

    // Pull the per-callee state, then call sub_90C180 to merge varying status
    bool changed = sub_90C180(callee_state_at(fn, v), &accumulator);
    if (rev) sub_907A00(&accumulator, rev_state(rev));
    if (fwd) sub_90DBD0(&accumulator, fwd_state(fwd));
    return changed;
}
```

Constants `0x811C9DC5` and `0x01000193` (FNV-1a 32-bit prime and offset basis) are recovered verbatim from `sub_90E3F0` and appear at `0x90e5da..0x90e5cd` in the binary. The big-endian byte order is unusual for FNV — standard implementations process bytes little-endian — and is a ptxas-specific quirk introduced because the callee-vreg index is stored in the IR as a big-endian-packed 32-bit field for compatibility with the Mercury encoder's operand layout. Confidence: **MED** (constants and order are HIGH; the rationale is reconstructed from the surrounding Mercury-encoder hash collisions that would arise under little-endian byte order, and is not directly stated in any string).

> ⚡ **QUIRK — FNV-1a with big-endian byte stream**
> The classical FNV-1a algorithm processes bytes in memory order. `sub_90E3F0` instead extracts bytes most-significant-first from a 32-bit integer (`HIBYTE(v) ^ ... ^ BYTE2(v) ^ ... ^ BYTE1(v) ^ ... ^ (uint8_t)v`). The hash is *still* a valid hash, but it is incompatible with any external FNV-1a output you might want to compare against. If you re-implement this analysis externally and use a stock FNV-1a, your hashes will not match ptxas's internal table layout — relevant only if you are reading per-callee state from a dumped IR snapshot.

## Pipeline Position and Consumer Map

Both runs land inside the partial-SSA window (phases 23–73). Outside that window, `MovPhi` is gone (phase 73 destroys SSA) and the merge-across-divergent-branch rule cannot be expressed, so varying analysis after phase 73 would be a different algorithm. The phase-70 result is therefore the **last** opportunity to compute divergence; everything that consumes the flag downstream of phase 73 either reads the phase-70 snapshot directly or relies on a coarser approximation.

| Consumer | Phase | Reads | What changes if the bit is wrong |
|---|---:|---|---|
| `OriDoRematEarly` | 54 | phase-53 | Wasted spills (UR-eligible value gets rematerialized into R file) |
| `SpeculativeHoistComInsts` | 56 | phase-53 | Unsafe hoist past a divergent guard |
| `OriDoPredication` | 63 | phase-53 | If-conversion picks the wrong merged operand for a live-out |
| `OriHoistInvariantsLate` | 66 | phase-53 (early) + `context+1392` (post-predication) | Hoists a load whose address became varying after predication |
| `OriDoRemat` | 69 | phase-53 | Same as 54, but on the second remat pass |
| `OptimizeSyncInstructions` | 71 | phase-70 | Removes a barrier whose all-thread arrival was *not* uniform |
| `ConvertToUniformReg` | 74 | phase-70 | Promotes a divergent register to UR (correctness bug, hardware fault) |
| `InsertPseudoUseDefForConvUR` | 86 | phase-70 (via UR file) | Wrong pseudo-use placement around convergent calls |
| `ConvertMemoryToRegisterOrUniform` | (out-of-band) | phase-70 | Promotes a stack slot to UR when it should be R |
| `OriBranchOpt` | 12 | phase-53 (via earlier seed propagation in `AnalyzeUniformsForSpeculation`) | Misses `UBRA` opportunity or, worse, emits `UBRA` for a divergent branch |
| Scheduler `sub_A0D800` | 97+ | phase-70 (latest) | Wrong dependency tracking for uniform-datapath operands |

## Cross-Reference: `AnalyzeUniformsForSpeculation` (Phase 27)

Phase 27 (`AnalyzeUniformsForSpeculation`) is **not** the same analysis. The phase-27 dataflow tracks *speculation safety of constant-bank loads* — whether `LDC c[bank][offset]` with a uniform address can be hoisted/sunk across a control-flow boundary without changing program semantics. Its output is a context flag (`context+1392` bit 0) and a hash set of surface/tensor loads that survived predication. It does **not** write `vreg+49` bit 2, and the binary confirms it (a 2026-04-16 correction in `passes/uniform-regs.md` retracted an earlier mis-attribution).

The two analyses interact: `OriHoistInvariantsLate` (phase 66) combines the phase-70 varying flag with the phase-27 speculation-safe set when deciding whether to hoist a load above its guard. Either condition can veto the hoist independently.

## Worked Example

Consider this fragment after lowering (registers are virtual, before allocation):

```
B0:  R10 = S2R SR_TID_X            // seed: R10 varying
     R11 = MOV.IMM 1024             // R11 uniform
     R12 = IMUL.WIDE R10, 4         // R10 varying → R12 varying
     R13 = LDC c[0][0x28]           // kernel param → R13 uniform
     R14 = IADD R12, R13            // R12 varying → R14 varying
     P0  = ISETP.GE R14, R11        // P0 varying
          @P0 BRA B2

B1:  R15 = MOV.IMM 7                // R15 uniform
     BRA B3

B2:  R16 = SHFL.IDX R14, 0          // seed: R16 varying (SHFL)

B3:  R17 = MOV.PHI [R15 from B1], [R16 from B2]
                                    // merge across divergent branch (P0 is varying)
                                    //   → R17 varying even though one source (R15) is uniform
```

Result of phase 53/70 on this fragment:

| vreg | varying? | Why |
|---|:---:|---|
| R10 | yes | Seed (`SR_TID_X`) |
| R11 | no | Constant immediate |
| R12 | yes | Propagated from R10 |
| R13 | no | Constant memory load |
| R14 | yes | Propagated from R12 |
| P0 | yes | Propagated from R14 (predicate is also a register class) |
| R15 | no | Constant immediate |
| R16 | yes | Seed (`SHFL`) |
| R17 | yes | `MovPhi` merge across a divergent branch — **always** varying, regardless of source uniformity |

After phase 63 if-converts the branch (if the region is small enough), `MovPhi` becomes a predicated `SEL.@P0 R17, R16, R15`. Phase 70 re-runs and reaches a structurally identical conclusion: R17 sources R16 (varying), so R17 stays varying. But if predication had failed (region too large or live-out conflicts), and the branch survived as a true CFG edge, then phase 70 would still mark R17 varying via the same MovPhi rule. The two-run design ensures the answer is *correct* under either fate of the branch.

## Knobs and Gating

Varying propagation is mandatory whenever UR-aware codegen is reachable. The phase-manager `isNoOp` check returns 0 unconditionally for both phases 53 and 70 on every SM target sm_70 and up (vtable slot +16 at the `OriPropagateVarying` vtables is a `return 0` stub). Gating instead happens through downstream consumers — disabling `ConvertToUniformReg` via knob 487 or knob 687 mode 0 makes the varying analysis still run but renders its main output unused.

The following knobs touch the consumers and therefore indirectly affect how much the analysis's output matters:

| Knob | Effect on varying-analysis utility |
|---|---|
| 487 | Master gate for `ConvertToUniformReg`; off → analysis still runs but UR file is empty |
| 628 | Pre-allocation UR promotion (`alloc+440`); cooperates with phase 74 |
| 687 | Uniform-register mode selector; mode 0 disables UR entirely |
| 510 | `OptimizeUniformAtomicMode` — atomic rewrites need the `vreg+48` bit 3 replica |

The pass itself has no documented disable knob. The string `"OriPropagateVarying"` does not appear in any `EnableXxx`/`DisableXxx` knob name, only in the static phase-name table at `off_22BD0C0`.

## Verification Anchors

| Claim | Anchor in raw data |
|---|---|
| Bit 2 of `vreg+49` is the varying flag | `passes/uniform-regs.md` correction note (2026-04-16); read sites in `sub_911030`, `sub_A0D800`, `sub_1381010` |
| Algorithm is iterative fixed-point, not single forward pass | `sub_90E620` outer `do {} while (worklist)` with `_BitScanReverse64` worklist drain (`0x90e9d8`, `0x90ed47`) |
| Inter-procedural propagation uses FNV-1a 32-bit | `sub_90E3F0` constants `0x811C9DC5` (offset basis), `0x01000193` (prime) at `0x90e5da..0x90e5cd` |
| Two pipeline positions (53, 70) | Static name table `off_22BD0C0`; `passes/index.md` rows 482 and 499 |
| Phase 70 re-seeds from scratch | `sub_900020` clears `vreg+49` bit 2 unconditionally on entry |
| `VOTE.BALLOT` is uniform; `SHFL` is varying | Seed table in `sub_900020` (verified against PTX special-register strings in `ptxas_strings.json`) |
| Bit 3 of `vreg+48` is the operand-replica | `sub_90C180` writes it in the load-address path; `sub_893100` (atomic uniformity test) reads it |
| Worklist uses packed 64-bit bitvector | `sub_90E620` stack slot `v55..v55[7]` (eight 64-bit words = 512 vregs per slab) |

## Related Pages

- [Uniform Register Optimization](uniform-regs.md) — phase 74 `ConvertToUniformReg`, the primary consumer of the phase-70 result
- [Predication](predication.md) — phase 63, between the two runs; predication changes which `MovPhi` merges remain
- [Rematerialization](rematerialization.md) — phases 54 and 69, both gated on uniform-vs-varying classification
- [Copy Propagation & CSE](copy-prop-cse.md) — GvnCse honors divergence boundaries
- [Branch & Switch Optimization](branch-switch.md) — `UBRA` (uniform branch) requires a uniform predicate
- [Synchronization & Barriers](sync-barriers.md) — uniform-predicate barrier elision
- [Register Model](../ir/registers.md) — vreg descriptor layout, register classes (R/UR/P/UP)
- [IR Overview](../ir/overview.md) — partial-SSA window (phases 23–73), `MovPhi` representation
- [Pass Inventory](index.md) — full 159-phase table with binary-to-wiki index translation
- [Optimization Pipeline](../pipeline/optimizer.md) — phase 53 and 70 positions in the master ordering
