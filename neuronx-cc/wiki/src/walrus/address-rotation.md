# Address Rotation and the Emergent Software Pipeline

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22; the `AddressRotation` pass lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text`/`.rodata` the virtual address equals the file offset; `.data` carries a fixed `+0x400000` delta and is called out where it matters. Body addresses and sizes are read from the binary's native export table; the `0x5xxxxx`/`0x6xxxxx` twins of any symbol are `.plt` thunks — every address here is the real `.text` body. Treat every address as version-pinned.*

## Abstract

The Neuron backend builds a software pipeline for ordinary, un-annotated tiled loops without ever asking for one. The mechanism is `AddressRotation` — a `DMAOptimizationBase` pass that, for each on-chip memory space, takes the one physical buffer the colorer assigned to a recurring tensor and **rotates** it across a small ring of distinct physical buffers, so that successive loop iterations of the same logical tensor land on different banks/tiles/regions. Double- and triple-buffering are never requested explicitly; they **emerge** as a side effect of that rotation, and the list scheduler downstream is then free to overlap `load(i+1)` with `compute(i)` because the two no longer alias. This is the *emergent* pipeline, the counterpart to the *declarative* one driven by `num_stages` annotations in the Penguin middle end (cross-ref [5.14 — Penguin Software Pipelining](../penguin/software-pipelining.md)); a given loop takes exactly one of the two paths.

If you know modulo scheduling, the analogy is exact in spirit but inverted in mechanism. Classical software pipelining computes an initiation interval and then *allocates rotating registers* to break the resulting loop-carried anti-dependences. Here the backend never computes an II. It allocates the physical ring first (`AddressRotation`), drops the stale dependence edges (`clearInstDeps`), and lets the ordinary list scheduler discover the overlap — the rotated addresses guarantee the overlap is hazard-free. The "rotating register file" is a ring of PSUM banks, SBUF tiles, or DRAM byte-regions, and the modulus is chosen by a **different rule per memory space**.

`AddressRotation` is registered **four** times, not three: `address_rotation_psum`, `address_rotation_sb`, `address_rotation_dram` run immediately after their space's `coloring_allocator`, and a fifth/sixth invocation — `address_rotation_psum_post_schedule` plus a re-run of `address_rotation_sb` — runs *after* the load/compute split (`separate_load_and_compute`). That two-phase structure is the multi-buffering machinery: the pre-split passes give a recurring tensor a ring; the post-split passes rotate the freshly-replicated stage buffers so producer iteration *i* and consumer iteration *i−1* resolve to different physical slots.

For reimplementation, the contract is:

- **The ring-window modulo addressing.** A rotated tensor's address is `base + (iter·stride) mod (N·slotSize)` — the same `(numer mod denom)` index `[pelican::ModuloExpr](../bir/pelican-moduleexpr.md)` produces, but this pass only chooses the **physical slot** the ring maps onto; it does not emit the `ModuloExpr` node (see the correction in [§6](#the-post-schedule-variant--multi-buffering-emerges)).
- **The per-space period-selection rule.** PSUM rotates over the architectural **bank count** `N`; SBUF rotates over a per-engine-class **period** (`1/5/10/20/60`); DRAM rotates inside a **CLI-capped GiB window** (`dram_rotation_size ≤ 8`). Three different moduli, three different free-slot allocators.
- **The dispatch and fixpoint structure.** Which path fires keyed on `Function` attribute flags 9/10/11/12; PSUM rotation runs as a **3-pass fixpoint**; SBUF rotation is partition-band-batched under TBB.

| | |
|---|---|
| **Class** | `neuronxcc::backend::AddressRotation : DMAOptimizationBase` |
| **Vtable** | `_ZTVN9neuronxcc7backend15AddressRotationE` @ `0x3d8d2d8` (64 B = 8 vfns) |
| **Object size** | `0x8C8` (2248 B); ctor `0x91af20` (88 B), two trailing `bool`s at `+0x8B8`/`+0x8B9` |
| **Pass entry** | `run` @ `0x91d980` (1783 B) → `rotateAddrs` @ `0x940e00` (5976 B) |
| **PSUM core** | `psum_rotation` @ `0x938050` (19085 B); `get_psum_partition` @ `0x918600` (61 B) |
| **SBUF core** | `sb_rotation` @ `0x91e080` (4122 B); `sb_rotation_batch` @ `0x92a1a0` (26530 B) |
| **DRAM core** | `dram_rotation` @ `0x93cae0` (16348 B) |
| **Dep reset** | `clearInstDeps` @ `0x940ac0` (646 B) |
| **Registered names** | `address_rotation_{psum,sb,dram}` + `address_rotation_psum_post_schedule` |
| **Companion** | `[pelican::ModuloExpr](../bir/pelican-moduleexpr.md)` (the symbolic ring index) |

---

## The pass family and where it runs

### Purpose

`AddressRotation` is one pass class instantiated under four registered names, distinguished by two construction `bool`s and by which `Function` attribute path `rotateAddrs` takes. The four names and their template signatures are firsthand from the export table: the `addModParallelPass<AddressRotation, char[20], bool>` (1-bool, mangled `RA20_Kcb`) template registers `address_rotation_psum`, `_sb`, `_dram`; the `addModParallelPass<AddressRotation, char[36], bool, bool>` (2-bool, mangled `RA36_Kcbb`) template registers only `address_rotation_psum_post_schedule`. The two-`bool` constructor signature `AddressRotation(PassOptions const&, std::string const&, bool, bool)` is confirmed in the ctor symbol `…AddressRotationC1ERK11PassOptionsRKNSt7__cxx1112basic_stringIcSt…EEbb`.

The two ctor booleans live at object byte offsets `+0x8B8`/`+0x8B9` and select the rotation variant: `b1` (`+0x8B8`) gates the SB-rotation scope, `b2` (`+0x8B9`) gates PSUM rotation inside the kernel-scope path. The object is `0x8C8` bytes — `tc_new(0x8C8)` is the allocation, and the boolean offsets are the last two bytes before the size boundary.

### Pipeline placement

The pass-pipeline builder (`sub_80A6E0` @ `0x80a6e0`) threads each per-space rotation immediately after that space's `coloring_allocator`, and re-runs the PSUM/SB pair after the pipelining split. The shape is what makes multi-buffering emerge — the *second* rotation has stage-replicated buffers to spread.

```text
  coloring_allocator_psum → dma_optimization_psum → address_rotation_psum         (order 46)
       → memory_analysis_after_address_rotation_psum
  coloring_allocator_sb   → dma_optimization_sb   → address_rotation_sb           (order 56)
       → memory_analysis_after_address_rotation_sb
  coloring_allocator_dram                         → address_rotation_dram         (order 69)
       → memory_analysis_after_address_rotation_dram
  ───────────────────────────────────────────────────────────────────────────────
  separate_load_and_compute[_with_memset]                ← the pipelining split
       → order_column_tiled_mms → post_sched → legalize_mm_accumulation_groups
       → address_rotation_psum_post_schedule   (2-bool variant)
       → address_rotation_sb  (re-run)         (rotate the new stage buffers)
```

> **QUIRK — the rotation runs twice for a reason.** The first `address_rotation_{psum,sb}` (orders 46/56) rotates a recurring tensor onto a small ring while it is still one logical loop body. `separate_load_and_compute` then *replicates* buffers per pipeline stage. The post-schedule `address_rotation_psum_post_schedule` + `address_rotation_sb` re-run is the half that rotates those freshly-created stage buffers, so producer iteration *i* and consumer iteration *i−1* end up on physically distinct slots. A reimplementation that rotates only once, before the split, will pipeline the loop but leave the two stages aliasing — a WAR/WAW hazard the scheduler must then serialize, defeating the overlap.

### Function map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `run` | `0x91d980` | 1783 | pass entry: `skip_pass` guard, TBB task spawn → `rotateAddrs` | CERTAIN |
| `rotateAddrs` | `0x940e00` | 5976 | master dispatch on attr 9/10/11/12; SB budget; PSUM ×3 | CERTAIN |
| `psum_rotation_prepare` | `0x921c70` | 880 | `opt_prepare` — per-function memloc tables | CERTAIN |
| `psum_rotation` | `0x938050` | 19085 | bank-ring rotation core | CERTAIN |
| `get_psum_partition` | `0x918600` | 61 | partition → 32-wide band span | CERTAIN |
| `update_psum_partition` | `0x922400` | 1552 | `live[band][bank] += delta` occupancy grid | CERTAIN |
| `sb_rotation_prepare` | `0x921fe0` | 41 | thin `opt_prepare` wrapper | CERTAIN |
| `sb_rotation` | `0x91e080` | 4122 | per-target SB schedule + TBB band-batch fan-out | CERTAIN |
| `sb_rotation_batch` | `0x92a1a0` | 26530 | per-band tile rotation core | CERTAIN |
| `is_memloc_in_batch` | `0x9187f0` | 201 | band-span ⊆ batch-range filter | CERTAIN |
| `check_no_in_place` | `0x928e20` | 88 | RMW/in-place guard | HIGH |
| `dram_rotation` | `0x93cae0` | 16348 | GiB-window byte-region rotation | CERTAIN |
| `clearInstDeps` | `0x940ac0` | 646 | attr-12-gated dependence-edge reset | CERTAIN |
| ctor (`C1`/`C2`) | `0x91af20` | 88 | `(PassOptions, name, b1, b2)`; bools at `+0x8B8/9` | CERTAIN |

---

## Entry and master dispatch — `rotateAddrs`

### Entry Point

```text
run @0x91d980                            ── pass entry
  ├─ DMAOptimizationBase::skip_pass       ── "Skipping pass due to no instruction or uncovered structure."
  └─ tbb::task_group { rotateAddrs(Module&), sibling worker }
       └─ rotateAddrs @0x940e00           ── the algorithm
```

`run` opens a `module_name` log scope, calls `DMAOptimizationBase::skip_pass`, and on a non-skip spawns two TBB `function_task`s (vtables `off_3D74AC0`/`off_3D74AF0`) on a `task_group`, then waits. One task body is `rotateAddrs`; the other is a sibling worker. The algorithm proper is `rotateAddrs`.

### Algorithm

`rotateAddrs` caches the arch geometry, refuses if-else regions (rotation is only correct on straight-line / loop-body code), computes the SBUF rotation budget once, then dispatches on `Function` attribute flags. The arithmetic and the three-way attribute dispatch are read directly from the decompiled body.

```c
void rotateAddrs(this, bir::Module& M) {                 // 0x940e00
    // arch geometry cached into the object (ArchModel walk)
    this->numPartitions = ArchModel.partitionCount;      // dword @ obj+382 (dword frame)
    this->numBanks      = ArchModel.psumBankCount;        // @ obj+385

    if (!bir::isArchSupported())                          // line 160
        NeuronAssertion("isArchSupported(arch)");         // line 183/213

    buildCFG(M);
    if (cfg has a non-loop block with >1 predecessor) {   // an if/else join
        log("Detected If else structure, skip DMA optimization for now");  // line 304
        return;                                           // rotation only on straight-line / loop bodies
    }

    // SB rotation budget — the per-target SBUF slice, computed ONCE.
    // 8 * roundup8( 2*numPartitions / 5 )   ==   8 * ceil_div8( 2N/5 )
    v22 = (2*numPartitions / 5) != 0;                     // line 235
    this->sbBudget = 8 * (v22 + (((2*numPartitions/5) - v22) >> 3));  // line 236, store @ obj+0x8C0 (=_QWORD*+280)

    fn = M.entryFunction;                                 // M+8, biased -160
    if (Function::hasAttribute(fn, 12)) {                 // line 318 — KERNEL-SCOPE / post-schedule
        if (this->b2 /*+0x8B9*/ && !has_uncovered_live_range()) {
            for (f : M.functions) {
                psum_rotation_prepare(f);
                psum_rotation(f); psum_rotation(f); psum_rotation(f);   // lines 333-336 — ×3 fixpoint
            }
        } else {
            for (f : M.functions) sb_rotation_prepare(f);
            // the full SB target schedule (see §4), gated by this->b1 /*+0x8B8*/
            sb_rotation(1,  5,  roundup8(sbBudget>>1), sbBudget, 1, 0);  // line 353
            sb_rotation(2, 20,  obj[560],             numPartitions, 0, 0);
            sb_rotation(5, 10,  roundup8(sbBudget>>1), sbBudget, 1, 0);
            sb_rotation(7, 20,  roundup8(sbBudget>>1), sbBudget, 1, 0);
            sb_rotation(3, 10,  0, roundup8(sbBudget>>1), 1, 0);
            sb_rotation(4,  1,  0, roundup8(numPartitions/5), 0, 5);     // engine 5
            sb_rotation(4,  1,  0, roundup8(numPartitions/5), 0, 1);     // engine 1
        }
    }
    if (Function::hasAttribute(fn, 11))                   // line 382 — DRAM-internal marker
        dram_rotation(M, /*target*/1, /*size_arg*/0x14);

    if (Function::hasAttribute(fn, 9) && !Function::hasAttribute(fn, 10)
        && !has_uncovered_live_range()) {                // line 405 — PSUM path
        psum_rotation_prepare(f);
        psum_rotation(f); psum_rotation(f); psum_rotation(f);           // lines 413-415 — ×3
    }
    if (Function::hasAttribute(fn, 9) || Function::hasAttribute(fn, 10)) { // line 419 — SB path
        sb_rotation_prepare(fn);
        // SB schedule: (1,5),(2,20),(5,10),(7,20),(3,10),(12,60)        lines 445-464
        // then a tail loop over InstNKIKernel insts (opcode 55, scope==1):
        for (k : kernels with scope==Kernel) {
            if (k.numPartitions==0 || k.sbSize==0) {
                log("Skipping kernel-scoped address rotation for <name> - SB shape not set");
                continue;
            }
            this->sbBudget = 8 * roundup8(2*k.numPartitions / 5);        // line 542 — re-derived per kernel
            sb_rotation_prepare(k.fn);
            // kernel-scoped SB schedule using k's own numPartitions/basePartition
        }
    }
}
```

> **NOTE — the SBUF budget idiom.** The decompiler renders `8 * roundup8(x)` as `8 * (v + ((x - v) >> 3))` with `v = (x != 0)`. That `v + ((x−v)>>3)` is integer `ceil(x/8)`; the outer `×8` re-scales it to a byte slice. So `sbBudget = 8·ceil((2N/5)/8)` — two-fifths of the partition count, rounded up to a multiple of 8, times 8. The `shr rax,3` at `rotateAddrs+235/236` is the divide-by-8 of the `roundup8`, and the same expression is re-derived per kernel at line 542. The store target is object byte offset **`0x8C0`** — a 64-bit `mov [r14+0x8C0], rax` @ `0x9411f7` (and `0x941348`), one qword below the ctor booleans.

> **GOTCHA —** the decompiler shows this store through a `_QWORD*` as index `+280`. Scale it by 8 (`280·8 = 0x8C0`, 2240); reading `+280` as a `_DWORD*` index instead gives `0x460` and lands the `sbBudget` field 1120 bytes short.

> **QUIRK — `psum_rotation` runs exactly three times, by hand.** There is no convergence test; `rotateAddrs` calls `psum_rotation(f)` literally three times in a row (lines 333-336 and 413-415). It is a fixed 3-sweep fixpoint: each sweep can free banks that the previous sweep's relocations made schedulable, and three is enough for the (typically ≤3-deep) pipelines this targets. A reimplementation that loops to convergence is *more* general but will not match the binary's buffer assignment.

---

## PSUM rotation — a ring of banks over a 32-partition band grid

### Purpose

PSUM is a 1-D bank-interval space: a small array of fixed 2048-byte banks (4 banks on Inferentia, 8 on gen2+; cross-ref [`arch/sbuf-psum-geometry.md`](../arch/sbuf-psum-geometry.md)), and a tensor occupies a whole number of banks. `psum_rotation` moves a recurring PSUM `MemoryLocation` onto a **different** bank so that successive pipeline iterations of the same logical tensor never collide. The ring modulus is the architectural bank count `N` (`obj+385`); the free-slot search runs against a 2-D occupancy grid indexed by `[partition-band][bank]`.

### The 32-partition band

`get_psum_partition` quantizes a memloc's partition window into bands of 32 — the PE-array partition-band a PSUM bank spans. This is exact, 61 bytes:

```c
// get_psum_partition @0x918600
void get_psum_partition(MemoryLocation* M, u64* start, u64* end) {
    *start = (u32)M.getBasePartition() >> 5;                       // band of the base partition
    *end   = (M.numPartitions /*M+128*/ + (u32)M.getBasePartition() - 1) >> 5;  // band of the last
}
```

`>> 5` is `/32`: partitions `0..31` → band 0, `32..63` → band 1, and so on. Both shift-by-5 computations and the `getBasePartition` calls are visible in the 61-byte body.

### The occupancy grid

`update_psum_partition` maintains the time-varying bank occupancy as a 2-D reference-count grid. Outputs increment (`delta = +1`, a write claims its banks for its live range); freed inputs decrement (`delta = −1`).

```c
// update_psum_partition @0x922400
void update_psum_partition(M, vector<vector<u32>>& live, int delta, set<u32> bankSet) {
    u64 start, end;
    get_psum_partition(M, &start, &end);
    assert(end < live.size() && "end < live.size()");             // line 80/146
    bankSet = M.getVectorizedPSUMBankIds();                       // line 155 — banks M occupies
    for (band = start; band <= end; band++)
        for (bank : bankSet)
            live[band][bank] += delta;                            // line 179 — the grid update
}
```

The grid is a `vector<vector<u32>>` — `live[band]` is a per-band row, `live[band][bank]` the occupancy count. Line 179 is literally `*(u32*)(live.data[band] + 4*bankId) += delta`, where the row stride is `24*band` (a 24-byte `vector` header per band) and the column stride is `4*bankId`.

### Algorithm

```c
void psum_rotation(this, Function& f, PSUM_ROTATE_TARGET target, int) {   // 0x938050
    // (a) Pre-scan: build the live[band][bank] grid.
    for (inst : f.instructions)
        for (op : inst.outputs ∪ inst.args)
            if (op is MemoryLocation && op.memoryType == 32 /*PSUM*/)      // (+216)==32
                update_psum_partition(op, live, op.isDef ? +1 : -1, op.bankIds);

    // (b) Rotation search.
    for (M : rotatable PSUM memlocs of target) {
        log("Attempting to find a rotation for <M>");                       // psum_rotation+1883
        [bandLo, bandHi] = band span M touches;
        width = divideCeil(size(M), psumBankSize);                          // banks M spans
        align = getPsumBankAlignment(M);                                    // = width (psum_rotation+2080)
        for (s = 0; s <= numBanks + 1 - width; s++) {
            if (s % align != 0) continue;                                   // bank-aligned start
            if (any band in [bandLo..bandHi] has live[band][s .. s+width] != 0) continue;  // not free
            if (prefer_bank_alignment(s, align)) { pick = s; break; }       // tie-break (+2087)
        }
        if (pick >= 0) {
            getBankId(M); getBasePartition(M); getAddress(M);
            MemoryLocation::allocate(M, /*new bank*/pick);                  // physically RE-BIND
            log("Rotating <M> from start bank … picked new range");        // +2446..
            update live grid: -old banks, +new banks;
        } else {
            log("Cannot Rotate PSUM Bank for <M> from start bank …");       // +2784 — left in place
        }
    }
    log("PSUM Rotation rotated <n> PSUM address for target <target>");      // +2997
}
```

`getPsumBankAlignment` (`0x1088ad0`) is `divideCeil(memlocSize, psumBankSize)` — the number of banks the memloc spans, which doubles as the start-bank alignment. `prefer_bank_alignment` (`0x9185c0`) is the tie-break: `align==2 → s even`, `align==4 → s%4==0`, `align==3 → s%3==0`. All the log strings and the `%`/`prefer_bank_alignment` call sites are present in the 19 KB body.

> **QUIRK — the ring is a priority queue, not a counter.** The choice of *which* free bank in the ring a memloc takes is not a literal circular `(iter mod N)` counter inside this pass. A `priority_queue<pair<MemoryLocation*, u32>, SortFn>` (`memQueue`, push `0x9721d0`) heap-orders the candidate buffers by `SortFn` and pops slots to assign. The modulo arithmetic lives in the *address* the consumer reads — the symbolic `(iter mod N)` index is the `ModuloExpr`, produced elsewhere (see [§6](#the-post-schedule-variant--multi-buffering-emerges)); this pass only decides which physical bank that ring index resolves to.

### Ring semantics

The modulus is the architectural bank count `N = obj+385`; rotation maps successive pipeline instances of a tensor onto bank `(start + k) mod N` within the band, i.e. `addr = bankBase + ((iter·width) mod (N·bankSize))`. That is exactly the `(numer mod denom)` shape of `[pelican::ModuloExpr](../bir/pelican-moduleexpr.md)` with `denom = N·bankSize`. The `ModuloExpr` page proves `getSupremumFast → denom−1`, i.e. the rotated address is confined to exactly `denom` slots — the ring of `N` banks.

---

## SBUF rotation — a ring of tiles, partition-band-batched

### Purpose

SBUF is a 2-D `(partition, byte-offset)` space. `sb_rotation` moves a recurring memloc to a new `(basePartition, byteAddress)` slot; the ring is the set of free SB tiles, tracked in a roaring bitmap and allocated by a `FreeMatrix`, and the period is the per-engine-class buffer count. Unlike PSUM's single fixed modulus, SBUF uses a **schedule** of `(target, period, …)` tuples — one per engine/tile class.

### The SB schedule

`rotateAddrs` calls `sb_rotation` with a fixed schedule; the per-target args derive from `sbBudget` (`obj+280`) and `numPartitions` (`obj+382`). The standard (attr 9||10) schedule, read firsthand:

| target | period | slice arg | size arg | rotate? | engine | line |
|---|---|---|---|---|---|---|
| 1 | 5 | `roundup8(sbBudget>>1)` | `sbBudget` | yes | 0 | 445 |
| 2 | 20 | `obj[560]` | `numPartitions` | no | 0 | 446 |
| 5 | 10 | `roundup8(sbBudget>>1)` | `sbBudget` | yes | 0 | 447 |
| 7 | 20 | `roundup8(sbBudget>>1)` | `sbBudget` | yes | 0 | 455 |
| 3 | 10 | 0 | `roundup8(sbBudget>>1)` | yes | 0 | 463 |
| 12 | 60 | 0 | `roundup8(numPartitions/5)` | yes | 0 | 464 |

Targets `1..12` are `SB_ROTATE_TARGET` — the engine/tile class to rotate (PE / Act / Pool / SP / GpSimd families). The **`period` ∈ {1,5,10,20,60} is the rotation modulus** — the number of buffers in that class's ring. The kernel-scope (attr-12) path uses a subset: targets `1,2,5,7,3` then `4` twice (engines 5 and 1), or just target `12` when `b1` is set. Every tuple is visible in `rotateAddrs` lines 353-373 / 445-464 with the exact period constants.

### Band-batched fan-out

`sb_rotation` collects the memlocs of a target, partitions them by partition-band, and runs `sb_rotation_batch` per band-chunk under a TBB `parallel_for` (grainsize 5, `start_for<blocked_range<size_t>>`, lambda `off_3D74A60`). The partitioning makes the batches independent — a memloc is rotated only by the batch whose band-range strictly contains it:

```c
// is_memloc_in_batch @0x9187f0
bool is_memloc_in_batch(MemoryLocation& M, u32 lo, u32 hi) {
    startBand = obj[98][M.id];   endBand = obj[82][M.id];     // per-id band maps from opt_prepare
    return !(startBand < lo || endBand > hi);                  // M's band span ⊆ [lo, hi]
}
```

### Algorithm

```c
void sb_rotation_batch(this, target, k, pair<u32,u32>& bandRange, u32 lo, u32 hi,
                       bool rotate, bir::EngineType eng) {     // 0x92a1a0
    roaring_bitmap& occupied = this->occupied /*obj+566*/;     // occupied SB tile ids (memloc+348)
    for (M : memlocs filtered by is_memloc_in_batch(M,lo,hi)
              && getEngineType(M)==eng && obj[34][M.id]==target) {
        // current footprint
        if (M.memoryType == 8 /*DRAM-spill*/) { size = M.numPartitions·width; }
        else { size = roundup8(width); partWindow = [M.basePartition, +numPartitions]; }

        for (other : batch-mates) {                            // double-loop @2298/2363
            if (other == M) continue;
            if (overlaps(M.[addr,addr+size]×partWindow, other.slot)) continue;
            if (!check_no_in_place(M, other)) continue;        // 0x928e20 — RMW/in-place guard
        }
        newAddr = FreeMatrix(FreeMatrixConfig::fromArch(arch))
                      .alloc(roundup8(slice));                 // free tile from the roaring ring
        if (newAddr) {
            getBasePartition(M); getAddress(M);
            MemoryLocation::allocate(M, newBasePartition, newAddr);  // re-bind
            log("<M> picked new range for <M> <addr> <basePartition>");
            roaring_bitmap_remove(occupied, oldTile);
            roaring_bitmap_add(occupied, newTile);
            batchCount++;
        } else {
            MemoryLocation::allocate(M, origAddr);             // re-bind to ORIGINAL — never lossy
            assert("success && failed to allocate back to original address range !");
        }
    }
}
```

> **GOTCHA — rotation is best-effort and must be reversible.** If `FreeMatrix.alloc` fails, the batch re-binds `M` to its *original* address and asserts the re-bind succeeded (`dma_optimization.cpp:0x1584`). A reimplementation that leaves a memloc unallocated on a failed rotation corrupts the layout. Rotation never drops a tensor; it either improves the slot or restores the original.

> **GOTCHA — `check_no_in_place` guards RMW.** `check_no_in_place(M, other)` (`0x928e20`) returns "safe to move" only if the two have different dtypes *or* a sub-check confirms there is no in-place `AccessPattern` alias. Relocating one side of a read-modify-write (e.g. a matmul accumulation group) would silently break it; this guard is why the matmul accumulation legalization runs adjacent in the pipeline.

### Ring semantics

The `period` arg is the modulus = the number of distinct SB tiles in the ring for that engine class; the `k`-th pipeline instance lands at `origBase + k·roundup8(slice)`, i.e. `addr = base + (iter mod period)·tileStride`. Again a `(numer mod denom)`-shaped index over the SB byte offset, with `denom = period` and the free-tile choice delegated to `FreeMatrix` + the roaring bitmap rather than a fixed counter.

---

## DRAM rotation — a ring of byte-regions in a capped GiB window

### Purpose

DRAM rotation lays recurring HBM buffers — chiefly collective-communication (CC) allreduce buffers and internal DRAM nodes that recur across iterations — at consecutive aligned offsets inside a bounded window, so iteration buffers do not pile up. It is the only space whose ring modulus is a **direct CLI knob** (`dram_rotation_size`); the other two derive their modulus from arch geometry.

### Window selection

```c
void dram_rotation(this, bir::Module& M, DRAM_ROTATE_TARGET target /*=1*/, u64 size_arg /*=0x14*/) {  // 0x93cae0
    log("Runtime page size at <p> MB");                        // line 539
    log("DRAM hwm before rotation <h>");                       // line 686

    v25 = options.dram_rotation_size;                          // a CLI dword knob
    assert(v25 <= 8 &&                                         // line 756
        "Setting rotation maximum memory size beyond 8G can cause runtime to complain");
    window = (u64)v25 << 30;                                   // line 761 — GiB → bytes; v25==-1 ⇒ prior hwm

    for (M : internal-DRAM / CC buffers) {
        if (M.memoryType != 8 /*DRAM*/) continue;
        if (((M.tensorKind /*(+224)*/ - 7) & 0xFFFFFFFD) != 0) continue;   // TensorKind 7/9 = internal
        if (M.isSingleBBAccessed()) continue;                  // must be live across iterations
        assert(M.isAllocated() && "Internal DRAM node not allocated");

        rawSize     = (M.memoryType==8) ? M.numPartitions·width : width;
        alignedSize = roundup(rawSize, getAlignmentSize(M));   // default alignment 8
        newAddr     = getAddress(base) + alignedSize;          // next consecutive slot in the window
        MemoryLocation::allocate(M, newAddr);
    }
}
```

The assert string, the `<< 30` (GiB→byte) shift, and the two log strings sit at `dram_rotation` lines 539/686/756/761. The window bounds the rotation so the runtime HBM allocator stays in range; `dram_rotation_size` and `disable_dram_rotation` are both real knob strings in the binary.

### Ring semantics

Successive iterations are laid at consecutive aligned offsets inside the GiB window: `addr = windowBase + (iter mod (window/stride))·alignedStride` — a `(numer mod denom)`-shaped index over the DRAM byte address with `denom = window/stride`.

---

## The post-schedule variant — multi-buffering emerges

This is the headline. The pre-split rotations give a recurring tensor a ring; the post-split `address_rotation_psum_post_schedule` (2-bool ctor) and the re-run of `address_rotation_sb` rotate the **stage-replicated** buffers `separate_load_and_compute` created, and that is when double/multi-buffering actually exists:

1. `separate_load_and_compute` splits each loop body into a **load** stage and a **compute** stage and replicates buffers per stage.
2. The post-schedule rotations rotate the now-distinct stage buffers so producer iteration *i* writes physical buffer `(i mod N)` while consumer iteration *(i−1)* reads buffer `((i−1) mod N)` — the classic `N`-buffer producer/consumer skew. The rotation chooses `N` physical banks/tiles (PSUM via §3, SB via §4) and the post-schedule order guarantees the *i*-th and *(i−1)*-th references resolve to different physical slots.
3. `clearInstDeps` drops the stale dependence edges that the buffer skew invalidated; `build_fdeps` / the anti-dependency analyzer (cross-ref [`walrus/dependence-graph.md`](dependence-graph.md)) then rebuild them against the rotated addresses, so the engines overlap `load(i+1)` with `compute(i)` with no WAR/WAW hazard.

```c
// clearInstDeps @0x940ac0 — attr-12-gated dependence-edge reset
void clearInstDeps(this, bir::Module& M) {
    fn = M.entryFunction - 160;
    if (!Function::hasAttribute(fn, 12)) return;               // gated on the post-schedule marker
    for (f : M.functions) {
        get_inst_in_order(f, &insts);                          // instructions in program order
        rec = tc_new(3);                                       // 3-byte edge-kind record
        *(u16*)rec     = 770;                                  // 0x0302  → clear Ordered/Anti/Output
        *(u8*)(rec+2)  = 4;                                    // 0x04    → clear Flow
        tbb::parallel_for(blocked_range, lambda off_3D74A90);  // reset edges per inst
    }
}
```

The `hasAttribute(12)` gate, the `tc_new(3)` 3-byte record, the `*(u16*)rec = 770` (`0x0302`) and `*(u8*)(rec+2) = 4`, and the TBB `parallel_for` over `get_inst_in_order` are all visible in `clearInstDeps`. The constants `0x0302`/`0x04` map to the `EdgeKind` enum `Ordered1/Anti2/Output3/Flow4` — i.e. all edge kinds are cleared so the dep graph rebuilds clean.

### Physical rebinding, not symbolic indexing

`AddressRotation` rebinds addresses and nothing else: every rotation it performs is a `MemoryLocation::allocate` onto a new bank / tile / region. It never constructs the symbolic `(iter mod N)` index — across the whole pass there are zero calls to `createModuloExpr`, `Expr::modulo` or `Expr::floordiv`. Of the seven callers of `createModuloExpr` in the binary, six are pelican-internal and exactly one is a backend pass: `Unroll::LowerDynamicExprs` @ `0xb38a50`. So `Unroll` writes the ring *index* and `AddressRotation` chooses the physical *slot* that index maps onto; the two halves are complementary, and this page documents the physical one. See [`pelican::ModuloExpr`](../bir/pelican-moduleexpr.md) for the symbolic half.

> **NOTE —** `address_rotation_psum_post_schedule` runs after the load/compute split and is the variant that rotates stage buffers (registration template `RA36_Kcbb`, placed after `separate_load_and_compute`). The exact in-body instruction that writes each rotated stage buffer's address inside the *post_schedule* body has not been traced individually; that it reuses the same `psum_rotation` / `sb_rotation_batch` cores documented above is [INFERRED].

---

## AddressRotationScope — the per-kernel scope knob

NKI kernels carry an `AddressRotationScope` field at `InstNKIKernel[+87]` (u32, 0..2). `birverifier::checkNKIKernelAddressRotationScope` @ `0xfe3450` asserts `scope ∈ {None=0, Kernel=1, Global=2}` (else `NeuronAssertion 1684`):

| Scope | Value | Effect |
|---|---|---|
| `None` | 0 | kernel opts out of rotation |
| `Kernel` | 1 | rotation confined to the kernel's own SB window — `rotateAddrs`' attr-9&10 tail loop re-derives the kernel's `numPartitions`/`basePartition` and rotates only inside it (the "Skipping kernel-scoped address rotation … SB shape not set" guard) |
| `Global` | 2 | rotation spans the whole module's address space |

The enum is the *user-facing* knob that controls rotation **scope**; the per-space passes control the rotation **mechanism**. Both the verifier assert and the kernel-scope tail loop in `rotateAddrs` read the same field.

---

## End-to-end — how multi-buffering falls out

```text
  1. coloring_allocator_{psum,sb,dram}   ── one physical buffer per logical tensor
  2. address_rotation_{psum,sb,dram}     ── rotate recurring buffers onto distinct
       (orders 46/56/69)                     banks/tiles/regions per partition-band:
                                             a tensor that recurs gets a small RING
  3. separate_load_and_compute           ── split loop into load/compute, replicate
                                             buffers per stage
  4. address_rotation_psum_post_schedule ── rotate the stage buffers; producer(i)→(i mod N),
     + address_rotation_sb (re-run)          consumer(i-1)→((i-1) mod N)
  5. clearInstDeps → build_fdeps         ── drop stale edges, rebuild against rotated addrs
                                          ⇒ scheduler overlaps load(i+1) with compute(i),
                                             hazard-free because the buffers differ
```

The Penguin `SoftwarePipelineCodeGen` (cross-ref [5.14](../penguin/software-pipelining.md)) chooses the stage count / `N` for the *annotated* path; this native walrus layer is the *emergent* path — it performs the physical address rotation, and `Unroll` (not this pass) writes the matching `ModuloExpr` ring index. Multi-buffering is never an explicit request: it is what you get when you give a recurring tensor a ring of `N` physical slots and then let an ordinary list scheduler overlap the iterations.

---

## Related Components

| Name | Relationship |
|---|---|
| `coloring_allocator_{psum,sb,dram}` | assigns the one buffer per tensor that rotation then spreads onto a ring |
| `separate_load_and_compute` | the pipelining split; creates the stage buffers the post-schedule rotation spreads |
| `Unroll::LowerDynamicExprs` | the sole backend producer of the `ModuloExpr` ring index this pass's slots realize |
| `build_fdeps` / anti-dependency-analyzer | rebuild the dependence graph against rotated addresses after `clearInstDeps` |

## Cross-References

- [pelican::ModuloExpr — the buffer-ring address-rotation math](../bir/pelican-moduleexpr.md) — the `(numer mod denom)` ring index; proves `AddressRotation` emits zero `ModuloExpr` nodes and `Unroll` is the sole producer (7.20)
- [Penguin Software Pipelining](../penguin/software-pipelining.md) — the *declarative* pipeline driven by `num_stages` annotations; the parallel path to this *emergent* one (5.14)
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the bank count `N`, 2048-byte bank size, and 128-partition layout that bound the PSUM and SBUF rings
- [The Dependence Graph — build_fdeps + anti-dependency-analyzer](dependence-graph.md) — rebuilds dep edges after `clearInstDeps` resets them against the rotated addresses
- [The walrus Pass Pipeline & Optlevel Planes](pass-pipeline-optlevels.md) — where the four rotation passes sit in the full backend pass order
