# NVPTX Peephole, MIR Cleanup, and Image Handles

## Abstract

This is the cleanup window around instruction selection. An IR-level NVVM peephole pass simplifies address arithmetic before SelectionDAG sees it. A MachineIR image-handle pass rewrites texture and surface operands from parameter handles into slot operands. The final MachineIR cleanup passes strip target pseudos, fix frame-index address forms, and tag invariant loads. Together they hand PTX printing concrete, target-legal instructions.

## BASR: Base-Address-Slice-Replace

`BASR` is the central NVPTX-MIR peephole rewriter. It runs after instruction selection on the MachineFunction form (the MIR equivalent of MLIR's `func.func`) and hunts redundant GEP base computations that survived selection. When a GEP feeds a load or store and both halves match a fixed pattern, BASR fuses them into a single `BASE_SLICE_OFFSET` MI, collapsing the redundant address arithmetic into the consumer.

Two procedures split the work. The core at `sub_2800C10` (13.3 KB, 642 basic blocks) walks each `MachineFunction`, inspects each `MachineInstr`, matches the GEP+LOAD or GEP+STORE pattern, and emits the fused opcode when both halves agree. The outer driver at `sub_2804670` (12 KB, 632 BB) wraps the core in the MachineFunctionPass iteration loop and emits a `"phi maxLoopInd = "` debug print before each iteration, logging the loop-nest depth BASR is about to traverse.

A standard LLVM `PassInfo` quad at `0x2807AE0` (56 bytes: id, short name, long name, ctor pointer) advertises the pass. The short name — used by `-print-after-all` and the debug pass registry — is `"BASR"`; the long name is `"Base-Address-Slice-Replace"`.

### Per-Function State

Each `MachineFunction` visit allocates a 344-byte `BasrState` scratch struct: the working pointer to the current MF, the work-list of pending MIs, an open-addressed intern table for canonicalized GEP bases, a DenseMap from `MachineInstr*` to the cached GEP-info for that instruction, the active opcode-class mask, and the debug flag.

```c
typedef struct BasrState {
    /*+0x000*/ MachineFunction       *mf;
    /*+0x008*/ uint64_t              *intern_buckets;   // open-addressed: -8192 tomb, -4096 empty
    /*+0x010*/ uint32_t               n_buckets;
    /*+0x014*/ uint32_t               n_live;
    /*+0x020*/ MachineInstr         **work_list;        // 24-strided SmallVector
    /*+0x028*/ uint32_t               n_work;
    /*+0x040*/ uint64_t               opcode_mask;      // 14 bits, see below
    /*+0x080*/ DenseMap<MI*, GepInfo> gep_cache;        // 72-B slot stride
    /*+0x150*/ uint8_t                debug_enabled;    // from qword_5B6BC40
} BasrState;
```

The intern table uses the LLVM convention of sentinel keys `-8192` (tombstone) and `-4096` (empty), keeping erase cheap without rehashing. The work-list is a 24-byte-strided SmallVector seeded from the function's instruction stream and drained in dominator order, so uses of a folded base are always rewritten before the base itself is erased.

### Opcode-Tag Dispatch

The 14-bit `opcode_mask` selects which MI opcode classes participate in folding. Dispatch flows through `sub_3B6DCD0`, the SubclassID table reader that maps each MI opcode to a 4-bit class index; the inline `case 1..F` switch in `sub_2800C10` then routes the MI to its class-specific handler. The 14 active classes are GEP, LOAD, STORE, ADD, SUB, MUL, AND, OR, SHL, SHR, BITCAST, EXTRACT, INSERT, and PHI. PHI is included so GEP bases threaded through loop headers can still be canonicalized; the BITCAST/EXTRACT/INSERT classes handle the pointer-typing pseudos NVPTX selection leaves around tensor-memory addresses.

### Debug Knob

A `cl::opt<bool>` at `qword_5B6BC40` is populated by the `-print-basr` flag. When set, BASR emits `"phi maxLoopInd = "` followed by the current loop induction-variable count for every MachineFunction it visits, so a `-print-basr` run shows the loop-nest depth the rewriter sees at each entry. The same flag gates the `BasrState::debug_enabled` byte that per-class handlers consult before emitting their finer-grained `dbgs()` prints.

## Image Handle Replacement

The image-handle pass is a MachineFunction pass operating on selected NVPTX
MachineIR (not MLIR ops). It rewrites parametric-form texture and surface MIs
into slot-form MIs immediately before PTX printing.

```text
input  (MI, parametric form):
  %v = TEX_2D_F32_F32_param %tex_handle_arg, %x, %y

output (MI, slot form):
  %v = TEX_2D_F32_F32_slot   slot=3, %x, %y
```

The slot is the runtime register-file index that the CUDA driver binds to the
texture or surface object at launch. The parametric opcode is one of 801 cases
across four families (`.tex`, `.sust`, `.suld`, `.suq`); each one has a sibling
slot opcode at the stride-2 offset that the rewrite tables encode.

Texture, surface-load, surface-store, and surface-query instructions arrive carrying kernel ABI parameter handles, but MachineIR needs slot-indexed operands. The image-handle pass walks copies and handle-move pseudos back to the kernel image-argument table, computes the slot, and rewrites the opcode from its parameter form to its slot form.

```c
void replace_image_handles(MachineFunction mf) {
    ImageArgTable images = collect_kernel_image_arguments(mf);

    for (MachineInstr mi : mf.instructions) {
        if (!is_texture_or_surface_instruction(mi)) {
            continue;
        }

        ImageHandle handle = trace_image_handle(mi);
        ImageSlot slot = images.lookup(handle);

        if (!slot.valid) {
            error(mi, "invalid image handle");
        }

        mi.opcode = slot_opcode_for(mi.opcode);
        replace_handle_operand_with_slot(mi, slot);
    }
}
```

The pass is family-aware. Texture, surface-load, surface-store, and surface-query instructions have different operand layouts, so each family carries its own slot computation and validation.

## Image-Handle Rewrite Tables

Every CUDA image-handle operation (`.tex`, `.sust`, `.suld`, `.suq`) gets rewritten from parametric form (`*_param_*` MI opcodes) into slot form (`*_slot_*` opcodes) at MIR pre-emission. Four dedicated opcode tables totalling 801 cases drive the rewrite. The transformation is mechanical: each `_param` MI opcode has a sibling `_slot` MI opcode at a fixed stride-2 offset, so the table lookup is a direct index from the parametric opcode value to the slot opcode value.

The four opcode rewrite tables live in `.rodata` at the following addresses:

| Family | Address | Cases | Opcode range | PTX op family |
|---|---|---:|---|---|
| `.tex` | `0x1AE5B30` | 165 | 3392–3819 | `tex.*` |
| `.sust` | `0x1AE5F30` | 210 | 3833–4293 | `sust.*` |
| `.suld` | `0x1AE6440` | 258 | 4644–5157 | `suld.*` |
| `.suq` | `0x1AE6A70` | 168 | 4643–5133 | `suq.*` |

Each row is a 4-byte `u32` mapping `param_opcode → slot_opcode` at a stride-2 increment. The driver at `0x1AEA3B0` walks the function, looks up each MI's opcode in the appropriate family table, and rewrites in place. The four tables together cover all 801 image-handle cases; nothing else in the backend reads them.

Image-handle MIs often flow through `COPY` and `PHI` instructions in MIR before reaching the consuming `.tex` / `.sust` / `.suld` / `.suq` op. A pair of chain walkers traces a virtual-register definition back to its original `nvvm.tex_handle_arg` source.

| Helper | Address | Direction |
|---|---|---|
| Forward chain walker | `sub_1AE7BB0` | follows uses through `COPY` / `PHI` toward the consumer |
| Backward chain walker | `sub_1AE94B0` | follows defs through `COPY` / `PHI` back to the handle arg |

The two-step lowering (`_param` first, `_slot` second) exists because upstream LLVM emits `*_param` MIs at MIR-build time, when parameter-AS is the only addressing mode visible. Final PTX `tex` / `sust` instructions take a slot index — the image-handle's runtime slot in the texture-unit register file — not a `*_param` pointer. The rewriter at `0x1AEA3B0` is the only consumer of the four tables; no other pass reads them.

```c
void rewriteImageHandles(MachineFunction *mf) {
    for (MachineBasicBlock &mbb : *mf) for (MachineInstr &mi : mbb) {
        uint32_t op = mi.getOpcode();
        if (op >= 3392 && op <= 3819) { mi.setOpcode(tex_tab[op - 3392]);   continue; }
        if (op >= 3833 && op <= 4293) { mi.setOpcode(sust_tab[op - 3833]);  continue; }
        if (op >= 4644 && op <= 5157) { mi.setOpcode(suld_tab[op - 4644]);  continue; }
        if (op >= 4643 && op <= 5133) { mi.setOpcode(suq_tab[op - 4643]);   continue; }
    }
}
```

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
