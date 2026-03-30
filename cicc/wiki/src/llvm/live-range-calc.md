# LiveRangeCalc

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

```
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

```
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

### Phase 2 -- Auxiliary Table Cleanup (0x2FC50C2 -- 0x2FC52A3)

Resets the old segment count, increments the auxiliary sequence counter, and walks three secondary tables:

- **Pending-def table** at `[this+0x40]` (16-byte stride): cleared with empty sentinels.
- **VNInfo chain** at `[this+0xA0]`: walked back-to-front, freeing each node through `sub_2E0AFD0` (getRegInfo) and `sub_2FBF390`.
- **Auxiliary tables** at offsets `0x130` (48-byte stride) and `0x480` (16-byte stride): freed/resized.

### Phase 3 -- Block Count and Threshold Check (0x2FC52A3 -- 0x2FC53F4)

Computes the active block count from the MBB array: `active = (total_blocks * 4/5) - dead_block_count`. The `* 4/5` fraction is computed via the classic `imul 0xCCCCCCCD` trick for unsigned division by 5 on x86. If the result is zero, the function returns immediately.

Two bitvectors are allocated on the stack for the live-in set. Initial inline capacity is 8 words (512 registers); if the block count exceeds 8, `SmallVector::grow` at `sub_C8D5F0` expands them.

**Small-function bypass:** If the total instruction count is 15 or fewer, OR the block count is 1 or fewer, OR the global flag `qword_5025F68` is set (likely `-Ofast-compile` mode), the function skips the full dataflow and returns early. This is an NVIDIA addition not present in upstream LLVM -- it avoids the quadratic cost of bitvector dataflow on trivial kernel bodies where liveness is obvious from local analysis alone.

### Phase 4 -- Per-Block Segment Allocation (0x2FC538D -- 0x2FC55E7)

Calls `sub_2FC1A70` (ensureCapacity) to prepare per-block storage, then loops over all non-dead blocks summing instruction counts. For each block:

1. Allocates a 120-byte VNInfo via the bump allocator (`sub_22077B0`).
2. Initializes inline buffers with capacity markers (`0x200000000`).
3. Records the block number at `+0x70` and clears the value number.
4. Allocates a 16-byte "pending use" object from the arena at `[this+0x4A0]`.
5. Inserts the VNInfo into the `[this+0xA0]` vector and registers the block in the `[this+0xC0]` map.

### Phase 5 -- Liveness Propagation via Bitvector Dataflow (0x2FC5656 -- 0x2FC5CC6)

This is the core computation -- a standard backward-dataflow fixed-point iteration, operating on 64-bit word bitvectors.

```
// Pseudocode for the inner loop
do {
    changed = false;
    for each pending_block in worklist {
        // Hash lookup for block's live set
        entry = hash_lookup(pending_block.id);  // hash = (id >> 4) ^ (id >> 9)

        // Kill set subtraction
        for each killed_reg in entry.kill_set {
            kill_entry = hash_lookup(killed_reg);
            src_bitvec |= kill_entry.kill_bitvec;  // accumulate kills
        }

        // Standard backward dataflow equation
        live_in = (live_out & ~kills) | defs;

        // Interference check (word-at-a-time)
        for each word i in bitvector {
            conflict = ~allocated[i] & live_in[i];
            if (conflict != 0) {
                allocated[i] |= live_in[i];  // extend coverage
                changed = true;
            }
        }

        // Repeat for predicate register bitvector (offset 0xE0)
        // ... identical logic on the second bitvector ...
    }
} while (changed);
```

The hash table uses open addressing with linear probing. Hash function: `(key >> 4) ^ (key >> 9)`, masked by `capacity - 1` (capacity is always a power of two). Table resize uses the standard bit-smearing pattern: `x |= x>>1; x |= x>>2; ... x += 1`.

Bitvector operations are word-at-a-time (64 bits per word). The last word is boundary-masked: `shl rdx, cl; not rdx; and [ptr], rdx` to clear unused high bits when the register count is not a multiple of 64.

### Phase 6 -- PHI Value Resolution (0x2FC5ED8 -- 0x2FC5F95)

After the dataflow converges, resolves PHI-def values at block boundaries. For each block, walks the predecessor chain at `[block+0x30]` and calls `sub_2FBF8B0` (resolvePhiValue / findReachingDef) with four arguments: the `LiveRangeCalc*`, predecessor MBB, current bitvector, and a stack-allocated phi resolution buffer. This is the same algorithm as upstream `LiveRangeCalc::updateSSA` -- it propagates live-out values down the dominator tree and inserts PHI-def VNInfo nodes where multiple values reach a merge point.

### Phase 7 -- Segment Endpoint Fixup (0x2FC5FA8 -- 0x2FC6021)

For each word in the destination bitvector that has bits set (masked with `0xFFFFFFFFFFFFFFF8` to skip low tag bits), looks up the block's `SlotIndex` and calls `sub_2E0F080` (addSegment / extendInBlock) to materialize the `[start, end)` segment in the `LiveRange` object.

### Phase 8 -- Finalization and Return (0x2FC5974 -- 0x2FC59E6)

If no interference was found across all iterations, frees pending blocks from the `[this+0x4A8]` array (via `sub_2E88E20`), sets the pending count to zero, frees any dynamically-allocated bitvectors, and returns `bool` indicating whether any live range was extended.

## Dual Bitvector Tracking

The most significant NVIDIA-specific modification is maintaining two independent bitvectors per segment:

| Offset | Register class | Purpose |
|---|---|---|
| `+0x98` | General-purpose registers | `%r`, `%rd`, `%f`, `%fd`, `%h`, `%fh` liveness |
| `+0xE0` | Predicate registers | `%p` liveness |

Both bitvectors are processed by identical code paths in Phase 5, but independently -- kills in one class do not affect the other. This separation reflects NVPTX's hardware architecture where predicate registers occupy a physically separate register file from data registers. Upstream LLVM's `LiveRangeCalc` handles all register classes through a single unified mechanism; CICC's split avoids interference-graph inflation by keeping the small predicate namespace out of the main bitvector.

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

## Complexity

- **Per iteration:** `O(N * W)` where `N` = number of basic blocks, `W` = bitvector width in words (`ceil(num_regs / 64)`).
- **Convergence:** Typically `O(D)` iterations where `D` = maximum loop nesting depth.
- **Total:** `O(N * W * D)` for the liveness computation.
- **Hash table operations:** `O(1)` amortized per lookup.
- **Memory:** `O(N * W)` for bitvectors + `O(S * 296)` for the segment table where `S` = number of live segments.

## Function Map

| Address | Identity |
|---|---|
| `sub_2FC4FC0` | **LiveRangeCalc::extend / calculateValues** -- main entry, self-recursive |
| `sub_2FC8470` | LiveIntervals::computeRegUnitRange (caller) |
| `sub_2FC8230` | LiveIntervals::createDeadDef / addSegment (caller) |
| `sub_2FC1A70` | ensureCapacity / resetLiveRanges |
| `sub_2FC1040` | grow per-block segment table |
| `sub_2FC0880` | hash table operations (insert/lookup/resize) |
| `sub_2FC0040` | segment creation / initialization |
| `sub_2FBF8B0` | resolvePhiValue / findReachingDef |
| `sub_2FBF390` | free VNInfo chain |
| `sub_2FBFCC0` | segment merge / extend |
| `sub_2FC3C20` | live range query |
| `sub_2FC3A50` | live range intersection test |
| `sub_2E0AFD0` | getRegInfo / MachineRegisterInfo query |
| `sub_2E0FDD0` | isAllocatable / reserved register check |
| `sub_2E0F080` | addSegment / extendInBlock |
| `sub_2E88E20` | eraseFromParent (MachineInstr deletion) |
| `sub_22077B0` | operator new (VNInfo allocation, 120 bytes) |
| `sub_1F10BF0` | SlotIndexes::runOnMachineFunction |
| `sub_1F112A0` | SlotIndexes insertion / repair |
| `sub_1F10810` | SlotIndex validity check (string: `"invalid"`) |
| `sub_2F54D60` | computeLiveIntervals (RA integration) |

## Cross-References

- [Register Allocation](./register-allocation.md) -- consumes live intervals to drive the pressure-based greedy allocator
- [Register Coalescing](./register-coalescing.md) -- merges live ranges of copy-connected virtual registers; runs before RA, feeds updated intervals back through LiveRangeCalc
- [Instruction Scheduling](./scheduling.md) -- the `SlotIndexes` numbering assigned here is consumed during post-RA scheduling for latency-aware reordering
- [SelectionDAG](./selectiondag.md) -- produces the initial `MachineInstr` stream that SlotIndexes numbers
