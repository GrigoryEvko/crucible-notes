# MXU Latency: GF (6acc60406 / Trillium)

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset; `.data` VMA − 0x400000 == file offset. All cell integers were read directly from the GF `MxuLatencyTable` constructor.*

## Abstract

This page dumps the **Trillium (v7, "GF" / `6acc60406`) `MxuLatencyTable`**: the per-modifier × `MxuResource` reservation matrix that prices how long each MXU op holds each internal sub-unit. It is the Trillium sibling of the Viperfish [`array<int,19>`](mxu-latency-vf.md) and Ghostlite [`array<int,11>`](mxu-latency-gl.md) matrices, read by the byte-identical model documented in [MXU Latency Overview](mxu-latency-overview.md). Trillium and Ghostlite share the *shape* (`array<int,11>`, the same modifier key types, the same `GainLatchMode` key helpers) but **not the instance**: the two generations are built by distinct constructors (`GlcCycleTable` vs `GfcCycleTable`), carry distinct base op-latency costs, and read the cells through distinct accessors. Everything below is the GF instance, allocated by `GfcCycleTable` and built by the constructor whose CHECK strings name `mxu_latency_table_gf.cc`.

Two facts make Trillium's numbers worth pinning. First, on a 256×256 systolic array the per-matmul hold is *half* Viperfish's: bf16 matmul holds the accumulate port for **4** cycles (VF: 8), so a back-to-back bf16 matmul stream pipelines at twice the VF issue rate. Second, the GF lookup reads the reservation array **directly** by `MxuResource` index — `array[3]` for matmul throughput, `array[8]` for matpush throughput — with **no** Ghostlite-style default-seed remap. The lookup also carries an `fp8-fnuz` (`F8E4M3Fn`, `F8E5M2`) GLM transform path that routes the fp8 latch modes into their own reservation buckets.

The page closes with `windowing_util::ComputeDmaLevels`, decompiled in full, because the Trillium matmul cost is multiplied by a DMA-efficiency factor whose input is the descriptor-fragment count this function computes. The fragment count buckets into one of `{1.6, 1.3, 1.1, 1.05, 2003.0}` (or `1.0` for a single contiguous transfer), and the bucketing is byte-exact.

For reimplementation, the contract is:

- The GF `MxuLatencyTable` object layout (`0xa0` bytes, maps at `this+0x00/0x20/0x40/0x60/0x80`) and the `array[res & 0xf] = cycles` insert mechanism with the `< 11` `kNumMxuResources` bound.
- The 16 matmul and 16 matpush reservation rows, cell-by-cell, plus the two Vlxmr rows and the `MatmulDataFormat → base-latency` cost map (`kF32`/`kBf16` = 211, `kF8E5M2`/`kF8E4M3Fn` = 204).
- The `GetResourceUsage` opcode dispatch (matmul 289/295/301/307; matpush 324–327 with the `^0xB`/`|0x30`/`|0x32` GLM pre-transforms), the direct `array[3]`/`array[8]` read, and the per-dtype throughput numbers.
- `ComputeDmaLevels`: the `DmaLevel` struct, the contiguity-break level-build loop, the `opcode_produced_register_type ∈ {2,4}` merge gate, and the fragment-count → efficiency-multiplier table.

| | |
|---|---|
| **Class** | GF/Trillium `MxuLatencyTable` (mis-symbolized; allocated by `GfcCycleTable`) |
| **Object** | `0xa0` B, at `GfcCycleTable this+0x18`; `array<int,11>` value type |
| **Ctor** | `@0x1c8bb1c0` — fills Matpush (`+0x00`), Matmul (`+0x20`), Vlxmr (`+0x40`), MatmulDataFormat→cost (`+0x80`) |
| **Lookup** | `@0x1c8bdb20` — direct `array[3]` (matmul) / `array[8]` (matpush); no remap |
| **Insert helpers** | matpush `@0x1c8bc2e0`, matmul `@0x1c8bc540`, vlxmr `@0x1c8bc760` — `array[res & 0xf] = cy` |
| **CT dispatch** | `GfcCycleTable::GetCyclesForThroughputHelper` `@0x1c89f400` |
| **Resource count** | `MxuResource::kNumMxuResources` = 11 — CHECK-anchored (`> 0xA` → fatal) |
| **Throughput (bf16 / fp8)** | matmul `array[3]` = 4 / 8 · matpush `array[8]` = 2 / 4 |
| **Base op latency** | `kBf16`/`kF32` = 211 · `kF8E5M2`/`kF8E4M3Fn` = 204 |
| **`ComputeDmaLevels`** | `@0x1c86a9e0` · efficiency buckets `{1.6, 1.3, 1.1, 1.05, 2003.0}`, `1.0` default |

---

## Object Layout and Build

### Purpose

The GF table is built once, when `GfcCycleTable` is constructed. The cycle table allocates two heap objects: a `0x30`-byte `GhostlitePerformance` table at `this+0x10` (the per-opcode grid of [Performance: GF](performance-gf-ghperf.md)) and the `0xa0`-byte `MxuLatencyTable` at `this+0x18`. Both are filled by their own constructor; this page covers the second.

```c
// GfcCycleTable::GfcCycleTable @0x1c89eec0
v4 = operator new(0x30u); sub_1C8D3740(v4);   // GhostlitePerformance grid → this+0x10
v6 = operator new(0xA0u); sub_1C8BB1C0(v6);   // MxuLatencyTable          → this+0x18
```

### Structure

The constructor `@0x1c8bb1c0` zero-inits five Abseil flat-hash-maps and a count word, then inserts every row:

```text
MxuLatencyTable (0xa0 bytes, at GfcCycleTable + 0x18)
  this + 0x00   flat_hash_map<MatpushModifier,     array<int,11>>   ── latch / matprep
  this + 0x20   flat_hash_map<MatmulModifier,      array<int,11>>   ── matmul
  this + 0x40   flat_hash_map<VlxmrModifier,       array<int,11>>   ── vector-latch-into-MRB
  this + 0x60   (Matres map slot — NOT populated by the GF ctor)
  this + 0x80   flat_hash_map<MatmulDataFormat, int>                ── base op latency; count word = 1
```

> **NOTE —** unlike the Ghostlite constructor (`@0x1c8b2920`, data-driven `SetReservations<Modifier>` + `insert_range`), the GF constructor writes the reservation arrays *inline* through three direct-write helpers, and it builds **no** separate Matres map (zero calls to a Matres helper). The `+0x60` slot is zeroed and left empty. A reimplementation that mirrors the GL build path will emit a Matres map GF never has.

### How a row is built

Each map value is an `array<int,11>` filled by `array[resource & 0xf] = cycles` for each `(resource, cycles)` pair, with the resource hard-bounded to `kNumMxuResources`:

```c
// matpush insert @0x1c8bc2e0 / matmul insert @0x1c8bc540 (identical mechanism)
function insert(key, pairs[]):
    array<int,11> res_vector = {0};                 // zero-init all 11 slots
    for (resource, cycles) in pairs:
        if (resource > 0xA)                          // > 10 → fatal
            LogFatal("resource_index < to_underlying(MxuResource::kNumMxuResources)");
        res_vector[resource & 0xF] = cycles;
    target_map.try_emplace(key, res_vector);
```

The `> 0xA` bound is the `11`-wide CHECK; the same `< 11` guard reappears on the read side (below) at `mxu_latency_table_gf.cc:415`. Any resource a row does not name holds it for 0 cycles.

---

## The Matmul Reservation Rows

### Purpose

The matmul family is keyed by a 32-bit `MatmulModifier` whose `byte[0]` is the `MatmulDataFormat` code (`1`, `2`, `9`, `0xa`), `byte[1]` is the transpose flag, and `byte[2]` is a high-variant bit. The constructor inserts 16 keys; `GetResourceUsage` reads cell `array[3]` (the matmul-accumulate throughput port).

### The rows (all 16 keys, cell-by-cell)

The reservation triple is `{res3, res9, res2}` for formats 1/2, and `{res3, res9}` for formats 9/0xa, byte-confirmed in the constructor at `@0x1c8bb7c9..0x1c8bbb31`.

| `MatmulModifier` key | format | `res2` (issue stage) | `res3` (**thru**) | `res9` (acc latch) | Confidence |
|---|---|---|---|---|---|
| `0x00000001` `0x00000101` `0x00010001` `0x00010101` | 1 (bf16) | 16 | **4** | 3 | CERTAIN |
| `0x00000002` `0x00010002` | 2 (bf16-alt) | 20 | **8** | 7 | CERTAIN |
| `0x00000102` `0x00010102` | 2, transpose | 16 | **4** | 3 | CERTAIN |
| `0x00000009` `0x00010009` | 9 (F8E5M2) | — | **8** | 7 | CERTAIN |
| `0x00000109` `0x00010109` | 9, transpose | — | **2** | 1 | CERTAIN |
| `0x0000000a` `0x0001000a` | 0xa (F8E4M3Fn) | — | **8** | 7 | CERTAIN |
| `0x0000010a` `0x0001010a` | 0xa, transpose | — | **2** | 1 | CERTAIN |

`res3` is the value `GetResourceUsage` returns for a matmul; `res2` is the larger issue-stage hold (16/20 cy) that gates the issue cursor; `res9` is the secondary accumulate latch.

> **QUIRK —** the transpose variants of the fp8 formats (`...0109`, `...010a`) drop the matmul hold to `{res3:2, res9:1}`, the *cheapest* matmul row in the table — a transposed fp8 matmul reuses an already-loaded staging path and prices nearly free. The non-transpose fp8 rows are the most expensive (`res3:8`).

---

## The Matpush Reservation Rows

### Purpose

The matpush (latch / matprep) family is keyed by a `MatpushModifier` whose low three bytes are `{[0]=GainLatchModeToMatmulDataFormat(mode), [1]=transpose, [2]=1}` and whose high byte carries the MSR (matrix-staging-register) variant (`0x01` vs `0x03`). The constructor inserts 16 keys; `GetResourceUsage` reads cell `array[8]`.

### The rows (all 16 keys)

The reservation widens with the format, falling into three value-sets — `narrow` `{res5/4:1, res7/6:1, res8:2, res10:7}`, `mid` `{:3, :2, res8:4, res10:9}`, `wide` `{:7, :6, res8:8}` — byte-confirmed at `@0x1c8bb28d..0x1c8bb737`.

| `MatpushModifier` key | variant | staging A · B | `res8` (**thru**) | `res10` (latch) | width | Confidence |
|---|---|---|---|---|---|---|
| `0x..010001` | v1/v3 | 1 · 1 | **2** | 7 | narrow | CERTAIN |
| `0x..010101` | v1/v3, xpose | 3 · 2 | **4** | — | mid | CERTAIN |
| `0x..010002` `0x..010009` `0x..01000a` | v1/v3 | 3 · 2 | **4** | 9 | mid | CERTAIN |
| `0x..010102` `0x..010109` `0x..01010a` | v1/v3, xpose | 7 · 6 | **8** | — | wide | CERTAIN |

> **GOTCHA —** the staging-port *indices* depend on the MSR variant the row is built for. The constructor selects `res4`/`res6` for one MSR and `res5`/`res7` for the other (a per-variant `Unexpected MSR` CHECK at `mxu_latency_table_gf.cc:94` guards the selector). The throughput cell read by the lookup is always `res8`; the staging holds are what shift. Treating the staging indices as fixed across variants will mis-place a hold.

---

## Throughput per Format — the Conv `R[0]`/`R[1]` Inputs

`GetResourceUsage` forces transpose to 0 when building the lookup key, so the convolution cost model reads the non-transpose rows. The `GfcCycleTable::GetCyclesForThroughputHelper` `@0x1c89f400` binds each cost-table `Instruction` (CT ordinal) to an LLO opcode, a resource index, and a transpose flag:

| CT | LLO opcode | family | key fmt | res read | cycles | Confidence |
|---|---|---|---|---|---|---|
| 0 | 289 (`0x121`) | matmul | 1 (bf16) | `array[3]` | **4** | CERTAIN |
| 1 | 295 (`0x127`) | matmul | 2 | `array[3]` | **8** | CERTAIN |
| 2 / 4 | 301 (`0x12d`) | matmul | 9 (F8E5M2) | `array[3]` | **8** | CERTAIN |
| 3 | 307 (`0x133`) | matmul | 0xa (F8E4M3Fn) | `array[3]` | **8** | CERTAIN |
| 5 | 324 (`0x144`) | matpush | 1 (bf16, direct GLM) | `array[8]` | **2** | CERTAIN |
| 6 | 326 (`0x146`) | matpush | GLM `^0xB` (transpose flip) | `array[8]` | per fmt | HIGH |
| 7 / 9 | 327 (`0x147`) | matpush | GLM `|0x30` → fmt 9 | `array[8]` | **4** | CERTAIN |
| 8 | 325 (`0x145`) | matpush | GLM `|0x32` → fmt 0xa | `array[8]` | **4** | CERTAIN |

The matpush transpose CTs (11–15) re-enter `sub_1C8BDB20` with the transpose flag set, which selects the `wide` value-set (`res8 = 8`).

> **TRILLIUM vs VIPERFISH —** Trillium thru(matmul) bf16 = **4**, fp8 = **8**; thru(matpush) bf16 = **2**, fp8 = **4**. Viperfish (per [VF](mxu-latency-vf.md)) is bf16 = 8 / int8 = 32 for matmul and 2 / 8 for matpush. The Trillium matmul hold is half VF's — the 256×256 systolic array doubles the per-cycle MAC rate, so the same op occupies the accumulate port for fewer cycles. The matpush narrow hold is unchanged (2), and the wide hold (8) equals the VF int8-wide cell.

The base op latency is a separate `MatmulDataFormat → int` map at `this+0x80`, named by the constructor's four `try_emplace` CHECK strings:

| `MatmulDataFormat` | base latency | Confidence |
|---|---|---|
| `kF32`, `kBf16` | **211** | CERTAIN |
| `kF8E5M2`, `kF8E4M3Fn` | **204** | CERTAIN |

These are the v7 per-format op latencies; the v6e (Ghostlite) constructor stores 192/182 for the same formats, the clearest single number proving the two tables are distinct instances.

---

## The Lookup

### Algorithm

`GetResourceUsage` `@0x1c8bdb20` (mis-symbolized as `raw_hash_map::at`) is the read path. It bounds-checks the resource, dispatches by opcode to a family, builds the key, finds the row, and reads the cell:

```c
function GF_MxuLatencyTable_GetResourceUsage(out, this, instr, res, latch_mode):  // @0x1c8bdb20
    if res > 0xA:                                      // mxu_latency_table_gf.cc:415
        LogFatal("mxu_resource_idx < to_underlying(MxuResource::kNumMxuResources)")
    switch (instr.opcode):
        case 289: key = MatmulModifier{fmt=1};  map = this+0x20      // matmul
        case 295: key = MatmulModifier{fmt=2};  map = this+0x20
        case 301: key = MatmulModifier{fmt=9};  map = this+0x20
        case 307: key = MatmulModifier{fmt=10}; map = this+0x20
        case 324: key = MatpushKey(latch_mode);          map = this+0x00  // matpush
        case 325: key = MatpushKey(latch_mode | 0x32);   map = this+0x00  // → fp8-fnuz (F8E4M3Fn)
        case 326: key = MatpushKey(latch_mode ^ 0x0B);   map = this+0x00  // transpose flip
        case 327: key = MatpushKey(latch_mode | 0x30);   map = this+0x00  // → fmt 9
        default:  return MakeErrorImpl("Unsupported opcode: ...")   // :455
    entry = map.find(key)
    if not entry: throw out_of_range                   // raw_hash_map::at
    array<int,11> res_vector = entry.value             // vmovups: [rdx+8] matmul / [rdx+4] matpush
    out = res_vector[res]                              // DIRECT — no remap
```

The matmul key is `{fmt, 0, 0, 0}` (transpose forced to 0). The matpush key is assembled from the three shared helpers `GainLatchModeToMatmulDataFormat` `@0x1d629260` (byte[0]), `LatchModeIsTranspose` `@0x1d628ea0` (byte[1]), and `LatchOpcodeToMsr(0x91)` `@0x1c8a1300` (byte[3]); byte[2] is the constant `1`. For opcode `0x91`, `LatchOpcodeToMsr` returns 0.

> **CONTRAST WITH GHOSTLITE —** the GF read is `array[res]` for the resource the CT helper passes (3 for matmul, 8 for matpush) with **no** transform. The Ghostlite lookup `@0x1c8b7560` instead seeds defaults (`res4 → idx3`, `res9 → idx9`) before reading. The two generations share the constructor *shape* and the key helpers, but the read indices and the seed remap differ — bind them per-gen. This is the `res-remap 3/8` distinction: GF is direct on indices 3 and 8.

The matpush `vmovups [rdx+4]` vs matmul `vmovups [rdx+8]` reflects the differing key widths (`MatpushModifier` precedes its value by 4 bytes, `MatmulModifier` by 8). The whole `array<int,11>` is copied to the stack before indexing.

### Vlxmr Rows

The Vlxmr map at `this+0x40` carries two rows, inserted from the rodata pair arrays `@0xb43cd6c` (key `0x0`) and `@0xb43cd74` (key `0x101`): `key0x0 = {res0:2}`, `key0x101 = {res0:2, res1:49}` — the same shape as the Ghostlite Vlxmr rows.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `GfcCycleTable::GfcCycleTable` | `0x1c89eec0` | allocs `0x30` perf + `0xa0` MxuLatency | CERTAIN |
| GF `MxuLatencyTable` ctor | `0x1c8bb1c0` | fills 16 matpush + 16 matmul + 2 vlxmr + cost map | CERTAIN |
| GF `MxuLatencyTable::GetResourceUsage` | `0x1c8bdb20` | opcode dispatch + `find` + direct `array[res]` | CERTAIN |
| matpush insert helper | `0x1c8bc2e0` | `array[res & 0xf] = cy`, `< 11` bound | CERTAIN |
| matmul insert helper | `0x1c8bc540` | as above, MatmulModifier map | CERTAIN |
| vlxmr insert helper | `0x1c8bc760` | VlxmrModifier rows from rodata pairs | CERTAIN |
| `GfcCycleTable::GetCyclesForThroughputHelper` | `0x1c89f400` | CT → opcode/res/transpose dispatch | CERTAIN |
| `GainLatchModeToMatmulDataFormat` | `0x1d629260` | matpush key byte[0] | CERTAIN |
| `LatchModeIsTranspose` | `0x1d628ea0` | matpush key byte[1] | HIGH |
| `LatchOpcodeToMsr` | `0x1c8a1300` | matpush key byte[3]; `(0x91) → 0` | HIGH |

---

## ComputeDmaLevels — the Fragment Count Bound to the Matmul Cost

### Purpose

The convolution / windowed-transfer cost path multiplies its bandwidth term by a DMA-efficiency factor whose input is the number of descriptor fragments a window decomposes into. `windowing_util::ComputeDmaLevels` `@0x1c86a9e0` computes that decomposition. The result feeds `WindowCyclesGenericTargetAgnostic` `@0x14552180`, which turns the fragment count into the efficiency multiplier.

### The DmaLevel struct and the level-build loop

`ComputeDmaLevels` returns a `std::vector<DmaLevel>`, each element 24 bytes:

```c
struct DmaLevel {                  // 24 B; vector stride lea[rax+rax*2]<<3
    long axis;                     // +0x00  first window-axis index of this level
    long count;                    // +0x08  init 1, then count *= window_stride[axis] per merged axis
    long flags;                    // +0x10  zero-init; level marker, not read by the cost path
};
```

The function first CHECKs that the window's per-axis inlined-vectors (`window_bounds`, `pad_high`, `pad_low`, and the dynamic-base second array) all have the same rank, then trims the contiguous minor dim when the window's `+0x338` config scalar is `≥ 2` (the row-major innermost run is not counted as its own level):

```c
rank = window_bounds.size()
CHECK(base_bounds.size() == rank && pad_high.size() == rank && pad_low.size() == rank)
N = rank - (WD[+0x338] >= 2 ? 1 : 0)                 // minor-dim trim
```

The level-build loop starts a new `DmaLevel` at each axis and merges following axes into it while **all** contiguity conditions hold:

```c
for axis in 0 .. N:                                  // outer: start a new level
    level = {axis, count=1, flags=0}
    for r in axis+1 .. N:                            // inner: try to merge axis r
        if elemental_stride[r] != 1: break
        operand = stride_level_operand[r]            // WD + 0xe0 [r], an LloValue*
        if operand:
            if operand.opcode >= 0x1cd: fatal()
            rt = opcode_produced_register_type[operand.opcode]
            if rt != 2 && rt != 4: fatal()           // ProducesSreg() || ProducesVreg()
            contiguous = llo::KnownEq(window_stride[r], operand)
        else:
            contiguous = (window_stride[r] == base_bounds[r])
        if not contiguous: break
        if pad_low[r] != 0 || dilation[r] != 0: break
        level.count *= window_stride[r]              // "access_multiplier"
    emit(level)                                      // a finalized contiguity break
```

> **NOTE —** the `+0xe0` per-axis second array (`stride_level_operand`) is the DMA-level *descriptor* operand, distinct from `window_stride` (which drives the byte count). Its register class gates the merge: the LLO value must produce a **Scalar** (`opcode_produced_register_type == 2`, `ProducesSreg()`) or a **Vector** (`== 4`, `ProducesVreg()`) — a predicate (1), a mask (3), or a no-result op (0) fails the gate and forces a contiguity break. The CHECK at `llo_util.h:35` (`value->ProducesSreg() || value->ProducesVreg()`) names this exactly. The canonical conv stride operands (`kScalarAddressCalculation` `@0x86` etc.) are all Scalar, hence mergeable; a mask-typed stride operand fragments the DMA.

The number of emitted `DmaLevel`s equals the number of contiguity breaks; the product of their `count` fields is the fragment count.

### The fragment count → efficiency multiplier

`WindowCyclesGenericTargetAgnostic` `@0x14552180` reads the level vector and buckets the fragment product, byte-exact:

| fragment product `r12` | multiplier | rodata | Confidence |
|---|---|---|---|
| `≤ 1` level (no fragmentation) | **1.0** | `@0xa2df230` | CERTAIN |
| `== 1` | **1.6** | `@0xa2d71a0` | CERTAIN |
| `∈ [2, 3]` | **1.3** | `@0xa2d71a8` | CERTAIN |
| `∈ [4, 7]` | **1.1** | `@0xa2df4c8` | CERTAIN |
| `∈ [8, 31]` | **1.05** | `@0xa2df2a8` | CERTAIN |
| `> 31` | **2003.0** | `@0xa2df1b0` | CERTAIN |

The `{1.6, 1.3}` pair is a 2-entry table at `@0xa2d71a0` indexed by `setge(r12 ≥ 2)`; `1.1`, `1.05`, and `2003.0` are standalone constants. The bandwidth deposit returned is `(byte_count · elem_size / bytes_per_cycle) · multiplier + residual`. The `2003.0` value is a deliberate penalty — a window that fragments into more than 31 descriptors is priced as catastrophically slow, steering the layout chooser away from it.

> **QUIRK —** a single contiguous transfer (`≤ 1` level) is multiplier `1.0`, but a fragment product of exactly `1` (one level whose merged element-count product is 1) is `1.6`. The two are distinct paths: the `≤ 1`-level default short-circuits before the bucket table; the `r12 == 1` bucket is reached only when the level vector is non-trivial. A reimplementation must keep both.

---

## Worked Example — Trillium bf16 Conv Deposit

For a Trillium bf16 convolution, the per-format MXU reservation reads are now numeric:

- `R[0]` matpush = matpush_count · thru(CT5)[bf16] = matpush_count · `array[8]` = matpush_count · **2**.
- `R[1]` matmul = op_count · thru(CT0)[bf16] · 0.5 / Target / EPF = op_count · `array[3]` · 0.5 / … = op_count · **4** · 0.5 / ….
- `R[2]` Xlu = `4` · ChunksPerTile · rem (the `GhostlitePerformance` grid cell, [Performance: GF](performance-gf-ghperf.md)).

For fp8 (`F8E5M2`/`F8E4M3Fn`), `R[0]` = 4 and `R[1]` = 8 — double the bf16 hold. If the conv input window fragments into 2 DMA levels with a per-level element product of 3 (a 3-tap kernel axis with non-unit stride), `ComputeDmaLevels` returns 2 levels, `r12 = 3`, and the bandwidth deposit is multiplied by `1.3`; a fully contiguous load multiplies by `1.0`; a heavily strided gather (`r12 > 31`) by `2003.0`.

---

## Related Components

| Name | Relationship |
|---|---|
| `mxu-latency-overview` | the shared model, key scheme, and `GetResourceUsage` read path |
| `mxu-latency-gl` | the Ghostlite `array<int,11>` — same shape, distinct ctor, 192/182 base latency |
| `mxu-latency-vf` | the Viperfish `array<int,19>` — double the matmul hold |
| `matmul-mode-modifiers` | the modifier ordinals, format codes, GLM transforms |
| `performance-gf-ghperf` | the GF grid carrying the conv `R[2]` Xlu cell and the same per-dtype magnitudes |
| `window-description-cost` | the windowed-transfer cost path that consumes `ComputeDmaLevels` |

---

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuResource` model, key construction, and the byte-shared `GetResourceUsage`
- [MXU Latency: GL (Ghostlite)](mxu-latency-gl.md) — the v6e `array<int,11>`; the `res4→3`/`res9→9` seed remap GF lacks
- [MXU Latency: VF](mxu-latency-vf.md) — the Viperfish `array<int,19>`; the half-VF matmul hold contrast
- [MatmulMode & Modifiers](matmul-mode-modifiers.md) — the `MatpushModifier`/`MatmulModifier` ordinals and the `GainLatchMode` → format jump table
- [Performance: GF (GhPerf 465×31)](performance-gf-ghperf.md) — the Trillium occupancy grid and the conv `R[2]` Xlu = 4 cell
- [MxuOpHoldIssues Stall Recurrence](mxu-opholdissues-stall.md) — how the reservation arrays become issue stalls
- [Resource Enum (23-slot)](resource-enum.md) — the higher-level `ResourceVector`, distinct from the MXU-internal `MxuResource`
- [Window Description Cost](window-description-cost.md) — the windowed-transfer cost path that calls `ComputeDmaLevels`
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU instruction slot these ops price
- [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) — the matprep / latch ops behind the matpush family
