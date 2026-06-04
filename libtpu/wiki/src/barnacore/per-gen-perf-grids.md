# BarnaCore Performance Grid

> *Every constructor address, `new` size, latency-array store, resource-grid cell, and accessor read-path on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol is a demangled C++ name. `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. All addresses are VMA. Other versions differ.*

## Abstract

`xla::pufferfish::PufferfishBarnaCorePerformance` is the cost-model grid for BarnaCore — the legacy embedding accelerator that Pufferfish (TPU v4) is the last generation to ship (see [Retirement Evidence](retirement.md)). It is the **variant-1 half** of the Pufferfish latency table: `LatencyTablePufferfish` prices a `std::variant<PufferfishPerformance, PufferfishBarnaCorePerformance>`, where variant 0 is the dense TensorCore grid (336×20, documented on [`performance-pf`](../cost/performance-pf.md)) and variant 1 is the embedding-engine grid documented here. The two are reached through the same `std::variant` visitor; an LLO opcode lands in one or the other by the high-16 variant tag that `GetPufferfishInstruction` packs into its return. This page is the BarnaCore-specific counterpart to the per-gen `performance-*` family — the narrowest grid in the entire cost model.

The object is the same two-array shape as every newer-gen `Performance` (the layout framed on [`performance-overview`](../cost/performance-overview.md)): a flat `int32 latency[134]` heap array read by `GetLatency(instr)`, and a 2-D `Instruction × Resource` occupancy grid read by `GetResourceUsage(instr, res)`. What makes BarnaCore distinctive is the **shape**: the latency array has only 134 entries (versus the TensorCore's 336), and the resource grid is **134 rows × exactly one column** — a single EUP/transcendental structural-hazard unit. Where the TensorCore variant-0 grid is 20 columns wide (and Viperfish 28, Ghostlite 31), BarnaCore tracks one resource. The embedding engine has no MXU, no transpose, no permute, no cross-lane reduce: the only intra-op port the scheduler must serialize is the Extended Unary Pipeline, and the grid says so by reserving column 0 on the 6 transcendental rows and nowhere else.

This page documents three structures, each recovered store-by-store from the constructor `PufferfishBarnaCorePerformanceC1 @0x1c8c38c0`: the **134-entry latency array** (the value distribution by band, and the named primitives at its high-latency rows); the **134×1 resource grid** (which 6 rows reserve the single EUP column, and the read path that hard-codes the column index); and the **per-gen EUP latency block** — the 6-cycle transcendental band (idx 0x77..0x7c) that is the BarnaCore EUP, one cycle cheaper than the TensorCore variant-0 EUP (7) and far cheaper than Ghostlite's (13/14). The thirteen LLO-classified primitives that feed grid rows through `GetPufferfishInstruction` are cross-linked to their op names on [`bcs-scalar-isa`](bcs-scalar-isa.md).

For reimplementation, the contract is:

- **The two-array object layout**: an `int32 latency[134]` heap array (`new 0x218`, memset `0xff` then fully overwritten) plus a 134-row grid of 1-wide `std::vector<int>` rows (`new 0xC90`, each row `new 4`, default `{0}`), with the exact constructor `new` sizes and field offsets.
- **The latency value distribution**: histogram `{0:1, 1:101, 2:21, 3:2, 4:2, 6:6, 12:1}` = 134, with the named rows — sync/wait/pop = 1, `SyncDoneRead` = 3, the EUP block = 6, `VectorStore` = 12.
- **The 134×1 resource grid**: a single EUP-unit column, reserved (`=1`) only by the 6 EUP-transcendental rows (idx 0x77..0x7c); every other row's single cell stays `{0}`.
- **The read paths**: `GetLatency` = `latency[instr]` with one bound check; `GetResourceUsage` = `grid[instr].data[res]` with two bound checks and a 24-byte (3-qword) row stride; the variant-1 visitor reads `instr` as a `uint8` byte and hard-codes `res = 0`.
- **The variant-1 dispatch**: how a BarnaCore LLO opcode reaches this grid through `GetPufferfishInstruction` (high-16 variant tag = 1) and the `std::variant` visitor — and why the 20 high-level embedding ops never do.

| | |
|---|---|
| **Class** | `xla::pufferfish::PufferfishBarnaCorePerformance` (non-polymorphic value type, 0x30 B) |
| **Constructor** | `PufferfishBarnaCorePerformanceC1Ev` `@0x1c8c38c0` |
| **Latency array** | `new 0x218` = 536 B = **134 `int32`**; `memset(_, 0xff, 536)` then all 134 overwritten |
| **Resource grid** | `new 0xC90` = 3216 B = **134 rows × 24 B** (`std::vector<int>`); each row `new 4` = **1 `int32`**, default `{0}` |
| **Read path** | `GetLatency` `@0x1c8c47e0`; `GetResourceUsage` `@0x1c8c4800` |
| **Grid shape** | 134 rows (`Instruction`, count `0x86`) × **1 column** (`Resource` = EUP unit); 6 populated cells |
| **EUP block** | latency idx `0x77..0x7c` = **6** (×6); the only rows reserving resource col 0 (`=1`) |
| **Highest latency** | idx `0x85` (`kBarnaCoreVectorStore`) = **12** — the embedding-row HBM write |
| **Classifier** | `GetPufferfishInstruction` `@0x1c8a1fe0` (variant-tagged; high-16 = 1 → this grid) |
| **Variant-1 visitor** | `__dispatcher<1ul>::__dispatch<…ResourceUsageFromInstruction>` `@0x1c8a31a0` (reads `uint8` instr, `res = 0`) |
| **Singletons** | `pf_shared` `@0x22579a10` (TensorCore), `pf_bc_shared` `@0x22579a20` (BarnaCore) |
| **Resource-count progression** | **BarnaCore 1** → PF-TC 20 → VF 28 → GL/GF 31 |
| **Confidence** | CONFIRMED (decompile-anchored, store-count integrity) unless a row says otherwise |

> **NOTE —** this page is the BarnaCore *Performance grid* — the per-instruction latency and resource arrays the cost model reads. The opcode → `Instruction`-ordinal classifier (the 13 LLO-classified primitives, the 20 channel-lowered embedding ops) and their op-class names live on [`bcs-scalar-isa`](bcs-scalar-isa.md); the per-gen EUP latency *integers* across all gens (PF 7 / VF 6 / GL 13-14) and the latency↔reservation orthogonality live on [`eup-per-gen-integers`](../cost/eup-per-gen-integers.md). This page pins the BarnaCore numbers and their layout.

---

## The Object: Two Heap Arrays

### Purpose

`PufferfishBarnaCorePerformance` is a value object holding two heap allocations and their bounds — the libtpu analog of one `SchedMachineModel` instance, but for the embedding engine rather than the dense datapath. The constructor builds both arrays inline; there is no `.td` table behind it. Recovering the cost model is exactly recovering what the constructor stores, which is what this page does store-by-store.

The two arrays answer two different scheduler questions. `latency[instr]` is the **data-dependency edge weight** — the minimum bundles a consumer must trail a producer of instruction `instr`. `grid[instr][res]` is the **resource occupancy** — how many cycles instruction `instr` holds micro-pipeline port `res`, fed into the bundle packer's `MaxResourceCycles` reduction. The two are read from different arrays by different accessors and never multiply; the orthogonality is structural at the array level (see [`eup-per-gen-integers`](../cost/eup-per-gen-integers.md)).

### Layout

```text
PufferfishBarnaCorePerformance  (0x30 bytes; field offsets in qwords)
  [+0x00]  latency_ptr   -> int32 latency[134]      (new 0x218 = 536 B)
  [+0x08]  latency_count = 134   (0x86)             // GetLatency bound
  [+0x10]  latency_cap   = 134                      // capacity mirror
  [+0x18]  grid_ptr      -> std::vector<int> rows[134]   (new 0xC90 = 3216 B = 134 × 24)
  [+0x20]  grid_count    = 134   (0x86)             // GetResourceUsage outer bound
  [+0x28]  grid_cap      = 134
  each grid row: { data_ptr -> int32[1], size=1, cap=1 }   // new 4 = 1 int, default {0}
```

The row count `134` is fixed by the `new 0x218` (536 ÷ 4 = 134) and corroborated three more times: `[+0x08]=[+0x10]=134` for the latency array, `[+0x20]=[+0x28]=134` for the grid, and the per-row alloc loop `for (i=16; i!=3232; i+=24)` which iterates `(3232−16)/24 = 134` times. A reimplementer must size both arrays to exactly 134 (`BarnaCorePerformance::Instruction` cardinality) and each grid row to width 1.

### Algorithm

```c
function PufferfishBarnaCorePerformanceC1(this):              // 0x1c8c38c0
    // --- latency array: 134 int32, sentinel then overwrite ---
    this[0]    = new(0x218)                  // 536 B = 134 int32
    this[+2]   = 134                          // capacity mirror
    memset(this[0], 0xff, 536)                // every slot = 0xffffffff sentinel
    this[+1]   = 134                          // latency_count (GetLatency bound)

    // --- resource grid: 134 rows of 1-wide vector<int>, default {0} ---
    grid = new(0xC90)                         // 3216 B = 134 × 24 (vector<int> header)
    this[+3]   = grid
    this[+5]   = 134
    for (i = 16; i != 3232; i += 24):         // 134 rows; i offsets the row's {ptr,size,cap}
        row = new(4)                          // 1 int32
        grid[i-16] = row                      // row.data
        grid[i]    = 1                         // row.cap
        *row       = 0                         // default cell value {0}
        grid[i-8]  = 1                         // row.size
    this[+4]   = 134                          // grid_count (GetResourceUsage outer bound)

    // --- fill latency[]: bound-checked, sequential idx 0..0x85 ---
    latency[0x00] = 1                          // (most ops default to 1)
    latency[0x01] = 0                          // no-op / null slot
    latency[0x02 .. 0x21] = 1
    latency[0x22] = 2
    ...                                        // see TABLE BC-L for the full distribution
    latency[0x77 .. 0x7c] = 6                  // the EUP/transcendental block
    // for each EUP row, ALSO reserve grid column 0:
    //   grid[0x77..0x7c].data[0] = 1          // the only resource cells written
    latency[0x85] = 12                         // kBarnaCoreVectorStore (HBM write)
    return this
```

Every store is bound-checked against `this[+1]` (`BUG()` on overflow) for the latency array, and the 6 EUP-row grid stores are bound-checked against `this[+4]` and the per-row size — the store-count integrity that proves the dump is complete (see [§ Store-Count Integrity](#store-count-integrity)).

### Function Map

| Symbol | Address | Evidence |
|---|---|---|
| `PufferfishBarnaCorePerformance::PufferfishBarnaCorePerformance` (ctor) | `0x1c8c38c0` | `new 0x218` latency, `new 0xC90` grid, 134 stores |
| `PufferfishBarnaCorePerformance::GetLatency` | `0x1c8c47e0` | `latency[instr]`, bound `[a1+8]` |
| `PufferfishBarnaCorePerformance::GetResourceUsage` | `0x1c8c4800` | `grid[instr].data[res]`, 3-qword stride |
| `__dispatcher<1ul>::__dispatch<…ResourceUsageFromInstruction>` | `0x1c8a31a0` | variant-1 arm; `uint8` instr, `res = 0` |
| `GetPufferfishInstruction` | `0x1c8a1fe0` | LLO opcode → `Instruction` + high-16 variant tag |
| `GetSharedPufferfishBarnaCorePerformance::pf_bc_shared` | `0x22579a20` | variant-1 singleton (guard `@0x22579a28`) |

---

## The Read Paths

### GetLatency — one array, one bound

`GetLatency(instr)` is the canonical 4-instruction grid-family read: bound-check `instr` against the count at `[+0x08]`, then return `latency[instr]`. It is byte-identical to the TensorCore (`@0x1c8c3860`), Viperfish, and Ghostlite `GetLatency` — only the array it reads differs.

```c
function GetLatency(this, instr):              // 0x1c8c47e0
    if (this[+1] <= instr):  BUG()             // bound: latency_count
    return latency_ptr[instr]                   // int32, 4-byte indexed
```

> **QUIRK — `instr` is a `uint8` here, a `uint16` on the TensorCore side.** The variant-1 dispatcher reads the BarnaCore `Instruction` as a single byte (`movzx ...,BYTE`), because the BarnaCore enum is ≤ 134 values and fits in 8 bits; the TensorCore variant-0 dispatcher reads a 16-bit `WORD` for its 336-value enum. A reimplementer who reads the wrong width on the wrong arm will misindex the array.

### GetResourceUsage — grid lookup, two bounds, hard-coded column

`GetResourceUsage(instr, res)` walks the 2-D grid: bound-check `instr` against the outer count at `[+0x20]`, compute the 24-byte (3-qword) row stride, bound-check `res` against the row's `size`, then return `grid[instr].data[res]`.

```c
function GetResourceUsage(this, instr, res):    // 0x1c8c4800
    if (this[+0x20] <= instr):  BUG()           // outer bound: grid_count
    grid = this[+0x18]
    row  = grid + 24*instr                       // 3 qwords per row: {data, size, cap}
    if (row.size <= res):  BUG()                 // inner bound: row width (= 1)
    return row.data[res]                         // int32, 4-byte indexed
```

> **NOTE — the variant-1 visitor hard-codes `res = 0`.** The `std::variant` dispatcher `__dispatcher<1ul>::__dispatch<…ResourceUsageFromInstruction>` (`@0x1c8a31a0`) calls `GetResourceUsage(perf, *instr_byte, 0)` — the third argument is the literal `0`. Because the BarnaCore `Resource` enum is exactly one wide, there is no other column to ask for; the cost model only ever queries the single EUP-unit cell. (The visitor also guards `variant_index == 2` before dispatching, the BarnaCore arm's tag.) There is no `kResources` / `GetResources` symbol for BarnaCore at all — unlike Ghostlite's `kResources @0xb43cdc4` (31 entries) — because a 1-wide enum needs no permutation table.

---

## The 134-Entry Latency Array

### Purpose

The latency array is the data-dependency depth of every BarnaCore `Instruction`. It is dominated by single-cycle scalar and cheap-vector ops; the only bands worth a reimplementer's attention are the small set of non-1 rows below. The full array is 134 entries but only 33 carry a non-1 value, so the table describes the *bands*, not all 134 rows.

### TABLE BC-L — Non-Default Latency Rows

All 134 entries are written (the leading `memset 0xff` is fully overwritten); every row not listed below is `1`. `Instr idx` is the `BarnaCorePerformance::Instruction` ordinal; byte offset in the array = `idx × 4`. The "primitive" column names the row only where `GetPufferfishInstruction` classifies an LLO opcode onto it (see [`bcs-scalar-isa`](bcs-scalar-isa.md) TABLE B); unnamed rows are channel-vector ALU ops reached via the channel emitter, not the LLO classifier.

| Instr idx | latency | primitive / band |
|---|---:|---|
| `0x01` | 0 | no-op / null slot |
| `0x22` | 2 | channel-vector binary/compare ALU |
| `0x33..0x38` | 2 (×6) | channel-vector binary/compare group |
| `0x3a, 0x3b` | 4 (×2) | mid-cost vector pack/select band |
| `0x3d` | 3 | `kBarnaCoreScalarSyncDoneRead` (sync-completion read) |
| `0x3f` | 3 | (vector op) |
| `0x41, 0x42` | 2 (×2) | (vector binary/compare) |
| `0x4c` | 2 | (vector op) |
| `0x50` | 2 | (vector op) |
| `0x5e..0x63` | 2 (×6) | channel-vector binary/compare group |
| `0x66` | 2 | (vector op) |
| `0x77..0x7c` | **6 (×6)** | **the EUP/transcendental block** — the only rows reserving the resource grid |
| `0x7d, 0x7e` | 2 (×2) | (vector op) |
| `0x82` | 2 | (vector op) |
| `0x85` | **12** | `kBarnaCoreVectorStore` — the embedding-row HBM write |

Histogram (byte-exact from the constructor): `{0: 1, 1: 101, 2: 21, 3: 2, 4: 2, 6: 6, 12: 1}` = **134**.

### Interpretation

The shape is the embedding engine in one table. **101 of 134 ops are single-cycle** — the scalar sync/wait/pop/move primitives and the cheap channel-vector ALU ops, the bulk of an embedding kernel's instruction mix. **21 are two-cycle** — the channel-vector binary/compare/pack groups (the contiguous bands `0x33..0x38` and `0x5e..0x63` are 12 of them). The **6-cycle band (`0x77..0x7c`) is the EUP**, the deepest compute pipe BarnaCore has. The single **12-cycle row (`0x85`, `kBarnaCoreVectorStore`)** is the highest latency in the whole model — the embedding-row store back to HBM-side memory, the operation that defines an embedding gather's critical path.

> **QUIRK — there is no matmul/transpose band.** The TensorCore variant-0 array (336 entries) has a deep MXU band (matmul ≈ 83/101, transpose ≈ 126). The BarnaCore array tops out at 12 because the embedding engine has no MXU and no transpose unit; the wide work it does (gather, scatter, sparse-reduce) is *lowered* into the cheap primitives above, not priced as a single deep op (the 20 high-level ops `LogFatal` in the classifier — see [`bcs-scalar-isa`](bcs-scalar-isa.md)). A reimplementation that expects a matmul row in this array is in the wrong variant.

---

## The 134×1 Resource Grid

### Purpose

The grid is the structural-hazard model: how many cycles each instruction holds each micro-pipeline port. For BarnaCore there is **exactly one port** — the EUP (Extended Unary Pipeline) transcendental unit. The grid is therefore 134 rows of a single column, and only the 6 EUP-transcendental rows write a non-zero cell. Every other instruction holds no tracked resource; its single cell stays at the loop-default `{0}`.

### TABLE BC-R — The One Resource Column

The grid is allocated as 134 rows, each a `std::vector<int>` of width 1 default-initialized to `{0}`. The constructor writes exactly 6 cells — all column 0, on the EUP rows. Row `r`'s data lives at `grid_base + r × 24`; the EUP rows are at offsets 2856, 2880, 2904, 2928, 2952, 2976 (= `0x77..0x7c × 24`), each store guarded by the per-row size check.

| Resource column | populated rows | value | occupant |
|---|---|---:|---|
| `r0` (EUP unit) | `0x77..0x7c` (6 cells) | 1 | the 6 EUP transcendentals reserve the EUP unit 1 cycle each |
| `r0` | all other 128 rows | 0 | no tracked structural hazard |

### Interpretation

The single column is the whole story: **BarnaCore's only structural hazard the cost model tracks is the EUP**. Contrast the resource-count progression across the grid family (read from each gen's `kResources` and grid width — see [`resource-enum`](../cost/resource-enum.md)):

| Grid | Resource columns | what they track |
|---|---:|---|
| **PufferfishBarnaCore (variant 1)** | **1** | the EUP unit only |
| Pufferfish TensorCore (variant 0) | 20 | MXU matmul/matprep/transpose/permute/reduce/CCF/RNG micro-pipeline ports |
| Viperfish | 28 | + wider MXU and Xlu ports |
| Ghostlite (v6e) | 31 | + the v6e micro-pipeline extensions |

The embedding engine's resource model is an order of magnitude narrower than the dense TensorCore's because it has none of the dense micro-pipeline: no systolic MXU, no transpose latch, no cross-lane permute. The one pipe it has that can stall a back-to-back issue is the transcendental EUP, so that is the one cell the bundle packer's `MaxResourceCycles` reduction consults for a BarnaCore bundle.

> **GOTCHA — the resource cell (`=1`) is the per-push *occupancy*, not the EUP *latency*.** Row `0x77..0x7c` carries latency 6 (TABLE BC-L) *and* a resource cell of 1 (TABLE BC-R). They are two different numbers read from two different arrays for two different questions: the latency-6 is the push→pop data-dependency depth a consumer must wait; the resource-1 is how many bundles the EUP port stays reserved (the back-to-back push spacing). The scheduler composes them as `max(latency-deadline, resource-availability)`, never as a product. The same orthogonality holds on every gen — see [`eup-per-gen-integers`](../cost/eup-per-gen-integers.md). (Whether the BarnaCore EUP's issue-spacing scalar `VectorEupReservationCycles` differs from this per-push cell of 1 was not separately isolated — **LOW confidence** on the back-to-back spacing accessor for BarnaCore specifically.)

---

## The Per-Gen EUP Latency Block

### Purpose

The 6-cycle band at idx `0x77..0x7c` is the BarnaCore EUP — the six channel transcendentals the cost model prices. It is the BarnaCore entry in the cross-gen EUP latency progression, and the reason this grid exists as a separate variant: the embedding engine has its *own* transcendental latency, distinct from the dense TensorCore's.

### The Six Functions and the Cross-Gen Comparison

The 6-entry width matches the BarnaCore channel's six-function transcendental block: `VectorReciprocalSquareRoot` (rsqrt), `VectorPow2` (2^x), `VectorLog2`, `VectorTanh`, `VectorReciprocal` (1/x), and `VectorRelux` (ReLU-with-upper-clamp). These reach the `Instruction` enum through the channel emitter's `MigrateInstruction<Alu0_VectorX, Alu1_VectorX>` templates rather than `GetPufferfishInstruction`, so the *block* is byte-confirmed (6 entries, latency 6, the only 6 EUP-resource cells) but the *within-block ordinal permutation* — which of `0x77..0x7c` is Tanh vs Pow2 vs Log2 vs Rsqrt vs Recip vs Relux — was not pinned (**LOW confidence** on the per-ordinal mapping; the block identity is CONFIRMED).

> **QUIRK — BarnaCore swaps `pushErf` for `Relux`.** The TensorCore variant-0 EUP block is `{rsqrt, pow2, log2, tanh, recip, pushErf}`; BarnaCore's is `{rsqrt, pow2, log2, tanh, recip, relux}`. The embedding accelerator prices a ReLU-clamp transcendental (an embedding-activation primitive) in the slot the dense engine spends on an `erf` push. A reimplementation that mirrors the TensorCore block onto BarnaCore will be one function wrong.

| Gen (grid) | EUP push latency | source | datatype split |
|---|---:|---|---|
| **PufferfishBarnaCore (variant 1)** | **6** | `latency[0x77..0x7c]` (this page) | uniform |
| Pufferfish TensorCore (variant 0) | 7 | `latency[0x67..0x6c]` | uniform |
| Viperfish | 6 | `latency[0xcc..0xd2]` | uniform |
| Ghostlite | 13 (F32) / 14 (BF16) | `latency[0x106..0x118]` | per-datatype |
| EUP pop (all gens) | 1 | pop ordinal | — |

The BarnaCore EUP is **one cycle cheaper than the Pufferfish TensorCore EUP** (6 vs 7) — structurally consistent with the leaner 134-entry embedding ISA — and dramatically cheaper than Ghostlite's 13/14, which reflects Ghostlite's deeper, FIFO-buffered transcendental unit. The per-gen EUP integers across the full family, the F32/BF16 split rationale, and the latency↔reservation decomposition are owned by [`eup-per-gen-integers`](../cost/eup-per-gen-integers.md); this page supplies the BarnaCore row.

---

## How an Opcode Reaches This Grid

### The Variant Dispatch

A BarnaCore LLO opcode reaches `PufferfishBarnaCorePerformance` through the same `std::variant<PufferfishPerformance, PufferfishBarnaCorePerformance>` machinery that prices every Pufferfish instruction. `GetPufferfishInstruction @0x1c8a1fe0` classifies the LLO opcode (jump table `@0xb43927c`, `idx = opcode − 2`) and packs **`low16 = Instruction`, `high16 = variant tag`** into its return. `LatencyTablePufferfish::LatencyBetweenInternal` extracts the variant tag (`shr ...,0x10`) and indexes a 2-arm function-pointer table: tag 0 → the TensorCore grid, tag 1 → this BarnaCore grid (`pf_bc_shared @0x22579a20`).

```text
  LLO opcode (0x1ac..0x1cc for kBarnaCore*)
        |
        v
  GetPufferfishInstruction  @0x1c8a1fe0   -> { low16: Instruction, high16: variant }
        |                                          variant == 1 for the 13 BarnaCore primitives
        v
  std::variant visitor  (high16 selects the arm)
        |
        +-- tag 0 -> PufferfishPerformance         (TensorCore grid, 336x20)
        +-- tag 1 -> PufferfishBarnaCorePerformance (this grid, 134x1)
                          |
                          +-- GetLatency(instr)             -> latency[instr]
                          +-- GetResourceUsage(instr, 0)    -> grid[instr].data[0]
```

### Which Opcodes Reach It

Exactly **13 LLO opcodes** route to variant 1 — the scalar sync/wait/pop and vector load/store primitives `0x1ac..0x1cc` (the `C`-classified rows on [`bcs-scalar-isa`](bcs-scalar-isa.md) TABLE B). Their `Instruction` ordinals (`0x04`, `0x05..0x0b`, `0x3d`, `0x3e`, `0x84`, `0x85`) index the latency array directly: e.g. `kBarnaCoreScalarSyncDoneRead` → `0x3d` (latency 3), `kBarnaCoreVectorStore` → `0x85` (latency 12). The other **20 `kBarnaCore*` opcodes** (gather/scatter/sparse-reduce/FSM/remote-buffer) are *not* classified into this grid — they `LogFatal` in `GetPufferfishInstruction` ("Pufferfish does not support: … is unsupported for Pufferfish.") and are lowered through the channel/sequencer emitter into the 13 priced primitives. The EUP-block rows (`0x77..0x7c`) and the 2-cycle vector groups are likewise reached via the channel emitter's `MigrateInstruction` templates, not the LLO classifier — they are priced when the channel-vector ALU ops they back are emitted.

> **NOTE — the 20 high-level ops carry NO standalone latency in this grid.** Their cost is the runtime-dynamic sum of the primitives they expand into (a `LocalGather` ≈ 10 scalar ALU + 2 DMA + sync; a `GlobalScatterGradients` ≈ a column loop of partition math + remote DMA). A cost model must walk the `BcsLloEmitter` expansion ([`bcs-scalar-isa`](bcs-scalar-isa.md)), not look up the high-level op in this array — which is precisely why those 20 opcodes `LogFatal` in the direct classifier.

---

## Store-Count Integrity

The dump is provably complete by the same store-count method used across the grid family. The constructor emits exactly the stores the layout requires, with no third store idiom (no `push_back`, no vector-grow — the only allocations are the two `new` + the per-row `new` loop):

| Store class | count |
|---|---:|
| latency-array stores | 134 |
| grid EUP-row stores | 6 |
| per-row default-init stores | 134 |

The 134 latency stores reconcile exactly with the histogram `{0:1, 1:101, 2:21, 3:2, 4:2, 6:6, 12:1}`; the 6 grid stores are all column 0 on the EUP rows; the 134 row-default stores are the loop body. Every store is bound-checked (`BUG()` on overflow) against the count fields, so the array dimensions (134 / 134×1) are self-evidenced by the constructor's own checks. Spot cells verified byte-exact against the decompile: `latency[0x22]=2` (offset 136), `latency[0x3a]=4` (offset 232), `latency[0x3d]=3` (offset 244), `latency[0x77]=6` (offset 476), `latency[0x85]=12` (offset 532), and `grid[0x77].data[0]=1` (row offset 2856).

---

## Considerations

- **The variant tag selects the array; read it before you index.** A BarnaCore opcode and a TensorCore opcode can share a numeric `Instruction` value; only the high-16 variant tag from `GetPufferfishInstruction` disambiguates which 134- or 336-entry array to index. A reimplementation that drops the variant tag will index the wrong grid.
- **The BarnaCore `Instruction` is a `uint8`; the TensorCore is a `uint16`.** The variant-1 dispatcher reads a single byte (`@0x1c8a31a0`). Reading a `WORD` on the BarnaCore arm walks off the intended index.
- **The resource grid is 1-wide and column 0 is the only column.** `GetResourceUsage` is always called with `res = 0` from the variant-1 visitor. There is no `kResources` permutation table for BarnaCore; do not look for one.
- **The 6-cycle EUP block's within-band ordinal map is LOW confidence.** The block is byte-confirmed (6 entries, latency 6, the only EUP-resource cells, identity = the 6 channel transcendentals incl. `Relux`), but the exact ordinal-to-function permutation within `0x77..0x7c` (Tanh vs Pow2 vs …) and the per-ordinal names of the 2-cycle vector groups (`0x33..0x38`, `0x5e..0x63`) were not pinned — they are reached through the channel emitter's `MigrateInstruction` templates, which have no jump-table classifier to read the ordinal from.
- **The latency-6 EUP cell and the resource-1 EUP cell are different numbers.** Latency 6 is the push→pop data deadline; resource 1 is the EUP-port hold (issue spacing). They live in different arrays and compose as a `max`, not a product. Whether the BarnaCore EUP issue-spacing scalar matches this cell of 1 (PF TensorCore uses 2 = half-rate) was not separately isolated — LOW confidence.
- **Pufferfish is the only gen that ships this variant.** From Viperfish onward BarnaCore is retired ([Retirement Evidence](retirement.md)); no later `Performance` object has a BarnaCore arm. A reimplementation targeting v5+ does not need this grid at all.

---

## Related Components

| Name | Relationship |
|---|---|
| `PufferfishPerformance` (variant 0) | the TensorCore grid the same `LatencyTablePufferfish` prices, 336×20 — [`performance-pf`](../cost/performance-pf.md) |
| `LatencyTablePufferfish` | the `std::variant<TensorCore, BarnaCore>` holder that selects this grid by variant tag |
| `GetPufferfishInstruction` `@0x1c8a1fe0` | the LLO opcode → `Instruction` + variant-tag classifier |
| `PufferfishBarnaCoreChannelEmitter` | emits the channel-vector ALU ops (incl. the 6 EUP transcendentals) that back the 6-cycle block |

## Cross-References

- [BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md) — the 13 LLO-classified primitives that feed this grid's rows, their op-class names, and the 20→13 embedding-op lowering that explains the `LogFatal` rows.
- [BarnaCore Overview](overview.md) — what BarnaCore is, the BCS/BCAH personality split, and where the Pufferfish embedding engine sits in the pipeline.
- [Retirement Evidence](retirement.md) — why Pufferfish is the last gen with this grid; the narrow 1-column resource model is part of the functional-gap argument.
- [Merged-ALU Bit Layout](merged-alu.md) — the channel vector/scalar ALU lineage the EUP-block and 2-cycle vector rows belong to.
- [Performance: PF](../cost/performance-pf.md) — the TensorCore variant-0 grid (336×20); this page is its variant-1 counterpart, reached through the same visitor.
- [Performance: GL (GhPerf 476×31)](../cost/performance-gl-ghperf.md) — a later, wider grid (476×31) for contrast on shape and resource count.
- [Performance Overview](../cost/performance-overview.md) — the shared two-array grid layout and `GetResourceUsage` read path common to all grid-family gens.
- [EUP Per-Gen Latency Integers](../cost/eup-per-gen-integers.md) — the BarnaCore EUP-6 in the cross-gen progression (PF 7 / VF 6 / GL 13-14) and the latency↔reservation orthogonality.
- [Resource Enum](../cost/resource-enum.md) — the cost-model `Resource` enum and the per-gen slot-count progression (BarnaCore 1 → PF-TC 20 → VF 28 → GL/GF 31).
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / BarnaCore (legacy v2–v4) — [back to index](../index.md)
