# SB Size Legalization

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The pass lives in `neuronxcc/starfish/lib/libwalrus.so`; for its `.text`/`.rodata` the virtual address equals the file offset. Other wheels differ — treat every address as version-pinned.*

## Abstract

`SBSizeLegalization` is the pre-allocator pass that makes oversized on-chip tensors *fit*. The SBUF coloring allocator ([8.17](sbuf-liveness-interference.md)) assigns every `bir::MemoryLocationSet` a base partition and a byte span inside a fixed 128-partition × *N*-byte budget; a tensor whose footprint exceeds that budget can never be colored — it fatals with `"is too big for SB"` no matter how the interference graph is ordered. This pass runs *before* coloring, scans every tensor against the same two SBUF geometry limits the allocator will later enforce, and rewrites the offenders' shapes so they fit. It is the size-fitting twin of the value-number splitter: where `vn_splitter` mints `_VN_<k>` sub-tensors and `ShrinkDN` shrinks footprints, this pass owns its *own* shrink, its *own* legality predicate, and — the architectural headline — it does **not** clone tensors itself.

The single fact that distinguishes this pass from every naive reading of it is the **delegated split**. A reader who sees the string `"tenosrizer split"` (sic) and the method `set_splitShape` expects this pass to clone an oversized `MemoryLocationSet` into *N* sub-tensors in place. It does not. `set_splitShape` computes a *per-dimension boolean mask* — `SmallVector<bool,4>` of "which dims are splittable" — and stores it via `MemoryLocationSet::setSplitShape`. The actual clone (allocate the sub-tensors, divide the marked dim, repoint the access patterns) is performed by `FullUnroll::tensorSplit(MemoryLocationSet&, Function&)` @ `0xb70000`, which *reads* `getSplitShape` ([8.3](translate-nki-unroll.md)). The split machinery is the unroller's; this pass is its client. The same mechanism reuse appears on the force-unroll fallback path: when shrink + split still cannot legalize a tensor, the pass does not unroll loops itself — it bumps an unroll factor and hands the loop back to the `FullUnroll` machinery to flatten.

The geometry the pass legalizes *against* is recovered separately ([1.05](../arch/sbuf-psum-geometry.md)): `SBModel+0x9c` is the partition count (128), `SBModel+0x98` is the per-partition byte budget, both read from the `getArchModel(codename)` singleton tree. The legality predicate `is_legal_tensor` is exactly those two comparisons; the legal *tile* shapes a matmul-touched tensor may be cut into come from a shared red-black-tree table `validTileSizes` (`{0:{32,64,128}, 32:{32}, 64:{32,64}, 96:{32}}`), the same util the coloring allocator uses so the two passes agree on which tile positions are placeable.

For reimplementation, the contract is:

- The legality predicate `is_legal_tensor` — the SBUF-fit compare against `SBModel+0x98`/`+0x9c`, dispatched on the storage discriminator at `StorageBase+0xd8`, with three distinct cases (MLSet partition-count, MemLoc partition-dim max-bytes, MemLoc free-dim rounded-up byte product).
- The driver `run` and its fixpoint: `flatten → shrink_tensor → (set_splitShape) | (force_unroll_update* → set_splitShape) → profile`, gated on `ArchLevel` (10 special, ≤19 no-op, >19 full).
- The delegated split: `set_splitShape` produces a `bool` mask consumed by `FullUnroll::tensorSplit`; this pass never clones a tensor.
- The shared tile-shape table `validTileSizes` and the `isValidTileSize` map lookup the matmul path consults.

| | |
|---|---|
| **Class** | `neuronxcc::backend::SBSizeLegalization : public BackendPass` |
| **vtable / typeinfo** | `0x3d8e6a0` / `0x3d8e678` |
| **Driver** | `run(bir::Module&)` @ `0xbc9fc0` (size `0x300`) |
| **Legality predicate** | `is_legal_tensor(StorageBase*, uint)` @ `0xbc1200` |
| **SBUF limits** | `SBModel+0x98` = bytes/partition; `SBModel+0x9c` = partition count (128) |
| **Mark-for-split** | `set_splitShape(Module&)` @ `0xbc6760` → `MemoryLocationSet::setSplitShape(SmallVector<bool,4>)` |
| **Delegated clone** | `FullUnroll::tensorSplit(MemoryLocationSet&, Function&)` @ `0xb70000` (reads `getSplitShape`) |
| **Shrink path** | `shrink_tensor(Module&)` @ `0xbc36d0` → `setLiveN` / `setShrinkDim` |
| **Force-unroll fallback** | `force_unroll_update(Module&)` @ `0xbc7e40` (fixpoint with `set_splitShape`) |
| **Tile-shape table** | `validTileSizes` @ `0x3e01ac0` (.bss); `isValidTileSize` @ `0x108e0c0` |
| **Source assertion** | `__PRETTY_FUNCTION__` cites `walrus/walrus_loop_flow/sb_size_legalization/src/sb_size_legalization.cpp` |

The full method roster — every address — was lifted directly from `nm -DC libwalrus.so`; the `.c`/`.asm` sidecars at low (`0x5e..`) VAs are PLT thunk stubs, and all bodies on this page are disassembled from the real high-VA addresses.

---

## The driver

### Purpose

`run` is the pass entry. It chooses, by hardware generation, whether to legalize at all, then sequences the four worker phases through a fixpoint: a one-shot shrink and split for tensors that fit after shrinking, and an iterated force-unroll-then-re-split loop for tensors that do not.

### Entry Point

```text
SBSizeLegalization::run(Module&)        @0xbc9fc0   ── BackendPass entry
  ├─ ArchLevel2string(Module+0xac) → getArchModel(s) → SBModel*   ── geometry handle
  ├─ flatten(bb, &counter)            @0xbc1ce0  ×(per BasicBlock) ── flat inst view
  ├─ shrink_tensor(Module&)           @0xbc36d0  ── footprint shrink (setLiveN/setShrinkDim)
  ├─ pick_unroll_loop()               @0xbc2360 → bool ── any tensor still illegal?
  │     ├─ false → set_splitShape; profile_sb_legalization; return  (one-shot)
  │     └─ true  → force-unroll fixpoint:
  │                  force_unroll_update(Module&) @0xbc7e40 → int
  │                     nonzero → force again
  │                     zero    → set_splitShape; profile_sb_legalization; return
  └─ (set_splitShape @0xbc6760 ; profile_sb_legalization @0xbc1440)
```

### Algorithm

```c
function SBSizeLegalization_run(Module M):              // 0xbc9fc0
    arch = *(int*)(M + 0xac)                            // bc9fd1 — ArchLevel enum
    model = getArchModel(ArchLevel2string(arch))        // SBModel* — geometry singleton

    // ---- ARCH GATE (bca01f) ----
    if arch == 0xa:                                      // 10 = gen1 Inferentia: special path
        goto full_path                                   // bca290 → bca038
    else if arch <= 0x13:                                // <= 19: pass is a NO-OP
        M[0x28] = 1                                       // bca038-style flag store
        emit_log_record()                                // LOG only
        return
    // else arch > 19: full legalization

  full_path:                                             // bca038
    for bb in M.basicBlocks():
        flatten(bb, &counter)                            // §flatten — flat index per inst
    shrink_tensor(M)                                     // §shrink — try to make tensors fit

    if !pick_unroll_loop():                              // §pick — no still-illegal tensor
        set_splitShape(M)                                // §mark — one-shot
        profile_sb_legalization(M)
        return
    // some tensor still illegal → force-unroll fixpoint
    loop:                                                // bca180
        if force_unroll_update(M) != 0:                  // §force — bump unroll factor
            goto loop                                    // keep forcing more unroll
    set_splitShape(M)                                    // bca0f0 — re-mark with new geometry
    profile_sb_legalization(M)
    return
```

The `cmp $0xa` / `cmp $0x13` gate is verbatim at `bca01f`/`bca02e`; the `ArchLevel` is read from `Module+0xac` at `bc9fd1`. The shape is therefore *flatten → shrink → (split-mark) | (force-unroll\* → split-mark) → profile*.

> **NOTE —** for `ArchLevel ≤ 19` (older parts) the pass is a deliberate no-op: it sets a flag at `Module+0x28`, emits a single log record, and returns without touching any tensor. The split/shrink/force-unroll machinery only runs for `arch > 19` (plus the `arch == 10` special-case that falls through to the full path). The exact `ArchLevel → codename` binding is not asserted in this body; only the gate constants 10 and 19 are observed (SPECULATIVE binding).

### Considerations

The fixpoint has two distinct exits. The cheap one — `pick_unroll_loop()` returns false — means shrink alone legalized every tensor, so a single `set_splitShape` marks whatever dims still need cutting and the pass is done. The expensive one means at least one tensor is *still* too big after shrinking; `force_unroll_update` escalates an unroll factor (delegating the actual loop cloning to `FullUnroll`), and the loop re-runs until no further forcing is needed, after which `set_splitShape` re-marks against the new, more-unrolled geometry. The two paths converge on the same terminal pair (`set_splitShape; profile_sb_legalization`).

---

## The legality predicate

### Purpose

`is_legal_tensor` is the SBUF-fit test the whole pass pivots on: given a storage object and an element size (or split count), does its footprint fit the 128-partition × *N*-byte budget? It is the same two limits the coloring allocator later enforces, applied here so offenders can be rewritten before they ever reach the graph.

### Algorithm

```c
function is_legal_tensor(StorageBase* sb, uint arg2) -> bool:   // 0xbc1200
    kind = *(int*)(sb + 0xd8)                           // bc1200 — MemoryType discriminator

    if kind == 0x20:                                     // MemoryLocationSet (bc1209)
        // arg2 reused as requested split-piece count
        return SBModel[0x9c] >= arg2                     // bc1260 cmp; setae — partCount >= N

    if kind == 0x10:                                     // MemoryLocation (bc120e)
        dimType = *(int*)(sb + 0x110)                    // bc1213 — dim-type field
        if dimType == 1:                                 // PARTITION dim (bc1219)
            // walk the AP list at sb+0x180..+0x188
            maxBytes = max over APs of *(uint*)(AP - 0x10)   // bc1277 lea 0x180
            return SBModel[0x98] >= maxBytes             // bc12b4 cmp; setae — bytes/part fit
        if dimType == 4:                                 // FREE dim (bc121e)
            x  = *(u64*)(sb + 0x108)                      // bc1227
            x8 = (x & 7) ? (x + 8 - (x & 7)) : x          // bc1236 and 0x7 — round UP to mult of 8
            return (x8 * arg2) <= SBModel[0x98]          // bc1243 cmp; setbe — free bytes fit
        return 1                                          // other dim types: conservatively legal

    assert(false && "is_legal_tensor only checks MemoryLocation and MemoryLocationSet")
```

Every offset is confirmed in the disassembly: the discriminator load `mov 0xd8(%rsi),%rax` at `bc1200`, the `cmp $0x20`/`cmp $0x10` dispatch, the dim-type load `mov 0x110(%rsi),%eax`, the free-dim byte load `mov 0x108(%rsi),%rax` with `and $0x7`, the partition AP list `lea 0x180(%rsi),%r8`, and the two SBUF compares `cmp 0x98(%rdi),%eax; setbe` and `cmp %edx,0x9c(%rdi); setae`. The `__PRETTY_FUNCTION__` string in `.rodata` names the function `bool neuronxcc::backend::SBSizeLegalization::is_legal_tensor(storage_base_p, unsigned int)` and asserts the source file `sb_size_legalization.cpp`.

> **QUIRK —** the second argument is overloaded. On the `MemoryLocationSet` branch it is the *requested number of split pieces* (`partCount >= N`); on the free-dim `MemoryLocation` branch it is the *element size* in bytes (`roundup8(count) * elemSize <= bytesPerPartition`). A reimplementer that treats `arg2` uniformly will mis-legalize one of the two branches. The predicate is also re-entered from `shrink_tensor` via the secondary entry at `0xbc12e0` (the MLSet branch), skipping the kind-dispatch.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `is_legal_tensor` | `0xbc1200` | SBUF-fit predicate; dispatch on `sb+0xd8` | CERTAIN |
| `is_legal_tensor` (MLSet entry) | `0xbc12e0` | `+0xe0` secondary entry; `shrink_tensor` calls this directly | CONFIRMED |
| `getArchModel` | — | resolves codename → `SBModel*` (geometry singleton, [1.05](../arch/sbuf-psum-geometry.md)) | CERTAIN |

---

## flatten — the flat instruction view

### Purpose

`flatten` runs once per basic block before legalization to build a flat, stably-indexed view of the instruction stream, so the split-dim and force-unroll logic can reason about loop axes uniformly across nested loops.

### Algorithm

```c
function flatten(BasicBlock& bb, ulong& counter):       // 0xbc1ce0
    for inst in BasicBlockHolder::blocks(bb):
        if *(int*)(inst + 0x58) == 0x69:                 // bc1dc8 — opcode 105 = InstLoop
            flatten(loop_body, counter)                  // RECURSE into nested loop body
        denseMap[inst] = counter                          // DenseMap<Instruction*, ulong>
        idxMap[counter] = inst                            // unordered_map<ulong, Instruction*>
        counter += 1                                      // threaded across recursive calls
```

The opcode test `cmp [rbx+0x58],0x69` at `bc1dc8` is the same `InstLoop` opcode (105 = `'i'`) the unroller keys on ([8.3](translate-nki-unroll.md)). The `&counter` reference threads a running index across the self-recursive calls so every instruction gets a unique, deterministic position regardless of loop nesting.

---

## shrink_tensor — the footprint shrink

### Purpose

`shrink_tensor` is this pass's *own* footprint-reduction step — the set-level analogue of `ShrinkDN`, but owned by `sb_size_legalization.cpp`, not `shrink_dn.cpp`. It runs before the split/force-unroll fixpoint and tries to make an oversized tensor fit purely by reducing how many partition-blocks it keeps live at once.

### Algorithm

```c
function shrink_tensor(Module& M):                      // 0xbc36d0
    shrink_factor = ... // [rsp+0x8] divisor, logged " shrink factor "
    for each tensor sb in M:
        nb  = getNBlocks(sb)                              // ×3 call sites
        dim = chosen shrink dim                           // from getPartitionDim / getIsBlock scan
        liveN = nb / shrink_factor                        // bc6258/bc62c3 — integer divide
        mem_loc_info[sb].liveN = liveN                    // DenseMap operator[] (bca740); +0x10
        MemoryLocationSet::setLiveN(sb, liveN)            // bc62f6 — record live span
        MemoryLocationSet::setShrinkDim(sb, dim)          // bc37cb/bc4879/bc4cfc — record dim
        if !is_legal_tensor(sb, ...):                     // bc5ab0/bc5f74 — did shrink suffice?
            // still illegal: this tensor will need split / force-unroll downstream
```

`setLiveN(unsigned int)` and `setShrinkDim(int)` are confirmed undefined-symbol imports the body calls; `getShrinkDim` is read back downstream (by the force-unroll path) so the shrink decision is a cross-phase record, not a local. The per-tensor side table is `DenseMap<StorageBase*, SBSizeLegalization::mem_loc_info>`, whose `mem_loc_info` carries the shrunk `liveN` at `+0x10` (the `operator[]`, `grow`, and `LookupBucketFor` instantiations are all emitted in the binary at `0xbca480`–`0xbca740`).

> **CORRECTION (D-K15) —** `update_liveN` @ `0xbc6510` *looks* like the shrink's liveN writer but is **not** part of this pass's driver. Its sole caller is `LoopOptimization::run`; it is a shared helper reused by loop-opt. The SBSize liveN write is `setLiveN` *inside* `shrink_tensor`. Do not attribute `update_liveN` to `SBSizeLegalization::run`.

> **NOTE —** the diagnostic string `"tenosrizer split "` (sic — the misspelling is in the binary) is a `shrink_tensor` *log message*, not a method name. The other `.rodata` diagnostics on this path — `"tensor shape: "`, `"partition_dim "`, `"dimension_idx "`, `" alive ones "`, `" shrink factor "` — are all log strings, not symbols.

---

## set_splitShape — mark, do not clone

### Purpose

`set_splitShape` decides *which dimensions* of a still-oversized tensor must be split, encodes that as a boolean mask, and stores it on the `MemoryLocationSet`. It is the pass's central output — and the point at which the architecture diverges sharply from the obvious design.

### Algorithm

```c
function set_splitShape(Module& M):                     // 0xbc6760
    for each MemoryLocationSet mls in M:                 // filter_iterator over StorageBase list
        pd = getPartitionDim(mls)                        // bc6973 — only dims >= pd are candidates
        n  = getTensorShape(mls)->ndims                  // [shape+8]

        splittable = bool[n] initialized false
        for dim in [0 .. pd):                            // candidate dims
            for AP in mls.writers<AccessPattern>():      // bc698d, then readers (bc6a86)
                if AP.vkind() == 2:                      // SymbolicAccessPattern — handled specially
                    axes = cast<SymbolicAccessPattern>(AP).loopAxes()   // boost vector copy
                    for axis in axes:
                        if axis == &BirLoopAxis_sentinel: continue        // @0x3dc6a30
                        matmul_flag = *(u8*)(axis.tile_info + 0x158)      // bc71b8
                        if matmul_flag:
                            // matmul tile-aligned path: consult validTileSizes (§tile)
                        else:
                            axis.flag[0x58] = 1           // bc71a3 — mark dim splittable
                            splittable[dim] = true
                            log(tensorName, " access ", dim)  // sev 0xa

        // BUILD MASK (bc7b57 .. bc7be7)
        mask = SmallVector<bool,4>(n)
        for i in [pd .. n): mask[i] = 0                  // tail zeroed — dims below pd never split
        for i in [0 .. pd):  mask[i] = splittable[i]     // marked dims true
        MemoryLocationSet::setSplitShape(mls, mask)      // bc7be2 — store the mask
```

> **QUIRK —** this is the corrective finding of the whole page. `set_splitShape` **does not clone the tensor**. It computes a per-dimension `SmallVector<bool,4>` "which dims to split" and stores it via `MemoryLocationSet::setSplitShape(SmallVector<bool,4> const&)` (confirmed import). The split *factor* is not a scalar here — it is encoded purely as which dims are marked. There is no `split_node` method on this class; `"split_node"` is a string in the binary, not a symbol.

### The delegated clone

The mask is consumed by the unroller, not by this pass. `FullUnroll::tensorSplit(bir::MemoryLocationSet&, bir::Function&)` @ `0xb70000` reads `getSplitShape` and performs the real split: allocate sub-tensors, divide each marked dim, repoint the access patterns onto the new storage. This is confirmed three ways:

- `nm -DC` resolves `FullUnroll::tensorSplit(MemoryLocationSet&, Function&)` to exactly `0xb70000` — the 2-argument overload, distinct from `Unroll::tensorSplit(MemoryLocationSet&, MemoryLocationSet&, Function&, int)` (the 4-argument production cloner at `0xb39e00`, [8.3](translate-nki-unroll.md)).
- `FullUnroll::tensorSplit` itself *calls* `MemoryLocationSet::getSplitShape()` at `0xb70421` and `0xb705c5` — the mask written by this pass is read at the clone site, pinning the data flow at the call, not merely by xref. `getSplitShape`'s other consumer is `profile_sb_legalization` (the stats reader).
- The pass's own `set_splitShape` writes `setSplitShape` and never allocates a `MemoryLocation`.

The split granularity FullUnroll applies to a marked dim is bounded by `getLargestFactorWithThreshold` (`MaxFactor = 16`, [8.3](translate-nki-unroll.md)) — so even a heavily-marked tensor is cut into at most that many pieces per pass.

> **GOTCHA —** a reimplementation that has `SBSizeLegalization` clone tensors in place will double-split: the mask plus the unroller's read of `getSplitShape` would each cut the dim. The split must live in exactly one place. In this build it is the unroller's `tensorSplit`; this pass only marks. (CONFIRMED: both symbols, the `0xb70000` address, and the `getSplitShape` read at `0xb70421`/`0xb705c5` inside `tensorSplit`.)

---

## The force-unroll fallback

When shrink + split still cannot legalize a tensor, the pass escalates: it forces extra loop unrolling so the offending dim is materialized into smaller per-iteration pieces, then re-marks.

### pick_unroll_loop — is anything still illegal?

```c
function pick_unroll_loop() -> bool:                    // 0xbc2360
    for each tensor sb in M:
        if is_legal_tensor(sb): continue
        for inst in producers(sb) + consumers(sb):       // Instruction::getLoopnest() ×2
            for AP in inst.accessPatterns():
                axes = cast<SymbolicAccessPattern>(AP).loopAxes()   // vector<LoopAxis*>
                flatten(...)                              // re-flatten (10 inlined sites, 0x5fbc40)
        if a candidate loop is selectable:
            return true                                   // diagnostic "(unroll) " @0x1c770ac
    return false
```

`pick_unroll_loop` walks every *still-illegal* tensor, collects the loop axes feeding it, and decides whether some loop can be force-unrolled to relieve it. It returns `true` iff a candidate loop was found — which is exactly the branch in `run` that enters the fixpoint.

### force_unroll_update — bump and re-bookkeep

```c
function force_unroll_update(Module& M) -> int:         // 0xbc7e40
    for each illegal tensor whose loop was picked:
        from = getShrinkDim(...) / current unroll factor    // ×2 reads
        to   = bumped factor
        setShrinkDim(...)                                 // re-record after the bump
        // refresh per-MLSet block geometry: getNBlocks ×2, getPartitionDim, getTensorShape
        log "force loop round <n>"                        // @0x1c770d3
        log tensorName, " forced to unroll more from ", from, " to ", to   // @0x1c770b6
    return needs_another_round ? nonzero : 0
```

The return value drives the `run` fixpoint: nonzero means "another forced round is needed" (loop back to `bca180`); zero means "stop forcing, re-run `set_splitShape`" (`bca0f0`). The actual loop cloning is again delegated to the `FullUnroll` machinery ([8.3](translate-nki-unroll.md)) — `force_unroll_update` only bumps the unroll factor and fixes the per-`MemoryLocationSet` shrink/block bookkeeping so the *next* `set_splitShape` sees the new, more-unrolled geometry. (STRONG: fixpoint semantics; CONFIRMED: the diagnostic strings `"force loop round "` and `"forced to unroll more from"` both present in the binary.)

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `pick_unroll_loop` | `0xbc2360` | Decide if a still-illegal tensor needs forced unroll | CONFIRMED |
| `force_unroll_update` | `0xbc7e40` | Bump unroll factor; refresh MLSet bookkeeping; drive fixpoint | CONFIRMED |
| `flatten` | `0xbc1ce0` | Flat indexed inst view; re-run inside `pick_unroll_loop` | CONFIRMED |

---

## validTileSizes — the shared tile-shape table

### Purpose

When `set_splitShape` hits a dimension that a matmul touches, it cannot cut to an arbitrary size — the PE array only accepts certain (row, col) tile shapes at certain base partitions. `validTileSizes` is the static table of legal tile sizes per base partition, and `isValidTileSize` is the membership test. Critically this util is **shared** with the coloring allocator (the K-strand), so the legalizer and the allocator agree on which matmul tile positions are placeable.

### Algorithm

```c
function isValidTileSize(uint base_partition, uint tile_size) -> bool:   // 0x108e0c0
    // validTileSizes : std::map<int, std::set<int>> @ 0x3e01ac0 (.bss)
    // red-black tree: node+0x10 = left, node+0x18 = right, node+0x20 = key
    node = validTileSizes.find(base_partition)           // 108e0f0 — tree walk
    if node == end: return false
    return node->second.count(tile_size) != 0            // lower_bound + key compare
```

The tree-walk offsets `+0x10`/`+0x18`/`+0x20` are confirmed in the body at `108e0f0`–`108e10d`. The table contents are decoded from the static constructor near `0x7d6d00` (the immediates `0x80`=128, `0x20`=32, `0x40`=64, `0x60`=96 are all present in that region):

| Base partition | Legal tile sizes | Source |
|---|---|---|
| `0` | `{32, 64, 128}` | ctor `0x7d6d00..` (immediates `0x20/0x40/0x80`) |
| `32` | `{32}` | ctor immediate `0x20` |
| `64` | `{32, 64}` | ctor immediates `0x20/0x40` |
| `96` | `{32}` | ctor immediate `0x20` |

These are the legal PE-array tile sizes per quadrant base partition of the 128-row array.

### The matmul tile-position predicate

`getLegalTilePosition(Instruction&)` @ `0x1094330` is the predicate the matmul path ultimately reaches. It gates on opcode `0x8`/`0x5f` (the matmul family), reads the input-1 and output `PhysicalAccessPattern` (AP role tag at `AP+0x18`: 1 = input, 2 = output), takes each operand's `getBasePartitionInUnderlyingMemory()`, and asserts:

```text
(int)in1_base_partition == tile_pos.first      // .rodata assertion
(int)out_base_partition == tile_pos.second     // .rodata assertion
tile_pos.first == -1 && tile_pos.second == -1  // fallback "legal" (no constraint)
err: "Cannot get legal tile position for non-Matmult instruction"
```

`getLegalTileSize(Instruction&)` @ `0x1094e70` calls `isValidTileSize` on the input row-tile and output col-tile. The error string is confirmed in the binary. `isValidTileSize` is invoked by a broad set of call sites — `checkMatmultInputs`, `getLegalTilePosition`, `getLegalTileSize`, `getMMPhyPartition`, the core-V3 transpose/matmul helpers — including paths in the coloring allocator, which is why the two passes share one definition of placeable tiles.

> **NOTE —** `SBSizeLegalization` reaches this predicate *indirectly*, through the matmul-axis flag at `set_splitShape` (`bc71b8`, the `*(u8*)(axis+0x158)` test) — not by a direct call in its own body. (STRONG: the matmul-axis flag path; CONFIRMED: the predicate is the shared util and the table contents.)

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `isValidTileSize` | `0x108e0c0` | `validTileSizes` map membership test | CONFIRMED |
| `getLegalTilePosition` | `0x1094330` | matmul base-partition vs `tile_pos` assertion | CONFIRMED |
| `getLegalTileSize` | `0x1094e70` | `isValidTileSize` on row/col tiles | CONFIRMED |
| `getMMPhyPartition` | `0x10921d0` | matmul physical base partition (opcode `0x5f`) | CONFIRMED |
| `validTileSizes` | `0x3e01ac0` | static `map<int,set<int>>` (.bss) | CONFIRMED |

---

## profile_sb_legalization — the statistics report

### Purpose

`profile_sb_legalization` is the terminal phase on both `run` exits. It walks the module, reads each `MemoryLocationSet`'s split mask and shape, totals the split bytes and illegal-location count, and emits the `"SB Legalization Statistic:"` report block.

### Algorithm

```c
function profile_sb_legalization(Module& M):            // 0xbc1440
    total_split_bytes = 0; n_split = 0; n_illegal = 0
    for inst in get_inst_in_order(M):
        mask  = getSplitShape(mls)                       // bc1a7d
        shape = getTensorShape(mls)                      // bc1a88
        if any(mask):
            bytes = product of shape dims                // imul over dims
            total_split_bytes += bytes
            n_split += 1
        if !is_legal_tensor(mls): n_illegal += 1
    emit("SB Legalization Statistic: ...")
```

The emitted report (all literals confirmed in `.rodata`): `"SB Legalization Statistic: "`, `"   Total trip count unrolled: "`, `"   Number of memory location split: "`, `"   Number of sub memory location created due to unroll: "`, `"   Total size of memory location split (in bytes): "`, `" Total number of illegal memory locations : "` (the percentage uses a `mulss` against a const float).

The pass also registers three `MetricStore` `BackendMetric` counters by name — `backend::CanSplitSbNodesCount` (SB nodes that *could* be split), `backend::ShrunkNodesCount` (nodes shrunk), `backend::TotalSplitSbNodesCount` (SB nodes actually split). All three name strings are present in the binary.

> **NOTE —** no counter-increment site was found inside the `0xbc1200..0xbca2c0` bodies. The names are registry entries consumed by the generic stats reporter; the increments live in the shared `FullUnroll`/`MetricStore` reporting path (STRONG, not pinned at the increment site). The report *strings* are CONFIRMED; the increment *site* is not in this pass's bodies.

---

## Method Roster

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `run` | `0xbc9fc0` | `0x300` | driver; arch gate; fixpoint | CONFIRMED |
| `is_legal_tensor` | `0xbc1200` | `0x240` | SBUF-fit predicate | CONFIRMED |
| `flatten` | `0xbc1ce0` | `0x680` | flat indexed inst view | CONFIRMED |
| `pick_unroll_loop` | `0xbc2360` | `0x1370` | still-illegal → force-unroll? | CONFIRMED |
| `shrink_tensor` | `0xbc36d0` | `0x2e40` | footprint shrink (`setLiveN`/`setShrinkDim`) | CONFIRMED |
| `set_splitShape` | `0xbc6760` | `0x16e0` | per-dim split mask | CONFIRMED |
| `force_unroll_update` | `0xbc7e40` | `0x2180` | escalate unroll factor; fixpoint | CONFIRMED |
| `profile_sb_legalization` | `0xbc1440` | `0x8a0` | statistics report | CONFIRMED |
| `ctor` / `dtor` | `0xbca840` / `0xbca2e0` | — | `SBSizeLegalization(PassOptions const&)` | CONFIRMED |
| `update_liveN` | `0xbc6510` | `0x250` | **not** this pass — `LoopOptimization` helper | CORRECTION |

Addresses and sizes are from `nm -DC libwalrus.so` (body frame); sizes by bisection. The `mem_loc_info` DenseMap helpers (`operator[]` `0xbca740`, `grow` `0xbca560`, `LookupBucketFor` `0xbca480`) are emitted inline.

---

## Related Components

| Name | Relationship |
|---|---|
| `FullUnroll::tensorSplit` @ `0xb70000` | **performs** the clone this pass only marks (reads `getSplitShape`); the delegation headline |
| `FullUnroll` / `Unroll` | the unroll machinery `force_unroll_update` escalates into; `getLargestFactorWithThreshold` (MaxFactor 16) caps split granularity |
| Coloring allocator (K-strand) | the consumer this pass prepares for; a too-big tensor can never be colored, so it is legalized first; shares `validTileSizes` |
| `vn_splitter` / `ShrinkDN` | distinct passes: `vn_splitter` mints `_VN_<k>` sub-tensors via `addMemoryLocation`; this pass marks dims and delegates. Same two SBUF limits, different mechanism |
| `memloc_split` @ `0xab9610` | live-range web splitting *inside* the allocator — unrelated goal; shares only the `MemoryLocationSet` type and the tile-position util |

---

## Cross-References

- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the `SBModel+0x98`/`+0x9c` partition×byte budget `is_legal_tensor` compares against; the `getArchModel` singleton tree.
- [translate-nki-ast-to-bir & Loop Unroll Passes](translate-nki-unroll.md) — `FullUnroll::tensorSplit` (the delegated clone), the `InstLoop` opcode 105, the `MaxFactor = 16` split cap.
- [SBUF Liveness & Interference](sbuf-liveness-interference.md) — the coloring allocator this pass prepares for; the `"is too big for SB"` fatal that a too-big tensor hits if it is not legalized first.
