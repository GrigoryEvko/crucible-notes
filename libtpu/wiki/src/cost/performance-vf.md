# Performance: VF

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset.*

## Abstract

This page dumps the Viperfish (v5/v5e) `ViperfishPerformance` object: the per-instruction latency array plus the **Instruction × Resource** occupancy grid that prices how many cycles each LLO instruction holds each intra-op micro-pipeline port. It is the VF concretization of the grid family documented in [`performance-overview`](performance-overview.md) — the heap latency-array + heap 2D `vector<int>` grid that Pufferfish, Viperfish, Ghostlite, and `6acc60406` all share, read by a byte-identical `GetResourceUsage`. Viperfish sits in the middle of the resource-count progression (7 → 7 → 20 → **28** → 31 → 31): it widens Pufferfish's 20-column grid by 8 columns, chiefly the 4-stage matprep throughput group and the transpose-binary result sub-stages.

The reference frame is an LLVM `SchedMachineModel`: `GetLatency(instr)` returns the instruction's pipeline depth (the value the scheduler raises a true-dependency edge to), and `GetResourceUsage(instr, res)` returns how many cycles `instr` occupies functional-unit port `res`. The VF grid is reconstructed by reading the constructor that fills it — `ViperfishPerformance::ViperfishPerformance` `@0x1c8c4840`, a `new 0x600` latency array (384 int32) and a `new 0x2400` grid (384 rows × 24-byte `vector<int>`, each row `new 0x70` = 28 int32) — not from a `.td` file. Both the latency array (memset to `0xff`, then *every* slot overwritten) and the 378 populated grid cells are byte-exact from the ctor's 762 DWORD-immediate stores (384 latency + 378 grid).

The page documents the object layout and the `GetResourceUsage` read path (the 24-byte row stride, the two bounds checks), the 28 resource columns named by occupant LLO class, the deposit column (res 0x0e, the conv `R[2]` Xlu term, read by `GetXluPathReservation`), the latency-array histogram (the 121/131 matmul/matprep base latencies), and how an LLO opcode reaches a grid row through `GetViperfishInstruction` and the MXU latch-mode fan-out. It closes by relating this grid to the separate [`MxuLatencyTable`](mxu-latency-vf.md) reservation matrix that co-exists with it.

For reimplementation, the contract is:

- The `ViperfishPerformance` object layout: heap latency array (384 int32) + heap 2D grid (384 × 28), with exact sizes and the `[this+0x18]`/`[this+0x20]` grid pointer/count.
- The `GetResourceUsage(instr, res)` read path — two bounds checks, the 24-byte (`3 × a2 << 3`) row stride.
- The 28 resource columns named by occupant, and the Xlu/matrix-result deposit column (res 0x0e) with its `GetXluPathReservation` accessor.
- How an LLO opcode reaches a grid row (`GetViperfishInstruction`, the matmul/matprep latch-mode WORD-table fan-out) and how the throughput cells co-exist with the `MxuLatencyTable`.

| | |
|---|---|
| **Class** | `xla::viperfish::ViperfishPerformance` |
| **Ctor** | `ViperfishPerformance::ViperfishPerformance` `@0x1c8c4840` (`new 0x600` latency, `new 0x2400` grid, row `new 0x70`) |
| **Read path** | `ViperfishPerformance::GetResourceUsage` `@0x1c8cbc40` · `GetLatency` `@0x1c8cbc20` · `GetResources` `@0x1c8cbc00` |
| **Resource count** | 28 (row width `new 0x70` = 28 int32; `GetResources` count `0x1c`) |
| **Latency rows** | 384 (`new 0x600`; memset `0xff`, all overwritten) |
| **Grid** | 384 rows × 28-wide; 378 populated cells across 128 rows |
| **Deposit column** | res `0x0e` (14) — conv `R[2]` Xlu term, cell = 8 for matrix-result ops |
| **`kResources` order** | `@0xb43cda8` (28 B permutation of `{0..27}`) |
| **Opcode → row** | `GetViperfishInstruction` `@0x1c8a3300` (jt `@0xb43a104`, idx = op−1) |
| **Xlu accessor** | `LatencyTableViperfish::GetXluPathReservation` `@0x1c8a3200` (`res = 0xe`) |
| **EUP push / pop latency** | push (instr 0xcc..0xd2) = 6, pop (instr 0x168) = 1 |

---

## Object Layout

### Purpose

`ViperfishPerformance` answers the scheduler's two per-instruction questions — pipeline depth (`GetLatency`) and per-port occupancy (`GetResourceUsage`) — from two heap objects: a flat latency array indexed by `Instruction`, and a 2D grid of one `vector<int>` per instruction.

### Structure

The layout is the shared grid-family layout ([`performance-overview`](performance-overview.md)), with VF's specific sizes:

```c
struct ViperfishPerformance {       // built by ctor @0x1c8c4840
    int32*  latency;                // +0x00 ; new 0x600 (384 int32), memset 0xff
    u64     latency_size;           // +0x08 ; = 384
    u64     latency_cap;            // +0x10 ; = 384
    vector<int>* grid;              // +0x18 ; new 0x2400 (384 × 24-byte vector<int>)
    u64     grid_outer_count;       // +0x20 ; = 384  (the GetResourceUsage outer bound)
    u64     grid_outer_cap;         // +0x28 ; = 384
    // each row (24-byte std::vector<int>): { int* data (new 0x70 = 28 int32, zero-init), size=28, cap=28 }
};
```

The ctor emits exactly **762 DWORD-immediate stores** = 384 latency + 378 grid cells. The store-count is the integrity proof: every `mov DWORD PTR [rax+off], imm` is classified by the base it loaded — `[rbx]` (latency base) or `[r12]` (grid base, `r12 = [rbx+0x18]`) — with zero non-`rax`-base DWORD-imm stores. Unlike Ghostlite/`6acc60406`, where the `0xff` memset default survives on unpriced rows, **every** VF latency slot (0..383) is explicitly overwritten; the `0xff = 255` default does not survive.

> **QUIRK —** the VF Performance ctor `@0x1c8c4840` does not produce Hex-Rays pseudocode (it is too large / mixes VEX stores), so the cell values here are read from the byte-exact objdump decode of its 762 DWORD-immediate stores, cross-checked by the store-count identity (`762 = 384 + 378`). The *read path* `GetResourceUsage` and `GetResources` decompile cleanly and confirm the 28-wide / 384-row shape; the cell integers are objdump-grade, not Hex-Rays-grade — HIGH confidence by the store-count method.

---

## The GetResourceUsage Read Path

### Purpose

`GetResourceUsage(instr, res)` is the single accessor the throughput model and the Xlu-reservation accessor go through to read a grid cell. It is byte-identical across PF/VF/GL/GF — two bounds checks and a `lea`-computed 24-byte row stride.

### Algorithm

Byte-confirmed in the decompile:

```c
function ViperfishPerformance::GetResourceUsage(perf, instr, res):   // @0x1c8cbc40
    if perf.grid_outer_count <= instr:        // [perf+0x20] = 384 ; outer bound
        BUG()                                  // ud2
    grid = perf.grid                           // [perf+0x18]
    v4 = 3 * instr                             // row index ×3 ...
    if perf.grid[v4].size <= res:              // [grid + 8*v4 + 8] ; inner bound = 28 (row width)
        BUG()
    return *(int*)(perf.grid[v4].data + 4*res) // [grid + 8*v4] + 4*res = grid[instr][res]
```

The `3 * instr` followed by the `8 *` indexing is the 24-byte `std::vector<int>` stride (`{int* data, u64 size, u64 cap}`). `GetLatency(instr)` `@0x1c8cbc20` is the simpler sibling — `latency[instr]` bounded by `[perf+0x8]`. `GetResources()` `@0x1c8cbc00` returns `&kResources` (the `.rodata` byte array `@0xb43cda8` listing the 28 column indices in fill order).

> **GOTCHA —** the OUTER index is the per-gen `ViperfishPerformance::Instruction`, **not** the raw LLO opcode. The mapping is `GetViperfishInstruction` `@0x1c8a3300` (jt `@0xb43a104`, idx = op−1, bound 0x1a9), and the MXU band fans a single matmul/matprep opcode out to many ordinals via a secondary latch-mode WORD table (`@0xb43b140`, valid-latch mask `0x3ffcc03`). A reimplementation that indexes the grid directly by LLO opcode mis-reads every MXU row.

### The matrix-result / Xlu deposit column

One column holds the matrix-result (Xlu) throughput that the convolution cost model reads as its `R[2]` term. On Viperfish it is **res 0x0e** (14), confirmed by the dedicated accessor:

```c
function LatencyTableViperfish::GetXluPathReservation(this, value):   // @0x1c8a3200 (verified)
    if value.opcode == 139:                        // kVectorSetPermutePattern, handled directly
        return (value[+0x40] != 0) ? 8 : 1
    instr = GetViperfishInstruction(value)
    return ViperfishPerformance::GetResourceUsage(this.perf /* [this+0x1d0] */, instr, 14)   // res 0x0e
```

The `res = 0x0e` immediate is byte-confirmed (`GetResourceUsage(*(this+58*8), instr, 14)`), and the permute-pattern opcode 139 (`kVectorSetPermutePattern`) returns a hardcoded `8`/`1`. The Xlu deposit column tracks MXU geometry across gens — res 6 PF → **res 0x0e VF** → res 0x0f GL → res 0x10 GF — and the populated cell is `8` for the VF matrix-result/transpose-result class (matching the `kVectorMatres` r22 cell, below).

---

## The 28 Resource Columns

### Purpose

The grid's INNER axis is the `ViperfishPerformance::Resource` enum: the 28 intra-op EUP/MXU/Xlu micro-pipeline reservation ports. There is no `Resource::ToString` in the binary, so the columns are named functionally — by the LLO-instruction class that deposits cycles into each, the OUTER index being the opcode resolved through `GetViperfishInstruction` cross-joined to `LloOpcodeName::opcode_name` `@0x21ccfef0`. The names are reimplementation-grade in *meaning* (which physical port each column reserves), not literal symbol names.

### The columns

`kResources` `@0xb43cda8` is the 28-byte traversal order `13 18 1a 19 08 02 03 16 09 0a 04 05 06 07 0b 12 0f 14 1b 0c 10 01 15 0d 11 17 00 0e` (all of `{0..27}` exactly once). Naming each column by dominant occupant:

| col | cells | val(s) | occupant LLO band (classifier-named) | physical reservation port |
|---|---|---|---|---|
| r0 | 2 | 2 | low-ordinal MXU/setup band (ins 0x0, 0x2) | MXU/setup address port A |
| r1 | 2 | 2,3 | `kDmaGeneral..` (ins 0x38) | DMA address/issue port |
| r2 | 27 | 7 | MXU matmul band 0xd4..0x106 (MatmulPackedMsk/Lmr) | MXU matmul prep/issue port |
| r3 | 51 | 8,16,32 | MXU matmul/matprep band | **MXU matmul throughput port** (×51) |
| r4 | 38 | 4,5 | matprep band + DoneWithGains/LoadLmr (0xd5..0x10a) | MXU matprep throughput stage A |
| r5 | 38 | 12,13 | same band | MXU matprep throughput stage B |
| r6 | 38 | 20,21 | same band | MXU matprep throughput stage C |
| r7 | 38 | 28,29 | same band | MXU matprep throughput stage D |
| r8 | 2 | 32 | `kVectorLoadLmr` (0x109..0x10a) | LMR / gain-load port |
| r9 | 16 | 2,6 | result-pop FIFO band (0x10b..0x11a) | MXU/EUP result-pop FIFO stage A |
| r10 | 16 | 1,5 | same | result-pop FIFO stage B |
| r11 | 16 | 3,7 | same | result-pop FIFO stage C |
| r12 | 9 | 41,48 | `kVectorPermute/Rotate/BroadcastLane` (0x11b..) | cross-lane result stage A |
| r13 | 14 | 52,57,59 | same + reduce band | cross-lane / reduce result stage B |
| **r14** | 23 | 8,16 | TransposeBinary(0x123)/Permute/Rotate/Broadcast | **Xlu / matrix-result deposit** (conv `R[2]`) |
| r15 | 5 | 109,117 | `kVectorTransposeBinary` (0x11f..0x127) | transpose-binary result sub-stage A |
| r16 | 5 | 112,120 | same | transpose-binary result sub-stage B |
| r17 | 3 | 7,15 | same | transpose-binary result sub-stage C |
| r18 | 5 | 24 | `kVector{Add,Max,Min,..}ReduceF32` (0x12e..0x132) | reduce-result port |
| r19 | 1 | 3 | `kVectorCcfPush` (0x134) | CCF push port |
| r20 | 1 | 1 | `kVectorPrng` (0x13e) | PRNG port |
| r21 | 14 | 1 | `kVectorSyncFlag*/kVectorWait*/SfrfPush` (0x13f..) | sync-flag / wait / SFRF port |
| r22 | 1 | 8 | `kVectorMatres` (0x169) | matrix-result (Matres) port |
| r23 | 1 | 8 | `kVectorXlaneResult/Transpose-result` (0x16a) | cross-lane / transpose result port |
| r24 | 1 | 3 | `kVectorCcfPop` (0x16b) | CCF pop port |
| r25 | 4 | 5 | `kVectorStoreEvenOddSublanes` (0x174..0x177) | sublane-store port |
| r26 | 6 | 6 | barnacore/store band (0x178..0x17d) | sublane-store / scatter port |
| r27 | 1 | 3 | `kVectorSetRngSeed` (0x17f) | RNG seed port |

Column population (cells/col), summing to 378: `r0:2 r1:2 r2:27 r3:51 r4:38 r5:38 r6:38 r7:38 r8:2 r9:16 r10:16 r11:16 r12:9 r13:14 r14:23 r15:5 r16:5 r17:3 r18:5 r19:1 r20:1 r21:14 r22:1 r23:1 r24:1 r25:4 r26:6 r27:1`.

> **NOTE —** the `ViperfishPerformance::Resource` enum (28 columns) is a *different, lower-level* enum than the 23-slot per-bundle `ResourceVector` ([`resource-enum`](resource-enum.md)) and a *third* enum apart from the 19-value `MxuResource` of [`mxu-latency-vf`](mxu-latency-vf.md). The grid here prices intra-op micro-pipeline-stage holds; `ResourceVector` is the per-bundle accumulator; `MxuResource` is the MXU-internal reservation port set. Three resource axes in the same cost model — conflating them is the central trap. The `kResources` byte array gives this grid's column traversal order, not the `ResourceVector` slot order.

### How VF widens over Pufferfish

Viperfish adds 8 columns over Pufferfish's 20: the 4-stage matprep throughput group (r4..r7, carrying the `{4,12,20,28}` & `{5,13,21,29}` step-8 holds) and the transpose-binary result sub-stages (r15..r17), plus CCF push/pop (r19/r24). The matmul-throughput representation itself widened — a single column on PF (res 9, 96 cells) became res r3 (51 cells) plus the 4-stage matprep group on VF.

> **QUIRK —** the r4..r7 matprep stages carry the `{4,5}/{12,13}/{20,21}/{28,29}` value pairs — the step-8 ramp `{4,12,20,28}` (and its `+1` companion `{5,13,21,29}`). This same `{5,13,21,29}` ramp appears as the `MxuResource` overrun-check insertion in [`mxu-latency-vf`](mxu-latency-vf.md) (`AddOverrunCheckReservations`), reflecting that the matprep throughput stages and the MSR overrun-check ports are two cost-model views of the same per-K-tile latch pipeline. They are *different enums in different tables* — do not index one with the other.

---

## The Latency Array

### Purpose

`latency[instr]` is the instruction's pipeline depth — the true-dependency edge weight. The VF ctor memsets the array to `0xff` then overwrites all 384 slots (the default does not survive).

### Histogram

The 384 entries take 19 distinct values:

```text
  1: 148    2: 113    131: 27    121: 25    7: 18    8: 14    6: 8    3: 5
  164: 5    115: 5    114: 4    9: 3    5: 2    4: 2    30: 1    122: 1    0: 1
  36: 1    49: 1
```

The meaningful clusters:
- `1`/`2` (148+113) — cheap vector/scalar/sync ops.
- `131` / `121` — the MXU **matmul / matprep base latencies** (the systolic pipeline depth; the [`MxuLatencyTable`](mxu-latency-vf.md) reservation arrays say how many of these cycles each MXU sub-port is *held*).
- `7` — matprep; `8` — result-pop; `6` — the **EUP push** latency (instr 0xcc..0xd2; pop instr 0x168 = 1).
- `164` — transpose-binary; `115` — reduce; `114`/`122` — permute/rotate/broadcast.
- `36` — CCF push; `0` — one MXU-setup ordinal; `49` — a single op.

> **NOTE —** the EUP push→pop edge on VF is latency 6 (push) / 1 (pop), and VF prices the EUP push via the latency array **alone** — the push rows reserve *no* grid cells (`ViperfishTarget::VectorEupReservationCycles` `@0x1d49b060` = 1, full-rate). This contrasts with Pufferfish, where the EUP push additionally reserves grid ports r2/r3 and runs at half-rate (`VectorEupReservationCycles` = 2). A reimplementation must not assume the EUP push is grid-priced on VF.

---

## Opcode → Grid Row

### The classifier

`GetViperfishInstruction` `@0x1c8a3300` maps an LLO opcode to a grid-row `Instruction` ordinal via a jump table `@0xb43a104` (idx = op−1, bound 0x1a9). Most opcodes map directly (`mov ax, IMM16`); the MXU band is the exception. The matmul arm `@0x1c8a3373` reads a secondary latch-mode WORD table `@0xb43b140` (valid-latch mask `0x3ffcc03`) that fans the single matmul/matprep opcode out to the ~0x60 band ordinals (VF Instruction 0xd4..0x106 + 0x10b..0x11a). So one `vmatmul`/`vmatprep` LLO opcode becomes many grid rows, one per `(opcode, latch_mode/MatmulDataFormat)` pair.

The grid OUTER index *is* the opcode for the priced rows (every populated row resolves to a coherent classifier-named LLO opcode), but the mapping is the per-gen classifier, not the raw opcode space. The MXU band's per-ordinal `(opcode, latch_mode)` decode was read as the latch-mode classifier *mechanism*, not enumerated ordinal-by-ordinal; the band identity (matmul/matprep), its base latencies (121/131), and its throughput cells (r3 `{8,16,32}`, r4..r7 4-stage) are byte-exact.

### Co-existence with the MxuLatencyTable

The matmul throughput cells in this grid (res r3 = `{8,16,32}` per `MatmulDataFormat` — fmt1=8, fmt2=16, fmt6/int8-x8=32; r4..r7 the 4-stage matprep holds) **duplicate** the matmul-rate magnitudes that also live in the separate [`MxuLatencyTable`](mxu-latency-vf.md) reservation matrix. The two cost tables co-exist and price different things: this grid prices the intra-op micro-pipeline-port holds keyed on the `Instruction` ordinal; the `MxuLatencyTable` prices the MXU sub-resource occupancy keyed on the `MatmulDataFormat`/`GainLatchMode` modifier. The base op latency (121/131) lives in this latency array; the per-`MxuResource` hold-cycle vector lives in the `array<int,19>`.

> **NOTE — the `{8,16,32}` matmul rate is byte-confirmed in *both* tables, and the throughput route reads the `MxuLatencyTable`, not this grid.** The grid col-r3 cells `{8,16,32}` are byte-anchored in the `ViperfishPerformance` ctor `@0x1c8c4840` (`mov DWORD PTR [data+0xc], 8/0x10/0x20`, 51 cells across the matmul band). The *same* triple is independently byte-anchored in the `MxuLatencyTable` ctor `@0x1c8a52c0` as `{MxuResource 15 (MatmulAccA) → 8/16/32}` per format (lines emitting `key=15, value=8`, then `=16`, then `=32`). The cost model's matmul-throughput route, `VfCycleTable::GetCyclesForThroughput(CT 0)` → `MxuLatencyTable::GetResourceUsage(0xd4, res 3, 0)`, remaps `res 3 → array[15]` and returns that reservation cell — so the *consumed* `{8,16,32}` is read from the `MxuLatencyTable`, while this grid's r3 holds a mirror that the matmul/matprep throughput path does **not** read for CT-class 0/1/4. A reimplementer must populate both, but wire the matmul-rate consumer to the `MxuLatencyTable` `array[15]`.

> **NOTE —** the parallel `opcode_produced_register_type` table (`@0x223a16c0`, byte[461]) and the convolution-window DMA-level merge gate it drives are gen-invariant — they are documented on [`performance-overview`](performance-overview.md) and apply to VF unchanged, since they key on the raw LLO opcode, not the per-gen `Instruction` ordinal.

---

## Resource-Count Context

VF's 28 columns sit in the cross-gen progression:

| Gen | Codename | Resource cols | latency rows | grid cells | populated rows | EUP push lat | Xlu deposit col |
|---|---|---|---|---|---|---|---|
| PF | Pufferfish | 20 | 336 | 265 | 180 | 7 | res 6 (conflict-penalty) |
| **VF** | **Viperfish** | **28** | **384** | **378** | **128** | **6** | **res 0x0e** (`GetXluPathReservation`) |
| GL | Ghostlite | 31 | 476 | 358 | 132 | 13/14 | res 0x0f |
| GF | `6acc60406` | 31 | 465 | 285 | 92 | 12 | res 0x10 |

The enum widened 20 → 28 → 31 across PF → VF → GL: VF added the 4-stage matprep group (r4..r7) + transpose-binary result sub-stages (r15..r17) + CCF push/pop; GL/GF added the BF16-EUP AndPop FIFO depth + the BarnaCore tail. The matmul throughput port moved (res 9 PF single col → res 3 VF over a 4-stage group), and the Xlu deposit column tracks MXU geometry (res 6 → 0x0e → 0x0f → 0x10).

---

## Related Components

| Name | Relationship |
|---|---|
| `performance-overview` | the grid-family object layout, the shared `GetResourceUsage` read path, and the `opcode_produced_register_type` gate |
| `mxu-latency-vf` | the separate `MxuLatencyTable` `array<int,19>` reservation matrix that co-exists with this grid |
| `resource-enum` | the 23-slot per-bundle `ResourceVector` — distinct from this 28-column `Resource` enum |
| `performance-pf` / `-gl-ghperf` / `-gf-ghperf` | the 20 / 31 / 31-column sibling grids |
| `slot-mxu` | the LLO MXU opcodes the matmul/matprep band rows price |

---

## Cross-References

- [Performance Family Overview](performance-overview.md) — the grid-family object layout, the byte-identical `GetResourceUsage`, the resource-count progression, and `opcode_produced_register_type`
- [MXU Latency: VF](mxu-latency-vf.md) — the `ViperfishMxuLatencyTable` `array<int,19>` reservation matrix; the matmul throughput cells here are its grid analog
- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuResource` reservation model that co-exists with this grid
- [Performance: PF](performance-pf.md) — the 20-column Pufferfish grid VF widens, and its conflict-penalty Xlu pricing
- [Performance: GL (GhPerf 476×31)](performance-gl-ghperf.md) — the Ghostlite v6e grid, Xlu deposit res 0x0f
- [Performance: GF (GhPerf 465×31)](performance-gf-ghperf.md) — the `6acc60406` (TPU7x) grid, Xlu deposit res 0x10
- [Resource Enum (23-slot)](resource-enum.md) — the per-bundle `ResourceVector`, distinct from this 28-column `Resource` micro-pipeline enum
- [MatmulMode & Modifiers](matmul-mode-modifiers.md) — the `MatmulDataFormat` codes the MXU band rows fan out on
- [MXU Slot](../isa/slot-mxu.md) — the physical MXU sub-units the matmul/matprep columns reserve
- [Decode-Side: VF / GXC](../isa/decode-side-vf-gxc.md) — the VF MXU bundle decode that produces the opcodes `GetViperfishInstruction` classifies
- [MxuOpHoldIssues Stall Recurrence](mxu-opholdissues-stall.md) — the back-to-back stall the latency array and reservation matrix jointly drive
