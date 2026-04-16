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
| **Phase index** | 27 (binary index 30; vtable `off_22BDA00`) |
| **Pipeline position** | Stage 2 (Early Optimization), after `OriRemoveRedundantBarriers` (26), before `SinkRemat` (28) |
| **Category** | Analysis (read-only -- annotates constant load speculation safety, does not transform instructions) |
| **String reference** | `"AnalyzeUniformsForSpeculation"` at `0x22BC647` |

### Purpose

> **Correction (2026-04-16).** The previous version of this section described phase 27 as a per-register uniformity marker that sets `vreg+49` bit 2. Cross-reference analysis of the binary revealed that description was incorrect. The per-register varying flag at `vreg+49` bit 2 is set by `OriPropagateVarying` (phases 53 and 70), not by this pass. The previous description was admittedly reconstructed from consumer passes without decompiling the execute body; this revision corrects the misattribution.

Analysis pass that examines constant bank (`c[]`) accesses and determines which constant memory loads can be safely speculated -- that is, hoisted or sunk across control-flow boundaries without changing program semantics. The pass name `AnalyzeUniformsForSpeculation` refers to the *speculation safety of uniform-address constant loads*, not to per-register divergence classification.

Constant memory loads via `LDC c[bank][offset]` are pure (no side effects) when the address is uniform, but hoisting them above a branch may introduce a load on a path that the original program never executed. This is benign for constant memory (the load cannot fault and the value is fixed), but the compiler must still confirm that the address expression is safe to evaluate speculatively. Phase 27 performs this confirmation and records the result so that downstream passes know which `LDC` instructions (and their dependent computations) may be freely moved across control flow.

### Consumers

Three later passes read the speculation-safety annotations produced by this pass:

1. **`SinkRemat` (phase 28)** -- the core sink+remat driver `sub_A0F020` calls `sub_A107B0` per definition. When a constant load has been flagged as speculation-safe, SinkRemat may sink it past a divergent branch and insert a rematerialized copy near the use, because the load is guaranteed side-effect-free.

2. **`SpeculativeHoistComInsts` (phase 56)** -- hoists common sub-expressions above divergent branches when operands are speculation-safe. For constant bank loads, the phase 27 annotation confirms that the `LDC` can be moved above the branch without introducing a fault or observably wrong value.

3. **`Predication` (phase 63)** -- the profitability scanner at `sub_1380190` checks `state->has_uniform_speculation` before scoring candidates. When set, the predication pass verifies that constant-load operands in the if-conversion region remain safe to speculatively execute after branch elimination.

Branch optimization also benefits: when a branch condition depends only on speculation-safe constant loads, it qualifies for `UBRA` encoding on sm_75+ and can eliminate `BSSY`/`BSYNC` pairs.

### What This Pass Does NOT Do

This pass does **not** set the per-register varying/uniform flag at bit 2 of `vreg+49`. That flag is the divergence marker set by `OriPropagateVarying` (phases 53 and 70), which performs a forward dataflow walk seeding divergence from thread-identity registers (`SR_TID_X/Y/Z`, `SR_LANEID`) and propagating it through the def-use chain. See the [Varying Propagation](#varying-propagation-supporting-analysis) section below for the algorithm.

The distinction matters: a value can be uniform (not varying) yet still unsafe to speculate if it involves a memory access whose address, while uniform, might trigger unwanted side effects on the wrong path. Conversely, a constant bank load is always speculation-safe regardless of the control-flow path, because constant memory is read-only and cannot fault.

### Interaction with Post-Predication Safety

Phase 27 operates before predication; a separate, narrower mechanism tracks speculation safety *after* predication. `sub_137EE50` (called from phase 63) scans predicated code for loads to `.surf`/tensor extended memory (category 18) and records them in a hash set at `state+240`, setting `context+1392` bit 0. This flag persists through later passes and is checked by `OriHoistInvariantsLate` (phase 66) to prevent hoisting those loads. The two mechanisms are complementary: phase 27 covers the pre-predication window for constant bank accesses, `context+1392` covers the post-predication residual for surface/tensor loads.

### Evidence Note

The execute body of this phase was not decompiled; the function at vtable `off_22BDA00` slot +0 has not been traced. The original description was reconstructed from (a) the phase name table, (b) `vreg+49` bit 2 usage in consumer passes, and (c) the predication pass's `has_uniform_speculation` field. That reconstruction incorrectly attributed the `vreg+49` bit 2 divergence flag to this pass. Subsequent cross-reference analysis of the binary established that `vreg+49` bit 2 is set exclusively by `OriPropagateVarying` (phases 53/70), and that the phase name "AnalyzeUniformsForSpeculation" refers to constant bank access speculation safety rather than per-register uniformity classification. The internal algorithm (worklist vs. single-pass, lattice width, convergence guarantee) remains unknown pending decompilation of the execute body.

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

### Conversion Criteria (5-Way Simultaneous Check)

The eligibility check (`sub_90C010`, 456 bytes) tests all five criteria simultaneously per instruction. A value qualifies for R-to-UR conversion when all hold:

1. **R-file source operand.** The operand word at `inst+84+8*idx` must have type field `(operand>>28) & 7 == 1` (R register) and byte +7 high bit clear (not special/fixed). Checked first -- if the operand is already UR or not R, the function returns 0 immediately.

2. **UR-expressible opcode.** The opcode (masked to `opcode & 0xFFFFCFFF` to strip modifier bits) must match the 64-bit bitmask `0x2080000010000001` shifted by base 22, selecting exactly four opcode classes: **IADD3** (22), **PRMT** (50), **SEL** (77), **SGXT** (83). Opcodes **297** and **352** (VOTEU and a predicate variant) pass via explicit equality checks. MOV (164) is explicitly *rejected* in `sub_90B790`, returning cost 1 (bridge-only).

3. **All consumers accept UR sources.** `sub_90B790` (1,720 bytes) performs a BFS through the use-chain. For each consumer, it checks whether the source slot accepts UR encoding. Instructions with `inst+48 == 0` (previously scored non-promotable) terminate with cost 0. The visited set uses FNV-1a hashing (prime `0x01000193`, basis `0x811C9DC5`) on `vreg+16`. For UR-eligible consumers, the cost increments by 1 if the consumer's program point (`inst+52`) precedes the definition's, reflecting a required `R2UR` bridge.

4. **No excluded operand categories.** `sub_90B790` calls `sub_7DF3A0` for the operand category byte. If bits 2-3 are set (`category & 0xC != 0`), conversion proceeds only if the kernel name contains `"cutlass"` (via `sub_8F47E0`: `strstr(kernel_name, "cutlass")`), providing a cutlass-specific UR promotion override.

5. **Bridge placement feasibility.** For 3-source instructions like SEL (77), each source gets an independent cost via `sub_7E36C0` (parametrized by position 0/1/2, plus modifier flags from `(operand>>4)&7`, `(operand>>11)&3`). The per-source costs feed three independent scoring trees. A candidate is rejected if any source's cost evaluates to 0.

### Algorithm

**Outer driver** (`sub_913A30`, 828 bytes) manages the retry loop. It iterates up to `max_attempts` times (default 5; overridden by knob 862 when `options+62064` is set). Each iteration calls `sub_911030` with the current attempt index; if the core returns false or `sub_8F59C0` detects no rearrangeable BST entries remain, the loop breaks early.

**Core function** (`sub_911030`, 10,741 bytes) operates in a single combined pass over the RPO block ordering stored in `codeobj+99`:

1. **Block numbering.** Assigns sequence IDs (`inst+52 = seqid++`, `inst+48 = -1`) to all instructions.

2. **Per-instruction scan.** For each instruction in RPO order, the opcode is tested against the eligible set. If eligible and the attempt index matches the retry counter (`a2 == v325`), the pass allocates four BSTs (via arena pools at `state+16`, `state+24`) for per-source-operand candidates. Each BST node is 40 bytes (`{left, right, parent, inst_ptr, flag}`) keyed by `inst+52`. It calls `sub_90C010` for source operands 1, 2, and optionally 3. On success, the instruction is inserted into a per-cost-class FNV-1a hash map (`state+48`, 8 initial buckets, grows 4x when `load > bucket_count/2`). Each bucket record is 64 bytes: `{next, cost_key, bst_root, bst_min, bst_max, count, hash, pool_ptr}`.

3. **Bridge placement.** After scoring, `sub_8FD920` extracts the minimum-cost entry from each BST. The entries become `R2UR` / `UR2R` bridge insertion points, placed immediately before the first consumer requiring a different register file.

4. **Conversion commit.** `sub_7E5060` (268 bytes) performs the R-to-UR flip. It verifies `ctx+1368` bit 4 is set (UR-capable target), walks the use-list checking `block+120 == 2` (depth) and single-use chain, then calls `sub_74D720` to rewrite the register file type.

### UR Pressure Management

The pass manages UR pressure via a greedy priority scheme with three independent BST-ordered candidate pools (one per source operand position):

- **Priority function.** Each candidate receives an integer cost from `sub_90B790`. The cost equals the number of consumers requiring bridge instructions, plus 1 for each consumer whose program point precedes the definition (backward reference needing an extra `R2UR`). Cost 0 = not promotable. The BST minimum (fewest bridges) is processed first.

- **Greedy extraction.** The driver iterates the BST in min-first order via `sub_902AB0` (red-black tree rebalance). Each extracted candidate is committed and removed. The hash map tracks which cost classes have been fully consumed.

- **Three-pool coordination.** For 3-input instructions, all three source positions must independently succeed. If source 1 passes but source 2 fails (`sub_90C010` returns 0), `inst+48` is set to the partial cost and the pass moves to the next candidate without committing.

- **Retry via flag at `ctx+1381` bit 6.** The outer driver checks `ctx+1381 & 0x40`. When set, it enters the retry loop (up to knob 862 iterations). Each retry re-initializes pass state via `sub_8F5220` (zeros all four BSTs and hash maps), calls `sub_8F5AD0` to reset the block cursor, and `sub_909A20` to clear candidate data. The retry counter `a2` is compared against `v325` (successful conversions so far); only instructions whose block index matches the current attempt are processed, providing round-robin pressure distribution across the function.

- **Early termination.** `sub_8F59C0` scans the BSTs (indexed 2..`state+80+1`) and returns 1 (bail out) if all remaining candidates have been rejected. The driver breaks when this returns true or the core returns false.

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

### Purpose and DCE Interaction

Phase 84 (`OriPerformLiveDeadFourth`) runs DCE between conversion (phase 74) and this pass. If a UR-converted value has no remaining R-file uses (because they were also converted), DCE would kill it. Phase 86 inserts CONV.ALLOC pseudo-instructions (opcode 286 / `0x11E`) that create artificial def-use links, keeping UR definitions alive through register allocation (phase 101). These pseudo-instructions have no hardware encoding and are removed before SASS emission.

### CONV.ALLOC Insertion Algorithm (`sub_19D7A70`)

The pass iterates every basic block, skipping blocks with null instruction lists or the non-schedulable flag (`block[281] & 0x08`). For each block it resolves the owning BB via the register-to-BB lookup array (`ctx+296`) and walks the successor chain. The walk stops early when `allow_reorder` is set and a reorder barrier is reached (`bb[245] != 0`).

**Convergent boundary scan.** Within each BB, the pass scans for CONV boundary markers -- instructions whose opcode after masking (`opcode & ~0x3000`) equals 27 (CS2R). A per-block bitmask (`live_mask`, sized `ceil(reg_count/64)` words) tracks which block IDs have been seen inside a convergent region. If the block's guard bit (`block.qword280 & 1`) is set, the bitmask bit is cleared instead of set.

**Eligibility check.** After reaching a use through the successor chain, the pass examines the defining instruction. If that definition is already a CONV.ALLOC (opcode 286), it is skipped. Otherwise, a multi-case opcode switch determines whether the use requires a convergent boundary. The switch tests modifier bits per opcode:

| Opcode | Modifier bit tested | Meaning |
|---|---|---|
| 18 (MOV) | bit 14, or bit 12 (alternate path) | Uniform move variant |
| 119 (IADD3) | bit 3, or bit 5 (alternate) | Uniform integer add |
| 186 (LD) | bit 7, or bit 6 (alternate) | Uniform load |
| 211 (MUFU) | bit 4, or bit 6 (alternate) | Uniform math function |
| 283 (PRMT) | bit 5, or bit 7 (alternate) | Uniform permute |
| 302 (SHF) | bit 3 | Shift funnel |
| 307 (FMA) | bit 1 | Fused multiply-add |
| 315 (SETMAXNREG) | bit 2 (inverted) of field at `nops` offset | Register count adjustment |
| 320 (LOP3) | bit 19 | 3-input logic |

For opcodes with the `0x1000` flag set, the pass falls through to a secondary check using the instruction info table (`ctx+776`), testing bit 2 of the 4-byte record at `info[opcode*4+2]`.

**Insertion.** When eligible, the pass builds a 2-operand CONV.ALLOC instruction via `sub_9314F0(buf, ctx, 0x11E, size=12, nops=2, ops)`. Operand 0 is the use register with tag `0x90000000` (UR class marker). Operand 1 is a fresh virtual register from `sub_91D160(ctx, -1)`. The new instruction is inserted before the use-site, and the parent block is marked with convergent flag 17. The pass returns the total insertion count.

### Convergent Validation (`sub_19D13F0`)

Before inserting markers, `sub_19D13F0` validates convergent boundaries around function calls annotated with `allowConvAlloc`. It walks the BB chain from the call instruction's defining BB, using the same successor resolution (field +72 == 97 for direct successor lookup, otherwise indirect via field +8). If `sub_75A300` reports a reachable call that lacks a convergent boundary (null at `+64` or `byte[+40] == 0`), the pass emits error 7020: `"Missing proper convergent boundary around func call annotated with allowConvAlloc"`.

The single-call checker (`sub_19C6400`) fires when walking the convergent region encounters opcode 159 (barrier) a second time, emitting error 7021: `"Multiple functions calls within the allowConvAlloc convergent boundary"`. This prevents the register allocator from assigning the same physical UR across a boundary where it could be redefined by a callee.

## Varying Propagation (Supporting Analysis)

The `OriPropagateVarying` passes (phases 53 and 70) propagate divergence information forward through the IR. They are not part of the four-pass uniform register group, but provide the critical input data: the varying flag at bit 2 of `vreg+49` that `ConvertToUniformReg` checks before promoting a register to UR.

### Algorithm

Both passes execute the same forward dataflow procedure. The analysis is an **iterative fixed-point loop**, not a single forward pass. Although the Ori IR is in partial-SSA form (phases 23--73) where intra-procedural def-use ordering is trivially satisfied by forward program order, inter-procedural divergence propagation requires re-iteration: when a function called on a divergent path is newly marked varying, the varying status must propagate through that callee's register definitions, which may in turn affect other call sites. The loop terminates when no register's varying status changes during a complete iteration.

Binary analysis of `sub_90E620` (1,919 bytes, called from `sub_90EDA0`) confirms this structure. The function contains an outer `do { ... } while (worklist)` loop driven by a bitvector of pending registers. Within the loop body, `sub_90C180` (2,093 bytes) propagates varying status to each register and returns a non-zero changed flag when the status was updated. When changes are detected and the affected register belongs to a callee function (checked via the call-graph edge list at `codeobj+128`), `sub_90E3F0` resolves the callee's divergence through FNV-1a hash lookups on the function-local state at offsets `+288`/`+328`. If the callee function itself was newly marked varying (comparing the callee's function record against the changed record), the loop restarts from the beginning via `goto LABEL_24`, re-processing all pending registers with the updated information.

```
PropagateVarying(code_object):
  // Step 1 -- seed: clear all flags, then mark intrinsic divergence roots
  for each vreg: clear bit 2 of vreg+49
  for each vreg defined by a divergent source:       // SR_TID_X/Y/Z,
    set bit 2 of vreg+49                             // SR_LANEID, SHFL,
    // also: LDG/LDS/LDL with varying base, VOTE, per-thread atomics

  // Step 2 -- iterative fixed-point over call graph + RPO walk
  do {
    Changed = false
    for each function in reverse-postorder call-graph traversal:
      for each basic_block in RPO order:
        for each instruction in block (forward):
          if instruction is MovPhi:
            // Divergent if any source is varying OR the merge follows
            // a divergent branch (even with all-uniform incoming values)
            if any source vreg has bit 2 set, or merge crosses divergent edge:
              if bit 2 of dest_vreg+49 not already set:
                set bit 2 of dest_vreg+49
                Changed = true
          else:
            for each source register operand:
              if bit 2 of src_vreg+49 is set:
                if bit 2 of dest_vreg+49 not already set:
                  set bit 2 of dest_vreg+49
                  Changed = true
                break   // one varying source suffices
      // inter-procedural: propagate varying through callee edges
      for each callee reachable from varying call sites:
        if callee's registers updated:
          Changed = true   // forces re-iteration
  } while (Changed)
```

Registers that remain with bit 2 clear after convergence are proven uniform and eligible for UR promotion.

### Pipeline Position

**Phase 53 (`OriPropagateVaryingFirst`)** runs after `OriReplaceEquivMultiDefMov` (phase 52) and before `OriDoRematEarly` (phase 54). This is the first divergence snapshot -- it feeds early rematerialization (54) and speculative hoisting (56), both of which need to know whether a value is uniform before deciding to duplicate or move it.

**Phase 70 (`OriPropagateVaryingSecond`)** runs after `OriDoRemat` (phase 69) and before `OptimizeSyncInstructions` (phase 71). It recomputes divergence because predication (phase 63) may have converted divergent branches into predicated straight-line code, changing which `MovPhi` nodes merge across divergent edges, and rematerialization (69) may have introduced new definitions. The refreshed annotations are the ones that `ConvertToUniformReg` (phase 74) consumes.

## Uniform Atomic Optimization (Phase 44)

`OptimizeUniformAtomic` (phase 44, `sub_893D30`) is a mid-pipeline pass that rewrites thread-uniform atomic operations into cheaper forms. The execute body is gated by `ctx+1045` bit 2 and knob 510 (`OptimizeUniformAtomicMode`, int32 mode selector at options offset `0x22B0`). When knob 510 equals 1 the pass runs unconditionally; when unset, it still runs if `codeobj+1412` bit 5 is set (compiler-determined flag). A secondary iteration-count gate reads knob 487 through the profile vtable.

**Algorithm.** The pass calls `sub_781F80` (reset liveness) and `sub_7E6090` (recompute single-def info), then walks every instruction in the function linearly via the linked list at `codeobj+272`. For each instruction the masked opcode (`instr+72`, with bits 12-13 cleared) selects one of three actions:

1. **Base-address tracking (opcode 97 / STG).** `sub_892F50` records the address register of the store into the pass-local state. Subsequent atomics targeting the same address inherit this base, enabling the uniformity check without a full reaching-definition analysis.

2. **Atomic candidate match (opcodes 228, 16 after mask).** `sub_893100` (12 KB) performs the eligibility test. It rejects the instruction if: (a) the operand type is not a supported memory width (type 12 = 32-bit, types 9--11 = 64/128-bit; type 6 = scope-qualified is accepted only when `codeobj+1397` bit 5 is set), (b) the address operand carries the varying flag (bit 3 of `vreg+48`), or (c) a CAS-ordered operand is present (bit 20 of the last operand word). After passing these filters, the function extracts the **reduction operation type** from operand bits `[8:4]` (for opcode 16) or `[8:5]` (for opcode 228) and dispatches through a switch:

| Case | Op   | Replacement strategy |
|------|------|----------------------|
| 0    | ADD  | Full coalescing -- `sub_88FC40` or `sub_88F810` depending on return-value liveness |
| 3    | MIN  | Warp-reduce to single lane via `sub_891280` |
| 4    | MAX  | Same as MIN path |
| 7    | AND  | Bitwise warp-reduce |
| 8    | OR   | Bitwise warp-reduce |
| 9    | XOR  | Bitwise warp-reduce |

For ADD with a uniform address on sm_80+ (`codeobj+1398` bit 2 and `ctx+1045` bit 1 both set, operand types 11--12), `sub_892420` emits `ATOM.UNIFORM` directly. Otherwise the general path in `sub_88FC40`/`sub_890C90` constructs an ELECT + REDUX + conditional-ATOM sequence: elect one lane, perform a warp-level REDUX to combine the per-thread values, then execute a single ATOM from the elected lane and broadcast the result.

3. **All other opcodes** are skipped.

If any instruction was rewritten, `sub_785E20` marks the function as changed so downstream passes re-run.

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
| `sub_913A30` | 828 B | Phase 74 outer driver | Retry loop (up to knob 862 iterations), calls `sub_911030` per attempt |
| `sub_90C010` | 456 B | 5-way eligibility check | Tests R-file type, opcode bitmask, consumer acceptance per operand |
| `sub_90B790` | 1.7 KB | BFS consumer cost scorer | Walks use-chain, computes bridge cost via FNV-1a visited set |
| `sub_7E5060` | 268 B | R-to-UR conversion commit | Verifies `ctx+1368` bit 4, calls `sub_74D720` to flip register file |
| `sub_8F5220` | 328 B | Pass state initializer | Zeros 4 BSTs, hash maps, and flags at state+32..+200 |
| `sub_8F59C0` | 400 B | Early termination check | Returns 1 if all BST candidates rejected; breaks retry loop |
| `sub_8F47E0` | 80 B | Cutlass kernel detector | `strstr(kernel_name, "cutlass")` -- enables UR override for cutlass |
| `sub_902AB0` | small | Red-black tree rebalance | Maintains BST ordering for min-first candidate extraction |
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
