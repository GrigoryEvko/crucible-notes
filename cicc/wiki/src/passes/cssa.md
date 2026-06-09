# CSSA — Conventional SSA for GPU Divergence

Standard SSA form assumes that a PHI node selects its incoming value based solely on the control flow edge along which execution arrived. On a scalar CPU, exactly one predecessor edge is taken per dynamic execution of the PHI, so this assumption holds trivially. On an NVIDIA GPU, it does not. A warp of 32 threads executes in lockstep, and when control flow diverges — different threads take different branches — all paths are eventually serialized and the warp reconverges. At the reconvergence point, a standard PHI node cannot correctly select a single incoming value because the warp carries live values from *multiple* predecessors simultaneously. The wrong thread could see the wrong value.

CSSA (Conventional SSA) is NVIDIA's transformation that rewrites the IR so that every PHI node is safe under warp-divergent execution. It does this by inserting explicit copy instructions at points where threads reconverge, ensuring that each thread's value is materialized into its own copy before the PHI merges anything. The name "Conventional SSA" comes from the SSA literature: a program is in CSSA form when every PHI node's operands can be simultaneously live without interfering — the PHI web has no overlapping live ranges. This property is exactly what GPU divergence demands.

| | |
|---|---|
| **Pass location** | `sub_3720740` (3,521 bytes native, 165 basic blocks, 17 callees) |
| **Address range** | `0x3720740`--`0x3721501` |
| **Single caller** | `sub_3721510` (pass driver thunk) |
| **Gate knob** | `do-cssa` (NVVMPassOptions boolean toggle) |
| **Coalesce knob** | `cssa-coalesce` (controls copy coalescing aggressiveness) |
| **Debug knobs** | `cssa-verbosity`, `dump-before-cssa` |
| **Container knob** | `CSSACoalescing` (NVVM container format, parsed at `sub_CD9990`) |
| **Debug string** | `"IR Module before CSSA:\n"` (sole string ref in pass body) |
| **Helper cluster** | `sub_371F790` (4014B, 197bb) PCP propagator, `sub_371F160` (1079B) copy materializer, `sub_371EDF0` (875B) inserter, `sub_371CDC0` (227B) instruction factory |
| **NVVM opcode** | `0x22D7` (8919) — divergence-safe copy mnemonic |
| **Pass-option slot** | One of the 221 NVVMPassOptions slots (boolean do/don't pair) |
| **Pipeline position** | Late IR, after optimization, before SelectionDAG lowering |
| **Upstream equivalent** | None. LLVM has no concept of warp-divergent PHI semantics. |

## GPU Divergence Background

### The Warp Execution Model

NVIDIA GPUs execute threads in warps of 32 under the [SIMT model](../gpu-execution-model.md#simt-warp-execution): all threads share a program counter, and [divergent branches](../gpu-execution-model.md#divergence) serialize both paths before the warp reconverges. The full warp execution model and its implications for cicc are documented in the [GPU Execution Model](../gpu-execution-model.md).

### Why Standard SSA Breaks

Consider a diamond CFG:

```text
         entry
        /     \
     then     else
        \     /
         join        <-- PHI(%x = [then: %a], [else: %b])
```

On a CPU, the PHI at `join` works correctly: execution came from exactly one predecessor, so the PHI selects the corresponding value. On a GPU warp where threads 0-15 took `then` and threads 16-31 took `else`, both paths executed sequentially. When the warp reconverges at `join`, the PHI must produce `%a` for threads 0-15 and `%b` for threads 16-31 *simultaneously in the same register*. A naive lowering of the PHI to a simple register copy is incorrect — whichever path executed last would overwrite the value from the first path.

### The CSSA Solution

CSSA transforms the IR so that the PHI web has non-interfering live ranges. Concretely, it inserts copy instructions at the end of each predecessor block so that each thread's value is written into a dedicated copy *before* the warp reconverges:

```text
         entry
        /     \
     then     else
     %a_copy  %b_copy     <-- inserted copies (one per predecessor)
        \     /
         join
     %x = PHI [then: %a_copy], [else: %b_copy]
```

Now the PHI's operands occupy distinct virtual registers. During later register allocation, the allocator can assign them the same physical register only when their live ranges truly do not overlap — which is the correct condition for divergent execution. The copies give the allocator the freedom to keep the values separate when divergence requires it.

## Algorithm

`sub_3720740` is organized as five distinct phases driven off a pass-state struct (the `Function`-level `a1` argument plus the `PassState` at `a1[1]`). The phase-1 latch byte at `[PassState+0x70]` lets the driver re-enter the pass without redoing block numbering: if the latch is set, only phases 2–5 run again on the updated CFG.

The five phases below correspond to the four major loop nests in the decompiler output (`0x3720740`--`0x37208BB` numbering, `0x37208C0`--`0x3720A28` PHI map, `0x3720A2F`--`0x3720A88` propagation + cleanup, with the copy-insertion block at `0x3720B40`--`0x3720E18` invoked from inside the PHI map walk).

### Phase 1: Basic Block Ordering and Numbering — HIGH

The function begins by walking every basic block in the LLVM `Function` (accessed via `[r15]` -> Module pointer) and stamping each block with a preorder index at offset `+0x48` and a reverse-postorder index at offset `+0x4C`. These indices are used downstream for dominance/reconvergence queries and to keep the inserted copies in a deterministic position relative to terminators. The block worklist lives on the stack in `v130[]` and grows via `sub_C8D5F0` (the standard SmallVector growth helper, see [Hash and Container Infrastructure](../infra/hash-infrastructure.md)).

```c
/* Phase 1: assign BB preorder + RPO numbers; idempotent via latch byte. */
void cssa_phase1_number_blocks(Function *F, PassState *PS) {
    if (PS->blocks_numbered) {           /* byte [PS+0x70] */
        PS->next_phi_id = 0;             /* dword [PS+0x74] reset */
        return;                          /* skip straight to phase 2 */
    }

    BasicBlock *entry = F->entry_block;  /* [F+0x60] */
    if (!entry) return;

    /* Iterative DFS with a SmallVector worklist (initial inline cap = 32). */
    Frame stack[32];                     /* v130 inline buffer */
    Frame *sp = stack;
    size_t cap = 32, len = 0;

    sp[len++] = (Frame){ entry, entry->succ_begin };
    entry->preorder = 0;                 /* [BB+0x48] */
    uint32_t preorder_counter = 1;
    uint32_t postorder_counter = 0;

    while (len) {
        Frame *top = &sp[len - 1];
        BasicBlock *bb = top->bb;
        SuccIter *it = top->it;

        if (it == bb->succ_end) {
            bb->rpo_index = postorder_counter++;   /* [BB+0x4C] */
            --len;
            continue;
        }

        BasicBlock *succ = *it++;
        top->it = it;

        if (succ->preorder == UINT32_MAX) {
            succ->preorder = preorder_counter++;
            if (len + 1 > cap) {
                cap = smallvec_grow(&sp, cap, len + 1, sizeof(Frame));
            }
            sp[len++] = (Frame){ succ, succ->succ_begin };
        }
    }

    PS->next_phi_id = 0;
    PS->blocks_numbered = 1;             /* latch */
}
```

The latch (byte `[PS+0x70]`) makes phase 1 strictly run-once per `Function`; later re-entries (for example after the pass driver discards and rerunning a verifier) skip directly to phase 2 with a reset PHI counter. HIGH confidence (latch read at `0x37207C0`, write at `0x3720881`).

### Phase 2: PHI Discovery and Hash Map Population — HIGH

After numbering, the pass walks every block (outer loop at `0x37208C0`), and within each block walks the instruction use-list at offset `+0x18` (inner loop at `0x3720930`). The block-list bounds come from the sentinel pointers `[F+0x48]` (begin) and `[F+0x50]` (end); each instruction header carries its LLVM opcode in the byte at `[I-0x18]`. PHI nodes are opcode `0x54` (decimal 84).

```c
/* Phase 2: every PHI gets a monotonic id and an entry in the open-addressed
 * hash table at PS->phi_map (PassState+0x60). Non-PHIs are skipped. */
#define LLVM_OPCODE_PHI 0x54

static inline uint32_t cssa_hash(void *p) {
    /* Same canonical NVIDIA DenseMap hash used across cicc. */
    uintptr_t v = (uintptr_t)p;
    return (uint32_t)((v >> 4) ^ (v >> 9));
}

void cssa_phase2_discover_phis(Function *F, PassState *PS) {
    for (BasicBlock *bb = F->blocks.first; bb != F->blocks.sentinel;
         bb = bb->next) {
        for (Instruction *I = bb->insts.first;
             I != bb->insts.sentinel;
             I = I->next) {

            if (I->opcode != LLVM_OPCODE_PHI) {     /* byte [I-0x18] */
                cssa_record_use(PS, I);             /* phase-2 inner table */
                continue;
            }

            uint32_t id = PS->next_phi_id++;        /* [PS+0x78] */
            cssa_phi_map_insert(PS->phi_map, I, id);

            /* Inline copy insertion runs immediately after recording the
             * PHI; see phase 3. */
            cssa_phase3_insert_copies(F, PS, bb, I);
        }
    }
}

/* Open-addressed quadratic-probe DenseMap with two tombstones.
 * LLVM_EMPTY = -4096 (0xfffffffffffff000)
 * LLVM_TOMB  = -8192 (0xffffffffffffe000)
 * Growth threshold: load > 3/4 of capacity. */
static void cssa_phi_map_insert(PhiMap *m, void *key, uint32_t id) {
    if (m->capacity == 0) { phi_map_grow(m, 2 * m->capacity); }

    uint32_t mask = m->capacity - 1;
    uint32_t h = cssa_hash(key) & mask;
    void **slot = &m->buckets[h].key;
    void **tomb = NULL;

    uint32_t probe = 1;
    while (*slot != key) {
        if (*slot == LLVM_EMPTY) break;
        if (*slot == LLVM_TOMB && !tomb) tomb = slot;
        h = (h + probe++) & mask;        /* triangular probe */
        slot = &m->buckets[h].key;
    }

    if (*slot != key) {
        slot = tomb ? tomb : slot;
        ++m->live;
        if (*slot != LLVM_EMPTY) --m->tombstones;
        *slot = key;
        slot[1] = (void *)(uintptr_t)id;
    }

    if (4 * (m->live + 1) >= 3 * m->capacity) {
        phi_map_grow(m, 2 * m->capacity);   /* sub_A41E30 */
    }
}
```

The map serves two purposes: it dedupes PHIs encountered through multiple control-flow paths, and it gives `sub_371F790` (phase 4) an O(1) test for "is this value a PHI node I just rewrote?". HIGH confidence (the inline body of `sub_A41E30` and the `-4096 / -8192` sentinels are visible verbatim in the decomp at `0x3720E15`).

### Phase 3: Copy Insertion at Predecessor Terminators — HIGH

For every PHI discovered in phase 2, the pass materializes one copy per incoming edge. The body lives at `0x3720B40`--`0x3720E18` and is invoked synchronously from inside the phase-2 walk (the decompiler hoists it out, but the call site is `LABEL_53`'s call to `sub_371F160`). For each PHI operand, three helper calls run in sequence:

1. `sub_371F160(out, F, phi, incoming_use, 1)` — finds the predecessor block and computes the position immediately before its terminator. The `flag=1` argument requests copy materialization. The four-byte return buffer (`v126[]`) carries the destination value pointer plus the predecessor-block handle.
2. `sub_371CDC0(0x22D7, builder, 1, args, 2, name_ptr, type)` — constructs the new IR instruction. Opcode `0x22D7` (8919 decimal) is the NVVM-internal "divergence-safe copy" mnemonic; the name pointer is the constant string `"pcp"` (PHI Copy Propagation prefix, set at `v128 = "pcp"` immediately before the call at `0x3720D34`).
3. `sub_371EDF0(F, new_inst, 1, ...)` followed by `sub_BD84D0(insert_point, new_inst, ...)` — splices the freshly built instruction into the predecessor block before its terminator and rewires the PHI's use-def chain.

```c
/* Phase 3: for every (PHI, incoming) pair, drop one "pcp" copy at the end
 * of the matching predecessor block, then redirect the PHI use to the copy. */
#define NVVM_OPC_DIV_SAFE_COPY 0x22D7

void cssa_phase3_insert_copies(Function *F, PassState *PS,
                               BasicBlock *home, Instruction *phi) {
    /* Walk PHI operand pairs: each (value, predecessor) is a 32-byte use
     * record at [phi+0x18] forward through the use-def chain. */
    for (Use *u = phi->operands; u != phi->operands_end; u += 1) {
        Value *incoming   = u->value;
        BasicBlock *pred  = u->parent;          /* [use+8] */

        /* Step 1: materialize / locate the copy slot before pred's terminator. */
        CopySlot slot;
        sub_371F160(&slot, F, phi, /*incoming=*/u, /*flag=*/1);

        /* Step 2: build the copy IR instruction (opcode 0x22D7, name "pcp"). */
        Type *ty = (Type *)slot.type;
        const char *name = "pcp";
        Value *name_obj  = sub_ACA8A0(/*scope=*/incoming);  /* setName helper */
        Instruction *cpy = sub_371CDC0(
            NVVM_OPC_DIV_SAFE_COPY,
            /*builder=*/&slot,
            /*nresults=*/1,
            /*args=*/(Value *[]){ slot.dst_proxy, name_obj },
            /*nargs=*/2,
            /*name_ptr=*/&name,
            /*type=*/ty);

        /* Step 3: splice into pred block before its terminator. */
        sub_371EDF0(F, cpy, /*flag=*/1, ...);
        sub_BD84D0(/*before=*/pred->terminator, /*what=*/cpy, ...);

        /* Step 4: rewire PHI's use chain so the operand points at the copy
         * instead of the original value. This is the four-pointer dance
         * around offsets [+0, +8, +0x10] (prev/next/parent in the use list). */
        Use *uses_head = (Use *)cpy->uses_head;
        if (uses_head->prev) {
            uses_head->prev->next = uses_head->next;
            if (uses_head->next)
                uses_head->next->prev = uses_head->prev;
        }
        uses_head->parent = u;
        uses_head->next   = u->next;
        if (u->next) u->next->prev = uses_head;
        u->next = uses_head;
        u->value = cpy;           /* PHI now reads from the copy */
    }
}
```

> ⚡ **QUIRK — Copies materialize at the terminator, not in the merge block**
> Every `"pcp"` copy is inserted before the predecessor's terminator (`sub_BD84D0` is called with `v47 - 24` as the anchor, which is the terminator instruction header minus its opcode prefix). Putting the copy in the merge block instead would defeat CSSA: the warp has already reconverged at that point, and whichever divergent path wrote last would clobber the values that the earlier paths produced. The terminator anchor is non-negotiable. HIGH confidence.

> ⚡ **QUIRK — Opcode `0x22D7` is invisible to upstream LLVM**
> The constant `8919` is hard-coded as the opcode for the inserted copy. It is an NVVM-internal opcode (not in LLVM's `Instruction.def`) that survives until the SelectionDAG lowering, where it is matched as a plain "move" but carries a flag bit telling the register allocator that this copy is allowed to coalesce with its source only when divergence analysis proves the live ranges are non-interfering. A naive lowerer that treated `0x22D7` as a generic copy and let the coalescer eliminate it unconditionally would reintroduce the original CPU-vs-GPU bug. MED confidence on the "flag bit" semantics (inferred from the surrounding coalescer knobs), HIGH on the opcode value itself.

### Phase 4: PHI-Map-Driven Copy Propagation — HIGH

Once every PHI has its predecessor-side copies, `sub_3720740` walks every basic block a second time (`0x3720A2F`--`0x3720A62`) and hands each block to `sub_371F790`, the 4 KB "PHI Copy Propagator" (PCP) helper:

```c
/* Phase 4: walk every block again, run PCP on each instruction stream. */
void cssa_phase4_propagate(Function *F, PassState *PS) {
    for (BasicBlock *bb = F->blocks.first; bb != F->blocks.sentinel;
         bb = bb->next) {
        Instruction *anchor = bb->insts.first;
        sub_371F790(PS, anchor, /*scratch=*/0, /*scratch=*/0, 0, 0);
    }
}
```

`sub_371F790` itself does three things per block, in order:

1. **Reconvergence-point hash-map build.** It scans the block looking for instruction opcodes in the range `[30, 40]` (the `(*v9 - 30) <= 0xA` test at `0x37203B6`), which is the NVVM control-flow family (branch, conditional branch, switch, indirect-branch, invoke, ret, unreachable, etc.). For each such terminator-like instruction it inserts an entry into a *local* DenseMap (`v150`/`v151` / capacity in `v153`) keyed on the instruction's operand-zero pointer. This local map is the "reconvergence index" — it tells the propagator, for any block, what set of predecessor terminators target it.

2. **Per-PHI-operand copy rewrite.** For every populated slot in the local map (the `*v45 != -8192 && *v45 != -4096` filter at `0x37200CB`), the helper calls `sub_371CDC0(0x22D7, ...)` *again* with the same `"pcp"` name and the local map's value as the type/template. This second construction creates a copy at the *consumer* side — inside the block that will read the PHI's result — and threads it through the same use-list manipulation that phase 3 did for the producer side.

```c
/* Sketch of sub_371F790's per-block work. Type names are reconstructed. */
typedef struct { void *key; uint32_t cap; uint8_t pad[20]; } LocalMap;
void pcp_run_on_block(PassState *PS, Instruction *anchor) {
    LocalMap recon = {0};                /* 32-byte buckets */

    /* (1) Walk operands of the block's terminator; record reconvergence
     *     points keyed on the source-value pointer. */
    for (Use *u = anchor->parent->terminator->uses; u; u = u->next) {
        Instruction *src = u->value;
        if ((uint8_t)(src->opcode - 30) > 0xA) continue;   /* not a CF op */
        local_map_insert(&recon, (void *)src->operand0);
    }

    /* (2) For every live slot in the local map, materialize a consumer-side
     *     "pcp" copy and link it onto the PHI's use chain. */
    for (uint32_t i = 0; i < recon.cap; ++i) {
        void *k = recon.buckets[i].key;
        if (k == LLVM_EMPTY || k == LLVM_TOMB) continue;

        Instruction *cpy = sub_371CDC0(
            NVVM_OPC_DIV_SAFE_COPY,
            /*builder=*/recon.buckets[i].builder,
            1, /*args=*/recon.buckets[i].args, 2,
            /*name=*/(const char *[]){ "pcp" },
            /*type=*/recon.buckets[i].type);

        pcp_rewire_use_chain(cpy, recon.buckets[i].use);
        pcp_propagate_live_range(PS, cpy);   /* offsets +0x40..+0x4C on PS */
    }

    /* (3) Sort the recorded reconvergence-point list; aborts use the BSR
     *     trick `_BitScanReverse64` to compute a heap-style introsort depth
     *     limit (visible at 0x371F951). */
    pcp_sort_records(/*begin=*/recon.records, /*end=*/recon.records_end);
    local_map_free(&recon);
}
```

3. **Live-range propagation.** Each consumer-side copy is added to the pass-level use-graph at `[PS+0x40..+0x4C]` — the *same* DenseMap structure used in phase 2, just keyed on different pointers. This is how PCP guarantees that the cleanup phase (phase 5) sees every freshly inserted copy: the live-range graph is the cleanup worklist.

> ⚡ **QUIRK — PHI propagation uses a *local* hash map per block, not a global one**
> Most LLVM-style propagators reuse a single global map across the whole function. CSSA's PCP allocates a fresh DenseMap on the stack for each block (the `v150 / v151 / v152 / v153` quartet in `sub_371F790`) and frees it with `sub_C7D6A0` at the end of the block. This means the same PHI pointer can hash to different slots in different blocks — a deliberate design choice that lets the propagator distinguish "this PHI was seen via predecessor edge A" from "this PHI was seen via predecessor edge B" without having to track edges explicitly. The cost is one DenseMap allocation per block, which the heap-style introsort depth limit `_BitScanReverse64(2 * (63 - log2(span)))` keeps cache-friendly. HIGH confidence.

> ⚡ **QUIRK — PHI-with-self-edge requires the propagator's deduplication**
> A loop header PHI whose incoming edge is its own block (`%x = phi [latch: %x.update], [preheader: %x.init]`) produces an instruction that is *both* a consumer (it reads `%x.update`) and a producer (every iteration overwrites `%x`). Without the local-map dedup in step (2), the propagator would insert an unbounded chain of copies as it follows the use-def cycle. The hash-map check at `0x37200CB` (`*v45 != -8192 && *v45 != -4096`) is what breaks the cycle: once a value has been mapped, it is never remapped, so the propagator visits each self-edge exactly once. HIGH confidence.

### Phase 5: Dead Copy Cleanup — HIGH

The final phase walks a linked list rooted at `[F+0x28]` (the pass-state's cleanup worklist, populated incrementally by phases 3 and 4). For each entry, if the instruction has zero remaining uses, it is erased via `sub_B43D60` (which is `Instruction::eraseFromParent`).

```c
/* Phase 5: walk cleanup worklist, erase any zero-use copies. */
void cssa_phase5_cleanup(Function *F) {
    for (CleanupNode *n = (CleanupNode *)F->cleanup_head; n; n = n->next) {
        Instruction *cand = n->inst;
        while (cand) {
            if (cand->use_count == 0) {     /* [I+0x10] */
                Instruction *next_cand = cand->cleanup_next;
                sub_B43D60(cand);            /* eraseFromParent */
                cand = next_cand;
            } else {
                break;
            }
        }
    }
}
```

The two-pointer loop at `0x3720A6F` (`while (1) { v38 = j[1]; if (!v38[2]) break; ... sub_B43D60(v38); }`) is exactly this pattern: it walks until it finds a copy that still has uses, then breaks. Cleanup is therefore a forward-only pass — it never revisits a copy once one is found live. HIGH confidence.

> ⚡ **QUIRK — Cleanup must run only after propagation, never interleaved**
> Erasing a copy mid-propagation would invalidate the use-def chains that PCP is still walking. The pass enforces ordering by populating the cleanup worklist (`[F+0x28]`) during phases 3 and 4 but never reading it until phase 5. A reimplementation that tries to be clever by erasing dead copies as soon as the propagator decides they are dead will hit use-after-free in the live-range propagation step, because the use-list nodes are reused across phases via the same arena. HIGH confidence (the worklist node layout at `[node+0]` / `[node+8]` matches the use-list node layout exactly).

## Copy Coalescing

The `cssa-coalesce` knob controls how aggressively the pass coalesces the inserted copies back together. Without coalescing, CSSA inserts one copy per PHI operand per predecessor — potentially a large number of copies in control flow with many branches. Coalescing identifies cases where two or more copies carry the same value and can share a single register, reducing the copy overhead.

The `CSSACoalescing` knob in the NVVM container format (parsed by `sub_CD9990` from the finalizer knobs structure) provides a separate control path for the same behavior. The container knob is categorized alongside register allocation and scheduling controls (`AdvancedRemat`, `DisablePredication`, `DisableXBlockSched`, `ReorderCSE`), confirming that CSSA coalescing is considered part of the register allocation subsystem.

> ⚡ **QUIRK — "Coalesce copies on split edges" is a subtarget default, not a global one**
> The strings `"Coalesce copies on split edges (default=subtarget)"` and `"Coalesce copies that span blocks (default=subtarget)"` appear in the binary alongside the CSSA strings. The `(default=subtarget)` suffix means the default value depends on the GPU architecture in use — newer SMs may aggressively coalesce edge-split copies while older SMs leave them alone. A reimplementor that hard-codes a single default will produce different code than cicc on at least one SM. MED confidence (string evidence is direct; subtarget-table cross-reference is inferred).

## deSSA Alternative

The `usedessa` knob (default value 2, registered at `ctor_358_0` at `0x50E8D0`, stored in `dword_4FD26A0`) selects an alternative path for PHI elimination during the transition from SSA to machine code. Despite its name suggesting "de-Static Single Assignment", analysis of the dispatch functions shows it controls the scheduling and PHI elimination pipeline:

| Mode | Pre-RA Scheduling | Post-RA Scheduling | Behavior |
|------|---|---|---|
| 1 | Skipped | Minimal (single pass) | Simple mode — no pre-RA scheduling |
| 2 (default) | Full (`&unk_4FC8A0C`) | Three passes + StackSlotColoring | Full mode — complete scheduling pipeline |

The deSSA mode and CSSA transformation are complementary. CSSA operates at the LLVM IR level, converting PHI nodes into a form safe for GPU divergence *before* instruction selection. The `usedessa` mode controls how PHI nodes are ultimately eliminated *during* the MachineIR lowering, after SelectionDAG has already consumed the CSSA-transformed IR. When `usedessa=2` (default), the full scheduling pipeline runs, giving the register allocator maximum flexibility to handle the extra copies that CSSA introduced. When `usedessa=1`, the minimal scheduling mode may be appropriate for debugging or for kernels where scheduling causes regressions.

## Configuration Knobs

### NVVMPassOptions Knob

| Knob | Type | Description |
|------|------|-------------|
| `do-cssa` | bool | Master enable/disable for the CSSA pass |

Set via `-opt "-do-cssa=0"` to disable the pass entirely.

### cl::opt Knobs (ctor_705 at `0x5BD430`)

| Knob | Type | Default | Global | Description |
|------|------|---------|--------|-------------|
| `cssa-coalesce` | int | (unknown) | (ctor_705 data) | Controls PHI operand coalescing aggressiveness. Higher values = more aggressive coalescing = fewer copies but higher risk of incorrect merging under divergence. |
| `cssa-verbosity` | int | 0 | (ctor_705 data) | Verbosity level for diagnostic output during the CSSA transformation. |
| `dump-before-cssa` | bool | false | `qword_5050A28` | When non-zero, dumps the entire IR module before CSSA runs. Triggers the `"IR Module before CSSA:\n"` output followed by `sub_A69980` (Module::print). |

### Container-Format Knob

| Knob | Parsed At | Category | Description |
|------|-----------|----------|-------------|
| `CSSACoalescing` | `sub_CD9990` | Register allocation / scheduling | Controls CSSA coalescing from the NVVM container format. Parsed alongside `AdvancedRemat`, `DisablePredication`, `DisableXBlockSched`. |

### Related Knob

| Knob | Type | Default | Global | Description |
|------|------|---------|--------|-------------|
| `usedessa` | int | 2 | `dword_4FD26A0` | Selects deSSA method / scheduling pipeline mode. Mode 1 = simple (no pre-RA scheduling), mode 2 = full. |

## Diagnostic Strings

```text
"IR Module before CSSA:\n"          -- Module dump header (dump-before-cssa)
"pcp"                               -- PHI copy propagation instruction name prefix
```

The `"pcp"` prefix is assigned to all copy instructions created by the CSSA pass. These copies can be identified in IR dumps by their `%pcp` naming. After register allocation, these copies may be eliminated (coalesced into the same physical register) or materialized as actual move instructions in the final PTX.

## Function Map

| Function | Address | Size | Role | Confidence |
|----------|---------|------|------|---|
| CSSA main | `sub_3720740` | 3521B / 165bb | BB ordering, PHI scanning, copy insertion, cleanup | HIGH |
| PCP propagator | `sub_371F790` | 4014B / 197bb | Per-block reconvergence map, consumer-side copy emission, live-range propagation | HIGH |
| Copy slot resolver | `sub_371F160` | 1079B / 60bb | Locates predecessor-block terminator anchor, returns CopySlot triple | HIGH |
| IR builder helper | `sub_371EDF0` | 875B / 47bb | Splices new instruction into block's instruction list (uses `_OWORD` for parent-block link) | HIGH |
| Instruction factory | `sub_371CDC0` | 227B / 5bb | Allocates and initializes the divergence-safe copy node (calls `sub_B6E160` for type, `sub_BD2C40(88, ...)` for the 88-byte instruction body) | HIGH |
| PCP map grow | `sub_371F5A0` | 494B / 28bb | DenseMap rehash for `sub_371F790`'s local maps (32-byte slots, distinct from `sub_A41E30`'s 16-byte slots) | HIGH |
| PCP map grow (small) | `sub_371EC10` | 478B / 28bb | Auxiliary DenseMap rehash for PCP use-list (16-byte slots) | HIGH |
| Hash table grow | `sub_A41E30` | 478B / 28bb | DenseMap resize for PHI hash table | HIGH |
| Module printer | `sub_A69980` | 416B / 17bb | Module::print (for dump-before-cssa) | HIGH |
| raw_ostream::write | `sub_CB6200` | 276B / 15bb | String output for debug dump | HIGH |
| Debug stream getter | `sub_C5F790` | 11B / 1bb | Returns current debug output stream | HIGH |
| Instruction eraser | `sub_B43D60` | 102B / 1bb | Erases dead instruction from parent block | HIGH |
| Instruction insert | `sub_BD84D0` | 10B / 2bb | BasicBlock::insert (instruction splice) | HIGH |
| Name setter | `sub_ACA8A0` | 728B / 37bb | Value::setName for `"pcp"` prefix | HIGH |
| Use chain rewrite | `sub_B96E90` | 94B / 5bb | replaceAllUsesWith on operand | HIGH |
| Use helper | `sub_B91220` | 66B / 6bb | Use-list manipulation | HIGH |
| Use list insert | `sub_B976B0` | 72B / 3bb | Inserts use into specific position in use-list | HIGH |
| DenseMap grow helper | `sub_C8D5F0` | 470B / 29bb | SmallVector/DenseMap capacity growth | HIGH |
| Knob registration | `ctor_705` (`0x5BD430`) | 5.4KB | Registers `cssa-coalesce`, `cssa-verbosity`, `dump-before-cssa` | HIGH |
| Container knob parser | `sub_CD9990` | 31KB | Parses `CSSACoalescing` from NVVM container | HIGH |
| deSSA dispatch (post-RA) | `sub_21668D0` | — | Scheduling pipeline mode selector | MED |
| deSSA dispatch (pre-RA) | `sub_2165850` | — | Pre-RA scheduling mode selector | MED |

## Differences from Upstream LLVM

LLVM's standard PHI elimination pass (`llvm::PHIEliminationPass`, registered as `"phi-node-elimination"` at pipeline slot 493 in CICC's pass parser) lowers PHI nodes to machine copies during the SelectionDAG-to-MachineIR transition. It operates under the assumption that PHI semantics follow scalar control flow — exactly one predecessor contributes a value at each dynamic execution.

NVIDIA's CSSA pass runs *before* instruction selection, at the LLVM IR level, and transforms the IR into a form where PHI elimination can proceed safely even when the underlying execution model is SIMT. The two passes are not alternatives — CSSA runs first to prepare the IR, then standard PHI elimination runs later to lower the CSSA-safe PHI nodes to machine copies.

This is one of the fundamental semantic gaps between LLVM's CPU-centric IR model and GPU reality. LLVM assumes sequential scalar semantics; NVIDIA's CSSA pass bridges that gap by making the implicit thread-level parallelism explicit in the copy structure of the IR.

## Common Pitfalls

These are mistakes a reimplementor is likely to make when building an equivalent CSSA transformation for GPU targets.

**1. Inserting copies only at the merge block instead of at the end of each predecessor.** The entire point of CSSA is that copies must be placed *before* the warp reconverges, not *at* the reconvergence point. If you insert the copy instruction at the beginning of the merge block (after the PHI), the warp has already reconverged and whichever path executed last has overwritten the register value for all threads. Copies must be at the terminator position of each predecessor block, before control leaves that block. This is the fundamental GPU-vs-CPU distinction: on a CPU, only one predecessor executes so placement does not matter; on a GPU, all predecessors may execute sequentially within the same warp.

**2. Coalescing copies that have divergent live ranges.** The `cssa-coalesce` knob controls how aggressively copies are merged back together. Over-aggressive coalescing can assign two copies to the same physical register when their live ranges overlap under divergence — threads from different predecessor paths would see each other's values. The coalescer must verify that live ranges are truly non-interfering under the SIMT execution model, not just under the sequential CFG model. A reimplementation that reuses a standard LLVM register coalescer without divergence-aware interference checking will produce silent miscompilation on any kernel with divergent control flow.

**3. Failing to insert copies for uniform PHI nodes that become divergent after later transformations.** CSSA runs before instruction selection, but divergence analysis at that point may be imprecise. A PHI node classified as uniform (all threads agree on the incoming edge) may become effectively divergent after subsequent loop transformations or predication changes the control flow. The safe approach is to insert copies for all PHI nodes and let the coalescing phase remove unnecessary ones. A reimplementation that skips "uniform" PHI nodes based on divergence analysis risks correctness if that analysis is later invalidated.

**4. Using a standard LLVM `PHIElimination` pass without the CSSA preprocessing step.** LLVM's built-in PHI elimination assumes scalar control flow semantics (exactly one predecessor contributes at runtime). Running it directly on GPU IR without first converting to CSSA form will produce incorrect register assignments whenever a warp diverges at a branch leading to a PHI merge point. CSSA is not a replacement for PHI elimination — it is a prerequisite that transforms PHI semantics into a form safe for the standard lowering.

**5. Not propagating the `"pcp"` copy through the instruction graph after insertion.** Phase 4 of the algorithm (copy propagation via `sub_371F790`) replaces uses of original values with uses of the inserted copies. A reimplementation that inserts copies but skips this propagation step will leave the PHI node still referencing the original value, making the copies dead. The subsequent dead-copy cleanup (Phase 5) will then erase them, and the transformation has no effect — the original divergence-unsafe PHI remains.

**6. Splitting critical edges before CSSA but not re-running block numbering.** If a downstream pass between phase 1 and phase 2 (or a re-entry into CSSA via the latch byte) splits a critical edge, the new edge-split block has no preorder/RPO index. The PHI map will still work because it is keyed on instruction pointers, but the `[BB+0x48]` / `[BB+0x4C]` indices will be stale for the new blocks. The pass guards against this with the latch byte: the second time `sub_3720740` runs on the same function with `[PS+0x70]` already set, only the PHI counter (`[PS+0x74]`) is reset; phase 1 is skipped. A reimplementor must either always re-run phase 1 or never split edges after CSSA.

## Reimplementation Checklist

1. **Basic block ordering and numbering.** Assign preorder and reverse-postorder indices to every basic block (stored at block offsets +0x48/+0x4C), used later for dominance and reconvergence queries. Latch the result so re-entries skip this phase.
2. **PHI node scanning and hash map population.** Walk all instructions across all basic blocks, identify PHI nodes (opcode 0x54), assign monotonic IDs, and insert into a DenseMap using the hash `(ptr >> 4) ^ (ptr >> 9)` with LLVM-layer sentinels (-4096/-8192) and 75% load-factor growth.
3. **Copy insertion at reconvergence points.** For each PHI node's incoming value, insert a `"pcp"`-prefixed copy instruction at the end of the predecessor block (before the terminator) using opcode 0x22D7 (divergence-safe copy), then rewire the PHI's use chain so the operand points to the copy instead of the original value.
4. **Copy propagation.** Iterate all blocks a second time, invoking the PCP builder on each block to populate per-block reconvergence maps, emit consumer-side copies, and propagate live ranges through the pass-level use-graph.
5. **Dead copy cleanup.** Walk the cleanup worklist, check each entry for zero remaining uses, and erase dead copy instructions via `eraseFromParent`. Never interleave with phases 3 or 4.
6. **Copy coalescing (cssa-coalesce).** Implement configurable coalescing that identifies cases where multiple `"pcp"` copies carry the same value and can share a single register, reducing copy overhead while preserving correctness under warp divergence. Honor the `(default=subtarget)` semantics for edge-split and block-spanning copies.

## Cross-References

- [NVIDIA Custom Passes](./index.md) — CSSA listed as `sub_3720740` with knobs `cssa-coalesce`, `cssa-verbosity`, `dump-before-cssa`
- [Rematerialization](./rematerialization.md) — runs before CSSA; rematerializable values reduce the number of PHI copies CSSA must insert
- [Sinking2](./sinking2.md) — runs adjacent to CSSA; can move definitions across PHI boundaries, increasing CSSA's workload
- [Register Allocation](../llvm/register-allocation.md) — greedy RA consumes CSSA-prepared IR
- [Scheduling](../llvm/scheduling.md) — `usedessa` knob controls pre-RA/post-RA scheduling mode
- [Code Generation Pipeline](../pipeline/codegen.md) — CSSA's position in the overall compilation flow
- [StructurizeCFG](../llvm/structurizecfg.md) — related pass that ensures structured control flow for PTX
- [Hash and Container Infrastructure](../infra/hash-infrastructure.md) — DenseMap and SmallVector primitives shared with phase-1 worklist and phase-2 PHI map
- [Configuration Knobs](../config/knobs.md) — full knob inventory
