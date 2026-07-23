# Tiling / Memory-Planning / Scheduling Backend

The three back-end passes that decide *where a GPSIMD op runs on the chip* and *when it runs
relative to everything else*: the **`SundaSizeTiling`** loop-nest tiler (which slices a
Vector/GPSIMD op onto the 128-partition / 8-GpSimd-core × 16-partition SBUF grid, ceiling its
tile at one PSUM bank), the **static memory planner** (linear-scan SBUF byte allocator +
modulo PSUM-bank allocator + the verifier-pinned *GPSIMD-cannot-touch-PSUM* rule), and the
**two-level list scheduler** (a Python DDG ready-list scheduler feeding a `libwalrus` C++
`TimeAwareScheduler` + `AntiDependencyAnalyzer` + the `AllocSemaphores` / trigger-engine /
`InsertPTCOMFlat` / `InsertCoreBarrier` passes that become the per-engine NEFF `.bin` sync
fields).

This is the host-side complement to the device geometry. The *why* of every constraint
below — 128 partitions, the 32-partition quadrant, the PSUM disjointness — is proven at the
silicon/aperture level in
[On-Chip State-Buffer (SBUF) + PSUM Bank Model](../dma/sbuf-psum-banks.md) and the
[Keystone Facts](../orientation/keystone-facts.md); the sync fields these passes emit are
*consumed* by [Per-Engine Instruction-Block Assembly Pipeline](../neff/assembly-pipeline.md).
The pass roster and `emit_*→opcode` map are in [The GPSIMD-Relevant Compiler Map](compiler-map.md);
the inter-engine sync detail in [Optimization + Inter-Engine Sync Insertion](opt-sync-insertion.md).

All symbols, docstrings, addresses and strings below are read directly from the
shipped, **not-stripped** `neuronx-cc 2.24.5133.0+58f8de22` wheel — the Cython codegen passes
carry verbatim `.py` method names as `__pyx_*` symbols + trace strings + docstrings, and
`libwalrus.so` carries C++ RTTI + assert strings + build-path leaks. The plaintext geometry
comes from the `neuronx-cc-stubs` `.pyi`. No string is paraphrased where it is quoted;
libwalrus addresses are from `nm -D libwalrus.so | c++filt`.

> **Confidence tags.** `[OBSERVED]` byte-exact from a shipped string/symbol/docstring;
> `[INFERRED]` reasoned over observed control/data flow; `[CARRIED]` carried from a
> cited sibling page, re-grounded here. `HIGH/MED/LOW` = confidence.

---

## 1. The geometry the three passes are constrained by

Every tile/place/schedule decision is bounded by the machine model below. The numbers are
the shipped compiler's own ISA-validity constants — they are not silicon-generation
inferences (the `sunda`/`cayman`/`mariana`/`maverick` axis is the known per-gen codegen
target; see [Keystone Facts](../orientation/keystone-facts.md) K6–K9). The `sunda` (NC-v2)
floor is the one this page targets.

| constant | value (sunda) | source (verbatim) | confidence |
| --- | --- | --- | --- |
| SBUF partitions | **128** | `tile_size.pmax` property (`nki/language/__init__.pyi`); verifier `AP accesses must <= 128 partitions, which is HW limit` | HIGH/OBS |
| SBUF partition physical | **192 KiB** | `sbuf.pyi`: *"192KiB is the physical size of a SBUF partition"* | HIGH/OBS |
| SBUF partition usable | **176 KiB** = `(192-16)*1024` | `sbuf.pyi`: *"to 192KiB-16KiB=(192-16)\*1024 (exclusive)"* | HIGH/OBS |
| SBUF partition **stride** | **256 KiB** | `STATE_BUF_PARTITION_SIZE` ([SBUF/PSUM Bank Model §1](../dma/sbuf-psum-banks.md#1-geometry-constants--the-per-generation-machine-model)) | HIGH/CARR |
| PSUM HW banks / partition | **8** | `private_nkl/utils/kernel_helpers.py`: `NUM_HW_PSUM_BANKS = 8`; `Tonga.so` property `psum_num_banks` | HIGH/OBS |
| PSUM bank size | **2048 B** = **512 fp32** | `kernel_helpers.py`: `PSUM_BANK_SIZE = 2048`; `psum.pyi`: *"``512*sizeof(nl.float32) = 2048`` bytes"* | HIGH/OBS |
| PSUM tile may span banks? | **NO** | `psum.pyi`: *"a physical PSUM tile cannot span multiple PSUM banks"* | HIGH/OBS |
| GpSimd cores | **8**, each → **16 contiguous** partitions | `isa/__init__.pyi`: *"Each of the eight GpSimd cores in GpSimd Engine connects to 16 contiguous SBUF partitions"* | HIGH/OBS |
| GpSimd engine enum | `gpsimd = 3` | `isa/__init__.pyi` (`gpsimd_engine`) | HIGH/OBS |
| Quadrant | **32 partitions**, base `{0,32,64,96}` | `sbuf.pyi` partition-start rules (§3); verifier `Sunda PSUM accesses must start at a %32 partition` | HIGH/OBS |

> **NOTE.** `Tonga.so` also exposes `psum_par_fp32_per_bank`, `statebuf_num_partitions`,
> `statebuf_par_size_in_bytes` and `psum_max_accessable_psum_bank` as Cython property names
> (`__pyx_n_s_*`); the SDMA 32-multiple rounding is
> `ProfileBasedDMALatencyModel.get_max_partitions_per_sdma`. The planner reads its geometry
> from these getters, not from a literal table. `[HIGH/OBSERVED]`

> **GOTCHA — the 8-vs-16 PSUM-bank trap.** The ISA header reports `PSUM_BUF_NUM_BANKS = 16,
> BANK_SIZE = 1024` on **sunda** but `8 × 2048` on cayman+ — both equal 16 KiB active =
> 512 fp32/partition. The compiler's `kernel_helpers.py` constants, the allocator and
> `psum.pyi` all use the **8-bank (2048 B)** view; that is the modulo-allocator's rotation
> modulus and the matmul accumulator unit. Do not seed the modulo allocator with 16. See
> [SBUF/PSUM Bank Model §4.2](../dma/sbuf-psum-banks.md#42-reconciling-8-bankspartition-with-32-ecc-banks--48).
> `[HIGH/CARRIED]`

---

## 2. Deliverable 1 — `SundaSizeTiling`: tiling onto the 128 / 8×16 / PSUM grid

`SundaSizeTiling.cpython-311-x86_64-linux-gnu.so` (7,224,624 bytes;
`targets/sunda/passes/`). It runs in the layout-tiling stage of the Sunda codegen flow, after
ISel/intrinsic-fusion and before legalize/PSUM-inference/memory-planning (§5).

### 2.1 The pass purpose — the PSUM-bank tile ceiling `[HIGH/OBSERVED]`

The docstring is read byte-exact:

> *"SundaSizeTiling - Tile the loopnests to generate tiles that can fit into a single tonga
> instruction. It will generate NeuronMacro to mark those tiles. A tonga macro will be later
> lowered to tonga instructions. This pass will also propagate tiling decisions, such that
> the data rate in producer and consumer match (this make it easier to find a good schedule).
> The size limit in this case is mainly coming from the size of PSUM buffer (i.e. the inner
> most and smallest [buffer)]."*

Two reimplementation-load consequences:

1. **The tile-size ceiling is one PSUM bank** = 512 fp32 = 2048 B per partition. Even though a
   GPSIMD op never *uses* PSUM (§4.4), the tiler's free-tile budget is anchored to the
   smallest on-chip buffer so a producer that *does* write PSUM and a GPSIMD consumer share a
   rate-matched tile.
2. **Tiling decisions propagate** along a producer→consumer chain so the data rate matches —
   the tiler chooses the same free-tile size down a dependency chain so the downstream
   scheduler (§6) does not stall on a rate mismatch.

### 2.2 The partition / free split is op-class-specific `[HIGH/OBSERVED]`

A tensor's axes are partitioned into one **partition axis** (mapped to the 128 SBUF
partitions, stride `STATE_BUF_PARTITION_SIZE`) and **free axes** (the per-partition byte
offset). The pass carries three op-class-specific candidate-extractor families — confirmed as
fully-qualified `__pyx` symbols of the form
`…targets.sunda.passes.SundaSizeTiling.SundaSizeTiling.<method>`:

| op class | partition-axis extractor | free-axis machinery | emit |
| --- | --- | --- | --- |
| **PE matmul** | `extractPartitionAxesCandidates` | `extractPEContractAxesCandidates`, `extractPELHSFreeAxisCandidates` (≤ `gemm_moving_fmax`), `extractPEFreeAxisCandidates` (≤ `gemm_stationary_fmax`) | `tileContractAxes` / `tileLHSFreeAxes` / `tileRHSFreeAxes` |
| **SIMD macro** (the GPSIMD/DVE elementwise leg) | `simd_partition_axes` | `simd_free_axes` | `extractAxesForSIMDMacro` → `packInstsToSIMDMacro` → `finalizeSIMDMacro` |
| **reduce / BN** | `collectBlockPartitionIndices` | — | `buildReduceMacro` / `buildTReduceOperator` / `buildBatchNormMacros` |

The matmul `fmax` caps are the `tile_size` properties `gemm_moving_fmax` /
`gemm_stationary_fmax` / `bn_stats_fmax` / `psum_fmax` (`nki/language/__init__.pyi`, verbatim
property docstrings). The systolic split is asymmetric by design — the LHS/moving and
RHS/stationary axes are tiled independently (the tiler does *not* assume a square array).

> **The GPSIMD-relevant leg is the SIMD-macro path.** A GPSIMD int32 `tensor_tensor` op flows
> through `extractAxesForSIMDMacro` (partition axis ← the 128-dim, mapped 8 cores × 16
> partitions) → `packInstsToSIMDMacro` (fuse the elementwise DAG) → `finalizeSIMDMacro`
> (emit one `NeuronMacro` = one tonga instruction stream). The 8 cores execute the op over
> their 16 contiguous partitions independently — the partition axis is naturally
> 16-core-parallel, with no cross-core sync inside the op. `[HIGH/OBSERVED — pyx symbols]`

### 2.3 The tile-size knobs `[HIGH/OBSERVED — string surface]`

Read verbatim from the `.so` string table:

| knob (string) | role |
| --- | --- |
| `partition_max_elts` | per-partition-axis tripcount cap (the partition tile size) |
| `max_computation_tile_size_in_bytes` | the free-dim byte budget for a compute tile |
| `max_local_tensor_tile_size_in_bytes` | the budget for an SBUF-local intermediate |
| `low_psum_usage_threshold` | *"The psum usage threshold to break global layout propagation"* (verbatim) — when PSUM pressure exceeds it, stop propagating one global layout and let local layouts win |
| `min_rate_matching_tile_size` / `rate_matching_free_axes` | the producer/consumer rate-match machinery (§2.1) |
| `hull_size` (`lhs_hull_size` / `rhs_hull_size` / `max_hull_size`) | the bounding tile volume; docstring fragment *"to account for untilable dims when calculating hull_size in tiling"* |
| `coalesce_dim` / `tensor_coalesce_dim` | const dims (batchnorm/concat) whose size offsets the max-tensor-size so a loop-nest tile still matches its peers |

> **QUIRK.** The *partition* tile is an **element count** (`partition_max_elts`), the *free*
> tile is a **byte budget** (`max_computation_tile_size_in_bytes`). They are not the same unit
> — partition counts are bounded by 128 / 32-quadrant alignment (§3), free bytes by the 176
> KiB usable partition (SBUF) or the 2048 B bank (PSUM). A reimplementer must keep the two
> axes in their native units; collapsing them to "tile size" loses the quadrant constraint on
> one axis and the bank ceiling on the other. `[HIGH/OBSERVED knobs; INFERRED unit reading]`

> **NOTE — `partition_max_elts` arithmetic is INFERRED.** The PSUM-bank 512-fp32 ceiling and
> the 176 KiB usable bound are OBSERVED upper bounds; the *exact* value the tiler picks for a
> given op is a `.so` immediate, not a string — the chosen tile is INFERRED to be ≤ those
> bounds and rate-matched to its producers. `[MED/INFERRED]`

### 2.4 Software replication — filling < 128-partition tiles `[HIGH/OBSERVED]`

When a tile uses fewer than the full partition span, the tiler can **replicate** a
small-partition operand across partitions in software rather than re-loading it
(`enable_software_replication`, `is_replication_candidate`). This is the host-side counterpart
of the gather-replication the ISA describes: *"users can generate indices into 16 partitions,
replicate them eight times to 128 partitions"* (`isa/__init__.pyi`, verbatim). A GPSIMD gather
that produces indices for one core's 16 partitions is broadcast to all eight cores' 128
partitions this way. `[HIGH/OBSERVED]`

### 2.5 Worked tile — int32 `tensor_tensor add`, shape `[128, 4096]` `[HIGH]`

The canonical GPSIMD case (the int32 op the front-end routes to GpSimd; see
[Keystone Facts](../orientation/keystone-facts.md) K2):

1. **Route** (CARRIED): `nki tensor_tensor(int32, add)`, no operand in PSUM → `resolved_engine
   = gpsimd` → `TENSOR_TENSOR_ARITH 0x41`, `engine = GpSimd`, BIR `NeuronEngine = Pool`.
2. **Tile** (OBSERVED): `extractAxesForSIMDMacro` picks partition axis = the 128-dim
   (`simd_partition_axes`), free axis = the 4096-dim, tiled by
   `max_computation_tile_size_in_bytes`. The op is SBUF-resident (§4.4), so the free budget is
   the 176 KiB usable partition — but rate-matching to producers/consumers usually pins a
   smaller free tile. Each of the 8 cores runs the op over its 16 partitions.
3. **Pack** (OBSERVED): `finalizeSIMDMacro` packs the (possibly fused) elementwise DAG into one
   `NeuronMacro`, lowered later to `0x41` POOL descriptors.

```c
// SundaSizeTiling SIMD-macro tiling for a GPSIMD elementwise op (reconstructed from the
// pyx symbol surface; all named symbols OBSERVED). [HIGH/INFERRED arithmetic]
//
// Geometry (all OBSERVED, §1):
//   PMAX            = 128       // SBUF partitions; tile_size.pmax
//   GPSIMD_CORES    = 8         // each owns 16 contiguous partitions
//   PARTS_PER_CORE  = 16        // 8 * 16 == 128
//   QUADRANT        = 32        // partition base must be {0,32,64,96}
//   SBUF_USABLE     = 176*1024  // usable bytes / partition (192 - 16 KiB carveout)
//   PSUM_BANK_FP32  = 512       // 2048 B; the tile-size *ceiling* (even though GPSIMD
//                               // never lands in PSUM, the ceiling is the rate anchor)

NeuronMacro tile_simd_macro(SimdDag dag, TileBudget b /* from the cost model */) {
    // (1) partition axis: a GPSIMD op spans all 128 partitions = 8 cores x 16.
    //     partition_max_elts caps the partition tripcount; quadrant alignment is
    //     enforced later by the allocator (base_partition_constrained, §3).
    Axis  p_axis  = extractAxesForSIMDMacro(dag).simd_partition_axes;   // -> 128
    size_t p_tile = min(p_axis.tripcount, b.partition_max_elts);         // <= 128

    // (2) free axis: a *byte* budget, NOT an element count. For an SBUF-only op the hard
    //     bound is SBUF_USABLE; the PSUM-bank ceiling (PSUM_BANK_FP32) is the rate anchor
    //     a co-scheduled PE producer would hit, so rate-matching usually pins f_tile lower.
    Axis  f_axis  = extractAxesForSIMDMacro(dag).simd_free_axes;
    size_t f_bytes = min(f_axis.size_bytes,
                         b.max_computation_tile_size_in_bytes);          // <= 176 KiB
    f_bytes = rate_match_to_chain(f_bytes, dag.producers, dag.consumers);// §2.1 propagation

    // (3) if PSUM pressure is high, stop propagating one global layout (low_psum_usage_threshold).
    // (4) software-replicate a < PMAX-partition operand across partitions to fill lanes.
    if (uses_fewer_than_full_partitions(dag) && is_replication_candidate(dag))
        replicate_across_partitions(dag);                               // §2.4

    return packInstsToSIMDMacro(dag, p_tile, f_bytes);  // -> finalizeSIMDMacro -> one macro
}
```

---

## 3. The partition-alignment constraints the allocator must satisfy

These are the placement rules `StackAllocator` (§4) emits and the `libwalrus` verifier
re-checks. Read verbatim from `sbuf.pyi` and the verifier string table.

**Partition-start rule** (`sbuf.pyi`, verbatim — the allocator's base-partition contract):

```text
- If  64 < pdim_size <= 128,  start_partition must be 0
- If  32 < pdim_size <=  64,  start_partition must be 0 or 64
- If   0 < pdim_size <=  32,  start_partition must be one of 0/32/64/96
```

This is `SB_Allocator::base_partition_constrained(Height)` in `libwalrus`
(`neuronxcc::backend::SB_Allocator::base_partition_constrained(neuronxcc::backend::Height) @0x9cfba0`).
The verifier strings that re-check it `[HIGH/OBSERVED]`:

| string (verbatim) | meaning |
| --- | --- |
| `AP accesses must <= 128 partitions, which is HW limit` | the 128-partition cap |
| `Pattern accesses > 128 partitions` | the same cap, the reject form |
| `Access pattern crosses 64th partition boundary.` | a tile may not straddle the 64-partition half |
| `0 == lowerPartition % 32 && "Sunda PSUM accesses must start at a %32 partition"` | PSUM base must be a quadrant base |
| `0 == lowerPartition && "CoreV1 PSUM accesses must start at partition 0"` | the gen-specific (Inferentia) form |
| `Matmult inputs must start at SB Partition 0` | the systolic load alignment |
| ` bytes in partitions [0, 31]` (also `[32, 63]`, `[64, 95]`) | the quadrant SB-demand prints |

> **CORRECTION (vs an earlier synthesis).** That synthesis cited the verifier string
> ` (must be <= 176)` as the SBUF 176 KiB usable-byte bound. **That string is a DMA-engine
> count message** (its neighbours in the string table are `DMA Engines` / ` GB)`), *not* an
> SBUF byte limit. The 176 KiB usable bound is grounded instead by `sbuf.pyi`
> (*"192KiB-16KiB=(192-16)\*1024 (exclusive)"*). The 176 KiB value is unchanged; only the
> cited anchor is corrected — do not point a reimplementation's SBUF bound at the
> `<= 176` DMA string. `[HIGH/OBSERVED]`

> **QUIRK — a GpSimd core is *half* a quadrant.** A quadrant is 32 partitions; a GpSimd core
> owns 16. So a single-core GPSIMD op (16 partitions) is sub-quadrant, and the SDMA that
> stages its inputs rounds the active-partition count up to a multiple of 32
> (`ceil(max(src,dst)/32)*32`) — even a 16-partition GPSIMD op triggers a one-quadrant (=
> 2-core) SDMA window. See
> [SBUF/PSUM Bank Model §7.5](../dma/sbuf-psum-banks.md#75-the-8-core--16-partition-gpsimdsbuf-wiring).
> `[HIGH/OBSERVED partition map; MED rounding tie]`

---

## 4. Deliverable 2 — the static memory planner

Runs *after* tiling (so it allocates fixed-size tiles) and *before* the scheduler. All passes
are Cython under `targets/transforms/`.

### 4.1 The allocation chain `[HIGH/OBSERVED]`

```text
AllocationDecision        "Allocation decision based on the loopnest structure"
   |  block_is_parallel_loop / block_is_distributed_loop / checkMatmultAccLoop
   v
AllocateBlocks            "Allocate blocks for local tensors and build live interval analysis."
   |  createLocalTensors ; _exclude_static_tensors_from_live_interval
   v
{ TensorLiveRange + NeuronStackLiveInterval }   stack-discipline live intervals
   |  "each tensor expand their live interval to the range of the parent loop that ..."
   v
StackAllocator::LinearScanStackAllocator
   |  "it tries to assign tensor addr in linear scan [manner]" ; "assign only byteaddr to SB tensors."
   |  assign_sb_addr  (byte cursor)   /   assign_psum_addr  (bank + in-bank byte)
   |  increase_scope_count / decrease_scope_count / expire_intervals
   v
ModuloAllocation          multi-buffer PSUM banks (software pipeline)
   |  build_modulo_alloc_bank_and_block ; calculateMaxInterleaveFactor ; enableModuloAllocationForCompute
   v
{ MaxLiveSpiller / SpillPSum }   spill SBUF/PSUM -> SB/HBM when live pressure overflows
```

**`AllocationDecision`** `[HIGH/OBSERVED]` — docstring verbatim: *"AllocationDecision -
Allocation decision based on the loopnest structure"*; `block_is_parallel_loop`,
`block_is_distributed_loop`, `checkMatmultAccLoop` (fully-qualified pyx symbols). It decides,
per loop-nest, which tensors are *top-level* (allocated once, resident) vs *local*
(re-allocated per iteration). A matmul accumulation loop keeps its PSUM accumulator resident
across the K-loop (`checkMatmultAccLoop`).

**`AllocateBlocks`** `[HIGH/OBSERVED]` — docstring verbatim: *"Allocate blocks for local
tensors and build live interval analysis."* Static (pre-allocated, always-resident) tensors
are short-circuited out of the live-interval algorithm
(`_exclude_static_tensors_from_live_interval`; *"Don't need spill-free kernel analysis (always
resident in memory)"*).

**Live intervals** `[HIGH/OBSERVED]` — `NeuronStackLiveInterval` docstring verbatim:
*"each tensor expand their live interval to the range of the parent loop that [contains
it]"*. This is the **stack discipline**: a tensor's lifetime is the span of its enclosing
loop, so allocation is a push at loop entry / pop at loop exit.

### 4.2 `LinearScanStackAllocator` — the two address kinds `[HIGH/OBSERVED]`

Docstring verbatim: *"LinearScanStackAllocator - it tries to assign tensor addr in linear scan
[manner with stack live interval]"*, and *"assign only byteaddr to SB tensors."* It is the
classic linear-scan expire-old-intervals algorithm (`expire_intervals`,
`increase_scope_count`/`decrease_scope_count` at loop boundaries). It emits **two distinct
address kinds**, matching the two allocator stub signatures exactly:

- **`assign_sb_addr`** → a 1-D **byte cursor** within the 176 KiB partition (docstring: *"assign
  only byteaddr to SB tensors"*). The `sbuf.pyi` allocator returns `(start_partition,
  byte_addr)` — a partition base (quadrant-aligned, §3) + an in-partition byte offset.
- **`assign_psum_addr`** → a **`(bank_id, start_partition, byte_addr)`** triple. The `psum.pyi`
  allocator returns exactly this: *"a tuple of three integers (bank_id, start_partition,
  byte_addr)"* — a bank index (0..7) + a quadrant base + an in-bank byte offset.

```c
// LinearScanStackAllocator — two address kinds, two stubs. [HIGH/OBSERVED]
// SBUF: 1-D byte cursor per partition.   PSUM: bank index + in-bank byte.
typedef struct { uint32_t start_partition; uint32_t byte_addr; }            SBAddr;    // sbuf.pyi
typedef struct { uint32_t bank_id; uint32_t start_partition; uint32_t byte_addr; } PSUMAddr; // psum.pyi

SBAddr assign_sb_addr(StackAllocator *a, TensorLiveRange *t) {
    expire_intervals(a, t->live_begin);                 // free SBUF whose interval ended
    SBAddr r = { .start_partition = quadrant_base(t->pdim_size),   // 0 / {0,64} / {0,32,64,96}
                 .byte_addr       = a->cur_sbuf_addr };  // running 1-D byte cursor
    a->cur_sbuf_addr += align_up(t->alloc_block_size_in_bytes, t->dtype_align);
    return r;                                            // 0 <= byte_addr < 176*1024  (§3)
}

PSUMAddr assign_psum_addr(StackAllocator *a, TensorLiveRange *t) {
    expire_intervals(a, t->live_begin);
    // PSUM is a bank index (0..7), NOT a byte cursor: a tile may not span banks (psum.pyi).
    PSUMAddr r = { .bank_id         = a->cur_psum_bank,            // rotated by ModuloAllocation
                   .start_partition = quadrant_base(t->pdim_size),
                   .byte_addr       = 0 };                          // base_addr "must be 0"
    a->cur_psum_bank = (a->cur_psum_bank + t->eff_banks) % 8;       // 8 HW banks
    return r;
}
```

### 4.3 `ModuloAllocation` — multi-buffer PSUM banks `[HIGH/OBSERVED]`

`build_modulo_alloc_bank_and_block`, `calculateMaxInterleaveFactor`,
`enableModuloAllocationForCompute` (all fully-qualified pyx symbols). It assigns successive
iterations of a pipelined loop to **different PSUM banks** (modulo the 8-bank count) so
producer and consumer overlap — a double/triple-buffer across the 8 banks. The `psum.pyi`
`mod_alloc(base_bank, num_bank_tiles, …)` stub is the user-facing form (verbatim:
*"num_bank_tiles: the number of PSUM banks allocated for the tensor"*). When the interleave
factor needs more than 8 banks the allocator errors — the runtime-built message fragment is
*") exceeded PSUM bank limit "* (neighbours `findAllocationCandidates` /
`compute_available_blocks`). This is the host realization of the PSUM-bank rotation the matmul
`MATMUL_ZERO_REGION` selects on the device side. `[HIGH/OBSERVED]`

> **QUIRK — modulo allocation is PSUM-only.** The interleave is a *bank* rotation (modulus 8);
> SBUF has no bank-index address kind (only a byte cursor), so a GPSIMD op — which lives in
> SBUF only (§4.4) — is never modulo-allocated. The double-buffer machinery applies to the
> PE/ACT PSUM accumulators, not to GPSIMD tiles. `[HIGH/INFERRED]`

### 4.4 The GPSIMD memory rule — no PSUM, verifier-pinned `[HIGH/OBSERVED]`

This is the deepest GPSIMD-relevant fact in the back end. The `libwalrus` verifier rejects any
GPSIMD operand in PSUM, byte-exact:

```text
GPSIMD engine cannot access PSUM
GPSIMD Instructions cannot access PSUM. Assign to a different Engine or move data to SB.
All args to a customop must be located in SBUF or HBM
All of a customop's outputs must be located in SBUF or HBM
```

The same rule appears in the front-end stubs verbatim: `isa/__init__.pyi` *"since GpSimd
Engine cannot access PSUM in NeuronCore, Scalar or Vector Engine must be chosen when the input
or [output is in PSUM]"* and *"Since GpSimd Engine cannot access PSUM, the input or output
tiles cannot be in PSUM"*.

The complement is **`InferPSumTensor`** (`targets/transforms/`) — docstring verbatim:
*"InferPSumTensor - Assign result of Pool/Act instructions to PSUM"*. It assigns PE-matmul /
ACT results *into* PSUM banks and keeps GpSimd (and customop) tensors in SBUF. The predicates
`can_write_to_psum` / `can_read_from_psum` gate which ops may touch a bank, and
`force_tensortensor_psum_sb` forces a `tensor_tensor` operand to SB when the engine cannot read
PSUM. The verifier gates are `birverifier::checkPSUMLegality(bir::Function const&) @0xfef9e0`
and `birverifier::checkOutputFitsInPSUM(bir::Instruction const&) @0xfee390`; the custom-op
placement check is `birverifier::InstVisitor::visitInstCustomOp(bir::InstCustomOp&) @0xfa7ba0`.

This is the *allocator-level* reason the front-end routes an int32 `tensor_tensor` to GpSimd
**only when no operand is in PSUM** (Keystone K2): a GpSimd op never gets a PSUM bank
assigned, so if any operand were in PSUM the op could not run on GpSimd at all — the front-end
rule and the back-end allocator agree. The *physical* reason (PSUM has no AXI aperture the Q7
can reach) is in
[SBUF/PSUM Bank Model §7.4](../dma/sbuf-psum-banks.md#74-why-gpsimd-cannot-touch-psum--the-keystone-proof)
and [Keystone Facts](../orientation/keystone-facts.md) K2.

> **CORRECTION (vs an earlier synthesis).** That synthesis quoted the custom-op rule as
> *"All args to a customop must be located in **SBUF**"* / *"…outputs must be located in
> **SBUF**"*. The actual verifier string is **"…in SBUF or HBM"** for *both* args and outputs.
> The no-PSUM constraint is unchanged (PSUM is excluded in every case), but a GPSIMD
> custom-op operand may legally live in **SBUF *or* HBM** — not SBUF alone. A reimplementer
> who restricts custom-op operands to SBUF rejects legal HBM-resident operands. `[HIGH/OBSERVED]`

### 4.5 PSUM spill `[HIGH/OBSERVED]`

`SpillPSum` (`spillPSum`, `preSpillPSum`, `find_psum_spill_point`) + `MaxLiveSpiller` spill a
live PSUM tensor to SB/HBM when the 8 banks are exhausted. This is the same machinery a
non-add partition reduce relies on when it needs a PSUM transpose buffer the bank budget
cannot hold; the spill keeps it schedulable. Because GPSIMD tiles never occupy a PSUM bank,
GPSIMD live-pressure overflow spills SBUF→HBM via `MaxLiveSpiller`, never PSUM. `[HIGH/OBSERVED]`

### 4.6 The SBUF carveout `[HIGH — libwalrus OBSERVED, runtime tie CARRIED]`

The 16 KiB each SBUF partition reserves (192 → 176 KiB usable) is the runtime/compiler
carveout. `libwalrus` (the NEFF packager) writes the carveout record into `def.json`; the
runtime reads it back as the `evtaccel` / `runtime_statebuffer_reservation` region. So the
`StackAllocator` allocates within `[0, 176 KiB)` and the packager emits `[176, 192) KiB` as
the carveout the runtime keeps away from its own allocator. See
[Assembly Pipeline](../neff/assembly-pipeline.md) (the `seq_feature_flag` `evt_accel`
carveout in the model-switch preamble).

---

## 5. Where tile / place / schedule sit in the Sunda flow `[HIGH/OBSERVED]`

Read from `CodeGenFlow.so` / `SharedCodeGenFlow.so` module references. The GPSIMD-relevant
codegen order:

| stage | pass(es) |
| --- | --- |
| ISel | `SundaISel` ([SundaISel](sundaisel.md)) |
| intrinsic fusion (CC) | `InferIntrinsic` / `InferIntrinsicOnCC` |
| layout + **TILING** | `OrigLayoutTilingPipeline` / `PGLayoutTilingPipeline`, `TileCCOps`, `FlattenAxesForTiling`, **`SundaSizeTiling`** (§2) |
| legalize | `LegalizeCCOpLayout`, `LegalizePartitionReduce`, `LegalizeSundaMacro`, `legalize_tensor_tensor_op` |
| PSUM inference | `InferPSumTensor` (§4.4) |
| **MEMORY PLANNING** | `AllocationDecision` → `AllocateBlocks` → {live intervals} → `StackAllocator` → `ModuloAllocation` → `MaxLiveSpiller` / `SpillPSum` (§4) |
| cross-loop-nest sync | `InsertCoreBarrier` (§6.5) |
| **SCHEDULING** | `Scheduler` (tonga, §6.1) — Python list scheduler |
| backend (`libwalrus`) | `build_flow_deps` → `AntiDependencyAnalyzer` → `TimeAwareScheduler` / `LncAwareScheduler` → `AllocSemaphores` / trigger-engine / `InsertPTCOMFlat` / `InsertCoreBarrier` → `CoreV{2,3,4}GenImpl` → NEFF (§6) |

---

## 6. Deliverable 3 — the scheduler + the SEQ dependency / semaphore model

Two-level: a Python per-engine DDG list scheduler, then the `libwalrus` C++ backend
cross-engine dep-based scheduler + the sync-insertion passes that become the per-engine NEFF
`.bin` sync fields.

### 6.1 The Python list scheduler `[HIGH/OBSERVED]`

`targets/tonga/passes/Scheduler.so` — a classic ready-list scheduler over a data-dependence
graph (DDG). Fully-qualified pyx symbols of the form `…tonga.passes.Scheduler.Scheduler.<m>`,
OBSERVED:

| symbol | role |
| --- | --- |
| `build_ddg` | build the DDG from the tiled BIR |
| `enumerate_dependencies` (`.lambda4`) | enumerate dep edges per node; `lambda4` is the edge predicate |
| `enumerate_ready` / `filter_readys` | the ready frontier (0 unscheduled predecessors) |
| `pick_candidate` / `pick_candidate_from_list` | select the next node(s) to schedule |
| `bump_preds_priority` | raise a node's predecessors' priority (critical-path bias) |
| `num_ready_succs` / `release_successors` / `init_ref_counts` | succ-readiness terms + ref-count bookkeeping |
| `def_bytes` / `kill_bytes` / `alloc_size_in_bytes` | the **live-byte-pressure** tie-break — schedule the node that frees the most / grows liveness least |
| `heuristics` | the priority function (the string `Non-deterministic heuristics` is a guarded non-default mode) |

> **NOTE.** The scheduler's priority is **register-pressure-aware**: `def_bytes` (bytes a node
> defines / grows liveness by) and `kill_bytes` (bytes it frees) are the tie-break terms, so a
> GPSIMD op that frees a large SBUF intermediate is scheduled to relieve pressure. The precise
> weighting of `num_ready_succs` vs `def_bytes`/`kill_bytes` vs critical-path is a standard
> list-scheduler priority — the *terms* are OBSERVED, the *combination* is INFERRED-HIGH.
> `[HIGH/OBSERVED terms; MED/INFERRED formula]`

### 6.2 The cost model `[HIGH/OBSERVED]`

The cost basis splits cleanly across two `.so` (a real division to respect):

- **`InstructionLatencyModel.so`** — docstring verbatim: *"InstructionLatencyModel - A cycle
  based cost model that uses instruction latency as the cost."* Per-op getters (OBSERVED):
  `get_matmult_latency`, `get_tensortensor_latency`, `get_intrinsic_latency`. (It does **not**
  carry `get_pipeline_ii`/`get_pipeline_latency`.)
- **`KernelLatencyInfo.so`** — carries `get_matmult_latency`, `get_pipeline_ii`,
  `get_pipeline_latency` (the kernel-level pipeline aggregation). (It does **not** carry the
  tensortensor/intrinsic getters or the cost-model docstring.)

> **The GPSIMD relevance.** A GpSimd op's large fixed launch cost enters the scheduler via
> `get_intrinsic_latency` (InstructionLatencyModel) + `get_pipeline_ii` (KernelLatencyInfo).
> The startup overhead is documented verbatim in `isa/__init__.pyi`: *"`GPSIMD_START` is the
> instruction startup overhead on GpSimdE, roughly **150 engine cycles**"* (and a
> constant-build op costs *"`150 + N` GpSimd Engine cycles"*). So the scheduler prices a
> *small* GpSimd tile heavily — the host half of the "Vector for small/local, GpSimd for
> large/non-local" policy (the `InferIntrinsicOnCC` `min_nonlocals_to_do_fma` gate is the
> front-end half). `[HIGH/OBSERVED — the 150-cycle constant is byte-read from the ISA pyi]`

### 6.3 The SEQ dependency model — `AntiDependencyAnalyzer` `[HIGH/OBSERVED]`

The cross-engine hazards that need semaphores are computed by the backend
`AntiDependencyAnalyzer` (`nm`: vtable `_ZTVN9neuronxcc7backend22AntiDependencyAnalyzerE`, ctor
`AntiDependencyAnalyzer(PassOptions const&, …)`). It runs **separate instances per memory
class** — the instance-name strings `AntiDependencyAnalyzer-SB` and
`AntiDependencyAnalyzer-PSUM` are OBSERVED — because the partitioned/aliased address space
means SB, PSUM and DRAM hazards are analyzed independently. Its ctor template carries
`bir::TensorKind` and `bir::MemoryAddressSpace` set parameters, and it has *separate*
per-space partition-range getters:

```text
AntiDependencyAnalyzer::getSBPartitionRange(bir::PhysicalAccessPattern const&)
AntiDependencyAnalyzer::getPSUMPartitionRange(bir::PhysicalAccessPattern const&)
AntiDependencyAnalyzer::collectAccessPatternRanges(... boost::icl::split_interval_set ...)
AntiDependencyAnalyzer::analyzeDependencies(...)
AntiDependencyAnalyzer::populatePsumUseMaps(...)
AntiDependencyAnalyzer::aligned_length(bir::MemoryLocation&)
```

The dependence kinds are a single concatenated enum-name table in the string section:
`ANTI_DEPENDENCEFLOW_DEPENDENCEINVALIDORDEREDOUTPUT_DEPENDENCE` — i.e. ANTI (WAR) / FLOW (RAW) /
OUTPUT (WAW) / ORDERED / INVALID. The analyzer uses `boost::icl` interval sets over the
physical access-pattern ranges, so the hazard test is an interval-overlap, not a whole-tensor
compare. Spilled tensors keep conservative deps: *"Avoided Dependency Reduction due to
too_many_spills attribute"*; new edges print as *"Added ordered dependency from "*. `[HIGH/OBSERVED]`

> **QUIRK — the GPSIMD partition range is the SB range.** Because a GPSIMD op is SB-only
> (§4.4), its hazards are always computed by `getSBPartitionRange` and the
> `AntiDependencyAnalyzer-SB` instance; it never appears in the `-PSUM` analyzer. A
> reimplementer's anti-dep pass must classify GPSIMD ops into the SB memory class exclusively.
> `[HIGH/INFERRED from the SB-only rule + the per-class instances]`

### 6.4 The backend scheduler — `TimeAwareScheduler` `[HIGH/OBSERVED]`

After `build_flow_deps` (`build_flow_deps::constructBIRGraph(int, bool)`,
`build_flow_deps::make_accumulation_sequence(bir::MemoryLocation*)`) and the dep analysis, the
schedule is produced by `TimeAwareScheduler` (`nm`, real methods + addresses):

```text
TimeAwareScheduler::linearize_insts_by_simulation(bir::BasicBlock&)            @0xc61180
TimeAwareScheduler::pick_candidate()                                          @0xc618f0
TimeAwareScheduler::reschedule_cc_related_inst(TimeAwareScheduler::Candidate&) @0xc577a0
TimeAwareScheduler::is_better_trigger(bir::Instruction*, unsigned long, unsigned long,
                                      bir::Instruction*, unsigned long, unsigned long)
```

It simulates engine timelines to **linearize** instructions
(`linearize_insts_by_simulation`), picks the next instruction (`pick_candidate`), and
re-places compute-cluster (GpSimd/CC) ops specially (`reschedule_cc_related_inst` — their fixed
launch cost, §6.2). `is_better_trigger` picks which candidate fires a DMA (the trigger-engine
choice, §6.5). Then `LncAwareScheduler::schedule()` (+ `get_lnc_sync_candidates()`) orders
across NeuronCores. A bounded out-of-order reorder window is gated by `Disabling reorder window
optimization.`, and the output checkpoint is `bir.scheduled.after-ooo.json`. `[HIGH/OBSERVED]`

### 6.5 Semaphore / trigger-engine / barrier insertion `[HIGH/OBSERVED]`

After scheduling, these passes turn the schedule into the per-engine NEFF `.bin` sync fields.
Note there are **two distinct semaphore classes** in `libwalrus`: `AllocSemaphores` (the pass)
and `AllocateSemaphores` (the per-instruction assigner) — both OBSERVED.

**`AllocSemaphores`** (`nm`, real methods + addresses):

```text
AllocSemaphores::run(bir::Module&)                                  @0x1126ce0
AllocSemaphores::run(bir::Function&)                                @0x1124460
AllocSemaphores::allocateSemaphore(bool)                            @0x1121430
AllocSemaphores::incrementSemaphore(bir::Instruction*, unsigned, unsigned)  @0x1120f70
AllocSemaphores::addSemaphoreRotationWait(bir::Instruction*, unsigned)      @0x1123780
AllocSemaphores::setupSync(bir::Instruction*, std::vector<unsigned>, unsigned)   @0x1123e70
AllocSemaphores::setupSync(bir::InstDMABlock*, unsigned, std::vector<unsigned> const&, unsigned)  @0x1124360
```

**`AllocateSemaphores`** assigns the concrete DMA vs engine semaphore:
`AllocateSemaphores::assignDMASemaphore(bir::Instruction&) @0x1138f10`,
`AllocateSemaphores::assignEngineSemaphore(bir::Instruction&) @0x1138b70`, constrained by
`datapath semaphore must be updated by datapath instr`.

The **EventSemaphore wire limit** is verifier-pinned, verbatim:
`EventSemaphore must have <= 2 wait commands, and <= 1 update command. TODO: support 2 update
command` (plus the `Too many sync wait commands` / `Too many sync update commands` rejects).
`GroupResetSemaphores` is the semaphore-group reset
(`birverifier::checkGroupResetSemaphores(bir::InstGroupResetSemaphores&)`).

**Trigger-engine assignment** `[HIGH/OBSERVED]` — picks which engine *fires* each DMA:
`Assigned trigger engine for `, gated by `enable-hwdge-trigger-engine-scheduling` (the
registered generator `register_generator_assign_trigger_engine__`). This ties the SWDGE/HWDGE
descriptor model.

**`InsertPTCOMFlat`** (`nm`, real methods) — for multi-core (LNC > 1), insert a point-to-point
completion after the last write to an output tensor:

```text
InsertPTCOMFlat::insertPTCOMs(std::vector<std::pair<bir::MemoryLocation*, bir::Instruction*>> const&)  @0x16acc10
InsertPTCOMFlat::extendLastWrites(...)                @0x16abd40
InsertPTCOMFlat::findCBOnCore0(bir::InstCoreBarrier const*)   @0x16a7fd0
InsertPTCOMFlat::run(bir::Module&)
```

**`InsertCoreBarrier`** (Python, `targets/transforms/`) — docstrings verbatim:
*"Insert corebarrier if there are raw, war, and waw dependencies cross loopnests"* and
*"InsertCoreBarrier -- Insert Corebarrier on shared HBM tensors"* (`insertCoreBarrierOnIO`).
It inserts an all-engine barrier for cross-loop-nest RAW/WAR/WAW and shared-HBM tensors.

```c
// The sync-insertion pipeline (post-schedule). All named passes/methods OBSERVED. [HIGH]
void emit_sync_fields(bir::Module &m, const Schedule &sched) {
    // (1) per-memory-class hazards (separate AntiDependencyAnalyzer-SB / -PSUM, §6.3).
    auto edges = AntiDependencyAnalyzer(opts, /*kinds*/{...}, /*spaces*/{SB,PSUM,DRAM}).run(m);

    // (2) attach a semaphore wait/update to each ordering edge. WIRE LIMIT, verifier-pinned:
    //     EventSemaphore: <= 2 wait commands, <= 1 update command.
    for (Edge e : edges) {
        unsigned sem = AllocSemaphores::allocateSemaphore(/*rotating=*/e.is_pipelined);
        AllocSemaphores::setupSync(e.consumer, /*wait=*/{sem}, /*cond=*/e.threshold);   // wait $S[sem]>0
        AllocSemaphores::incrementSemaphore(e.producer, sem, /*delta=*/1);              // $S[sem]++@complete
    }

    // (3) which engine fires each DMA (trigger-engine; HWDGE-gated).
    for (DMA d : m.dmas())
        AllocateSemaphores::assignDMASemaphore(d);   // "Assigned trigger engine for ..."

    // (4) multi-core: PTCOM after the last write to each output; all-engine CoreBarriers.
    if (m.lnc_count() > 1) InsertPTCOMFlat::insertPTCOMs(InsertPTCOMFlat::extendLastWrites(m.outputs()));
    InsertCoreBarrier::run(m);   // RAW/WAR/WAW cross-loopnest + shared-HBM
}
```

### 6.6 How this becomes the NEFF `.bin` `[HIGH — backend OBSERVED, .bin tie CARRIED]`

The semaphore IDs + wait/update commands `AllocSemaphores`/`AllocateSemaphores` assign are
emitted by the `libwalrus` `CoreV{2,3,4}GenImpl` generator into the per-engine 64-byte TPB
sequencer slots (`CoreV2GenImpl::visitInstEventSemaphore`,
`CoreV2GenImpl::visitInstGroupResetSemaphores`, `CoreV2GenImpl::visitInstCustomOp @0x1261370`).
On the device side these become the `.asm` `$S[k]>0 $S[j]++@complete` semaphore conditions and
the `def.json` `dma_queue {owner, semaphore, type}` rows that the
[Assembly Pipeline](../neff/assembly-pipeline.md) consumes. The full chain:

```text
schedule (TimeAwareScheduler, §6.4)
  -> AntiDependencyAnalyzer-SB / -PSUM edges (§6.3)
  -> AllocSemaphores/AllocateSemaphores / trigger-engine / InsertPTCOMFlat / InsertCoreBarrier (§6.5)
  -> CoreV{2,3,4}GenImpl::visitInst* emits semaphore fields into the 64-B TPB slots
  -> per-engine <eng>.bin  ->  NEFF   (assembled by ib_create_one_block, Assembly Pipeline)
```

The per-gen Psumbuf classes the backend allocates into are confirmed by `nm`:
`InferentiaPsumbuf::InferentiaPsumbuf() @0x1734670` (CoreV1/tonga),
`SundaPsumbuf @0x1734a60` (CoreV2), `CaymanPsumbuf @0x1734e50` (CoreV3),
`CoreV4Psumbuf @0x1735250` (CoreV4) — each carries that gen's arch partition/bank counts
(base `Psumbuf::Psumbuf(unsigned, unsigned, unsigned)`).

---

## 7. End-to-end — a GPSIMD `tensor_tensor` op through the back end `[HIGH]`

For an int32 `tensor_tensor add` over `[128, F]`:

1. **Front-end** (CARRIED [SundaISel](sundaisel.md)): `nki.isa.tensor_tensor(int32, add)`, no
   operand in PSUM → `resolved_engine = gpsimd` → `TENSOR_TENSOR_ARITH 0x41`,
   `NeuronEngine = Pool`. A custom-op instead → `EXTENDED_INST 0xf0`, linked into the NEFF as
   the ucode lib (`visitInstCustomOp`).
2. **Tile** (§2): `SundaSizeTiling` → partition axis = 128 (8 cores × 16), free axis tiled to
   `max_computation_tile_size_in_bytes` / rate-matched; `packInstsToSIMDMacro` packs the fused
   DAG into one `NeuronMacro`. Tile ceiling = the PSUM bank — but GPSIMD lives entirely in
   SBUF (176 KiB usable).
3. **Place** (§4): `InferPSumTensor` leaves the GpSimd result in SBUF (never PSUM, §4.4);
   `AllocationDecision` classes top-level vs local; `AllocateBlocks` builds stack-discipline
   live intervals; `LinearScanStackAllocator::assign_sb_addr` gives each SBUF tile a
   `(start_partition, byte_addr)` within `[0, 176 KiB)`, quadrant-aligned (§3). No PSUM bank is
   ever assigned; overflow spills SBUF→HBM via `MaxLiveSpiller`.
4. **Sync + schedule** (§6): `InsertCoreBarrier` inserts a barrier if this op's SBUF tensors
   RAW/WAR/WAW-conflict across a loop-nest; the Python `Scheduler` orders the POOL-engine
   stream by the ready-list heuristic priced by `InstructionLatencyModel` (the ~150-cycle
   `GPSIMD_START` biases a small GpSimd tile later/coarser); `AntiDependencyAnalyzer-SB`
   recomputes the SB hazards; `TimeAwareScheduler::linearize_insts_by_simulation` linearizes;
   `AllocateSemaphores::assignDMASemaphore` + `AllocSemaphores::setupSync` attach the
   wait/update that orders this POOL op vs the DMA staging its SBUF inputs and the consumer
   reading its output.
5. **Emit** (CARRIED [Assembly Pipeline](../neff/assembly-pipeline.md)): `CoreV2GenImpl` emits
   the `0x41` (or `0xf0`) descriptor + the semaphore fields into the POOL engine's 64-byte TPB
   slots → `pool.bin` → NEFF. The custom-op device code rides as the ucode lib, loaded into
   the Q7 IMEM and dispatched through the POOL engine.

---

## 8. Adversarial self-verification of the five strongest claims

Each claim re-grounded against the binary; a sceptic's re-run is given. (Two earlier
draft "corrections" were themselves wrong — caught here — so the surviving corrections in §9
are only the byte-confirmed ones.)

1. **"The tile-size ceiling is one PSUM bank."** Re-run: `strings SundaSizeTiling.so | rg
   'size of PSUM buffer'` → the docstring fragment *"is mainly coming from the size of PSUM
   buffer (i.e. the inner most and smallest [buffer])"*. The bank size 2048 B = 512 fp32 is
   `private_nkl/utils/kernel_helpers.py: PSUM_BANK_SIZE = 2048` + `psum.pyi`
   *"512\*sizeof(fp32)=2048"* + *"cannot span multiple PSUM banks"*. **Three independent
   anchors, all verbatim. Holds.** Caveat: the *exact* per-op tile size is a `.so` immediate
   (INFERRED ≤ the ceiling), flagged §2.3.

2. **"GPSIMD cannot touch PSUM; custom-op operands are SBUF *or HBM*."** Re-run: `strings -n 8
   libwalrus.so | rg 'cannot access PSUM'` → `GPSIMD engine cannot access PSUM` +
   `GPSIMD Instructions cannot access PSUM. Assign to a different Engine or move data to SB.`;
   `rg 'customop must be located'` → `All args to a customop must be located in SBUF or HBM`;
   and `isa/__init__.pyi` carries *"Since GpSimd Engine cannot access PSUM, the input or output
   tiles cannot be in PSUM"*. **The "or HBM" defeats the naive "SBUF-only" reading — the one
   real CORRECTION (§4.4), confirmed verbatim across both binary and stub. Holds.**

3. **"The SBUF allocator emits a byte cursor; PSUM emits `(bank_id, start_partition,
   byte_addr)`."** Re-run: `psum.pyi` returns *"a tuple of three integers (bank_id,
   start_partition, byte_addr)"*; `sbuf.pyi` returns *"a tuple of two integers
   (start_partition, byte_addr)"*; `StackAllocator.so` carries `assign_psum_addr`,
   `assign_sb_addr` (with the docstring *"assign only byteaddr to SB tensors"*) as
   `LinearScanStackAllocator` methods. **Two address kinds, two stub signatures, two methods,
   one anchoring docstring. Holds.**

4. **"The dep analyzer is partitioned by memory address space (separate SB/PSUM instances and
   ranges)."** Re-run: `strings libwalrus.so | rg 'AntiDependencyAnalyzer-'` →
   `AntiDependencyAnalyzer-SB` *and* `AntiDependencyAnalyzer-PSUM` instance strings, and
   `nm | c++filt` → distinct `getSBPartitionRange(...)` and `getPSUMPartitionRange(...)`
   methods + `bir::MemoryAddressSpace` ctor template params. **Both the per-class instance
   names and the per-space getter methods are real. Holds.**

5. **"`GPSIMD_START` ≈ 150 cycles drives the cost model's GpSimd pricing."** Re-run:
   `rg 'GPSIMD_START' isa/__init__.pyi` → *"`GPSIMD_START` is the instruction startup overhead
   on GpSimdE, roughly 150 engine cycles"* + a constant-build op costing *"`150 + N` GpSimd
   Engine cycles"*; the latency getter `get_intrinsic_latency` exists in
   `InstructionLatencyModel.so` (docstring *"A cycle based cost model that uses instruction
   latency as the cost"*), and `get_pipeline_ii` in `KernelLatencyInfo.so`. **The 150-cycle
   constant is byte-read from the ISA pyi; both getters exist in the split cost
   model (§6.2). Holds.**

---

## 9. Confidence ledger & corrections

**HIGH / OBSERVED** (byte-exact): the `SundaSizeTiling` docstring + the SIMD-macro
tiling surface (`simd_partition_axes`, `simd_free_axes`, `extractAxesForSIMDMacro`,
`packInstsToSIMDMacro`, `partition_max_elts`, `max_computation_tile_size_in_bytes`,
`low_psum_usage_threshold` + its docstring, `hull_size`, `coalesce_dim`); the geometry (128
partitions / 176 KiB usable / 8 PSUM banks × 2048 B = 512 fp32 / 8 GpSimd cores × 16
partitions / 32-quadrant rule) from `kernel_helpers.py`, `sbuf.pyi`, `psum.pyi`, `isa.pyi`,
`tile_size` + `Tonga.so` getters; the allocator chain docstrings (`AllocationDecision`,
`AllocateBlocks`, `NeuronStackLiveInterval`, `LinearScanStackAllocator` +
`assign_sb_addr`/`assign_psum_addr`/`expire_intervals`, `ModuloAllocation`, `SpillPSum`); the
GPSIMD-no-PSUM + customop-SBUF-or-HBM verifier strings + `InferPSumTensor` docstring +
`can_write_to_psum`/`can_read_from_psum`/`force_tensortensor_psum_sb`; the Python `Scheduler`
DDG symbols; `InstructionLatencyModel`/`KernelLatencyInfo` docstrings + getters (the split);
the `AntiDependencyAnalyzer-SB`/`-PSUM` instances + per-space getters + the dep-enum table;
`TimeAwareScheduler` / `LncAwareScheduler` methods (with addresses) +
`bir.scheduled.after-ooo.json`; `AllocSemaphores` / `AllocateSemaphores` / `InsertPTCOMFlat`
methods (with addresses) + the EventSemaphore wire limit; `InsertCoreBarrier` docstrings; the
per-gen Psumbuf classes (with addresses); `checkPSUMLegality` / `checkOutputFitsInPSUM` /
`base_partition_constrained` (with addresses).

**MED / INFERRED**: the exact `partition_max_elts` / `max_computation_tile_size` arithmetic
(`.so` immediates, not strings — the chosen tile is ≤ the OBSERVED ceiling); the scheduler
heuristic *formula* (terms OBSERVED, combination INFERRED); the GPSIMD-only-SB-range
classification (INFERRED from the SB-only rule + the per-class instances).

**LOW / CARRIED**: the 256 KiB SBUF partition stride and the SBUF/PSUM disjoint-aperture
physical proof (CARRIED [SBUF/PSUM Bank Model](../dma/sbuf-psum-banks.md)); the on-device
64-byte TPB-slot semaphore wire encoding (CARRIED [Assembly Pipeline](../neff/assembly-pipeline.md)).

**Corrections issued** (only the byte-confirmed ones survive):

| # | claim corrected (earlier synthesis) | truth |
| - | --- | --- |
| 1 | customop operands must be in **SBUF** | verifier: *"…must be located in **SBUF or HBM**"* (both args and outputs) — PSUM excluded, HBM allowed |
| 2 | the verifier ` (must be <= 176)` string is the SBUF usable-byte bound | that string is a **DMA-engine count** message; the 176 KiB usable bound is grounded by `sbuf.pyi` (*"192KiB-16KiB=(192-16)\*1024"*), not that string |

> **CORRECTION (self, vs an intermediate draft of this page).** An intermediate draft of this
> page asserted that `NUM_HW_PSUM_BANKS`/`PSUM_BANK_SIZE` did not exist, and that the
> `InferPSumTensor` "Assign result of Pool/Act" docstring, the `TimeAwareScheduler`
> `linearize_insts_by_simulation`/`reschedule_cc_related_inst` symbols, and the
> `assignDMASemaphore`/`assignEngineSemaphore` symbols were absent — all on the strength of
> *incomplete* `nm`/`strings` greps. A second pass found every one of them present
> (`kernel_helpers.py` at the `private_nkl/utils/` path, the docstring in `InferPSumTensor.so`,
> the scheduler symbols in `nm -D`, and `AllocateSemaphores::assignDMASemaphore @0x1138f10`).
> Those four spurious "corrections" were removed; the earlier synthesis was right on each.
> The lesson: ground a *negative* claim against `nm -D` **and** the full path tree before
> publishing it. `[HIGH/OBSERVED]`

---

*The geometry these passes target: [SBUF + PSUM Bank Model](../dma/sbuf-psum-banks.md). The
keystone GPSIMD-no-PSUM rule: [Keystone Facts](../orientation/keystone-facts.md). The sync
fields they emit, assembled into the NEFF: [Assembly Pipeline](../neff/assembly-pipeline.md).
The pass roster + `emit_*→opcode`: [The GPSIMD-Relevant Compiler Map](compiler-map.md). The
inter-engine sync detail: [Optimization + Inter-Engine Sync Insertion](opt-sync-insertion.md).*
