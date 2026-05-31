# LiveRangeCalc

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **LLVM version note:** Based on LLVM 17.x `LiveRangeCalc.cpp` (the page's own diff table cites LLVM 17.x as baseline). NVIDIA adds dual-bitvector GP/predicate tracking, a small-function bypass (instruction count <= 15), an enlarged 296-byte segment structure with inlined SmallVectors, and a 4/5 active-block fraction not present in any upstream version.

LiveRangeCalc is the low-level engine inside LLVM's CodeGen that turns def/use information into live intervals -- contiguous `[SlotIndex, SlotIndex)` segments describing when each virtual register holds a value. It sits between the `SlotIndexes` numbering pass and the `LiveIntervals` analysis, performing the actual iterative dataflow computation that propagates liveness backward through the CFG and inserts PHI-def value numbers at merge points. In CICC v13.0 the implementation at `sub_2FC4FC0` is structurally based on upstream LLVM's `LiveRangeCalc::extend` / `calculateValues` but carries several NVIDIA-specific modifications: a dual-bitvector tracking scheme that separates general-purpose and predicate register liveness, a small-function bypass that skips the full dataflow for trivial kernels, and an enlarged per-segment structure (296 bytes) that inlines four separate SmallVector buffers to avoid heap allocations on the hot path.

| | |
|---|---|
| **Main entry** | `sub_2FC4FC0` (12,900 bytes, 78KB decompiled) |
| **Stack frame** | 504 bytes (`0x1F8`) |
| **Callers** | `sub_2FC8470` (LiveIntervals::computeRegUnitRange), `sub_2FC8230` (createDeadDef/addSegment), self-recursive |
| **SlotIndexes pass** | `sub_1F10BF0` (11KB), registered as `"slotindexes"` / `"Slot index numbering"` |
| **LiveIntervals analysis** | pipeline entry `"live-intervals"` (analysis ID `unk_4F96DB4`) |
| **Address range** | `0x2FBF390` -- `0x2FC8470` (full LiveRangeCalc cluster) |
| **Returns** | `bool` -- whether any live range was extended |

## SlotIndex Infrastructure

Before LiveRangeCalc can operate, every `MachineInstr` must have a `SlotIndex` -- a monotonically increasing integer that encodes both the instruction's position and a sub-slot discriminator (early-clobber, register, dead, etc.). The `SlotIndexes` pass at `sub_1F10BF0` walks the `MachineFunction` and assigns these numbers. CICC's implementation matches upstream LLVM: each `MachineBasicBlock` owns a contiguous range `[StartIdx, EndIdx)`, and the mapping from `SlotIndex` back to `MachineBasicBlock*` is maintained in a sorted array that supports binary search.

The sentinel values found in the binary confirm standard LLVM `DenseMap` usage:

| Sentinel | Value | Meaning |
|---|---|---|
| Empty key | `0xFFFFFFFFFFFFF000` | Slot has never been occupied |
| Tombstone | `0xFFFFFFFFFFFFE000` | Slot was occupied, then erased |

These appear throughout the segment hash table, the pending-def table, and the VNInfo chain, always as `DenseMap<SlotIndex, ...>` sentinels.

## Segment Structure Layout

Each live range segment in CICC is 296 bytes (`0x128`), substantially larger than upstream's `LiveRange::Segment` (which is 24 bytes). The inflation comes from four inlined SmallVector buffers that avoid separate heap allocations for the common case:

```text
Segment (296 bytes / 0x128):
  +0x00   u64   status / SlotIndex start (sentinel if free)
  +0x08   ptr   endpoint buffer (or inline at +0x18)
  +0x18   [16]  inline endpoint buffer
  +0x28         additional metadata (segment flags, subrange info)
  +0x50   ptr   register mask buffer (or inline at +0x60)
  +0x60   [56]  inline register mask buffer
  +0x98   ptr   kill-set buffer (or inline at +0xA8)
  +0xA8   [48]  inline kill-set buffer
  +0xD8   u32   kill count
  +0xE0   ptr   use-def chain buffer (or inline at +0xF0)
  +0xF0   [48]  inline use-def chain buffer
  +0x120  u32   total instruction count covered
```

Each pointer field follows the LLVM SmallVector convention: if the pointer equals the address of the inline buffer immediately following it, the data lives inline; otherwise it points to a heap allocation. During cleanup (Phase 1 of the algorithm), each segment's four buffers are freed individually before the segment is marked with the empty sentinel.

## VNInfo Structure

Value numbers are tracked via 120-byte (`0x78`) VNInfo nodes, allocated from a bump-pointer allocator at `[this+0x4A0]`:

```text
VNInfo (120 bytes / 0x78):
  +0x00   ptr   endpoint buffer (inline at +0x10)
  +0x08   u64   capacity (initial: 0x200000000 = inline cap 2)
  +0x10   [48]  inline endpoint buffer
  +0x40   ptr   kill-set buffer (inline at +0x50)
  +0x48   u64   capacity for kill-set
  +0x60   ptr   sub-chain pointer (phi resolution)
  +0x68   ptr   sub-chain pointer 2
  +0x70   u32   block number
  +0x74   u32   value number (initially unassigned)
```

The allocator is a classic bump allocator: a cursor at `[this+0x4A0]` advances by `0x10` per allocation, checked against capacity at `[this+0x448]`. When the arena fills, a slow-path reallocation grows the backing store. Deallocation chains through `sub_2FBF390`, which walks sub-chains and calls `free` with size `0x38` (56 bytes) per intermediate node and `0x78` (120 bytes) for the VNInfo itself.

## Algorithm

The computation in `sub_2FC4FC0` proceeds in eight phases. It is self-recursive: when iterative refinement discovers new work, the function calls itself to converge.

### Phase 1 -- Initialization and Cleanup (0x2FC4FC0 -- 0x2FC50C2)

Links the `SlotIndex` base (`[rdi] = [rsi+0x30]`), increments the iteration counter at `[this+0x10]`, and walks the existing segment table (stride `0x128`) freeing stale entries. Segments marked with the empty sentinel (`0xFFFFFFFFFFFFF000`) are skipped; tombstoned entries (`0xFFFFFFFFFFFFE000`) and live entries both have their four internal buffers freed and are then marked empty.

The cleanup loop at `0x2FC5040`--`0x2FC50AE` iterates with stride `0x128` over the segment array beginning at `rbx`. For each entry it checks `[rbx+0x00]` against both sentinels. If the entry is live or tombstoned, it frees four inlined SmallVector buffers in reverse allocation order:

1. `[rbx+0xE0]` -- use-def chain buffer (freed if pointer differs from inline region at `rbx+0xF0`).
2. `[rbx+0x98]` -- kill-set buffer (freed if pointer differs from inline region at `rbx+0xA8`).
3. `[rbx+0x50]` -- register mask buffer (freed if pointer differs from inline region at `rbx+0x60`).
4. `[rbx+0x08]` -- segment endpoint buffer (freed if pointer differs from inline region at `rbx+0x18`).

After freeing, the entry is stamped with the empty sentinel: `mov qword [rbx], 0xFFFFFFFFFFFFF000`. The old segment count stored at `[rdi+0x20]` is loaded into `r15d` at entry and used to bound the cleanup iteration.

### Phase 2 -- Auxiliary Table Cleanup (0x2FC50C2 -- 0x2FC52A3)

Resets the old segment count, increments the auxiliary sequence counter, and walks three secondary tables:

- **Pending-def table** at `[this+0x40]` (16-byte stride): cleared with empty sentinels.
- **VNInfo chain** at `[this+0xA0]`: walked back-to-front, freeing each node through `sub_2E0AFD0` (getRegInfo) and `sub_2FBF390`. The walk reads count from `[r13+0xA8]`, loads each entry at `[r12-8]`, decrements `r12`. For each VNInfo: frees sub-chains via `sub_2FBF390` (size `0x38` = 56 bytes per intermediate node), then frees the VNInfo itself (size `0x78` = 120 bytes) via `j_j___libc_free_0`.
- **Auxiliary tables** at offsets `0x130` (48-byte stride) and `0x480` (16-byte stride): freed/resized via `sub_C7D6A0` (realloc).
- Checks `[r13+0x458]` for additional pending work from a previous iteration.

### Phase 3 -- Block Count and Threshold Check (0x2FC52A3 -- 0x2FC53F4)

Computes the active block count from the MBB array: `active = (total_blocks * 4/5) - dead_block_count`. The `* 4/5` fraction is computed via the classic `imul 0xCCCCCCCD` trick for unsigned division by 5 on x86. If the result is zero, the function returns immediately.

The precise x86 idiom:

```asm
mov   rax, [rdx+10h]
sub   rax, [rdx+8]          ; pointer diff on MBB array
sar   rax, 3                ; divide by sizeof(pointer) = 8
imul  eax, 0xCCCCCCCD       ; unsigned multiply by magic constant
shr   eax, 2                ; result = total_blocks * 4 / 5 (rounded down)
sub   eax, [rdx+20h]        ; subtract dead_block_count
```

Two bitvectors are allocated on the stack for the live-in set. Initial inline capacity is 8 words (512 registers); if the block count exceeds 8, `SmallVector::grow` at `sub_C8D5F0` expands them. The pre-allocated capacity at `[r13+0xAC]` is also checked; if insufficient, `sub_2FC1040` (grow per-block segment table) is called.

**Small-function bypass:** If the total instruction count is 15 or fewer, OR the block count is 1 or fewer, OR the global flag `qword_5025F68` is set (`-Ofast-compile` mode `[LOW confidence]` -- the flag triggers a compile-time shortcut consistent with a fast-compile option, but no string or CLI mapping for this global has been recovered; it could also be a debug-only override or an internal tuning knob), the function skips the full dataflow and returns early. This is an NVIDIA addition not present in upstream LLVM -- it avoids the quadratic cost of bitvector dataflow on trivial kernel bodies where liveness is obvious from local analysis alone.

### Phase 4 -- Per-Block Segment Allocation (0x2FC538D -- 0x2FC55E7)

Calls `sub_2FC1A70` (ensureCapacity) to prepare per-block storage, then loops over all non-dead blocks summing instruction counts. For each block:

1. Allocates a 120-byte VNInfo via the bump allocator (`sub_22077B0`). If allocation fails, jumps to error path at `0x2FC7E1C`.
2. Initializes inline buffers with capacity markers (`0x200000000` -- encodes inline capacity 2 in the high 32 bits with size 0 in the low 32 bits, the standard LLVM SmallVector representation).
3. Sets `[vn+0x00]` = pointer to inline endpoint buffer (`rax+0x10`), `[vn+0x40]` = pointer to inline kill-set buffer (`rax+0x50`).
4. Clears sub-chain pointers: `[vn+0x60] = 0`, `[vn+0x68] = 0`.
5. Records the block number at `[vn+0x70] = ebx` and clears the value number `[vn+0x74] = 0`.
6. Advances the bump-pointer allocator at `[r14+0x4A0]` by `0x10` to allocate a "pending use" object. The allocator checks against capacity at `[r14+0x448]` and falls back to a slow-path reallocation when the arena fills.
7. Inserts the VNInfo into the `[this+0xA0]` vector (grows if needed via `sub_C7D6A0`).
8. Registers the block number in the `[this+0xC0]` map (grows if needed).
9. Frees old VNInfo if it was a placeholder from a previous iteration.

### Phase 5 -- Liveness Propagation via Bitvector Dataflow (0x2FC5656 -- 0x2FC5CC6)

This is the core computation -- a standard backward-dataflow fixed-point iteration, operating on 64-bit word bitvectors. It implements the classic liveness equation:

```text
LiveIn(B) = (LiveOut(B) \ Kill(B)) | Def(B)
LiveOut(B) = Union over all successors S of LiveIn(S)
```

The iteration continues until no bitvector word changes across a complete pass over all pending blocks. The `changed` flag (`var_1B0` on the stack) is cleared at the top of each outer iteration and set whenever any bitvector word is modified.

#### Detailed dataflow pseudocode

```rust
// Phase 5 reconstructed from sub_2FC4FC0 at 0x2FC5656--0x2FC5CC6
//
// State:
//   segment_table[]    -- hash table, stride 0x128, keyed by block ID
//     .gp_bv   (+0x98) -- general-purpose register bitvector (live set)
//     .pred_bv (+0xE0) -- predicate register bitvector (live set)
//     .kill_set(+0xA8) -- inline kill-set buffer
//     .kill_cnt(+0xD8) -- number of killed registers
//     .def_bv  (+0x08) -- def-set bitvector
//   worklist           -- pending blocks at [r13+0x50]
//   bv_words           -- number of 64-bit words = ceil(num_regs / 64)
//   changed            -- var_1B0 on stack

fn liveness_propagation(this: &mut LiveRangeCalc) -> bool {
    let bv_words: usize = (this.num_regs + 63) / 64;
    loop {
        let mut changed: bool = false;

        for block in this.worklist.iter() {
            // --- Step 1: Hash lookup for block's segment entry ---
            // Hash function: h = ((block.id >> 4) ^ (block.id >> 9))
            //                     & (capacity - 1)
            // Linear probing until key match or empty sentinel
            let entry = this.segment_table.lookup(block.id);

            // --- Step 2: Accumulate kill bitvector from kill set ---
            // The kill set at entry.kill_set contains register IDs
            // that are killed (last-use) within this block.
            // For each killed register, look up its own segment entry
            // and OR its kill bitvector into a local accumulator.
            let mut kill_accum: [u64; bv_words] = [0; bv_words];
            for i in 0..entry.kill_cnt {
                let killed_reg = entry.kill_set[i];
                let kill_entry = this.segment_table.lookup(killed_reg);
                // x86: OR [kill_accum + rdx*8], [kill_entry.kill_bv + rdx*8]
                for w in 0..bv_words {
                    kill_accum[w] |= kill_entry.gp_bv[w];
                }
            }

            // --- Step 3: Compute live_in for general-purpose registers ---
            // Standard backward dataflow: live_in = (live_out & ~kills) | defs
            // live_out is the current content of entry.gp_bv (propagated
            // from successors in previous iterations or initialization)
            let mut src: [u64; bv_words];
            for w in 0..bv_words {
                // x86: rax = NOT [kill_accum + w*8]
                //       rax = AND rax, [entry.gp_bv + w*8]    -- live_out & ~kills
                //       rax = OR  rax, [entry.def_bv + w*8]   -- | defs
                src[w] = (entry.gp_bv[w] & !kill_accum[w]) | entry.def_bv[w];
            }

            // Boundary mask: clear unused high bits in last word
            // x86: ecx = num_regs & 63
            //       shl rdx, cl; not rdx; and [src + (bv_words-1)*8], rdx
            if this.num_regs % 64 != 0 {
                let tail_bits = this.num_regs % 64;
                let mask = (1u64 << tail_bits) - 1;
                src[bv_words - 1] &= mask;
            }

            // --- Step 4: Interference check against allocated set ---
            // Compares computed live_in against the segment's "allocated"
            // bitvector at +0x98. Any bit set in src but NOT in allocated
            // indicates a new live register that extends the range.
            // x86 at 0x2FC5B86:
            //   rax = NOT [entry.gp_bv + rdx*8]   -- ~allocated
            //   rax = AND rax, [src + rdx*8]       -- new bits
            //   test rax, rax / jnz -> extend
            for w in 0..bv_words {
                let new_bits = src[w] & !entry.gp_bv[w];
                if new_bits != 0 {
                    entry.gp_bv[w] |= src[w];   // extend coverage
                    changed = true;
                }
            }

            // --- Step 5: Repeat identically for predicate register bv ---
            // The predicate bitvector at entry offset +0xE0 is processed
            // with exactly the same kill-accumulate / dataflow / interference
            // sequence. Predicate registers (%p0, %p1, ...) occupy a
            // physically separate register file in NVPTX hardware, so they
            // get their own independent bitvector to avoid inflating the
            // interference graph of the main register namespace.
            // [identical loop over pred_bv words omitted for brevity]

        } // end for each block

        if !changed {
            break;  // Fixed point reached
        }
        // Otherwise: var_1B0 was set to 1, loop back to top
    }
}
```

#### Convergence criteria

The fixed-point iteration terminates when a complete pass over all pending blocks produces no change to any bitvector word. Formally, convergence is guaranteed because:

1. **Monotonicity.** Each bitvector word can only gain bits (the `|=` operation in the interference-check step is monotone). Bits are never cleared during the iteration.
2. **Finite lattice.** The bitvector domain is a finite lattice of height `num_regs`. Each word can change at most 64 times (once per bit), so the total number of changes across all words and all blocks is bounded by `N * W * 64` where `N` = block count and `W` = bitvector width in words.
3. **Worst-case iterations.** In practice, the iteration converges in `O(D)` passes where `D` = maximum loop nesting depth of the CFG. Each pass propagates liveness information one level deeper through nested loops. The theoretical worst case is `N` iterations for a pathological CFG with a chain of `N` blocks each feeding into the next, but CUDA kernels rarely exhibit such structure.

The `changed` flag (`var_1B0`) is a single byte on the stack. It is zeroed with `mov byte [rbp+var_1B0], 0` at the top of each outer iteration and set with `mov byte [rbp+var_1B0], 1` whenever the interference check finds new bits. The outer `do { ... } while (changed)` loop tests this byte at `0x2FC5CC0` with `cmp byte [rbp+var_1B0], 0; jne` back to the loop head at `0x2FC5656`.

#### Kill and Def computation

The kill and def sets are **not** computed inside `sub_2FC4FC0` itself. They are pre-populated by callers before invoking the dataflow engine:

- **Kill set** (`+0xA8` inline buffer, count at `+0xD8`): Populated by `sub_2FC8470` (LiveIntervals::computeRegUnitRange) which walks each `MachineBasicBlock`'s instruction list. A register is added to the kill set when an instruction has a use operand that is the **last use** before the next def (or end of block). The kill set is stored as a flat array of register IDs, not a bitvector -- the dataflow loop then expands it into a bitvector accumulator by looking up each killed register in the hash table.

- **Def set** (`+0x08` endpoint buffer): Populated by the same caller. A register is added when a `MachineInstr` defines it (operand flag `isDef`). For NVPTX, since all registers are virtual, every def creates a fresh value number. The def set is stored as a bitvector where bit `i` is set if virtual register `i` is defined in the block.

- **Initial live-out** (`+0x98` for GP, `+0xE0` for predicate): Initialized to the empty set for all blocks. The dataflow iteration propagates liveness backward: when a use is found in a successor block with no preceding def, the register becomes live-out in the current block. The first iteration seeds liveness from the use/def information; subsequent iterations propagate it through the CFG.

This separation means the hash table must be fully populated with per-block kill and def information before `sub_2FC4FC0` enters Phase 5. The hash table at `sub_2FC0880` supports insert, lookup, and resize operations with open addressing.

#### Bitvector word-at-a-time implementation

All bitvector operations operate on 64-bit words with standard x86-64 bitwise instructions:

| Operation | x86 pattern | Semantics |
|---|---|---|
| Union (OR) | `or [rdx+rax*8], rcx` | `bv[w] |= val` |
| Difference (AND-NOT) | `mov rax, [rsi+rdx*8]; not rax; and rax, [rdi+rdx*8]` | `new = src[w] & ~allocated[w]` |
| Boundary mask | `mov ecx, count_mod_64; mov rdx, -1; shl rdx, cl; not rdx; and [ptr+last_word], rdx` | Clear unused high bits |
| Zero test | `test rax, rax; jnz target` | Any bit set? |

The boundary mask is critical for correctness: without it, garbage bits in the padding region of the last word would create phantom interference. The mask is computed once per iteration entry and applied after every live-in computation. The instruction sequence `shl rdx, cl; not rdx` creates a mask with `count % 64` low bits set and the rest cleared.

#### Hash table for segment lookup

The segment hash table (`sub_2FC0880`) uses the standard DenseMap infrastructure with LLVM-layer sentinels (-4096 / -8192) and an entry stride of `0x128` (296 bytes), matching the full segment structure size. See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing, and growth policy.

During the dataflow iteration, each block requires two hash lookups per killed register (one for the block entry, one for each killed register's entry), so the total hash table traffic per iteration is `O(N * K_max)` where `K_max` is the maximum kill-set size across all blocks. Since NVPTX virtual register counts are typically in the hundreds (bounded by `-maxreg`, default 70), the hash table remains small and cache-friendly.

### Phase 6 -- PHI Value Resolution (0x2FC5ED8 -- 0x2FC5F95)

After the dataflow converges, resolves PHI-def values at block boundaries. For each block, walks the predecessor chain at `[block+0x30]` and calls `sub_2FBF8B0` (resolvePhiValue / findReachingDef) with four arguments: the `LiveRangeCalc*`, predecessor MBB, current bitvector, and a stack-allocated phi resolution buffer. This is the same algorithm as upstream `LiveRangeCalc::updateSSA` -- it propagates live-out values down the dominator tree and inserts PHI-def VNInfo nodes where multiple values reach a merge point.

The `var_181` byte is initialized to 0 before each block as a "phi_resolved" flag. If `sub_2FBF8B0` returns true, control jumps to `0x2FC710C` for phi merge handling -- this path allocates a new VNInfo, links it into the sub-chain at `[vn+0x60]`/`[vn+0x68]`, and updates the block's value number at `[vn+0x74]`. The temporary phi resolution buffer is freed after each block regardless of the outcome.

### Phase 7 -- Segment Endpoint Fixup (0x2FC5FA8 -- 0x2FC6021)

For each word in the destination bitvector that has bits set (masked with `0xFFFFFFFFFFFFFFF8` to skip low tag bits), looks up the block's `SlotIndex` via `[r14+0x18]` shifted and indexed into the SlotIndex table at `[rcx+0x98]`, retrieves the segment's use-def chain at `[rdi+0x40]`, and calls `sub_2E0F080` (addSegment / extendInBlock) to materialize the `[start, end)` segment in the `LiveRange` object. After processing all pending blocks, advances to the next MBB in the linked list via `[r14+8]`, continuing until hitting the sentinel at `[rbp+var_1F0]`.

### Phase 8 -- Finalization and Return (0x2FC5974 -- 0x2FC59E6)

If no interference was found across all iterations, frees pending blocks from the `[this+0x4A8]` array (via `sub_2E88E20`), sets the pending count to zero (`[r13+0x4B0] = 0`), frees any dynamically-allocated bitvectors, and returns `bool` indicating whether any live range was extended. The return value is derived from `var_1F0 = (count != 0)`.

## Dual Bitvector Tracking

The most significant NVIDIA-specific modification is maintaining two independent bitvectors per segment:

| Offset | Register class | Purpose |
|---|---|---|
| `+0x98` | General-purpose registers | `%r`, `%rd`, `%f`, `%fd`, `%h`, `%fh` liveness |
| `+0xE0` | Predicate registers | `%p` liveness |

Both bitvectors are processed by identical code paths in Phase 5, but independently -- kills in one class do not affect the other. This separation reflects NVPTX's hardware architecture where predicate registers occupy a physically separate register file from data registers. Upstream LLVM's `LiveRangeCalc` handles all register classes through a single unified mechanism; CICC's split avoids interference-graph inflation by keeping the small predicate namespace out of the main bitvector.

The two bitvectors are processed sequentially within the same iteration body (not in separate passes). For each pending block, the general-purpose bitvector at `+0x98` is processed first, then the predicate bitvector at `+0xE0` is processed with structurally identical code. The `changed` flag is shared between both -- a change in either bitvector triggers another iteration of the outer loop. This means the predicate register dataflow rides for free on the same convergence pass, and the two bitvectors converge simultaneously.

The register coalescer at `sub_34A46B0` also maintains a bitvector-per-block structure (a 12,336-byte stack buffer `v90[12336]` at offset `0x270` used as a bitmap for tracking live-through blocks during range rebuild after coalescing). That coalescer bitvector feeds updated information back into the LiveRangeCalc segment table when live intervals are modified by register coalescing.

## Differences from Upstream LLVM

CICC v13.0's LiveRangeCalc diverges from upstream LLVM `LiveRangeCalc` (as of LLVM 17.x) in these specific ways:

1. **Dual bitvector tracking.** Upstream uses a single mechanism for all register classes. CICC splits GP and predicate into independent bitvectors to exploit the physical separation in NVPTX hardware.

2. **Small-function bypass.** The instruction-count threshold of 15 and the block-count threshold of 1 are NVIDIA additions. Upstream always runs the full dataflow. This optimization is significant because CUDA kernels frequently contain tiny `__device__` helper functions that are inlined by the optimizer.

3. **Global fast-compile flag.** The `qword_5025F68` check that bypasses the entire dataflow loop has no upstream equivalent. It is likely tied to the `-Ofast-compile` or `-O0` optimization level in cicc.

4. **Enlarged segment structure.** Upstream's `LiveRange::Segment` is 24 bytes (start SlotIndex, end SlotIndex, VNInfo pointer). CICC's segment is 296 bytes (`0x128`), inlining four SmallVector buffers to avoid heap allocations on the hot path. This is a performance optimization for the common case where segments have small kill sets and few endpoints.

5. **Active-block fraction.** The `* 4/5` computation in Phase 3 (via `imul 0xCCCCCCCD`) to determine the active block count is not present in upstream. Upstream counts all blocks equally. CICC discounts approximately 20% of blocks, likely accounting for unreachable or dead blocks that StructurizeCFG may have created but not yet eliminated.

6. **PhysReg parameter always zero.** Upstream's `findReachingDefs` takes a `Register PhysReg` parameter for physical register interference. Since NVPTX has no physical registers (all registers are virtual and hardware-mapped at launch time), this parameter is always `Register()` (zero). The binary confirms: `sub_2E0FDD0` (isAllocatable) is called but its return value never gates segment creation.

## GPU-Specific Considerations

**Virtual-only register file.** NVPTX has no physical registers in the LLVM sense -- all registers are virtual (`%r0`, `%f0`, `%p0`, ...) and the hardware thread scheduler maps them at launch time. This means LiveRangeCalc never needs to handle physical register liveness, live-in lists for calling conventions, or register unit interference. The `PhysReg` parameter in upstream's `findReachingDefs` is always `Register()` (zero). The binary confirms this: `sub_2E0FDD0` (isAllocatable / reserved register check) is called but its return value is never used to gate segment creation.

**Pressure-driven analysis.** The live intervals produced by LiveRangeCalc feed directly into the greedy register allocator's interference cache (at `selectOrSplit` offset `+648`). Since NVPTX allocation is pressure-driven rather than assignment-driven, the intervals primarily serve to detect which virtual registers are simultaneously live, not to assign physical registers. The total count of simultaneously-live intervals at any program point determines the register pressure, which the allocator compares against the `-maxreg` limit (default 70).

**Small-kernel bypass.** The threshold check in Phase 3 (instruction count <= 15 OR block count <= 1) is absent from upstream LLVM. CUDA kernels frequently contain tiny helper device functions that are inlined into the caller; computing full dataflow liveness for a 10-instruction single-block function is pure overhead. The bypass returns immediately, letting the register allocator fall back to local analysis.

## Configuration

| Knob | Default | Effect |
|---|---|---|
| `early-live-intervals` | `false` | Runs LiveIntervals analysis earlier in the pipeline, before the standard scheduling pass |
| `join-liveintervals` | `true` | Master enable for register coalescing over live intervals |
| `qword_5025F68` (global flag) | `0` | When nonzero (likely `-Ofast-compile`), skips the full dataflow loop entirely |

The instruction-count threshold of 15 and the block-count threshold of 1 are hardcoded constants, not configurable via LLVM `cl::opt` flags.

## LiveRangeCalc Object Layout

The `LiveRangeCalc` object (`this` pointer passed in `rdi`) is reconstructed from register offsets observed throughout `sub_2FC4FC0`:

```text
LiveRangeCalc (approx 0x4C0 bytes):
  +0x00   ptr    SlotIndex base (set from [rsi+0x30] in Phase 1)
  +0x08   ptr    VNInfo* / MBB* parameter (set from rsi in Phase 1)
  +0x10   u32    iteration counter (incremented each call)
  +0x14   u32    (padding / alignment)
  +0x20   u32    old segment count (r15d loaded in Phase 1)
  +0x30   u32    auxiliary sequence counter (incremented in Phase 2)
  +0x40   ptr    pending-def table (16-byte stride)
  +0x50   ptr    worklist (pending blocks array)
  +0xA0   ptr    VNInfo chain (vector of VNInfo*)
  +0xA8   u64    VNInfo chain count
  +0xAC   u32    pre-allocated capacity for per-block segment table
  +0xC0   ptr    block-number-to-VNInfo map
  +0x130  ptr    auxiliary table (48-byte stride)
  +0x440  ptr    bump allocator arena base
  +0x448  u64    bump allocator capacity
  +0x458  ptr    additional pending work (checked in Phase 2)
  +0x480  ptr    secondary auxiliary table (16-byte stride)
  +0x4A0  ptr    bump allocator cursor (advances by 0x10 per allocation)
  +0x4A8  ptr    pending-blocks array (freed in Phase 8)
  +0x4B0  u64    pending block count (zeroed in Phase 8)
```

## Complexity

- **Per iteration:** `O(N * W)` where `N` = number of basic blocks, `W` = bitvector width in words (`ceil(num_regs / 64)`). Both GP and predicate bitvectors are processed per iteration, so the actual cost is `O(N * (W_gp + W_pred))`, but since predicate register counts are small (typically < 64, fitting in a single word), the predicate contribution is `O(N)`.
- **Kill-set expansion per iteration:** `O(N * K_max * W)` where `K_max` = maximum kill-set size per block. For each of the `N` blocks, up to `K_max` hash lookups and `W`-word OR operations are performed.
- **Convergence:** Typically `O(D)` iterations where `D` = maximum loop nesting depth. The monotonicity of the OR-based bitvector union guarantees termination. Worst case is `O(N)` iterations for a pathological single-predecessor chain, but CUDA kernels (especially after StructurizeCFG) have bounded nesting depth.
- **Total:** `O(N * W * D)` for the core liveness computation, plus `O(N * K_max * W * D)` for kill-set expansion.
- **Hash table operations:** `O(1)` amortized per lookup. Load factor is maintained below 75% by the DenseMap rehash policy.
- **Memory:** `O(N * W)` for bitvectors + `O(S * 296)` for the segment table where `S` = number of live segments + `O(V * 120)` for VNInfo nodes where `V` = number of value numbers.
- **Phase 1 cleanup:** `O(S_old)` where `S_old` = segment count from previous iteration. Each segment requires checking four buffer pointers and potentially freeing four allocations.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| **LiveRangeCalc::extend / calculateValues** -- main entry, self-recursive (12,900 bytes, 78KB decompiled) | `sub_2FC4FC0` | -- | -- |
| LiveIntervals::computeRegUnitRange (caller, populates kill/def sets) | `sub_2FC8470` | -- | -- |
| LiveIntervals::createDeadDef / addSegment (caller) | `sub_2FC8230` | -- | -- |
| ensureCapacity / resetLiveRanges (per-block storage preparation) | `sub_2FC1A70` | -- | -- |
| grow per-block segment table (called when `[r13+0xAC]` insufficient) | `sub_2FC1040` | -- | -- |
| interval building helper (called from `sub_2FC1040`) | `sub_2FC1190` | -- | -- |
| hash table operations: insert/lookup/resize with open addressing | `sub_2FC0880` | -- | -- |
| segment creation / initialization (296-byte struct setup) | `sub_2FC0040` | -- | -- |
| resolvePhiValue / findReachingDef (PHI resolution, 4 args) | `sub_2FBF8B0` | -- | -- |
| free VNInfo chain (frees 0x38-byte intermediate nodes, 0x78-byte VNInfo) | `sub_2FBF390` | -- | -- |
| segment merge / extend (interference update) | `sub_2FBFCC0` | -- | -- |
| live range query | `sub_2FC3C20` | -- | -- |
| live range intersection test | `sub_2FC3A50` | -- | -- |
| getRegInfo / MachineRegisterInfo query | `sub_2E0AFD0` | -- | -- |
| isAllocatable / reserved register check (return value unused in NVPTX) | `sub_2E0FDD0` | -- | -- |
| addSegment / extendInBlock (materializes `[start, end)` segments) | `sub_2E0F080` | -- | -- |
| MachineFunction helper | `sub_2E76F70` | -- | -- |
| eraseFromParent (MachineInstr deletion, used in Phase 8 cleanup) | `sub_2E88E20` | -- | -- |
| register property check (called with flags `0x80000`, `0x100000`) | `sub_2E88A90` | -- | -- |
| operator new (VNInfo allocation, 120 bytes) | `sub_22077B0` | -- | -- |
| SlotIndexes::runOnMachineFunction (11KB) | `sub_1F10BF0` | -- | -- |
| SlotIndexes pass registration (`"slotindexes"` / `"Slot index numbering"`) | `sub_1F10320` | -- | -- |
| SlotIndexes insertion / repair (13KB) | `sub_1F112A0` | -- | -- |
| SlotIndex validity check (string: `"invalid"`) | `sub_1F10810` | -- | -- |
| computeLiveIntervals (RA integration, called from greedy RA init) | `sub_2F54D60` | -- | -- |
| SmallVector::grow (bitvector expansion when block count > 8) | `sub_C8D5F0` | -- | -- |
| realloc (SmallVector resize / auxiliary table resize) | `sub_C7D6A0` | -- | -- |
| malloc (new allocation) | `sub_C7D670` | -- | -- |

## Cross-References

- [Register Allocation](./register-allocation.md) -- consumes live intervals to drive the pressure-based greedy allocator
- [Register Coalescing](./register-coalescing.md) -- merges live ranges of copy-connected virtual registers; runs before RA, feeds updated intervals back through LiveRangeCalc
- [Instruction Scheduling](./scheduling.md) -- the `SlotIndexes` numbering assigned here is consumed during post-RA scheduling for latency-aware reordering
- [SelectionDAG](./selectiondag.md) -- produces the initial `MachineInstr` stream that SlotIndexes numbers
