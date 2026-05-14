# Dead Sync Elimination and Common Base Elimination

## Abstract

Two NVPTX middle-end passes attack different forms of redundancy. Dead Sync Elimination deletes barriers that don't separate visible memory traffic. Common Base Elimination collapses repeated address arithmetic by hoisting a shared base pointer and rewriting related GEP chains as deltas off that base. Both passes lean conservative: a missed optimization is acceptable, but deleting a required barrier or changing pointer provenance is not.

## Dead Sync Elimination

DeadSyncElim deletes `nvvm.barrier0` and the other synchronisation intrinsics whose only purpose is to order shared-memory traffic — when neither side of the barrier actually touches shared memory. The body at `sub_27DA690` (11.6 KB, 815 basic blocks) walks each function with a per-block worklist. Enable flag `byte_5B6A640` is a `cl::opt` set to `1` by default; flipping it to `0` disables the pass without a rebuild.

Each candidate barrier carries four `std::map<uint64_t, uint8_t>` instances on the per-block state record at slots `a1[39..58]`. Keys are SMEM addresses (or address-class tags when the address isn't a constant); values are one-byte access summaries. Splitting reads and writes into "above" and "below" reduces the test to a single equation per barrier, sidestepping a full bidirectional dataflow.

| Map | Meaning |
| --- | --- |
| `read_above` | SMEM addresses read before the barrier inside the same basic block |
| `write_above` | SMEM addresses written before the barrier inside the same basic block |
| `read_below` | SMEM addresses read after the barrier inside the same basic block |
| `write_below` | SMEM addresses written after the barrier inside the same basic block |

A barrier is dead exactly when `(write_above × read_below) ∪ (write_below × read_above)` is empty: no producer-consumer pair on shared memory crosses it. This cross-product test is the central correctness predicate. Address-class collisions count as may-alias and keep the barrier alive.

`sub_40D0CF0`, exported as `isBarrierIntrinsic(MachineInstr *mi)`, returns true when the opcode appears in the barrier intrinsic table. Three IDs are permanently exempted even when the dataflow declares them dead — their effects reach past shared memory in ways the lightweight scan cannot see:

| Intrinsic ID | Name | Reason for exemption |
| --- | --- | --- |
| `7` | `nvvm.bar.warp.sync` | Intra-warp lane synchronisation; mask-only effects |
| `296` | `nvvm.cp.async.bulk.wait_group` | TMA bulk-copy completion wait |
| `380` | `nvvm.cluster.arrive.relaxed` | Cluster-wide handshake across CTAs |

Every deletion emits the diagnostic `"Removed dead synch: "` (verbatim, trailing space included) followed by a four-line `"Read/Write above/below"` summary of the four maps. The `-print-dead-sync-elim` flag gates the dump, and the dump is the primary debugging surface when a kernel misbehaves after the pass runs.

One implementation detail bears mentioning. Per-block scratch is keyed in a sentinel three-element ilist by computing the key as `(uintptr_t)BB - 3`, so the ilist's terminator `nullptr` cannot collide with a valid map entry. This is the standard LLVM ilist tagless-key idiom, and it explains why the slot offsets above are `a1[39..58]` rather than starting at a round number.

```c
LogicalResult deadSyncElim(Function *F) {
    for (BasicBlock &bb : *F) {
        State s = computeState(&bb);

        for (Instruction *barrier : findBarriers(&bb)) {
            uint32_t id = getIntrinsicID(barrier);

            if (id == 7 || id == 296 || id == 380) {
                continue;
            }

            if (isDead(s, barrier)) {
                emit("Removed dead synch: " + format(s));
                barrier->eraseFromParent();
            }
        }
    }

    return success();
}
```

The candidate set otherwise spans CTA barriers, named barriers, warp-scope barriers, and cluster barriers — every barrier whose semantics surface through ordinary shared-memory ordering. Tensor-memory waits and WGMMA fences go through their own target-specific lowering and must not be folded here unless their memory-order contract is explicitly modeled in the four-map state.

## Common Base Elimination

Common Base Elimination is GEP-CSE with teeth. The syntactic version in InstCombine matches GEPs whose operand chains are literally identical; this pass uses LLVM `ScalarEvolution` to merge GEPs that share a common base pointer at the same SCEV-expression level. Two GEPs whose bases hash to the same SCEV key are mergeable even when their operand chains differ — a frequent shape after loop unrolling and affine-to-LLVM lowering, where one address is reached through algebraically equal but textually distinct sequences of `add`, `mul`, `shl`, and integer extensions. Once a group is identified, an `AllocaCloner` plus PHI nodes materialise a single canonical base and the remaining group members get rewritten as deltas off it.

### Driver and Body

An outer driver and an inner per-function body split the work. The driver `sub_27F91D0` walks every function and every basic block, emitting `"Processing X / Block Y"` diagnostics where X and Y are sequential counters for visited functions and blocks. The diagnostics are the user-visible output of `-debug-pass=common-base-elim` and double as a progress indicator on very large modules.

The CSE engine itself lives in `sub_27F7D20`. For each function it visits every GEP, computes the base-expression SCEV through the SCEV analysis cache, and groups matching GEPs under a single canonical representative. The representative is rewritten to dominate every group member; the remaining members become deltas off it.

### IRBuilder Temporary Prefixes

Stable name prefixes mark every rewrite-produced IR value, so they jump out in dumps and `--print-after` traces. Four prefixes, each tied to a distinct role:

| Prefix | Meaning |
|---|---|
| `scevcgp_` | SCEV-canonicalised GEP, the merged representative produced by the CSE |
| `scevcgptmp_` | Temporary value holding a partial SCEV computation during materialisation |
| `baseValue` | Cloned alloca base pointer emitted into the function entry block |
| `bitCastEnd` | Optional bitcast applied when the merged GEP type differs from the original use |

The `bitCastEnd` cast lands only when the canonical representative's pointer element type does not match a specific user. Skipping the cast otherwise keeps the rewritten IR free of no-op casts that would otherwise survive into instruction selection.

### Alloca Cloning and PHI Insertion

When a merged GEP's base is an alloca, the canonical representative must dominate every consumer. Two helpers cooperate. `sub_27E5340` is the AllocaCloner: it clones the original alloca into the function entry block, where it dominates the entire function, and rewires the original uses to refer to the clone. The cloned alloca carries the `baseValue` name prefix. `sub_27E21D0` is the companion PHI inserter — when the cloned base must be visible across a CFG merge, it places a PHI at the merge point with one incoming value per predecessor so the cloned base flows through control flow without partial-dominance bugs. That PHI is what makes the canonical base usable across loops and around `if`/`else` regions where the original alloca would not have dominated every deduplicated GEP.

### Tunables

Five `cl::opt` knobs configure the pass. Each is backed by a `.bss` slot the body reads directly without a wrapper accessor, so a runtime change takes effect on the next function.

| Knob | bss slot | Default | Meaning |
|---|---|---|---|
| `cbe-enable` | `dword_5B6B7C0` | 1 | Master enable for the whole pass |
| `cbe-max-depth` | `qword_5B6B880` | 8 | Maximum SCEV-tree depth to consider when matching bases |
| `cbe-max-iter` | `qword_5B6AEC0` | 16 | Maximum number of CSE iterations per function before giving up |
| `cbe-clone-allocas` | `byte_5B6AF80` | 1 | Enable the AllocaCloner step |
| `cbe-min-uses` | `dword_5B6B940` | 2 | Minimum number of uses before CSE fires on a candidate base |

`cbe-max-depth` caps SCEV traversal cost on pathological index expressions. `cbe-max-iter` caps the outer fixed point: each iteration can expose new mergeable bases by replacing one GEP with a delta off another, and the bound prevents runaway behaviour on adversarial inputs. `cbe-min-uses` blocks rewrites on single-user GEPs, where the rewrite would add a PHI or a cast without saving any address arithmetic. With `cbe-clone-allocas` disabled, the AllocaCloner branch is skipped and any group whose base would have required cloning falls out of the merge — correct, but at the cost of some missed CSE on alloca-rooted addresses.

### SCEV Visitor

The SCEV computation walks each GEP's IR operand graph through a small fixed opcode set. Anything outside the set becomes an opaque leaf and stops the recursion, which keeps the implementation robust against unfamiliar IR shapes:

- `getelementptr inbounds` is the base case and contributes the pointer-typed leaf of the SCEV.
- `add nsw` and `add nuw` are folded into a single SCEV add with the appropriate no-wrap flag.
- `mul nsw` and `mul nuw` are folded into a single SCEV mul, again preserving no-wrap flags.
- `shl` is converted to `mul (1 << shamt)` so it can participate in the same SCEV-mul nodes.
- `sext`, `zext`, and `trunc` are recursed past, with the SCEV extension or truncation applied to the
  result of the recursion.
- `phi` is recursed via the SCEV merge rule so loop-variant addresses are handled symbolically rather
  than blocking the match.

### Pseudocode

```c
LogicalResult commonBaseElim(Function *F) {
    SCEVCache cache = computeSCEVAll(F);
    DenseMap<SCEVKey, GEP*> groups;
    for (GEP *gep : F->getAllGeps()) {
        SCEVKey key = scevOfBase(cache, gep);
        if (auto &existing = groups[key]) {
            if (sub_27E5340(F, &existing))    cloneAllocaToEntry(F, existing);  // AllocaCloner
            replaceGep(gep, existing, /*phi=*/sub_27E21D0(gep));
        } else {
            groups[key] = gep;
        }
    }
    return success();
}
```

The driver wraps this body in the outer per-function loop and owns the diagnostic counters. A naive replacement loses the dominance property whenever the original GEP base is an alloca that doesn't dominate all the deduplicated uses — exactly the case the AllocaCloner plus PHI insertion exists to handle, and why the pass produces correct IR even when the merged group spans multiple basic blocks.

The final materialisation rule: single-predecessor regions reuse the incoming base directly without a PHI; multi-predecessor regions need one incoming value per predecessor and a final `bitCastEnd` when the original pointer type differs from the canonical representative.

## Cross-References

[NVPTX Backend Passes Overview](overview.md#pipeline-position) places this pass at the tail of the LLVM-IR middle end, after [MemorySpaceOpt](memory-space-opt-and-process-restrict.md#memoryspaceopt) and before [NVVM IR Verifier](nvvm-ir-verifier.md). [BASR: Base-Address-Slice-Replace](peephole-mir-and-image-handles.md#basr-base-address-slice-replace) is the post-ISel MIR-level peephole that performs the analogous address-arithmetic fusion on selected machine instructions — Common Base Elimination is its IR-level counterpart.
