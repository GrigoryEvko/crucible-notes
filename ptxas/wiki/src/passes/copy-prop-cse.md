# Copy Propagation & CSE

Copy propagation and common subexpression elimination in ptxas are spread across four dedicated pipeline phases (49, 50, 64, 83) plus a forward copy propagation sub-pass (`OriCopyProp`) embedded inside every [GeneralOptimize](general-optimize.md) bundle. Together these passes form the value-redundancy elimination subsystem: they detect computations that produce values already available elsewhere in the program, then eliminate the redundant instructions or replace them with cheaper copies.

The four dedicated phases run at specific pipeline positions chosen to exploit opportunities created by preceding transformations. GvnCse (phase 49) runs after mid-level expansion and argument enforcement when the IR is maximally normalized. OriReassociateAndCommon (phase 50) immediately follows GvnCse to catch near-misses through algebraic normalization. LateOriCommoning (phase 64) runs after predication (phase 63) converts branches into predicated instructions, exposing new redundancies. OriBackCopyPropagate (phase 83) runs late in the pipeline to shorten MOV chains before register allocation.

| | |
|---|---|
| **Phases covered** | 49 (GvnCse), 50 (OriReassociateAndCommon), 64 (LateOriCommoning), 83 (OriBackCopyPropagate) |
| **Forward copy prop** | `OriCopyProp` sub-pass inside each GeneralOptimize bundle (phases 13, 29, 37, 46, 58, 65) |
| **Related knobs** | 22 knobs controlling budgets, modes, and enable/disable flags |
| **Pipeline position** | Mid-optimization (49--50), post-predication (64), pre-regalloc legalization (83) |
| **Prerequisite passes** | AnalyzeControlFlow (3), GeneralOptimizeMid2 (46), EnforceArgumentRestrictions (48) |
| **Downstream consumers** | ExtractShaderConstsFinal (51), OriDoPredication (63), register allocation (101) |

## Phase Summary Table

| Phase | Name | Vtable | execute | getName | isNoOp | Default |
|---|---|---|---|---|---|---|
| 49 | `GvnCse` | `off_22BDD70` | `0xC5F000` (thunk) | `0xC5F010` (ret 49) | `0xC5F020` (ret 0) | Enabled |
| 50 | `OriReassociateAndCommon` | `off_22BDD98` | `sub_C604D0` | `0xC5EFE0` (ret 50) | `0xC5EFF0` (ret 0) | Enabled |
| 64 | `LateOriCommoning` | `off_22BDFC8` | `sub_C60020` | `0xC5EDF0` (ret 64) | `0xC5EE00` (ret 0) | Enabled |
| 83 | `OriBackCopyPropagate` | `off_22BE2C0` | `sub_C5EB80` | `0xC5EB90` (ret 83) | `0xC5EBA0` (ret 1) | **Disabled** |

Phase name strings (from static name table at `off_22BD0C0`, verified in `ptxas_strings.json`):

| Phase | String Address | Name Table Ref |
|---|---|---|
| 49 | `0x22BC80C` | `0x22BD280` |
| 50 | `0x22BC813` | `0x22BD290` |
| 64 | `0x22BC949` | `0x22BD310` |
| 83 | `0x22BCAE5` | `0x22BD3C8` |

All four vtables are laid out at uniform 0x28-byte (40-byte) spacing in `.data.rel.ro`, matching the 5-pointer-per-vtable pattern used by all 159 phases. The factory switch at `sub_C60D30` allocates each phase as a 16-byte object and installs the corresponding vtable pointer.

Phase 83 is disabled by default (`isNoOp` returns 1). It is activated through the `AdvancedPhaseBackPropVReg` gate (phase 82), which architecture-specific backends override to enable backward copy propagation for their target.

---

## Phase 49 -- GvnCse (Global Value Numbering + CSE)

### Overview

GvnCse combines global value numbering (GVN) with common subexpression elimination (CSE) in a single pass. GVN assigns a canonical "value number" to every expression in the program such that two expressions with the same value number are guaranteed to compute the same result. CSE then uses these value numbers to detect and eliminate redundant computations.

The pass is gated by the `EnableGvnCse` knob (address `0x21BDA50`). When disabled, the pass is skipped entirely.

### Dispatch Mechanism

The execute function at `0xC5F000` is a 16-byte thunk:

```asm
mov  rdi, [rsi+0x630]     ; rdi = compilation_context->sm_backend
mov  rax, [rdi]            ; rax = sm_backend->vtable
jmp  [rax+0xB8]            ; tail-call vtable[23] -- the actual GVN-CSE implementation
```

The real implementation lives in the compilation context's SM backend object (at context+`0x630` / +1584), dispatched through its vtable at offset `0xB8` (slot 23). This indirection means the GVN-CSE algorithm can be overridden by architecture-specific backends that provide a different SM backend vtable. (This object was previously called "optimizer_state" on this page, but it is the same polymorphic SM backend used for legalization, scheduling, and all other architecture-dependent dispatch -- see [data-structures.md](../ir/data-structures.md#sm-backend-object-at-1584).)

### Algorithm (Reconstructed)

The ptxas GVN-CSE operates on the Ori IR basic block list with dominator-tree-guided traversal:

```
procedure GvnCse(function F):
    build dominator tree DT for F
    initialize value_table: hash_map<expression_key, value_number>
    vn_counter = 0

    for each block B in RPO(DT):
        for each instruction I in B:
            key = canonicalize(I.opcode, I.type, [lookup_vn(op) for op in I.operands])
            if key in value_table:
                existing_vn = value_table[key]
                replace all uses of I.dest with representative(existing_vn)
                mark I as dead
            else:
                value_table[key] = ++vn_counter
                set_representative(vn_counter, I.dest)

    run dead code elimination to remove marked instructions
```

**Key design decisions visible from the binary:**

1. **Hash-based value table.** The value numbering table uses FNV-1a hashing (seed `0x811C9DC5`, prime `16777619` / `0x01000193`), the same hash primitive used throughout ptxas for instruction fingerprinting, code caching, and scheduling table lookups. The hash function incorporates the opcode, type, and recursively resolved value numbers of all operands. Hash table entries are 24 bytes each: `[next_ptr (8B), key (8B), value/metadata (8B)]` with chained collision resolution.

2. **Dominator-tree scoping.** Values defined in block B are only visible to blocks dominated by B. When the walk exits a dominator subtree, value table entries scoped to that subtree are removed. This prevents CSE from moving computations to positions where they would not dominate all uses. Dominance is checked via `sub_1245740`, which performs a single-bit test against a per-block dominator bitvector: the dominator set at block descriptor offset `+176` is indexed by the dominator block's ID from offset `+144`. The check is O(1).

3. **Commutativity normalization.** For commutative operations (ADD, MUL, AND, OR, XOR, MIN, MAX), operands are sorted by value number before hashing. This ensures `a + b` and `b + a` get the same value number without requiring a separate reassociation pass.

4. **Address space awareness.** Memory operations in different address spaces (shared, global, local, constant) are never considered equivalent even if they have identical operands. The address space qualifier is encoded in the instruction opcode or modifier bits (not the operand), so the opcode comparison in the structural equivalence check inherently preserves this distinction.

5. **Predicate handling.** Predicated instructions (`@P0 IADD R1, R2, R3`) hash the predicate register's value number as an additional operand. Two identical computations under different predicates are distinct values.

6. **Structural equivalence.** After hash collision, `sub_7E7380` performs full structural comparison: (a) opcodes masked with `& 0xFFFFCFFF` to strip modifier bits 12-13, (b) data types at offset `+76`, (c) operand counts at offset `+80`, (d) per-operand encoding and modifier values, (e) register class/type at offset `+64`. Instructions with volatile flags (bit `0x20` at register descriptor offset `+48`) and barrier-type registers (type 9) are excluded from CSE entirely.

### GPU-Specific CSE Constraints

GPU CSE must respect constraints that do not arise in CPU compilers:

- **Divergence.** A uniform subexpression (same value across all threads in a warp) can be safely hoisted. A divergent subexpression may have different values per thread and must only be CSE'd within the same control-flow path. The GvnCse pass runs after `AnalyzeUniformsForSpeculation` (phase 27), which provides divergence annotations.

- **Barrier sensitivity.** A computation that reads shared memory before a `BAR.SYNC` cannot be commoned with an identical computation after the barrier, because intervening threads may have written different values. Memory operations with barrier dependencies are assigned unique value numbers.

- **Register pressure.** Aggressive CSE can increase register pressure by extending the live range of the representative value. The `EnableGvnCse` knob allows the pass to be disabled when register pressure is the binding constraint.

---

## Phase 50 -- OriReassociateAndCommon

### Overview

Reassociation normalizes the algebraic structure of expressions to expose commoning opportunities that GvnCse missed. GvnCse cannot detect that `(a + b) + c` and `(a + c) + b` compute the same value unless the expressions are first reassociated into a canonical form. This pass performs that reassociation and then runs a second commoning pass over the normalized IR.

### Dispatch Mechanism

```c
// sub_C604D0 -- OriReassociateAndCommon::execute
int64 execute(phase* self, compilation_context* ctx) {
    int func_count = get_function_count(ctx);   // sub_7DDB50
    if (func_count > 1)
        return ctx->field_1584->vtable[44](ctx->field_1584, ctx);
    return func_count;
}
```

For multi-function compilation units, the pass dispatches through the compilation context's SM backend (field `+1584` / `0x630`), calling vtable slot 44 (offset `0x160`). This enables per-function reassociation with function-level isolation of value numbering state.

### Algorithm (Reconstructed)

Reassociation works on associative and commutative operators:

```
procedure ReassociateAndCommon(function F):
    for each basic block B in RPO:
        for each instruction I in B:
            if I.opcode is associative+commutative (ADD, MUL, AND, OR, XOR):
                flatten expression tree rooted at I into a list of leaves
                sort leaves by canonical order (constants last, then by register number)
                rebuild balanced binary tree from sorted leaves
            if I.opcode is SUB:
                rewrite (a - b) as (a + (-b)) for uniformity

    // Second pass: hash-based commoning over the reassociated IR
    run local CSE over each basic block
```

### Why Reassociation Matters

The reassociation and commoning phases are tightly coupled because reassociation's primary goal is to enable commoning:

```
BB0:  R5 = (R2 + R3) + R4 ; GvnCse sees: VN(ADD, VN(ADD,vn(R2),vn(R3)), vn(R4))
BB1:  R6 = (R2 + R4) + R3 ; GvnCse sees: VN(ADD, VN(ADD,vn(R2),vn(R4)), vn(R3))
      -- These are NOT the same VN because the inner ADDs differ.
```

After reassociation, both flatten to `{R2, R3, R4}` sorted canonically, then rebuild as `(R2 + R3) + R4`. Now they share the same value number and the second is eliminated.

### Controlling Knobs

| Knob | Address | Purpose |
|---|---|---|
| `AllowReassociateCSE` | `0x21C0180` | Master enable/disable |
| `ReassociateCSEBudget` | `0x21BA810` | Max instructions processed per function |
| `ReassociateCSEWindow` | `0x21BA7D0` | Sliding window size for local CSE after reassociation |
| `ReassociateCSESkip` | `0x21BA7F0` | Skip first N instructions (debugging) |
| `ReassociateLargeImmInUIADD64` | `0x21BA7A0` | Large immediates in 64-bit unsigned ADD |
| `DistributeAndReassociateMulBudget` | `0x21BDDC0` | Budget for `a*b + a*c -> a*(b+c)` |

---

## Phase 64 -- LateOriCommoning

### Overview

LateOriCommoning is a late CSE pass that runs immediately after predication (phase 63, `OriDoPredication`). If-conversion transforms conditional branches into predicated instructions, which can expose new redundancies: two computations that were previously in mutually exclusive branches become adjacent predicated instructions that may compute the same value.

### Dispatch Mechanism

```c
// sub_C60020 -- LateOriCommoning::execute
char execute(phase* self, compilation_context* ctx) {
    int func_count = get_function_count(ctx);    // sub_7DDB50
    if (func_count > 1)
        return sub_9059B0(ctx);                  // late commoning implementation
    return func_count;
}
```

### Implementation -- `sub_9059B0`

`sub_9059B0` is the entry point for late commoning. It:

1. Checks knob 487 (`ForceLateCommoning` at `0x21BD2F0`) to determine whether the pass is enabled
2. Verifies the function's optimization state has commoning enabled: the byte at `context->field_1664->field_72 + 60696` must be 1, and the dword at offset `+60704` must be nonzero
3. Allocates a ref-counted working set via the pool allocator
4. Calls `sub_9055F0` -- the core commoning walker

### Core Commoning Walker -- `sub_9055F0`

`sub_9055F0` (203 lines decompiled) is the central commoning algorithm for late CSE. Its structure, reconstructed from the decompilation:

```
procedure LateCommoning(function_state S):
    if not knob_enabled(487):  return
    if S.flags & 0x02:  return                 // already processed
    if (S.flags | S.flags2) & 0x08:  return    // conflicting mode

    rebuild_def_chains(S, mode=1)              // sub_781F80
    rebuild_use_chains(S)                      // sub_763070
    compute_hash_values(S, 0, 0, 0, 0)        // sub_7E6090

    block_count = S.field_520 + 1
    allocate bit_array[block_count]

    // Reset hash/VN slots on all instructions
    for each instruction I in S.instruction_list:
        I.field_88 = 0xFFFFFFFF00000000        // upper 32 bits = -1, lower = 0

    // Main commoning loop over code list
    for each instruction I in S.code_list:
        // Phase 1: Remap operands through equivalence table
        for each operand (reverse order):
            if operand is register ref (type 0x10000000):
                resolve to canonical representative

        // Phase 2: Try commoning based on opcode class
        if I.opcode == 72 (MOV):
            propagate_equivalence(I)            // sub_8F2CD0
        elif is_pure(I):                        // sub_7DF3A0
            opcode_class = I.opcode & 0xCF00
            if opcode_class == 0x0061 (SEL):    // conditional select
                reset_tracking()
            elif opcode_class == 0x0034 (PHI):
                record_phi_equivalence(S, I)
            else:
                if not try_common(S, I):        // sub_901A90
                    hash = compute_hash(S, I)   // sub_74ED70
                    record_hash_for_future_matching(hash)
```

The three infrastructure functions called at the beginning are shared with the GeneralOptimize sub-passes:

- `sub_781F80` -- rebuilds reaching definition chains (also used by GeneralOptimizeEarly)
- `sub_763070` -- rebuilds use-def chains
- `sub_7E6090` -- pre-computes instruction hash values

### Commoning Check -- `sub_901A90`

`sub_901A90` (387 lines) is the instruction-level CSE checker. It:

1. Examines the instruction's opcode, type, and operand value numbers
2. Looks up the instruction's hash in the per-block equivalence table
3. If a match is found, verifies that the matched instruction dominates the current position via `sub_1245740` (O(1) bitvector bit test: `(1 << def_dom_id) & dom_set[def_dom_id >> 5]`)
4. If domination holds, replaces the current instruction's destination with the matched instruction's destination
5. Returns true if commoning succeeded, false otherwise

A related commoning pattern was confirmed from `sub_90A340` (1670 bytes, 21 callees), which performs commoning on opcode 130 (MOV) instructions. From the decompilation, the operand comparison loop:

```c
// Operand-by-operand equivalence check within commoning body
for (i = operand_count - 1; i >= 0; i--) {
    if (candidate.operands[2*i + 21] != existing.operands[2*i + 21])
        break;  // operand value mismatch
    if (candidate.operands[2*i + 22] != existing.operands[2*i + 22])
        break;  // operand modifier mismatch
}
// If all operands match AND opcodes match AND operand counts match:
//   verify dominance, then replace
```

The reverse iteration order (from last operand to first) is an optimization: destination operands at lower indices are more likely to differ, so checking source operands first (higher indices) allows early exit.

### Instruction Hashing -- `sub_74ED70`

`sub_74ED70` (304 lines) computes a hash value for an instruction, incorporating:

- Opcode and type qualifiers
- Value numbers of all source operands (recursively resolved through MOV chains)
- Address space for memory operations
- Predicate register (if predicated)
- Immediate values (folded into the hash)

The hash is stored at instruction field `+88` (the upper 32 bits that were reset to `0xFFFFFFFF` during initialization). The function calls `sub_7DF3A0` (purity check), `sub_7E0030` and `sub_7E2530` (operand accessors), and `sub_748440` (hash combining).

### Controlling Knobs

| Knob | Address | Purpose |
|---|---|---|
| `ForceLateCommoning` | `0x21BD2F0` | Force-enable late commoning |
| `DisableMoveCommoning` | `0x21BE2C0` | Disable MOV-based equivalence propagation within the commoning walker |

---

## Phase 83 -- OriBackCopyPropagate

### Overview

Backward copy propagation propagates values backward through MOV chains, eliminating intermediate copies. Unlike forward copy propagation (which replaces uses of a copy's destination with the copy's source), backward copy propagation replaces the **definition** of a copy's source with the copy's destination, allowing the copy instruction itself to be deleted.

Phase 83 uses a split-phase design with phase 82 (`AdvancedPhaseBackPropVReg`). The actual backward copy propagation algorithm lives in architecture-specific SM backend overrides of phase 82. Phase 83 is a pipeline progress marker that advances the pipeline counter `context+1552` to 9 after backward copy propagation completes, signaling to downstream operand encoding functions that they may apply relaxed register constraints.

**This phase is disabled by default** (`isNoOp` returns 1). It is activated only when an architecture backend overrides phase 82 to provide its own backward propagation implementation.

### Dispatch Mechanism

The execute function is a 7-byte stub that advances the pipeline progress counter:

```c
// sub_C5EB80 -- OriBackCopyPropagate::execute
void execute(phase* self, compilation_context* ctx) {
    ctx->field_1552 = 9;   // advance pipeline progress counter to backward-copy-prop stage
}
```

Phase 83 does not contain the backward copy propagation algorithm. The actual algorithm is provided by the architecture-specific SM backend that overrides phase 82 (`AdvancedPhaseBackPropVReg`). The split-phase design works as follows:

| Phase | Role | Default behavior | When arch-activated |
|---|---|---|---|
| 82 (`AdvancedPhaseBackPropVReg`) | Gate + algorithm provider | No-op (hook, `isNoOp` = 1) | Arch backend installs backward copy propagation body |
| 83 (`OriBackCopyPropagate`) | Pipeline progress marker | No-op (`isNoOp` = 1) | Sets `context+1552 = 9`, enabling downstream constraint relaxation |

The factory switch at `sub_C60D30` installs vtable `off_22BE298` for phase 82 and `off_22BE2C0` for phase 83. Both vtables are 40-byte (5-pointer) structures at consecutive addresses in `.data.rel.ro`.

### Gate Mechanism (Phase 82)

Phase 82 (`AdvancedPhaseBackPropVReg`) is one of 16 `AdvancedPhase` hook points in the pipeline. By default its `isNoOp` returns true, meaning the phase is skipped entirely. When an architecture backend needs backward copy propagation, it:

1. Overrides phase 82's vtable to install the actual backward propagation algorithm as the execute function
2. Overrides phase 82's `isNoOp` to return 0 (enabled)
3. Configures phase 83's `isNoOp` to return 0, enabling the pipeline counter advancement

The `BackCopyPropBudget` knob (index 808, address `0x21BFDF0`) limits the number of backward propagations performed. This knob is read by `sub_8C0270` (scheduler initialization) at the point where the scheduler allocates its per-function work structure. When knob 808 is not set by the user, the budget falls back to a default stored in the scheduler state object at offset `+92`.

### Algorithm (Reconstructed)

The backward copy propagation algorithm is reconstructed from the phase name, the infrastructure it shares with forward copy propagation (`sub_781F80`, `sub_763070`), the `BackCopyPropBudget` knob, and the pipeline position constraints. The actual algorithm body resides in architecture-specific SM backend code, not in the generic binary.

```
procedure BackCopyPropagate(function F):
    budget = knob(808)     // BackCopyPropBudget
    count = 0

    // Phase 1: rebuild def-use chains (shared infrastructure)
    rebuild_def_chains(F)  // sub_781F80
    rebuild_use_chains(F)  // sub_763070

    // Phase 2: walk blocks in RPO, instructions in reverse
    for each basic block B in reverse postorder:
        for each instruction I in B (last to first):
            if count >= budget:
                return

            if I is not MOV (opcode & 0xCF00 != MOV class):
                continue

            // I is: Rd = MOV Rs
            def_of_Rs = reaching_def(Rs)

            // Guard 1: Rs must have exactly one use (this MOV)
            if use_count(Rs) != 1:
                continue

            // Guard 2: def(Rs).dest can be renamed to Rd without conflict
            if not can_rename(def_of_Rs.dest, Rd):
                continue

            // Guard 3: no intervening definition of Rd between def(Rs) and I
            if has_intervening_def(Rd, def_of_Rs, I):
                continue

            // Perform backward propagation: rename definition
            rename def_of_Rs.dest from Rs to Rd
            delete I  // MOV is now redundant
            count++
```

The backward walk direction is essential for cascading chain collapse:

```
Before:    R1 = expr;    R2 = R1;    R3 = R2
                                      ^^^^^^ processed first (backward)
Step 1:    R1 = expr;    R3 = R1;    (deleted R3=R2, renamed R2→R3 in "R2=R1")
                         ^^^^^^ processed next
Step 2:    R3 = expr;                (deleted R3=R1, renamed R1→R3 in "R1=expr")

Result: entire 3-instruction chain collapses to single "R3 = expr"
```

If the walk were forward, only `R2 = R1` would be processed first (renaming `R1 = expr` to `R2 = expr`), but then `R3 = R2` would need a second pass to collapse further. The backward direction achieves full chain collapse in a single pass.

### Why Phase 83 Runs So Late

Phase 83 is positioned at pipeline slot 83 out of 158, immediately before the register attribute computation sequence (phases 84--95). This late position serves three purposes:

1. **Catches late-created copies.** Phases 66--81 include late optimizations (LICM, texture movement, rematerialization, late arch-specific peepholes) that frequently insert new MOV instructions. Backward copy propagation after these passes cleans up the residual chains that forward propagation (which last ran in phase 65) cannot see.

2. **Reduces register pressure for allocation.** Every eliminated MOV is one fewer live range the register allocator (phase 101) must handle. By running just before the liveness/DCE pass (phase 84, `OriPerformLiveDeadFourth`), backward copy propagation minimizes the input to register allocation.

3. **Safe renaming window.** After phase 83, the pipeline enters the register attribute and legalization sequence. Renaming destinations before this point avoids conflicts with the fixed register assignments that legalization may impose.

### Why Disabled by Default

Phase 83 is disabled by default (`isNoOp` returns 1) for several reasons:

1. **Backward renaming is inherently riskier than forward propagation.** Forward copy propagation modifies uses (safe because the original definition still exists). Backward copy propagation modifies definitions -- changing which register an instruction writes to. A bug here can silently corrupt values used by other instructions.

2. **Architecture-specific register constraints.** The legality of renaming a destination depends on target-specific constraints: fixed-function registers (thread ID, special purpose), register bank conflicts, paired/grouped register requirements for 64-bit operations, and uniform register constraints on newer architectures (Volta+). Only the architecture backend knows which renames are safe.

3. **Diminishing returns.** Forward copy propagation (`OriCopyProp`) runs six times during the GeneralOptimize bundles (phases 13, 29, 37, 46, 58, 65) and handles the majority of copy elimination. Backward propagation catches only residual chains that forward propagation structurally cannot eliminate.

4. **Gate requirement.** Architecture backends that enable backward copy propagation via phase 82 may also need to pre-process the IR (e.g., marking registers that must not be renamed, or inserting constraints that protect fixed-function registers).

### Downstream Effects: Pipeline Counter and Encoding Relaxation

When phase 83 sets `context+1552` to 9, two operand encoding pattern functions (`sub_9BF350` and `sub_9BFAF0`) change behavior. These functions gate on two conditions:

```c
// Gate check in sub_9BF350 and sub_9BFAF0
if ((context->field_1398 & 0x04) != 0 && context->field_1552 > 9) {
    // Apply register constraint relaxation
    // Check if operand register class == 3 (address register) or reg_id == 41
    // Assign special operand mask 0xFFFFFA (16777210) instead of 0xFFFFFF
}
```

The flag at `context+1398` bit 2 is an architecture capability flag. When both conditions are met (capability flag set AND pipeline has progressed past phase 83), the encoding functions relax operand constraints for address registers (class 3) and special register 41, allowing these to participate in operand patterns that they would otherwise be excluded from.

The pipeline counter value 9 is part of a progression: phase 95 (`SetAfterLegalization`, `sub_C5E440`) later advances the counter to 19, enabling a further tier of relaxation in the scheduler initialization (`sub_8C0270`).

### Forward vs. Backward Copy Propagation

The two propagation directions are complementary and handle different structural patterns:

| Property | Forward (OriCopyProp) | Backward (OriBackCopyPropagate) |
|---|---|---|
| Direction | Replaces **uses** of copy destination with copy source | Replaces **definitions** to eliminate copies |
| Example | `R2=R1; ADD R3,R2,R4` -> `ADD R3,R1,R4` | `R1=expr; R2=R1` -> `R2=expr` |
| Runs | 6 times (phases 13,29,37,46,58,65) | Once (phase 83) |
| Default | Always enabled | Disabled (arch-gated) |
| Risk | Low (original def unchanged) | Higher (modifies defs) |
| Catches | Most copies from expansion and lowering | Residual chains from late passes (66--81) |

### Controlling Knobs

| Knob | Address | Purpose |
|---|---|---|
| `BackCopyPropBudget` | `0x21BFDF0` | Maximum backward propagations per function (knob index 808) |

---

## Forward Copy Propagation -- OriCopyProp

### Overview

Forward copy propagation is not a standalone pipeline phase but a sub-pass within each of the six [GeneralOptimize](general-optimize.md) bundles (phases 13, 29, 37, 46, 58, 65). It is identified by the name string `OriCopyProp` at address `0x21E6CE1` and can be individually targeted via the `--named-phases` mechanism.

The `OriCopyProp` name appears in the NamedPhases parser (`sub_9F4040` at offset `+1648`), where it is looked up via `sub_C641D0` (case-insensitive binary search over the phase name table). When the user specifies `--named-phases OriCopyProp`, the system resolves this to the appropriate sub-pass within GeneralOptimize.

### Target Opcodes and Flag Bits

Three Ori opcodes are candidates for forward copy propagation:

| Opcode | Meaning | Propagation Rule |
|---|---|---|
| 97 | Register-to-register MOV | Replace uses of destination with source |
| 18 | Predicated copy | Propagate only under matching predicate guard |
| 124 | Conditional select (CSEL) | Propagate when select condition is provably constant |

Opcode matching uses a mask: `(instr.opcode & 0xCF00) == target`, stripping modifier bits in the upper nibble of the opcode field at instruction offset `+72`.

Three flag bits on instruction field `[6]` (byte offset 24) track propagation state:

| Bit | Hex | Meaning |
|---|---|---|
| 8 | `0x100` | Copy has been propagated |
| 9 | `0x200` | Deferred cleanup (instruction may still be needed) |
| 10 | `0x400` | Under predicate guard (requires predicate-aware handling) |

### Eligibility Check (`sub_8F2E50`)

The eligibility function checks whether a copy can be safely propagated, with an SM-version-dependent constraint:

```
function isEligibleForPropagation(instr, ctx):
    sm_version = *(ctx + 372)
    operand_type = instr.operand_type & 0xF
    if sm_version <= 20479:        // pre-Turing (sm_70 and earlier)
        return true                // unconditionally safe
    else:                          // Turing+ (sm_75+)
        return (operand_type & 0x1C00) == 0   // constraint bits must be clear
```

The SM version threshold 20479 corresponds to the boundary between Volta (sm_70) and Turing (sm_75). Turing introduced new operand constraint bits that restrict when copies can be folded.

### Algorithm

Forward copy propagation replaces uses of a copy's destination with the copy's source:

```
procedure OriCopyProp(function F):
    for each basic block B in RPO:
        for each instruction I in B:
            if I is MOV Rd, Rs:
                for each use U of Rd that I dominates:
                    if Rs is still live at U:
                        replace Rd with Rs in U
                if Rd has no remaining uses:
                    mark I as dead
```

Within the GeneralOptimize loop, copy propagation interacts with constant folding and algebraic simplification: a copy propagation may expose a constant operand, enabling constant folding in the next iteration, which may create a dead instruction for DCE. This is why GeneralOptimize runs as a fixed-point loop. In Variant A (phases 13, 29), the fixed-point iteration is capped by knob 464. In Variant B (phases 37, 58), convergence uses a cost-based threshold of 0.25 (knob 474). Two-pass predicate simplification via `sub_908A60` runs within the copy propagation loop to handle predicate-conditional copies.

### Controlling Knobs

| Knob | Address | Purpose |
|---|---|---|
| `CopyPropBudget` | `0x21BECD0` | Maximum instructions processed per invocation |
| `CopyPropGlobalBudget` | `0x21BEC70` | Budget for cross-block (global) copy propagation |
| `CopyPropForceGlobal` | `0x21BEC90` | Force global copy propagation |
| `CopyPropAddr` | `0x21BECE8` | Propagate through address computations |
| `CopyPropConstantBank` | `0x21BECB0` | Propagate constant bank references |
| `CopyPropUseReachingDefs` | `0x21BEBD0` | Use reaching definitions for more aggressive propagation |
| `CopyPropPreAllocReg` | `0x21BEBF0` | Enable for pre-allocated (fixed) registers |
| `CopyPropNoWriteNonRR` | `0x21BEC10` | Disable into non-register-register contexts |
| `CopyPropNonRegMultiDef` | `0x21BEC30` | Handle non-register multi-definition copies |
| `CopyPropNoMmaCb` | `0x21BEC50` | Disable into MMA constant bank operands |
| `LateCopyPropComplPred` | `0x21BC680` | Late copy propagation for complementary predicates |

The `CopyPropUseReachingDefs` knob is particularly significant: when enabled, the pass uses reaching definitions analysis (built by `sub_781F80`) instead of simple dominator checks, allowing more aggressive propagation at the cost of additional analysis time.

---

## Complete Knob Reference

All 22 knobs controlling copy propagation and CSE:

| Knob | ROT13 | Address | Controls |
|---|---|---|---|
| `EnableGvnCse` | `RanoyrTiaPfr` | `0x21BDA50` | Master enable for phase 49 |
| `AllowReassociateCSE` | `NyybjErnffbpvngrPFR` | `0x21C0180` | Master enable for reassociation CSE |
| `ReassociateCSEBudget` | `ErnffbpvngrPFROhqtrg` | `0x21BA810` | Instruction budget |
| `ReassociateCSEWindow` | `ErnffbpvngrPFRJvaqbj` | `0x21BA7D0` | Sliding window size |
| `ReassociateCSESkip` | `ErnffbpvngrPFRFxvc` | `0x21BA7F0` | Skip first N |
| `ReassociateLargeImmInUIADD64` | `ErnffbpvngrYnetrVzzVaHVNQQ64` | `0x21BA7A0` | 64-bit ADD imm |
| `DistributeAndReassociateMulBudget` | `QvfgevohgrNaqErnffbpvngrZhyOhqtrg` | `0x21BDDC0` | Distributive law |
| `ForceLateCommoning` | `SbeprYngrPbzzbavat` | `0x21BD2F0` | Force phase 64 |
| `DisableMoveCommoning` | `QvfnoyrZbirPbzzbavat` | `0x21BE2C0` | Disable MOV commoning |
| `BackCopyPropBudget` | `OnpxPbclCebcOhqtrg` | `0x21BFDF0` | Phase 83 budget |
| `CopyPropBudget` | `PbclCebcOhqtrg` | `0x21BECD0` | Per-invocation budget |
| `CopyPropGlobalBudget` | `PbclCebcTybonyOhqtrg` | `0x21BEC70` | Cross-block budget |
| `CopyPropForceGlobal` | `PbclCebcSbeprTybony` | `0x21BEC90` | Force global |
| `CopyPropAddr` | `PbclCebcNqqe` | `0x21BECE8` | Address prop |
| `CopyPropConstantBank` | `PbclCebcPbafgnagOnax` | `0x21BECB0` | Constant bank |
| `CopyPropUseReachingDefs` | `PbclCebcHfrErnpuvatQrsf` | `0x21BEBD0` | Reaching defs |
| `CopyPropPreAllocReg` | `PbclCebcCerNyybpErt` | `0x21BEBF0` | Fixed registers |
| `CopyPropNoWriteNonRR` | `PbclCebcAbJevgrAbaEE` | `0x21BEC10` | Non-RR disable |
| `CopyPropNonRegMultiDef` | `PbclCebcAbaErtZhygvQrs` | `0x21BEC30` | Multi-def |
| `CopyPropNoMmaCb` | `PbclCebcAbZznPo` | `0x21BEC50` | MMA disable |
| `LateCopyPropComplPred` | `YngrPbclCebcPbzcyCerq` | `0x21BC680` | Compl pred |
| `SpeculativeHoistCommonInsts` | `FcrphyngivruBvfgPbzzbaVafgf` | `0x21B81B0` | Spec hoist (phase 56) |

---

## Interaction Between Passes

The copy propagation and CSE passes interact with each other and with the rest of the pipeline in a specific sequence designed to maximize redundancy elimination:

```
Phase 46: GeneralOptimizeMid2
  |-- OriCopyProp (forward copy propagation)
  |-- constant folding, algebraic simplification, DCE

Phase 48: EnforceArgumentRestrictions
  |-- may insert MOVs for ABI compliance -> new copy prop opportunities

Phase 49: GvnCse
  |-- global value numbering + CSE
  |-- eliminates redundant computations across basic blocks

Phase 50: OriReassociateAndCommon
  |-- normalizes expression trees for better commoning
  |-- local CSE over reassociated IR
  |-- catches cases GvnCse missed due to non-canonical form

Phase 51: ExtractShaderConstsFinal
  |-- may replace computations with constant loads -> dead code

Phase 58: GeneralOptimizeLate
  |-- OriCopyProp again (cleans up after expansion passes)

Phase 63: OriDoPredication
  |-- converts branches to predicated instructions
  |-- previously mutually-exclusive code becomes linear

Phase 64: LateOriCommoning
  |-- CSE on newly-linearized predicated code
  |-- eliminates redundancies exposed by if-conversion

Phase 65: GeneralOptimizeLate2
  |-- OriCopyProp + DCE (final cleanup)

Phase 82: AdvancedPhaseBackPropVReg (gate, arch-specific)
Phase 83: OriBackCopyPropagate
  |-- backward MOV chain elimination (disabled by default)
  |-- reduces copy count before register allocation
```

---

## Key Function Map

| Address | Size | Name | Purpose |
|---|---|---|---|
| `0xC5F000` | 16 B | GvnCse::execute | Thunk to sm_backend (context+0x630)->vtable[23] |
| `0xC5F010` | 6 B | GvnCse::getName | Returns 49 |
| `0xC5F020` | 6 B | GvnCse::isNoOp | Returns 0 (enabled) |
| `0xC604D0` | 42 B | OriReassociate::execute | Dispatches to sm_backend (context+1584)->vtable[44] |
| `0xC5EFE0` | 6 B | OriReassociate::getName | Returns 50 |
| `0xC5EFF0` | 6 B | OriReassociate::isNoOp | Returns 0 (enabled) |
| `0xC60020` | 48 B | LateOriCommoning::execute | Calls `sub_9059B0` |
| `0xC5EDF0` | 6 B | LateOriCommoning::getName | Returns 64 |
| `0xC5EE00` | 6 B | LateOriCommoning::isNoOp | Returns 0 (enabled) |
| `0xC5EB80` | 7 B | BackCopyProp::execute | Sets context+1552 = 9 (pipeline progress marker) |
| `0xC5EB90` | 6 B | BackCopyProp::getName | Returns 83 |
| `0xC5EBA0` | 6 B | BackCopyProp::isNoOp | Returns 1 (**disabled**) |
| `0xC5EBB0` | 6 B | AdvancedPhaseBackPropVReg::getName | Returns 82 |
| `0xC5EBC0` | 6 B | AdvancedPhaseBackPropVReg::isNoOp | Returns 0 (overridden to 1 at runtime by default vtable) |
| `sub_9BF350` | 8.6 KB | Encoding pattern (post-phase-83) | Checks context+1552 > 9 for register constraint relaxation |
| `sub_9BFAF0` | 9.0 KB | Encoding pattern (post-phase-83) | Checks context+1552 > 9 for register constraint relaxation |
| `sub_8C0270` | 14 KB | Scheduler vtable init | Reads knob 808 (BackCopyPropBudget), checks +1552 == 19 |
| `sub_9059B0` | ~320 B | LateOriCommoning impl | Knob check + ref-counted working set + core walker |
| `sub_9055F0` | ~800 B | LateCommoning core | Iterates code list, remaps operands, calls commoning check |
| `sub_901A90` | ~1.5 KB | Commoning check | Hash lookup + dominance verify + replacement |
| `sub_74ED70` | ~1.2 KB | Instruction hash | Opcode + type + operand VNs + address space -> hash |
| `sub_781F80` | -- | Rebuild def chains | Reaching definitions for commoning |
| `sub_763070` | -- | Rebuild use chains | Use-def chains |
| `sub_7E6090` | -- | Compute hash values | Pre-computes per-instruction hashes |
| `sub_7DDB50` | ~140 B | get_function_count | Returns func count from compilation context |
| `sub_7DF3A0` | ~80 B | is_pure_instruction | Side-effect-free check (bits 2-3 of status word) |
| `sub_748440` | -- | Hash combine | Mixes operand hashes into instruction hash |
| `sub_8F2CD0` | -- | Propagate equivalence | MOV-based value equivalence propagation |
| `sub_8FCE70` | ~150 B | Ref-count release | Releases ref-counted working set objects |
| `sub_1245740` | -- | Dominance check | O(1) bitvector bit test for CSE safety |
| `sub_6B9180` | -- | Set membership test | Commoning set contains check |
| `sub_9253C0` | -- | Instruction deletion | Removes dead/redundant instructions |
| `sub_90A340` | 1.7 KB | Commoning body | Commoning pass instance (21 callees, confirms operand comparison pattern) |
| `sub_908A60` | -- | Predicate simplifier | Two-pass (forward+backward) predicate simplification in copy prop |
| `sub_8F2E50` | -- | Copy/fold eligibility | SM-version-dependent eligibility check (threshold 20479) |
| `sub_7BA510` | 5.2 KB | HashCompute | Program/instruction sequence hash (FNV/Jenkins variant) |
| `sub_7BB260` | 3.5 KB | HashAccumulate | Incremental hash accumulation |
| `sub_8DCF20` | 23 KB | FNV-1a hash table | 8-byte key hash table with chained collision (24-byte entries) |
| `sub_8DF1C0` | 16 KB | FNV-1a hash table | 32-bit key hash table, two-level structure |
| `sub_9B1200` | 7.7 KB | Code-caching hash | Jenkins-style instruction fingerprint for RA cache |

---

## Hash Infrastructure

The GVN/CSE passes share hash infrastructure with other subsystems (scheduling, code caching, register allocation). All FNV-1a implementations in ptxas use the same constants:

| Constant | Value | Purpose |
|---|---|---|
| FNV offset basis | `0x811C9DC5` | Initial hash state |
| FNV prime | `16777619` (`0x01000193`) | Multiplication factor per byte |

Hash-related functions identified in the binary:

| Address | Size | Function | Used By |
|---|---|---|---|
| `sub_7BA510` | 5.2 KB | `HashCompute` -- program/instruction sequence hash | Shader hash matching (`SH=` knob) |
| `sub_7BB260` | 3.5 KB | `HashAccumulate` -- incremental hash accumulation | Instruction-at-a-time hashing |
| `sub_8DCF20` | 23 KB | FNV-1a hash table (8-byte keys, chained collision) | Instruction deduplication in scheduling |
| `sub_8DF1C0` | 16 KB | FNV-1a hash table (32-bit keys, two-level) | Opcode pattern classification |
| `sub_9B1200` | 7.7 KB | Jenkins-style instruction hash for code caching | Register allocator cache hit detection |
| `sub_74ED70` | ~1.2 KB | Per-instruction hash for commoning | LateOriCommoning (phase 64) |
| `sub_748440` | -- | Hash combine helper | Mixes operand hashes into instruction hash |

The code-caching hash at `sub_9B1200` uses a different algorithm from FNV-1a:

```
hash = (1025 * (value + hash)) ^ ((1025 * (value + hash)) >> 6)
```

It processes instruction opcodes (offset `+72`), operand counts (`+80`), operand encodings (`+76`), register properties (`+64`), and variable pair mode (bits 20-21 of the descriptor at offset `+48`).

---

## Cross-References

- [Pass Inventory](index.md) -- complete 159-phase table
- [GeneralOptimize Bundles](general-optimize.md) -- forward copy propagation (OriCopyProp) sub-pass
- [Predication](predication.md) -- phase 63 creates opportunities for LateOriCommoning
- [Liveness Analysis](liveness.md) -- liveness data consumed by copy propagation
- [Strength Reduction](strength-reduction.md) -- produces normalized expressions for GvnCse
- [Knobs System](../config/knobs.md) -- ROT13-encoded knob infrastructure
- [Phase Manager](phase-manager.md) -- vtable dispatch, phase factory
- [Ori IR](../ir/overview.md) -- instruction representation, operand encoding
