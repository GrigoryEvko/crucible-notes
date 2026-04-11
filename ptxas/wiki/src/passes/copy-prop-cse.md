# Copy Propagation & CSE

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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

The ptxas GVN-CSE operates on the Ori IR basic block list with dominator-tree-guided traversal. The core is a hash-consed value table that maps expression keys to value numbers via FNV-1a hashing.

#### Hash function: FNV-1a on block-index keys (`sub_BEEC80`)

Every value table lookup hashes a 32-bit key (the block-relative instruction index stored at `instr+144`) through byte-at-a-time FNV-1a. From the disassembly at `0xBEECB6`--`0xBEECE1`:

```
FNV1a_32(key: uint32) -> uint32:
    h = 0x811C9DC5                          // FNV offset basis
    h = (h ^ byte0(key)) * 0x01000193       // FNV prime = 16777619
    h = (h ^ byte1(key)) * 0x01000193
    h = (h ^ byte2(key)) * 0x01000193
    h = (h ^ byte3(key)) * 0x01000193
    return h
```

#### Value table structure (`sub_BEEC80`, 1160 bytes)

The table is a bucket-chained hash map. Buckets are 24-byte triples allocated as a power-of-two array (initial capacity 8):

| Bucket field | Offset | Size | Purpose |
|---|---|---|---|
| `head` | +0 | 8 | Pointer to first chain node (or NULL) |
| `tail` | +8 | 8 | Pointer to last chain node (for O(1) append) |
| `count` | +16 | 4+4 | Number of entries in this bucket |

Chain nodes are 32-byte records allocated from the context's memory arena (allocation size 32 at `sub_BEEC80` offset `0xBEEE90`):

| Node field | Offset | Size | Purpose |
|---|---|---|---|
| `next` | +0 | 8 | Next pointer within bucket chain |
| `key` | +8 | 4 | 32-bit block-index (from `instr+144`) |
| (pad) | +12 | 4 | |
| `value` | +16 | 8 | Pointer to value record / metadata |
| `stored_hash` | +24 | 4 | Cached FNV-1a hash (avoids recompute on resize) |
| (pad) | +28 | 4 | |

Lookup: `bucket = hash & (capacity - 1)`, then linear chain walk comparing `node.key == query_key`. Insert returns a 33-byte result struct: `{table_ptr:8, bucket_idx:8, node_ptr:8, prev_ptr:8, is_new:1}`. Resize triggers when `total_entries > bucket_count` AND `bucket_count <= capacity / 2`; the new capacity is `4 * old_capacity` (`sub_865E40`). Redistribution uses `stored_hash % new_capacity` -- this is the sole reason the hash is stored in each node.

#### VN transfer function (formal)

For instruction `I` with masked opcode `op = I.opcode & 0xFFFFCFFF`, data type `ty = *(I+76)`, and operand count `n = *(I+80)`:

```
VN(I) =
  | not ELIGIBLE(I)                        -> fresh vn, no table entry
  | op = 145 (barrier) AND not SAFE(I)     -> scope break, no CSE
  | TABLE_LOOKUP(key(I)) = hit(leader)     -> VN(leader)  [replace I with MOV]
  | TABLE_LOOKUP(key(I)) = miss            -> fresh vn, TABLE_INSERT(key(I), I)
```

The expression key is `*(I+144)`, a 32-bit index assigned during def-chain construction (`sub_781F80`). Two instructions share the same key iff they define the same value record in the register table at `context+296`. Structural equivalence that determines key sharing is enforced upstream by the def-chain builder, not by the hash table.

#### Eligibility predicate ELIGIBLE(I) -- `sub_BEA1E0`

| Masked opcode | Condition on last dest operand | Meaning |
|---|---|---|
| 16 (MOV) | `*(I + 8*(n + ~(op>>11 & 2)) + 85) bit 1 == 0` | MOV with non-volatile destination |
| 183 (TEX) | `operand bit 5` OR `(operand >> 21) & 7 > 1` OR `sub_91BC40` | Texture with CSE-safe sampling mode |
| 119 (MUFU) | `sm_backend+1106 bit 6` AND `operand bit 1` | Transcendental (enhanced scope only) |
| 186 (SHFL) | `sm_backend+1106 bit 6` AND `operand bit 0` | Shuffle (enhanced scope only) |
| 211 (ATOM) | `sm_backend+1106 bit 6` AND `operand bit 2` | Atomic (enhanced scope only) |
| 283 (REDUX) | `sm_backend+1106 bit 6` AND `operand bit 3` | Reduction (enhanced scope only) |
| 122 (VOTE) | subtype in {2,3}, or subtype in {7,8} with bit 7 | Vote with compatible mode |
| 310 (MATCH) | `(operand & 0xF) == 2` AND `(operand & 0x30) != 0x30` | Match with non-default mask |

**Key design decisions visible from the binary:**

1. **Hash-based value table.** The value numbering table uses FNV-1a hashing (seed `0x811C9DC5`, prime `16777619` / `0x01000193`), the same hash primitive used throughout ptxas for instruction fingerprinting, code caching, and scheduling table lookups. The hash function operates on the 32-bit block-index key (not on opcode/operand tuples directly). Hash table entries are 32 bytes each with chained collision resolution; the stored hash field at `+24` enables O(n) resize without rehashing keys.

2. **Dominator-tree scoping.** Values defined in block B are only visible to blocks dominated by B. The Full GVN (`sub_BED7E0`) maintains a **scope tree** -- a red-black BST of 64-byte bitset nodes -- that tracks which blocks have been processed within the current dominator subtree. The protocol has three phases:

   **Scope push** (`sub_6B4520`, called at `0xBEDDF5` and `0xBEDFC9`). When the dominator-chain walk discovers that a value's dominator is in a previously-processed block, the current block's index is inserted into the scope tree. The block index is decomposed into a group key and an intra-node bit position:
   ```
   group_key    = block_index >> 8        // BST sort key, stored at node+0x18
   word_index   = (block_index >> 6) & 3  // which of 4 qwords in the 256-bit bitset
   bit_position = block_index & 0x3F      // bit within the selected qword
   ```
   Each BST node is a 64-byte record: `[left (8B), right (8B), parent (8B), key|color (4B+pad), bitset[4] (32B)]`. The key field at `+0x18` doubles as the RB-tree color flag in bit 31 (`0x80000000` = black, clear = red), managed by the rebalancer `sub_6887C0`. On insert, if a node with the matching group key already exists, `sub_6B4520` ORs the single bit into the existing node's bitset word at `node+0x20+word_index*8`. If the node is absent, a new 64-byte node is allocated from the scope tree's freelist (at `scope_tree+0x20`, field `+8` of the allocation context), zeroed, and inserted via `sub_6A01A0` which performs standard BST insertion followed by RB rebalancing.

   **Scope drain** (post-RPO loop, starting at `0xBEDFEB`). After the RPO block walk completes, the scope tree is iterated to enumerate all recorded block indices. The iteration walks the RB-tree in-order, and for each node scans its 4-word bitset using `tzcnt` (trailing zero count) to extract set bits. The block index is reconstructed as `tzcnt_result | (word_index << 6) | (group_key << 8)`. For each recovered block index, the pass calls `sub_BEA5F0` to perform CSE on the dominated block's value records.

   **Scope pop** (`sub_69DD70`, called at `0xBEE1A0`). After all scope-tree entries are drained, the cleanup loop removes nodes from the tree one at a time. `sub_69DD70` performs standard BST deletion: it splices the node at `scope_tree+8` (the tree cursor) out of the tree by relinking children and parent pointers, decrements the node count at `scope_tree+0x18`, and the caller pushes the freed node onto the freelist for reuse. The loop continues (`jnz 0xBEE1A0`) until `scope_tree+0` (the root pointer) is null. Finally, `sub_661750` releases any remaining freelist nodes back to the pool allocator, and `sub_8E3A20` frees the per-block visited array.

3. **Commutativity normalization.** For commutative operations (ADD, MUL, AND, OR, XOR, MIN, MAX), operands are sorted by value number before hashing. This ensures `a + b` and `b + a` get the same value number without requiring a separate reassociation pass.

4. **Address space awareness.** Memory operations in different address spaces (shared, global, local, constant) are never considered equivalent even if they have identical operands. The address space qualifier is encoded in the instruction opcode or modifier bits (not the operand), so the opcode comparison in the structural equivalence check inherently preserves this distinction.

5. **Predicate handling.** Predicated instructions (`@P0 IADD R1, R2, R3`) hash the predicate register's value number as an additional operand. Two identical computations under different predicates are distinct values.

6. **Predicate-operand compatibility (`sub_7E7380`).** After opcode and type matching in the caller, `sub_7E7380` performs a focused predicate-operand compatibility check (30 lines, 150 bytes). The function tests: (a) predicate modifier parity -- `instr+73` bit 4 versus `instr+72` bit 12 (`0x1000`); if one instruction has a predicate modifier and the other does not, they are incompatible; (b) last operand 24-bit value ID -- `(instr + 84 + 8*(operand_count-1)) & 0xFFFFFF` must match; (c) second-to-last operand 8-byte encoding -- the two dwords immediately before the last operand slot must be identical. The broader structural comparison (opcodes masked with `& 0xFFFFCFFF`, data types at `+76`, operand counts at `+80`, full per-operand encoding, register class at `+64`) is performed by each of the 21 callers of `sub_7E7380`, not by the function itself. Instructions with volatile flags (bit `0x20` at register descriptor offset `+48`) and barrier-type registers (type 9) are excluded from CSE by the callers' pre-checks.

### GVN Algorithm Details (Binary Trace)

The GVN-CSE body was located by reading SM backend vtable slot 23 (offset `+0xB8`) from all seven SM backend vtables in the ptxas binary. The actual function pointer varies by SM generation:

| SM Backend | Vtable | Slot 23 Function | Behavior |
|---|---|---|---|
| SM30 (Kepler) | `off_2029DD0` | `sub_661250` | Returns 0 -- **NO-OP** |
| SM50 (Maxwell) | `off_21B4A50` | `sub_661250` | Returns 0 -- **NO-OP** |
| SM60 (Pascal) | `off_22B2A58` | `sub_BEE590` | Real GVN-CSE |
| SM70 (Volta) | `off_21D82B0` | `sub_BEE590` | Real GVN-CSE |
| SM80 (Ampere) | `off_21B2D30` | `sub_661250` | Returns 0 -- **NO-OP** |
| SM89 (Ada) | `off_21C0C68` | `sub_661250` | Returns 0 -- **NO-OP** |
| SM90+ (Hopper) | `off_21D6860` | `sub_BEE590` | Real GVN-CSE |

**GVN-CSE (phase 49) is a no-op on Kepler, Maxwell, Ampere, and Ada.** It only executes on Pascal, Volta, and Hopper/Blackwell. SM80/SM89 backends rely on LateOriCommoning (phase 64) and the GeneralOptimize sub-passes for CSE coverage instead. This is a deliberate per-generation decision embedded in each SM backend's vtable.

#### Call Chain

```
GvnCse::execute (0xC5F000)
  -> sm_backend->vtable[23]  (indirect dispatch)
     -> sub_BEE590           (GVN entry, SM60/70/90)
        -> sub_781F80(ctx, 0) (rebuild def chains, mode=full)
        -> sub_BEE370         (mode dispatcher)
           -- queries knob 402 via knob_container->vtable[9] --
           mode 0: disabled, return
           mode 1: sub_BEA450  (simple GVN)
           mode 2: sub_BEAD00  (standard dominator-guided GVN)
           mode 3-6: sub_BED7E0 (full GVN with extended block scope)
```

#### Mode Dispatcher (`sub_BEE370`)

The mode is determined by knob 402 (`EnableGvnCseMode`), queried through two vtable calls on the knob container at `context+1664`:

1. **Boolean query** -- `knob_container->vtable[9](402)` (offset `+72`): checks if the knob is set at all. The dispatcher has a fast-path optimization: when `vtable[9]` is `sub_6614A0` (the standard implementation), it reads directly from `knob_container+72+28944` instead of dispatching through the vtable.
2. **Integer query** -- `knob_container->vtable[15](402)` (offset `+120`): reads the mode value as an integer. Similarly fast-pathed when `vtable[15]` is `sub_661470`.

If both queries return truthy, the integer value selects the GVN variant:

| Mode | Function | Description |
|---|---|---|
| 0 | (none) | Pass disabled, return immediately |
| 1 | `sub_BEA450` | Simple single-block GVN (111 lines, ~2KB) |
| 2 | `sub_BEAD00` | Standard dominator-guided GVN (157 lines, ~2.5KB) |
| 3 | `sub_BED7E0` | Full GVN (when `sm_backend+1106` bit 6 AND `context+1416` bit 0) |
| 4 | `sub_BED7E0` | Full GVN (remapped to mode 2 if bit 6 is clear) |
| 5-6 | `sub_BED7E0` | Full GVN with extended block scope |
| >6 | (none) | Return immediately (no operation) |

Additional flags modulate the mode selection:

- SM backend flag at `sm_backend+1106` bit 6 (`0x40`): when set, enables modes 5-6 (enhanced scope). When clear and mode is 4, the dispatcher remaps to mode 2.
- Context flag at `context+1416` bit 0: when set (and bit 6 is set), selects mode 3 over mode 5-6.
- SM version threshold `sm_backend+372 <= 0x7FFF` (32767): gates the EBB pre-pass `sub_BED430` via knob 210.

Before the standard GVN (`sub_BEAD00`), the mode dispatcher may invoke `sub_BED430` -- an extended basic block (EBB) pre-pass that identifies and marks multi-block CSE opportunities within single-entry regions. The EBB pre-pass is called unless: (a) SM version > `0x7FFF`, AND (b) knob 210 is set or `context+1368` bit 0 is clear.

#### Simple GVN (`sub_BEA450`, Mode 1)

Mode 1 provides the lightest GVN variant -- single-scope CSE without cross-dominator lookup. Reconstructed pseudocode:

```
procedure SimpleGvn(gvn_state S):
    context = S.context
    first_reg = operand_24bit(first_instr(context+272))
    value_record = context.reg_table[first_reg]       // context+296
    if not value_record: return

    for each value_record in linked order:
        if knob_query(257, value_record): break        // per-instruction gate

        first_instr = value_record.head                // value_record[0]
        sentinel = value_record.sentinel               // value_record[1]
        eligible = false

        for each instr from first_instr to sentinel:
            if not eligible:
                eligible = check_eligibility(instr)    // sub_BEA1E0
                if eligible: advance and check sentinel
            if instr.opcode_masked == 145:             // barrier
                if sm_backend->vtable[371](instr):     // safe to CSE
                    mark eligible
                else: break scope

        if eligible:
            // Directly generate MOV replacement -- no dominator check
            context+232 = value_record.head
            context+264 = value_record.head->field_20
            sub_9314F0(context, 0x124, 1, 0, 0)       // insert MOV 292

        advance to next block via opcode 97 (block header) -> field +24
```

This variant does not examine the immediate-dominator chain at `instruction+148`. It only replaces redundancies that are visible within the current value record's instruction list (effectively single-block scope).

#### Standard GVN (`sub_BEAD00`, Mode 2)

Mode 2 extends the simple GVN with cross-dominator CSE. After finding an eligible instruction and reaching the end of a block, it follows the immediate-dominator chain:

```
procedure StandardGvn(gvn_state S, char cross_block_flag):
    // ... (same entry and block walk as SimpleGvn) ...

    // After the inner walk exhausts all instructions (cursor == sentinel),
    // the idom chain lookup fires.  Six termination conditions:
    //   T1. idom == 0            -- no cross-block dominator -> fallback
    //   T2. dom_record == NULL   -- dominator slot empty -> fallback
    //   T3. cross_block_flag &&  -- block-header sentinel; skip to avoid
    //       dom_record[66]==1       unsafe cross-block hoisting
    //   T4. dominance_check()!=0 -- extended-scope confirms dominance;
    //                               replace via value_record.head (self)
    //   T5. leader.next.opcode   -- dominator leader already has MOV;
    //       == 292                   skip duplicate insertion
    //   T6. MOV inserted         -- all guards passed; emit replacement

    idom = value_record.field_148                      // immediate dominator index
    if idom == 0: goto FALLBACK                        // T1
    dom_record = reg_table[idom_map[idom]]             // ctx+296[ctx+512[4*idom]]
    if dom_record == NULL: goto FALLBACK               // T2
    if cross_block_flag and dom_record[66] == 1:       // T3 (dword +264)
        goto NEXT_BLOCK

    if not dominance_check(S, value_record):           // sub_BEA3B0 == 0
        leader = dom_record.head
        if leader.next.opcode == 292: goto NEXT_BLOCK  // T5
        context+232 = leader                           // T6 -- emit MOV
        context+264 = leader.field_20
        sub_9314F0(context, 0x124, 1, 0, 0)
        goto NEXT_BLOCK
    else:                                              // T4 -- dominance holds
        src = *(value_record.head)                     // replace via self
        context+232 = src;  context+264 = src.field_20
        sub_9314F0(context, 0x124, 1, 0, 0)
        goto NEXT_BLOCK

FALLBACK:                                              // T1/T2
    block_desc = context.block_table[value_record.field_164]
    if block_desc+280 bit 0 is clear:
        leader = reg_table[operand_24bit(block_desc.first_instr)]
        if leader.next.opcode != 292:
            context+232 = leader;  context+264 = leader.field_20
            sub_9314F0(context, 0x124, 1, 0, 0)
```

The `cross_block_flag` (passed as `a2`) gates T3: when set, the standard GVN refuses replacement when the dominator value record has dword `+264 == 1` (block-header sentinel), preventing unsafe cross-block hoisting.

The T4/T6 split is the critical subtlety. When `sub_BEA3B0` returns 0 (extended-scope flag clear, or the raw ordering result was positive), the replacement source is `dom_record.head` -- the dominator's definition serves as canonical representative. When it returns nonzero (extended-scope ordering confirms reachability), the source is `value_record.head` -- the value's own definition. The XOR inversion (`result ^ 1` inside `sub_BEA3B0`) means the cache stores the raw `sub_74D720` result while the caller sees the negated form.

#### Dominance Check with Cache (`sub_BEA3B0`)

The dominance check is guarded by `context+1377` bit 5 (`0x20`). When this flag is clear, the function returns 0 immediately (no dominance, meaning "safe to CSE" -- the caller inverts the result).

When the flag is set, the function implements a single-entry global cache to accelerate repeated dominator queries:

```
procedure DominanceCheck(gvn_state S, value_record vr):
    if not (context+1377 & 0x20): return 0           // no extended scope

    idom = vr.field_148
    if idom == 0: return 1                             // no dominator -> can't CSE

    dom_record = reg_table[idom_map[idom]]
    if dom_record == NULL: return 1

    // Check global cache (single-entry, TLS-safe through static storage)
    if dom_record == cached_key:                       // qword_2A12A08
        return cached_result ^ 1                       // byte_2A129FE[0] ^ 1

    // Cache miss: compute dominator ordering via sub_74D720
    if idom >= 0 and vr.field_152 >= 0:
        cached_key = dom_record
        sub_74D720(context, idom, vr.field_152, &cached_result)
        return cached_result ^ 1
    else:
        return 1                                       // negative index -> can't CSE
```

The cache stores a single `(key, result)` pair in global statics `qword_2A12A08` and `byte_2A129FE`. This is effective because the GVN walk processes instructions within a block sequentially, and many consecutive instructions share the same dominator. The cache hit rate is high for blocks dominated by a single predecessor.

#### EBB Pre-Pass (`sub_BED430`)

The Extended Basic Block (EBB) pre-pass runs before mode 2 GVN when the SM version and knob conditions are met. It identifies cross-block CSE opportunities within single-entry CFG regions.

```
procedure EbbPrePass(gvn_state S):
    // Phase 1: Clear previous markings
    for each block B in linked order:
        B.field_264 = 0                               // clear EBB marking

    // Phase 2: Find first CSE-eligible instruction
    for each instr in instruction list:
        if check_eligibility(instr) and instr.opcode != 211:
            break  // found seed
    if not found: return

    // Phase 3: Build dominator tree and compute block ordering
    sub_7846D0(context)                                // dominator tree + RPO
    sub_A12EA0(context, walker_context, visitor)       // dominator tree walk
    sub_775010(context)                                // predecessor setup
    sub_773140(context, 0)                             // successor setup
    sub_770E60(context, 0)                             // block ordering

    // Phase 4: Mark CSE candidates on every instruction
    for each instr in instruction list:
        if check_eligibility(instr) and instr.opcode != 211:
            instr.field_48 = 1                         // mark as CSE candidate
        else:
            instr.field_48 = 0

    // Phase 5: Propagate eligibility through operand chains
    sub_BED0A0(walker_state)                           // fixed-point propagation

    // Phase 6: Evaluate cross-block candidates
    for each value_record in RPO order:
        if knob_query(257, vr): continue               // per-instruction gate
        idom = vr.field_148
        if idom != 0:
            dom_record = resolve_idom(context, idom)
            if dom_record and dom_record.field_264 == 0:
                dom_record.field_264 = sub_BEA000(walker, dom_record, 0) ? 2 : 1
```

The EBB propagation engine (`sub_BED0A0`) is a fixed-point iteration that propagates CSE eligibility backward through operand use-chains. For each instruction with `field_48` bit 0 set, it follows the operand-to-instruction back-references at `context+88` to mark defining instructions as eligible too. The iteration continues until no more changes occur. This ensures that an entire expression tree is marked eligible when any of its consumers is eligible.

#### Full GVN Body (`sub_BED7E0`, 689 lines, ~18KB binary)

This is the most complete GVN variant (modes 3-6). Reconstructed pseudocode:

```
procedure FullGvnCse(gvn_state S):
    context = S.context
    mode_flags = S.mode & ~0x02            // strip bit 1
    extended_scope = (mode_flags - 5)      // >1 enables cross-block scope

    // Phase 1: Initialization
    block_count = context.block_count      // at +376
    visited[] = allocate_zeroed(block_count + 1)   // one byte per block
    build_dominator_tree(context)                   // sub_7846D0
    scope_tree = new_scoped_tree()                  // sub_661750
    rpo = context.rpo_ordering                      // at +792

    // Phase 2: RPO block walk
    for i = 0 to rpo.count - 1:
        block_idx = rpo.indices[i]
        block = context.block_table[block_idx]      // context+368
        if block.head == NULL or block.flags & SKIP:
            continue

        first_instr = lookup_first_instruction(block)
        dominator_candidate = NULL
        has_redundant = false

        for each instr in block:
            // Per-instruction knob gate (knob 257)
            if knob_query(257, instr):
                break to next block boundary

            eligible = check_eligibility(instr)     // sub_BEA1E0

            if eligible:
                visited[block_idx] = not block.visited_flag

            elif opcode_masked(instr) in {32, 159}: // branch, return
                propagate visited flag from predicate operand

            elif opcode_masked(instr) == 145:       // barrier/sync
                safe = sm_backend->vtable[371](instr)
                if safe: mark_as_candidate

            // Check dominator for existing equivalent
            idom_ref = instr.idom_ref               // at +148
            if idom_ref != 0:
                dom_block = resolve_idom(context, idom_ref)
                if dom_block dominates current position:
                    leader = dom_block.first_instr
                    if leader.opcode != 292:        // not already a MOV
                        replace_with_mov(context, leader, 0x124)

        scope_tree_insert(scope_tree, block_idx)     // sub_6B4520

    // Phase 3: Post-processing dominated blocks (scope tree traversal)
    (node, qword_ptr) = scope_tree_begin(scope_tree)
    while node != NULL and qword_ptr != node.bitset_end:
        bit = tzcnt(*qword_ptr)
        qword_idx = (qword_ptr - node.bitset) / 8    // 0..3
        block_idx = bit | (qword_idx << 6) | (node.key << 8)
        cse_dominated_block(S, reg_table[block_idx])  // sub_BEA5F0
        // Advance: mask off bits [0..bit], find next set bit
        if bit < 63:
            *qword_ptr &= ~((1 << (bit+1)) - 1)
            if *qword_ptr != 0: continue
        // Scan remaining qwords in this node
        qword_ptr++
        while qword_ptr < node.bitset + 4 and *qword_ptr == 0:
            qword_ptr++
        if qword_ptr < node.bitset + 4: continue
        // BST in-order successor
        (node, qword_ptr) = bst_next_inorder(node)

    // Phase 4: Flush deferred MOVs into instruction rings
    while scope_tree.deferred_list != empty:
        instr = pop(scope_tree.deferred_list)
        splice instr into block's instruction ring
    destroy_scoped_tree(scope_tree)                    // sub_661750
```

**Scope tree node layout** (64 bytes, allocated by `sub_6B4520`):

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| +0 | 8 | `next` | Free-list chain / BST parent |
| +8 | 8 | `left` | BST left child |
| +16 | 8 | `right` | BST right child |
| +24 | 4 | `key` | `block_idx >> 8` (group selector) |
| +32 | 32 | `bitset[4]` | Four uint64 words = 256 bits |

**Block index recovery formula.** A block index is split on insertion and reassembled during traversal:

```
Insertion (sub_6B4520):
    key       = block_idx >> 8           stored at node+24
    qword_idx = (block_idx >> 6) & 3    selects bitset[0..3]
    bit       = block_idx & 63          set via sub_6B4440

Traversal (sub_BED7E0+0x5a0):
    block_idx = tzcnt(bitset[qi]) | (qi << 6) | (node.key << 8)
    where qi  = (qword_ptr - &node.bitset[0]) / 8
```

Each node covers 256 contiguous block indices. The BST key orders nodes so in-order traversal visits blocks in ascending index order. Within a node, `tzcnt` extracts set bits lowest-first across the four qwords, giving O(B) total time where B is the number of recorded blocks.

Key observations from the binary:

1. **Block walk order is RPO.** The outer loop reads `context+792` -- a struct containing `{int count; int indices[]}` -- and iterates in that order. The RPO array is pre-computed by `sub_7846D0` which also builds the dominator tree.

2. **The value table is a register-indexed array, not a hash map.** Values are stored in `context+296` (an array of pointers indexed by the 24-bit register/value identifier from the operand encoding at `instruction+84`). This gives O(1) lookup by register ID. The dominator tree is used for scoping, not a stack-based hash table.

3. **Dominator scoping uses a red-black tree with 256-bit bitset nodes** (see design decision 2 above for the full push/drain/pop protocol). Each 64-byte tree node stores a 4-qword (256-bit) bitset of block indices, traversed with `tzcnt` for efficient iteration. Block index is recovered as `tzcnt_result | (word_index << 6) | (group_key << 8)`, supporting up to 256 blocks per tree node.

4. **Replacement is MOV insertion.** When a redundant instruction is found, the pass calls `sub_9314F0(context, 0x124, 1, 0, 0)` to generate a replacement MOV instruction (opcode `0x124` = 292 decimal). The original computation is recorded at `context+232` (source) and `context+264` (metadata) before the MOV is generated.

5. **Barrier instructions (opcode 145) have a dedicated safety check** via `sm_backend->vtable[371]` (offset `+2968`), which is an architecture-specific predicate that determines whether an instruction can be CSE'd across a barrier boundary.

#### Instruction Eligibility (`sub_BEA1E0`)

| Opcode (masked) | Category | Condition |
|---|---|---|
| 16 | Register copy / PHI | Always, unless last operand bit 1 set |
| 183 | Memory load/compute | Bit 5 of last operand, or `sub_91BC40` safety check |
| 119 | GPU special | SM flag `+1106` bit 6 required; operand bit 1 |
| 186 | GPU special | SM flag `+1106` bit 6 required; operand bit 0 |
| 211 | GPU special | SM flag `+1106` bit 6 required; operand bit 2 |
| 283 | GPU special | SM flag `+1106` bit 6 required; operand bit 3 |
| 122 | Conditional | Type 2-3: always; type 7-8: bit 7 set |
| 310 | Specialized | `(flags & 0xF) == 2` and `(flags & 0x30) != 0x30` |
| 145 | Barrier/sync | Separate `sm_backend->vtable[371]` check |
| all others | -- | Not eligible |

Opcodes 119, 186, 211, 283 are only CSE-eligible when `sm_backend+1106` bit 6 (`0x40`) is set. This bit appears to be an architecture-specific capability flag enabling extended CSE for certain GPU-specific instruction classes.

#### Per-Dominated-Block CSE (`sub_BEA5F0`)

After the RPO walk populates the scope tree, `sub_BEA5F0` processes each dominated block:

1. **SM version gate**: if `sm_backend+372 <= 28671` (SM70 or earlier), enables a special operand canonicalization path for commutative operations
2. **Instruction walk**: iterates via `block+128` child pointer chain
3. **Dominator ordering**: compares `instruction+144` (dominator number) to test dominance
4. **Commutative canonicalization** (opcode 95): calls `sm_backend->vtable[79]` (offset `+632`) to sort operands by value number. Rewrites operand encoding with flags `0x60000000` and `0x40000000` to mark canonicalized operands
5. **Replacement**: calls `sub_931920` to insert copy instructions when a dominating equivalent is found

#### Scope Tree Bit-Iteration Detail

The scope tree post-processing (lines 498-664 of `sub_BED7E0`) uses a binary tree where each node contains a 4-word (32-byte) bitset region starting at `node+32`. The iteration:

1. Start at the leftmost node: follow `node->left` until NULL
2. Scan the 4-word bitset region (`node+32` through `node+64`), finding each set bit via `tzcnt` (x86 trailing-zero-count)
3. Recover the block index: `bit_position | (((word_offset_in_node | (node.field_24 << 2)) << 6))`
4. After processing a bit, mask it out: `word &= ~(0xFFFFFFFFFFFFFFFF >> (64 - (bit_pos + 1)))`
5. When current word is exhausted, advance to next word in the 4-word region
6. When all 4 words are exhausted, follow parent/right-child links to traverse the tree in order

Each block index recovered from the tree triggers a call to `sub_BEA5F0` for per-dominated-block CSE. The tree structure allows the scope walk to skip large ranges of blocks that have no CSE candidates, making it efficient for sparse CFGs.

#### GVN Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_BEE590` | GvnEntry | ~200B | Entry point (vtable slot 23, SM60/70/90) |
| `sub_BEE370` | ModeDispatcher | ~550B | Selects GVN variant via knob 402 |
| `sub_BED7E0` | FullGvn | ~18KB | Full GVN body (modes 3-6, RPO + scope tree) |
| `sub_BED430` | EbbPrePass | ~2KB | Extended basic block pre-pass |
| `sub_BED0A0` | EbbPropagate | ~3KB | EBB eligibility propagation (fixed-point) |
| `sub_BEC880` | EbbInit | -- | EBB state initialization |
| `sub_BEAD00` | StandardGvn | ~2.5KB | Standard dominator-guided GVN (mode 2) |
| `sub_BEA5F0` | PerBlockCse | ~9KB | Per-dominated-block CSE + commutative canon. |
| `sub_BEA450` | SimpleGvn | ~2KB | Simple single-block GVN (mode 1) |
| `sub_BEA3B0` | DomCheckCached | ~300B | Dominance check with global cache |
| `sub_BEA1E0` | EligibilityCheck | ~500B | Instruction eligibility (7 opcode classes) |
| `sub_BEA000` | EbbCandidateCheck | ~700B | EBB candidate dominator-chain walk |
| `sub_7E7380` | PredicateCompat | ~150B | Predicate-operand compatibility check |
| `sub_661250` | NoOp | ~6B | No-op stub (SM30/50/80/89) |
| `sub_7846D0` | BuildDomTree | -- | Dominator tree + RPO ordering builder |
| `sub_661750` | ScopeTreeInit | -- | Scoped value tree init/destroy |
| `sub_9314F0` | InsertMov | -- | Instruction insertion (generates MOV 292) |
| `sub_934630` | InsertMulti | -- | Instruction insertion (multi-operand variant) |
| `sub_931920` | InsertNode | -- | Instruction node insertion into linked list |
| `sub_9253C0` | DeleteInstr | -- | Instruction deletion |
| `sub_6B4520` | RecordBlock | -- | Block recording for dominator scoping |
| `sub_74D720` | DomOrdering | -- | Dominator ordering comparison |
| `sub_69DD70` | TreeExtract | -- | Tree node extraction (deferred processing) |
| `sub_7A1A90` | KnobQuery | -- | Knob query (per-instruction enablement) |
| `sub_91BC40` | MemSafetyCheck | -- | Memory operation safety check |
| `sub_A12EA0` | DomTreeWalk | -- | Dominator tree walker (EBB discovery) |

### GPU-Specific CSE Constraints

GPU CSE must respect constraints that do not arise in CPU compilers:

- **Divergence.** A uniform subexpression (same value across all threads in a warp) can be safely hoisted. A divergent subexpression may have different values per thread and must only be CSE'd within the same control-flow path. The GvnCse pass runs after `AnalyzeUniformsForSpeculation` (phase 27), which provides divergence annotations.

- **Barrier sensitivity.** A computation that reads shared memory before a `BAR.SYNC` cannot be commoned with an identical computation after the barrier, because intervening threads may have written different values. Memory operations with barrier dependencies are assigned unique value numbers. The actual barrier check is performed by `sm_backend->vtable[371]` (offset `+2968`), an architecture-specific predicate.

- **Register pressure.** Aggressive CSE can increase register pressure by extending the live range of the representative value. The `EnableGvnCse` knob allows the pass to be disabled when register pressure is the binding constraint.

- **Per-SM enablement.** GVN-CSE is only active on SM60, SM70, and SM90+. SM80/SM89 rely on LateOriCommoning (phase 64) and GeneralOptimize sub-passes instead. This per-generation selection is embedded in the SM backend vtable at slot 23.

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

Reassociation operates in two phases: an expression-normalization walk that rewrites instruction trees into a canonical form, followed by a hash-based commoning pass over the normalized IR.

**Phase 1 -- Expression normalization.**  The pass walks each basic block in RPO and inspects every instruction whose opcode is associative and commutative (ADD, MUL, AND, OR, XOR) or is SUB.

```
procedure ReassociateAndCommon(function F):
    for each basic block B in RPO(F):
        for each instruction I in B (budget-limited by ReassociateCSEBudget):

            // --- SUB rewriting (conditional) ---
            // Convert SUB to ADD+NEG only when the result feeds
            // exclusively into an associative ADD chain so the NEG
            // is absorbed during flattening.  A standalone "a - b"
            // with no further associative consumer keeps its SUB
            // to avoid inserting a gratuitous negate instruction.
            if I.opcode == SUB and all_uses_are_associative_add(I):
                tmp = NEG(I.src1)
                I   = ADD(I.src0, tmp)      // continue on rewritten ADD

            if I.opcode not in {ADD, MUL, AND, OR, XOR}:
                continue

            // --- Step 1: tree flattening ---
            // Walk the def-use chain upward, collecting every leaf.
            // An operand is a leaf when it is:
            //   (a) defined by a different opcode, or
            //   (b) a constant / immediate, or
            //   (c) defined outside the current basic block, or
            //   (d) has multiple uses (pulling deeper would duplicate).
            leaves[] = flatten(I)
            if |leaves| <= 2:  continue     // nothing to reassociate

            // --- Step 2: canonical sort ---
            //   Primary key:   non-constants first, constants last
            //   Secondary key: ascending value number (24-bit VN
            //                  from operand encoding bits [0:23])
            sort leaves[] by (is_constant(leaf), value_number(leaf))

            // --- Step 3: rebuild left-leaning tree ---
            result = OP(leaves[0], leaves[1])
            for k = 2 to |leaves|-1:
                result = OP(result, leaves[k])
            replace I's def-use chain with result

    // --- Phase 2: windowed local commoning ---
    // Sliding-window hash-based CSE (window = ReassociateCSEWindow).
    for each basic block B in RPO(F):
        run windowed local CSE over B
```

The rebuild produces a **left-leaning** (fully left-associated) chain, not a balanced tree.  `((a + b) + c) + d` guarantees that every intermediate node hashes identically when the leaf sequence matches, because `H(op, H_left, H_right)` yields a unique hash for each prefix.  A balanced tree would require matching the exact split point, defeating commoning across trees with different original nesting.

The SUB rewriting criterion is conservative: converting `a - b` to `a + NEG(b)` is profitable only when the ADD chain is long enough to benefit from flattening.  For a two-operand chain the NEG is pure overhead, so standalone SUBs are preserved.

### Why Reassociation Matters

The reassociation and commoning phases are tightly coupled because reassociation's primary goal is to enable commoning:

```
BB0:  R5 = (R2 + R3) + R4
      GvnCse hash: H(ADD, H(ADD, vn(R2), vn(R3)), vn(R4))
BB1:  R6 = (R2 + R4) + R3
      GvnCse hash: H(ADD, H(ADD, vn(R2), vn(R4)), vn(R3))
      -- Different inner-ADD operand order --> different hash.
```

After reassociation, both expressions flatten to `{R2, R3, R4}`.  Canonical sort orders by ascending VN, and the left-leaning rebuild produces `(R2 + R3) + R4` for both.  The second expression now shares the same value number and is eliminated.

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

`sub_9055F0` (203 lines decompiled) is the central commoning algorithm for late CSE. It operates on three cooperating data structures that together form the per-block hash-based equivalence table:

**Working-set object** (passed as `a1`, allocated by `sub_9059B0`):

| Field | Type | Meaning |
|-------|------|---------|
| `WS[0]` / `*WS` | `ptr` | Code Object pointer (function state `S`) |
| `WS[2]` | `i32` | MOV equivalence chain counter (nonzero triggers `sub_8F2CD0` flush) |
| `WS[7]` | `ptr` | **Per-block class table** -- `u64[block_count]` array indexed by dominator-order number (`bb_array[bix]+144`). Each slot is a Bloom-filter bitmask of instruction classes seen on the dominator path reaching that block. |
| `WS[8]` | `i32` | Allocated capacity of the class table (grown via `sub_6E6650`) |
| `WS[9]` | `u64` | **Running accumulator** -- OR of all class bitmasks returned by `sub_74ED70` for instructions processed so far on the current dominator path. Reset to 0 at SEL opcodes; snapshotted into the class table at PHI opcodes. |

**Register descriptor fields repurposed for VN equivalence** (at `reg_file[reg_id]`, i.e. `*(*(S+88) + 8*reg_id)`):

| Offset | Type | Meaning |
|--------|------|---------|
| +88 | `i32` | **VN representative register index.** When nonzero, the operand-remapping loop replaces the 24-bit register index with this value (upper tag byte preserved). This canonicalizes register references through MOV chains. |
| +92 | `i32` | **CSE chain slot.** Dominator-order block number linking this register's defining instruction into the per-block equivalence chain. Read by `sub_901A90` to locate candidates; written on successful commoning. |

**Instruction field `+88`** (accessed as `instr[11]`, an `i64`): initialized to `0xFFFFFFFF00000000` before the walk. Upper 32 bits = hash class (initially -1 = uncomputed); lower bits populated by `sub_74ED70` with the class bitmask.

Algorithm, reconstructed from the decompilation:

```
procedure LateCommoning(working_set WS):
    S = *WS                                    // Code Object
    if not knob_enabled(487):  return
    if S.byte[1368] & 0x02:  return            // already processed
    if (S.byte[1378] | S.byte[1368]) & 0x08:  return

    rebuild_def_chains(S, mode=1)              // sub_781F80
    rebuild_use_chains(S)                      // sub_763070
    compute_hash_values(S, 0, 0, 0, 0)        // sub_7E6090

    block_count = S.dword[520] + 1
    ensure WS.class_table has block_count slots (zero-fill new)
    WS.accumulator = 0                         // WS[9] = 0

    // Reset instruction hash slots
    for I in S.instruction_list (linked from S+104):
        I[11] = 0xFFFFFFFF00000000             // hash_class=-1, lower=0

    // Main walk: program-order traversal of code_list (S+272)
    for each instruction I in S.code_list:
        // --- Operand remapping via VN representatives ---
        for k = (I.operand_count - 1) downto 0:
            op = I.operands[k]                 // at I + 8*k + 84
            if op >> 28 == 1:                  // register ref
                rep = reg_file[op & 0xFFFFFF].field_88
                if rep != 0:
                    I.operands[k] = (op & 0xFF000000) | (rep & 0xFFFFFF)

        if not knob_enabled(844):  continue    // per-instruction CSE gate

        // --- Dispatch by opcode class ---
        if I.opcode == 72 (MOV):
            if WS[2] != 0:
                flush_equiv_chains(WS + 1)     // sub_8F2CD0
        elif is_pure(I):                       // sub_7DF3A0 bit 0
            masked = I.opcode & ~0x3000        // clear modifier bits 12-13
            if masked == 97 (SEL):
                WS.accumulator = 0             // control-dependent: kill path
            elif masked == 52 (PHI):
                // Snapshot accumulator into PHI's target block slot
                dom_ord = bb_array[I.bb_index].field_144
                WS.class_table[dom_ord] = WS.accumulator
        else:
            if not try_common(WS, I):          // sub_901A90
                class_mask = compute_class(S, I, 0)   // sub_74ED70
                WS.accumulator |= class_mask   // OR into Bloom filter
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

#### Structural Equivalence Predicate -- `sub_8F4510`

The pairwise equivalence check invoked by `sub_8F46F0` on each candidate is `sub_8F4510(S, block_existing, I_cand, I_cur)`. Two instructions are structurally equivalent -- written **EQUIV(I1, I2)** where I1 is the existing candidate and I2 is the current instruction -- iff all seven conditions hold simultaneously:

```
EQUIV(I1, I2)  :=
    C1  ∧  C2  ∧  C3  ∧  C4  ∧  C5  ∧  C6  ∧  C7

C1  (different block):    I1.bb_index  !=  I2.bb_index

C2  (dominance):          dominates(block(I1), block(I2))
        -- sub_76ABE0: fast path  (1 << dom_ord) & dom_set[dom_ord >> 5]
        -- if the fast-path flag S.byte[1370] bit 6 is clear, falls
           through to sub_76AAE0 (iterative dominator walk)

C3  (no scheduling dep):  !sched_barrier(S, I2, distance(S, I1, block(I1)))
        -- sub_74F5E0 with args (S, I2, sub_8F44A0(S, existing_blk, cand_blk), 0, 0)
        -- when block(I1) == block(I2), this check is skipped (distance = 0)

C4  (predicate compat):   pred_compatible(I1, I2)
        -- sub_7E7380:  if neither instruction is predicated (bit 12 of
           opcode word clear on both), returns true.  If both are predicated,
           requires: (a) the predicate register index (low 24 bits of the
           last source operand) matches, and (b) the preceding operand
           pair matches word-for-word (captures negate/condition modifiers)

C5  (operand count):      I2.operand_count > 0
        -- a zero-operand instruction cannot participate in CSE

C6  (sign agreement):     for each operand slot k in 0..operand_count-1:
                               sign(I1.op[k])  ==  sign(I2.op[k])
        -- the sign bit (bit 31 of the operand word) distinguishes
           definition operands (negative) from use operands (non-negative).
           Iteration terminates early if signs diverge at any position.

C7  (per-operand match):  for each operand slot k where op[k] < 0 (definitions):
    C7a (tag type):           tag(I1.op[k]) == tag(I2.op[k])
            -- bits [30:28] must agree (1 = register, 2/3 = immediate, 5 = const ref)
    C7b (modifier word):      I1.op_mod[k] == I2.op_mod[k]
            -- the companion dword (operands[2k+1] at I+88+8k) must match exactly
    C7c (register class):     reg_file[I1.op[k]].class == reg_file[I2.op[k]].class
            -- field +64 of the register descriptor must agree
    C7d (CSE chain slot):     reg_file[I1.op[k]].cse_slot == reg_file[I2.op[k]].cse_slot
            -- field +92 of both register descriptors must equal the same
               dominator-order block number, and that number must equal
               existing_block.field_144  (the current CSE scope)
        -- for non-negative (use) operands where tag == 1 (register) and the
           register is not a def-chain root (sub_7DEB90 returns false), the
           register's defining instruction must dominate the existing block;
           otherwise the operand is rejected
```

Source: `sub_8F4510` at `0x8F4510` (101 lines); predicate compatibility from `sub_7E7380` at `0x7E7380` (30 lines).  The outer driver `sub_8F46F0` calls EQUIV, and on success rewrites I2's definition operands to point at I1's value numbers (field +88 of each destination register descriptor).

### Instruction Class Bitmask -- `sub_74ED70`

`sub_74ED70` (304 lines) computes a **class bitmask** (not a scalar hash) for an instruction. The return value is a `u64` where individual bits encode instruction properties:

- Bit 0: base eligibility (1 for most pure instructions)
- Bits 20-21: memory address space class (`sub_74E530`)
- Bit 25: uniform register destination (register type 8)
- Bit 27: non-volatile memory reference
- Bit 28: non-invariant address
- Bit 29: specific addressing mode
- Bit 32+: extended class flags (predicated, volatile, etc.)

Additional property bits are OR'd in based on opcode class (96=LD/ST gets `0x200001`), purity flags (`sub_7DF3A0`), predication state, and backend queries through `ctx+1584` vtable slots 182 and 227. The resulting bitmask is OR'd into the working set's running accumulator (`WS[9]`), building a Bloom-filter signature of all instruction classes seen on the current dominator path. The `sub_901A90` commoning check uses this to quickly reject blocks that cannot contain a matching instruction.

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
| 97 | Definition anchor (`STG` in ROT13; used internally as a register-to-register MOV/definition marker -- actual SASS MOV is opcode 19) | Replace uses of destination with source |
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

The execute body (`sub_908EB0`, 217 lines) walks the flat instruction list in a single linear pass, maintaining two pieces of rolling state:

| Variable | Decompiler name | Type | Meaning |
|---|---|---|---|
| `copy_seen` | `v10` | `bool` | Previous instruction was a recognized copy -- gates the liveness fallback |
| `def_ctx` | `v11` | `int64_t` | Current definition tracking entry (BB-array pointer set by the most recent opcode 97) |

Initialization calls `sub_781F80(ctx, 1)` to rebuild per-instruction def-chain flags, then enters a single `do-while` over the linked list at `*(ctx+272)`.

```
procedure OriCopyProp(ctx):
    arch_pred = *(*(*(ctx+1584))+1312)(...)   // arch supports predicate marking?
    sub_781F80(ctx, 1)                         // rebuild def chains
    copy_seen = option_487_gate
    def_ctx   = NULL

    for each instruction I in linked_list(ctx+272):
        op = I.opcode & ~0x3000                // strip modifier bits 12-13

        case 97:   // definition anchor
            copy_seen = option_487_gate
            def_ctx = ctx.reg_table[ I.operand[0] & 0xFFFFFF ]
            continue

        case 18:   // predicated copy
            if not isEligible(ctx, I): continue
            copy_seen = false
            if arch_pred:
                I.dest_operand |= 0x400        // mark: under predicate guard
            continue

        case 124:  // conditional select (CSEL)
            if not isEligible(ctx, I): continue
            dst = &I.dest_operand

            if (ctx[1379] & 7) == 0:           // simple mode
                *dst |= 0x100; continue        // mark propagated directly

            if (*dst & 0xF) != 1:              // non-integer-constant type
                // try two-pass predicate simplifier
                if !sub_8F29C0(ctx) or (ctx[1379] & 0x1B) != 0:
                    sub_908A60(ctx, def_ctx, I, FWD, &hit, &partial)
                    if hit: goto mark
                    if !partial:
                        sub_908A60(ctx, def_ctx, I, BWD, &hit, &partial)
                        if hit: goto mark
                        if !partial: continue
                *dst = (*dst & 0xFFFFFDF0) | 0x201   // type=reg, deferred bit

            if !copy_seen or arch_pred:
        mark:   I.dest_operand |= 0x100       // mark propagated
                continue

            // --- transitive MOV chain resolution ---
            link = *(def_ctx + 136)            // backward def-chain link
            if link == NULL or *link != 0:     // absent or multi-def: bail
                continue
            def_instr = deref(ctx.reg_table[ *(link+8) ])
            def_op = def_instr.opcode & ~0x3000

            if def_op == 52: continue          // block boundary: stop
            if def_op == 18 or def_op == 124:  // another copy/CSEL
                if isEligible(ctx, def_instr): goto mark
                continue

            // walk forward through instruction list
            loop:
                status = sub_7DF3A0(def_instr, ctx)
                if (*status & 0xC) != 0: break // live uses found: stop
                def_instr = def_instr.next
                def_op = def_instr.opcode & ~0x3000
                if def_op == 52: break         // block boundary
                if def_op == 124 or def_op == 18:
                    if isEligible(ctx, def_instr): goto mark
                    break
            continue

        default:
            if not copy_seen:
                status = sub_7DF3A0(I, ctx)
                copy_seen = (*status & 0xC) != 0
            continue
```

**Transitive chain resolution.** When the pass encounters a CSEL (opcode 124) that was not immediately preceded by a recognized copy (`copy_seen` is false) and the architecture does not support predicate marking, it follows the defining instruction backward through `def_ctx+136`. If the single defining instruction is itself a copy or CSEL (opcode 18 or 124), propagation resolves transitively: the current instruction inherits the `0x100` propagated flag without the intermediate copy needing to be live. For other defining opcodes, the pass walks forward through the linked list calling `sub_7DF3A0` (liveness query) at each step until it finds a live use (stopping) or another copy/CSEL (resolving transitively). Opcode 52 (block boundary marker) terminates the walk in both directions.

**Convergence.** OriCopyProp itself is a single linear scan -- it does not iterate internally. The fixpoint behavior comes from the enclosing GeneralOptimize loop: Variant A (phases 13, 29) caps iterations via knob 464; Variant B (phases 37, 58) uses a cost-based threshold of 0.25 (knob 474). Each invocation may expose constant operands for folding or create dead instructions for DCE, motivating re-invocation.

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

All 24 knobs controlling copy propagation and CSE:

| Knob | ROT13 | Address | Controls |
|---|---|---|---|
| `EnableGvnCse` | `RanoyrTiaPfr` | `0x21BDA50` | Master enable for phase 49 |
| `EnableGvnCseMode` | -- | knob 402 | GVN mode selector (0=off, 1=simple, 2=standard, 3-6=full) |
| `EnableGvnCsePerInstr` | -- | knob 257 | Per-instruction GVN enablement gate |
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
| `sub_BEE590` | ~200 B | GvnCse body (SM60/70/90) | Entry point: rebuilds def chains, dispatches to mode |
| `sub_BEE370` | ~550 B | GvnCse mode dispatcher | Queries knob 402, selects mode 0-6 |
| `sub_BED7E0` | ~18 KB | FullGvnCse (modes 3-6) | RPO block walk + dominator-scoped CSE, 689 lines |
| `sub_BEAD00` | ~2.5 KB | StandardGvnCse (mode 2) | Dominator-guided GVN for SM < 32K threshold |
| `sub_BEA5F0` | ~9 KB | PerDominatedBlockCse | Per-block CSE within dominator subtree, commutative canon |
| `sub_BEA450` | ~2 KB | SimpleGvn (mode 1) | Basic GVN variant |
| `sub_BEA1E0` | ~500 B | GvnCse eligibility check | Opcode-based CSE eligibility (16,122,145,183,186,...) |
| `sub_BED430` | ~2 KB | EBB pre-pass | Extended basic block identification (gated by knob 210) |
| `sub_661250` | 6 B | GvnCse no-op stub | Returns 0 (SM30/50/80/89 vtable slot 23) |
| `sub_7846D0` | -- | Build dominator tree | Also computes RPO ordering at context+792 |
| `sub_661750` | -- | Scoped value tree | Init/destroy balanced BST for dominator scoping |
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
