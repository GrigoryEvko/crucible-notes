# Dead Sync Elimination and Common Base Elimination

## Abstract

Two NVPTX middle-end passes attack different forms of redundancy. Dead Sync Elimination deletes barriers that don't separate visible memory traffic, using a four-map cross-product over the cross-warp dependence graph as the correctness predicate. Common Base Elimination collapses repeated address arithmetic by hoisting a shared base pointer and rewriting related GEP chains as deltas off that base, using `ScalarEvolution` to recognize algebraically equal bases written through different operand sequences. Both passes are sound by construction: deletion happens only when the dependence graph or the SCEV equivalence proves the rewrite is an identity transformation on observable behavior.

## Dead Sync Elimination

### Input and Output IR Shape

DeadSyncElim consumes LLVM IR carrying NVPTX sync intrinsics and emits the same IR with provably redundant calls removed. The intrinsic family it deletes covers `nvvm.bar.sync.aligned`, `nvvm.barrier0` and its named variants, and the `nvvm.fence.*` ordering intrinsics. A representative pair of fragments:

```llvm
; before
define void @kernel(ptr addrspace(3) %s, i32 %i) {
entry:
  %p = getelementptr i32, ptr addrspace(3) %s, i32 %i
  %v = load i32, ptr addrspace(3) %p, align 4
  call void @llvm.nvvm.barrier0()
  %w = add i32 %v, 1
  store i32 %w, ptr addrspace(3) %p, align 4
  ret void
}

; after
define void @kernel(ptr addrspace(3) %s, i32 %i) {
entry:
  %p = getelementptr i32, ptr addrspace(3) %s, i32 %i
  %v = load i32, ptr addrspace(3) %p, align 4
  %w = add i32 %v, 1
  store i32 %w, ptr addrspace(3) %p, align 4
  ret void
}
```

The `nvvm.barrier0` here separates a per-thread load from a per-thread store on the same address; no other warp can observe a value through that barrier that it could not observe in its absence, so the pass deletes it. The deletion is local to a basic block — the analyzer never claims a barrier dead when it spans CFG edges that could carry cross-warp dependences from outside the block.

### Matching Predicate

The pass operates per basic block. For each barrier site it maintains four maps from shared-memory address (or, when the address is non-constant, an address-class tag) to a one-byte access summary:

| Map | Meaning |
| --- | --- |
| `read_above` | SMEM reads before the barrier in this block |
| `write_above` | SMEM writes before the barrier in this block |
| `read_below` | SMEM reads after the barrier in this block |
| `write_below` | SMEM writes after the barrier in this block |

A barrier is dead exactly when

```text
(write_above × read_below) ∪ (write_below × read_above) = ∅
```

— no producer-consumer pair on shared memory crosses it. Address-class collisions count as may-alias and keep the barrier alive.

The four maps are a compact encoding of the cross-warp dependence graph at the barrier. Each entry `(t, A, ↑)` representing "thread `t` accesses address `A` above the barrier" induces a potential cross-warp edge to every entry `(t', A, ↓)` with the opposite read/write polarity. The barrier is observably necessary iff at least one such edge exists; the four-map cross-product is precisely that emptiness check, computed once per address class instead of per `(thread, thread)` pair.

### Algebraic Correctness Condition

The elimination is safe iff the cross-warp dependence graph is unchanged after the deletion. With the barrier present, every pair `((t, A, write_above), (t', A, read_below))` carries a `happens-before` edge that the memory model would otherwise leave undefined; deleting the barrier removes that edge. The maps' emptiness guarantees no such edge exists to begin with, which makes deletion an identity transformation on the dependence graph and therefore on the program's observable behavior. The condition is necessary and sufficient: an empty cross-product is what `happens-before` actually demands, not a sufficient over-approximation.

### Exempt Intrinsics

Three sync intrinsics are permanently exempted from deletion even when the four-map predicate declares them dead. Their semantics reach past shared memory in ways the lightweight scan cannot represent:

| Intrinsic | Reason for exemption |
| --- | --- |
| `llvm.nvvm.exit` | Terminates the thread; any preceding ordering must survive. |
| `llvm.nvvm.trap` | Aborts the device; same argument. |
| `llvm.nvvm.bar.warp.sync` | Intra-warp lane convergence; mask-only effects, not modeled by the SMEM maps. |
| `llvm.nvvm.cp.async.bulk.wait_group` | TMA bulk-copy completion wait; ordering is against the async DMA engine, not SMEM. |
| `llvm.nvvm.cluster.arrive.relaxed` | Cluster-wide CTA handshake; orders against the cluster fabric. |

The `exit` and `trap` cases are the conservative additions: any preceding sync may be the only thing guaranteeing a store visibility before the abort, and the analyzer has no way to prove otherwise without a full inter-block walk.

### SCEV Opcode Set for the Address Builder

Both maps key on a shared-memory address. The analyzer normalizes each address through a `ScalarEvolution` walk so that algebraically equal addresses written through different operand sequences collapse to the same map key. Anything outside the recognized opcode set becomes an opaque leaf, which is conservative: it widens to an address-class tag and keeps every barrier with a may-alias hit alive.

The recognized opcodes are exactly those that admit safe SCEV reordering across a sync without changing the address each thread computes:

- `getelementptr inbounds` — base case; produces the SCEV pointer leaf.
- `add nsw`, `add nuw` — folded into a single SCEV add with no-wrap flags preserved.
- `mul nsw`, `mul nuw` — folded into a SCEV mul, same flag handling.
- `shl nuw`, `shl nsw` — rewritten as `mul (1 << shamt)` and joined with the SCEV-mul chain.
- `sext`, `zext`, `trunc` — recursed past with the matching SCEV extension applied.
- `phi` — handled through the SCEV merge rule so loop-variant addresses stay symbolic.

Any other opcode (a load, an opaque call, a vector shuffle, a bitcast across address spaces) terminates the SCEV walk at that operand and forces the conservative address-class fallback.

### Algorithm

```c
LogicalResult deadSyncElim(Function *F) {
    for (BasicBlock &bb : *F) {
        SMEMState s = build_smem_state(&bb);     // empty maps initially

        for (Instruction &inst : bb) {
            if (is_sync_intrinsic(&inst)) {
                if (is_exempt_intrinsic(&inst)) {
                    s = split_maps_at(&inst, s); // refresh above/below split
                    continue;
                }

                if (cross_product_empty(s.write_above, s.read_below) &&
                    cross_product_empty(s.write_below, s.read_above)) {
                    emit_remark(&inst, "Removed dead synch: ", format_state(s));
                    inst.eraseFromParent();
                    continue;
                }

                s = split_maps_at(&inst, s);
                continue;
            }

            if (is_shared_load(&inst))  s.read_above.insert(addr_key_scev(inst));
            if (is_shared_store(&inst)) s.write_above.insert(addr_key_scev(inst));
        }
    }
    return success();
}
```

### Failure Modes

Three observable failure modes deserve mention:

- **Address-class collision keeps a barrier alive.** When `ScalarEvolution` cannot resolve the address (a non-constant index through an opaque pointer), both halves collapse to a single tag and any read/write pair on opposite sides keeps the barrier. The pass logs the conservative miss only under the verbose dump flag.
- **Cross-block dependences are invisible.** The maps are per-block. A barrier that separates a producer in one block from a consumer in another is never deletable here; that is a global dataflow problem that this pass intentionally avoids.
- **Wrong sync family on the exempt list.** A reimplementation that loses the exempt entry for `nvvm.exit` or `nvvm.trap` deletes the last fence before a process abort and silently changes observable store visibility. Diagnostics never fire for this case; the bug surfaces as memory inconsistency on the host side.

Every deletion emits the diagnostic `"Removed dead synch: "` followed by a four-line `"Read/Write above/below"` summary of the four maps. The `-print-dead-sync-elim` flag gates the dump.

## Common Base Elimination

### Input and Output IR Shape

Common Base Elimination is GEP-CSE with teeth. The syntactic CSE in InstCombine matches GEPs whose operand chains are literally identical; this pass uses LLVM `ScalarEvolution` to merge GEPs that share a common base pointer at the same SCEV-expression level. Two GEPs whose bases hash to the same SCEV key are mergeable even when their operand chains differ — a frequent shape after loop unrolling and affine-to-LLVM lowering, where one address is reached through algebraically equal but textually distinct sequences of `add`, `mul`, `shl`, and integer extensions.

```llvm
; before: two GEPs with textually distinct but SCEV-equal bases
define void @k(ptr %p, i64 %i) {
entry:
  %t0 = mul nsw i64 %i, 4
  %a0 = getelementptr i8, ptr %p, i64 %t0
  %v0 = load i32, ptr %a0, align 4

  %t1 = shl nsw i64 %i, 2          ; SCEV-equal to %t0
  %a1 = getelementptr i8, ptr %p, i64 %t1
  store i32 %v0, ptr %a1, align 4
  ret void
}

; after: one canonical base, second use becomes a delta-zero load/store
define void @k(ptr %p, i64 %i) {
entry:
  %t0 = mul nsw i64 %i, 4
  %scevcgp_0 = getelementptr i8, ptr %p, i64 %t0
  %v0 = load i32, ptr %scevcgp_0, align 4
  store i32 %v0, ptr %scevcgp_0, align 4
  ret void
}
```

### Matching Predicate

A pair of GEPs is mergeable iff their base expressions normalize to the same SCEV value under the visitor below. Once a group is identified, the pass picks a canonical representative, hoists it to a position that dominates every consumer, and rewrites the remaining members as the difference between their SCEV and the representative's SCEV.

### Dominance and the Alloca + PHI Argument

The merge is only correct when the canonical representative dominates every original use. Two alloca-based GEPs with the same allocation size and address space can share a single base safely iff their lifetimes don't overlap and at least one of them is reachable through an entry-block-dominated alloca. The argument: the function entry block dominates every basic block by construction, so any value materialized there dominates every use in the function. Cloning an alloca into the entry block lifts its dominance to that of the entry; cloning is the prerequisite for any merge where the original alloca lived in a block that didn't dominate every group member.

When the cloned base must flow through a CFG merge — a loop header, the join of an `if`/`else`, the post-dominator of a switch — a PHI node at the merge point assembles one incoming value per predecessor. The PHI is what makes the canonical base usable across loops and around branching regions where the entry-block alloca would not by itself reach every deduplicated GEP without an explicit dataflow merge.

### SCEV Visitor

The SCEV computation walks each GEP's IR operand graph through a small fixed opcode set. Anything outside the set becomes an opaque leaf and stops the recursion, which keeps the implementation robust against unfamiliar IR shapes:

- `getelementptr inbounds` is the base case and contributes the pointer-typed leaf of the SCEV.
- `add nsw` and `add nuw` are folded into a single SCEV add with the no-wrap flag preserved.
- `mul nsw` and `mul nuw` are folded into a single SCEV mul, again preserving no-wrap flags.
- `shl nuw` and `shl nsw` are converted to `mul (1 << shamt)` so they participate in the SCEV-mul chain.
- `sext`, `zext`, and `trunc` are recursed past, with the SCEV extension or truncation applied to the result.
- `phi` is recursed via the SCEV merge rule so loop-variant addresses stay symbolic rather than blocking the match.

### Driver and Body

The pass splits into an outer driver and an inner body. The driver walks every function and every basic block, emitting `"Processing X / Block Y"` diagnostics where X and Y are sequential counters for visited functions and blocks; the diagnostics double as a progress indicator on very large modules. The body runs per basic block and performs the actual rewrite: for each GEP it consults the SCEV cache, looks up an existing canonical representative in a hash from SCEV key to representative, and either records the GEP as the new representative or rewrites it as a delta off the existing one.

### IRBuilder Temporary Prefixes

Stable name prefixes mark every rewrite-produced IR value, so they jump out in dumps and `--print-after` traces. Four prefixes, each tied to a distinct role:

| Prefix | Meaning |
|---|---|
| `scevcgp_` | SCEV-canonicalised GEP, the merged representative produced by the CSE |
| `scevcgptmp_` | Temporary value holding a partial SCEV computation during materialisation |
| `baseValue` | Cloned alloca base pointer emitted into the function entry block |
| `bitCastEnd` | Optional bitcast applied when the merged GEP's pointer element type differs from a user's expected type |

The `bitCastEnd` cast lands only when the canonical representative's pointer element type does not match a specific user. Skipping the cast otherwise keeps the rewritten IR free of no-op casts that would otherwise survive into instruction selection.

### Tunables

Five `cl::opt` knobs configure the pass. Each takes effect at the next function the driver visits.

| Knob | Default | Meaning |
|---|---|---|
| `cbe-enable` | 1 | Master enable for the whole pass. |
| `cbe-max-depth` | 8 | Maximum SCEV-tree depth to consider when matching bases. |
| `cbe-max-iter` | 16 | Maximum number of CSE iterations per function before giving up. |
| `cbe-clone-allocas` | 1 | Enable the alloca-cloning step. |
| `cbe-min-uses` | 2 | Minimum number of uses before CSE fires on a candidate base. |

`cbe-max-depth` caps SCEV traversal cost on pathological index expressions. `cbe-max-iter` caps the outer fixed point: each iteration can expose new mergeable bases by replacing one GEP with a delta off another, and the bound prevents runaway behaviour on adversarial inputs. `cbe-min-uses` blocks rewrites on single-user GEPs, where the rewrite would add a PHI or a cast without saving any address arithmetic. With `cbe-clone-allocas` disabled, the alloca-cloning branch is skipped and any group whose base would have required cloning falls out of the merge — correct, but at the cost of some missed CSE on alloca-rooted addresses.

### Algorithm

```c
LogicalResult commonBaseElim(Function *F) {
    SCEVCache cache = computeSCEVAll(F);
    DenseMap<SCEVKey, GEP*> groups;

    for (BasicBlock &bb : *F) {
        for (Instruction &inst : bb) {
            if (auto *gep = dyn_cast<GetElementPtrInst>(&inst)) {
                if (gep->getNumUses() < cbe_min_uses) continue;

                SCEVKey key = scevOfBase(cache, gep);
                auto &rep = groups[key];

                if (!rep) { rep = gep; continue; }

                if (!dominates(rep, gep)) {
                    if (isa<AllocaInst>(rep_base(rep)) && cbe_clone_allocas)
                        cloneAllocaToEntry(F, rep);
                    insertMergePHIs(F, rep, gep);
                }

                Value *delta = buildDelta(cache, rep, gep);
                replaceWithCanonical(gep, rep, delta, /*bitcastIfNeeded=*/true);
            }
        }
    }
    return success();
}
```

### Failure Modes

- **Dominance lift fails.** When `cbe-clone-allocas` is off and the original alloca does not dominate every consumer, the group is discarded silently. The IR is unchanged; the only signal is a missing rewrite.
- **SCEV depth cap hit.** A deep index expression yields an opaque SCEV leaf at the cap, so distinct deep expressions never collide. The pass stays correct but misses the merge.
- **Iteration cap hit.** Each round may expose new mergeable bases; hitting `cbe-max-iter` leaves residual GEPs whose mergeability would have been visible to a later round. Increasing the cap is safe but linear in compile time.
- **Type mismatch without `bitCastEnd`.** A reimplementation that skips the bitcast on type mismatch produces ill-typed IR; the verifier rejects the function. The cast is mandatory whenever the representative and the user disagree on pointer element type.

The final materialisation rule: single-predecessor regions reuse the incoming base directly without a PHI; multi-predecessor regions need one incoming value per predecessor and a final `bitCastEnd` when the original pointer type differs from the canonical representative.

## Cross-References

[NVPTX Backend Passes Overview](overview.md#pipeline-position) places this pass at the tail of the LLVM-IR middle end, after [MemorySpaceOpt](memory-space-opt-and-process-restrict.md#memoryspaceopt) and before [NVVM IR Verifier](nvvm-ir-verifier.md). [BASR: Base-Address-Slice-Replace](peephole-mir-and-image-handles.md#basr-specific-algorithm) is the post-ISel MIR-level peephole that performs the analogous address-arithmetic fusion on selected machine instructions — Common Base Elimination is its IR-level counterpart.
