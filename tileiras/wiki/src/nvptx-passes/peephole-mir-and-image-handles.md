# NVPTX Peephole, MIR Cleanup, and Image Handles

## Abstract

This is the cleanup window around instruction selection. A post-ISel MachineIR peephole pass walks an 801-row pattern table and fuses address-arithmetic chains into their consumers; the same dispatcher hosts BASR (the central base-address-slice-replace fold) and the image-handle replacement that rewrites texture and surface operands from parameter handles into slot operands. The final MachineIR cleanup passes strip target pseudos, fix frame-index address forms, and tag invariant loads. Together they hand PTX printing concrete, target-legal instructions.

## Peephole MIR

The MachineIR peephole pass is the central post-ISel rewriter for NVPTX. It runs after instruction selection on the MachineFunction form and applies an 801-row pattern table that matches MIR sequences and rewrites them in place. The two canonical rewrites are BASR (Base-Address-Slice-Replace), which fuses a GEP-style base computation into its consuming load or store, and the image-handle table which is documented in its own section below.

### Input and Output MIR Shape

```text
input  (MIR, before peephole):
  %1:i64 = MUL64ri %iv, 4
  %2:p1  = ADD64rr %base, %1
  %3:i32 = LD32 %2, 0

output (MIR, after BASR):
  %3:i32 = LD32_BASE_SLICE_OFFSET %base, %iv, 4
```

The fused `LD32_BASE_SLICE_OFFSET` carries the base pointer, the slice (index) register, and the constant stride directly; the intermediate `MUL64` and `ADD64` MIs are dead and removed in the same pass. The same shape applies to stores and to a handful of address-arithmetic chains that ISel leaves around tensor-memory addresses.

### Pattern Table Structure

The pattern table is 801 rows, one per recognized rewrite. Each row carries:

- an `opcode_mask` field giving the set of MI opcodes that can trigger this row;
- a forward or backward direction marker selecting which chain walker to use;
- a per-row matcher that inspects the candidate MI's operands and predecessors (or successors);
- an emit function that constructs the replacement MI and erases the matched sequence.

Dispatch over `opcode_mask` is constant-time: the pass maintains an `opcode → row[]` index built once at module entry, so each MI scan does a single hash lookup rather than walking all 801 rows. The mask is a bitset over the 14 active opcode classes — GEP, LOAD, STORE, ADD, SUB, MUL, AND, OR, SHL, SHR, BITCAST, EXTRACT, INSERT, PHI. PHI is included so GEP bases threaded through loop headers can still be canonicalized; the BITCAST / EXTRACT / INSERT classes handle the pointer-typing pseudos NVPTX selection leaves around tensor-memory addresses.

### BasrState

Each MachineFunction visit allocates a `BasrState` scratch record that tracks per-basic-block state across pattern attempts:

```c
typedef struct BasrState {
    MachineFunction       *mf;
    DenseMap<unsigned, MachineInstr*> intern;     // canonical-base interning
    SmallVector<MachineInstr*, 16>    work_list;  // pending MIs to retry
    DenseMap<MachineInstr*, GepInfo>  gep_cache;  // memoized base decomposition
    uint64_t                          opcode_mask;
    bool                              debug_enabled;
} BasrState;
```

The intern map collapses syntactically distinct but semantically equal base computations to the same canonical MI, so a second occurrence of `base + i*4` reuses the first occurrence's BASR output instead of allocating new operands. The work list is drained in dominator order, which guarantees that uses of a folded base are rewritten before the base itself is erased.

### Forward and Backward Chain Walkers

The 801 rows split into forward and backward families:

- **Forward chain walker.** Matches sequences of N MIs starting at a given root, walking forward through users. A BASR row that fuses `MUL + ADD + LOAD` into `LD_BASE_SLICE_OFFSET` is forward-rooted at the `MUL`, then descends to the `ADD`, then to the `LOAD`. The walker stops at the first non-matching user or at a use that escapes the basic block under the row's locality requirement.
- **Backward chain walker.** Matches sequences ending at a given sink, walking backward through defs. Dead-code-style rewrites — where the consumer is the trigger and the producers are folded into it — use the backward walker. An `LD32` row that absorbs a preceding `ADD64` into its addressing mode is backward-rooted at the load and traces defs back through the `ADD` to the `MUL`.

The split exists because some patterns are cheaper to match top-down (a single root with many possible tails) and others bottom-up (a single sink with many possible heads). The dispatcher picks the right walker per row from the direction marker; the row itself does not see the choice.

### Invariant-Load Whitelist

Certain loads are never rewritten away even when the pattern table's matcher claims a fold. The whitelist is the set of loads whose result is observably stable across all reachable program points, so any rewrite that erases or reorders them changes the program:

- loads of program-counter-relative globals (CUDA kernel constants emitted into `.text`);
- loads from the constant address space (`addrspace(4)`);
- loads with `!invariant.load` metadata;
- loads from grid-constant parameters;
- loads from the special-register file (thread/block/grid IDs, `clock64`, `globaltimer`).

The whitelist is enforced as the first check in every row's emit function: if the candidate matches the load shape but is on the whitelist, the row's emit short-circuits and the pass continues. Removing a row's whitelist check produces a kernel that loses its broadcasted constants, which manifests as nondeterministic kernel outputs depending on warp scheduling.

### BASR-Specific Algorithm

```c
void run_basr(MachineFunction *mf, BasrState *s) {
    seed_work_list(s, mf);
    while (!s->work_list.empty()) {
        MachineInstr *mi = s->work_list.pop_back();
        for (Pattern *row : pattern_table_for_opcode(mi->opcode)) {
            if (row->direction == FORWARD) {
                if (match_forward_chain(s, mi, row)) {
                    emit_replacement(s, mi, row);   // erases matched chain
                    requeue_users(s, mi);
                    break;
                }
            } else {
                if (match_backward_chain(s, mi, row)) {
                    emit_replacement(s, mi, row);
                    requeue_defs(s, mi);
                    break;
                }
            }
        }
    }
}
```

A `cl::opt<bool>` named `-print-basr` turns on the BASR debug print. When set, BASR emits `"phi maxLoopInd = "` followed by the current loop induction-variable count for every MachineFunction it visits, so a `-print-basr` run shows the loop-nest depth the rewriter sees at each entry.

### Failure Modes

- **Pattern miss leaves redundant arithmetic.** A row that fails to match because its operand shape diverges from the canonical form (a different operand order, a non-constant stride) leaves the original `MUL + ADD + LOAD` chain. Correct but suboptimal.
- **Whitelist erosion changes program semantics.** A reimplementation that loses the invariant-load whitelist will fold an `LD32` of a kernel constant into an addressing-mode field and erase the original constant load; downstream consumers see uninitialized data.
- **Dominator-order violation requeues forever.** The work list is drained in dominator order on purpose. A naive FIFO can requeue an instruction whose dependency has not been rewritten yet, leading to oscillation. The intern map breaks the cycle, but losing it causes the pass to fail to terminate on adversarial inputs.

## Image Handle Replacement

### Input and Output MIR Shape

The image-handle pass is a MachineFunction pass operating on selected NVPTX MachineIR. It rewrites parametric-form texture and surface MIs into slot-form MIs immediately before PTX printing.

```text
input  (MIR, parametric form):
  %h:p4 = LD_PARAM_p4 %image_arg_offset             ; load handle from .param space
  %v:v4f32 = TEX_2D_F32_F32_param %h, %x, %y

output (MIR, slot form):
  %v:v4f32 = TEX_2D_F32_F32_slot 3, %x, %y          ; slot 3 of the texture-unit register file
```

The slot is the runtime register-file index that the CUDA driver binds to the texture or surface object at launch. The parametric opcode is one of 801 cases across four families (`tex`, `sust`, `suld`, `suq`); each one has a sibling slot opcode in a parallel table that the rewriter looks up directly.

### Matching Predicate

A MachineInstr matches iff its opcode is a `*_param` form in one of the four families and every operand resolves to a kernel image-argument handle. The handle resolution is the analytical core: the actual `TEX_*_param` MI may not see the handle as a direct operand because MIR has rerouted it through `COPY` and `PHI` instructions, so the pass uses two chain walkers to trace the virtual-register definition back to a kernel image-argument table entry.

| Helper | Direction |
|---|---|
| Forward chain walker | follows uses through `COPY` / `PHI` toward the consumer |
| Backward chain walker | follows defs through `COPY` / `PHI` back to the handle argument |

The forward walker is what the consumer uses to discover that a particular handle definition reaches it; the backward walker is what the consumer uses to find the slot.

### Rewrite Tables

Each family carries its own opcode rewrite table. The transformation is a single integer indexed lookup: each `_param` opcode value has a sibling `_slot` opcode value at a fixed offset, so the lookup is a direct index from the parametric opcode value to the slot opcode value.

| Family | Cases | PTX op family |
|---:|---:|---|
| `tex` | 165 | `tex.*` |
| `sust` | 210 | `sust.*` |
| `suld` | 258 | `suld.*` |
| `suq` | 168 | `suq.*` |

The four tables together cover all 801 image-handle opcodes.

### Algorithm

```c
void rewriteImageHandles(MachineFunction *mf) {
    ImageArgTable images = collect_kernel_image_arguments(mf);

    for (MachineBasicBlock &mbb : *mf) {
        for (MachineInstr &mi : mbb) {
            if (!is_image_param_opcode(mi.getOpcode())) continue;

            ImageHandle handle = trace_image_handle_backward(&mi);
            ImageSlot slot = images.lookup(handle);
            if (!slot.valid) {
                emit_error(mi, "invalid image handle");
                continue;
            }

            unsigned slot_op = slot_opcode_for(mi.getOpcode());  // table lookup
            mi.setOpcode(slot_op);
            replace_handle_operand_with_slot(&mi, slot);
        }
    }
}
```

### Failure Modes

- **Handle does not resolve to a kernel argument.** A handle threaded through an opaque pointer or a non-image global makes the backward walker terminate at a non-image-arg definition; the pass emits a diagnostic and leaves the MI as a `*_param` form, which the PTX printer cannot handle. This is a hard error.
- **Slot table lookup miss.** A `*_param` opcode without a sibling `*_slot` entry in the family table indicates a pattern the rewriter does not cover; the pass leaves the MI in place and the printer fails downstream.
- **Family confusion.** A reimplementation that picks the wrong family table for an opcode produces a slot MI of the wrong family — e.g. a `tex` opcode mapped through the `sust` table — and the GPU traps at runtime when the texture unit decodes the wrong descriptor format.

## MachineIR Peepholes

The post-ISel peephole pass strips target pseudos that were useful during selection but illegal for printing. The central canonical cleanup is frame-index address folding: a temporary local address move followed by a local-address conversion can often be replaced by the frame index itself.

```c
void run_machine_peepholes(MachineFunction mf) {
    for (MachineBasicBlock mbb : mf.blocks) {
        for (MachineInstr mi : mbb.instructions) {
            if (is_local_cvta_of_frame_address(mi)) {
                replace_uses_with_frame_index(mi);
                erase_dead_address_pseudos(mi);
            }

            if (matches_target_specific_copy_chain(mi)) {
                fold_copy_chain(mi);
            }
        }
    }
}
```

Gate target-specific copy-chain folding behind a command-line or build-time option — it is more sensitive to TableGen opcode layout than the canonical frame-address fold.

## Prolog/Epilog, Proxy Registers, and Invariant Loads

The remaining MachineIR cleanup is conventional NVPTX target work:

| Pass | Contract |
| --- | --- |
| Prolog/Epilog | Lay out frame objects, replace frame indices, and emit target prolog/epilog code. |
| Proxy register erasure | Replace proxy-register pseudos with the real source register and erase the pseudos. |
| Invariant-load tagging | Mark loads as invariant only when all bounded uses preserve the invariant contract. |

Invariant-load tagging should be conservative. Parameter, constant, and global loads usually qualify when their use graph is simple. Tensor-memory loads stay off the whitelist unless their selected opcodes and memory operands already carry the needed semantics.

```c
bool load_can_be_invariant(MachineInstr load) {
    if (!address_space_allows_invariant_load(load.mem_operand.space)) {
        return false;
    }

    for (MachineInstr user : bounded_use_graph(load, MAX_INVARIANT_DEPTH)) {
        if (!is_allowed_invariant_use(user)) {
            return false;
        }
    }

    return true;
}
```

## Cross-References

[ISel DAG and MatcherTable](../codegen/iseldag-and-matchertable.md) is what feeds BASR with the post-selection `MachineInstr` opcodes it folds. [Common Base Elimination](dead-sync-elim-and-common-base.md#common-base-elimination) is the IR-level sibling that performs the analogous GEP-CSE before SelectionDAG runs. [AsmPrinter](../codegen/asm-printer-monster-and-windows.md) is the PTX-printing consumer that requires the slot-form image opcodes this pass produces. [NVPTX Backend Passes Overview](overview.md#pipeline-position) places these MIR cleanup passes after instruction selection.
