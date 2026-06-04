# Per-Opcode Cycle Constants

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.rodata` addresses are virtual addresses; for this binary `.rodata` VMA == file offset (section `[11]` at `0x84a0000`).*

## Abstract

This page is the data sheet for the TensorCore cost model: the actual cycle-cost integers baked into `libtpu.so`'s `.rodata`, grouped by generation and engine, plus the rule by which the bundle-latency cost model sums them. The numbers come from two places that a reimplementer must keep distinct. (1) The **throughput** cycle of a cycle class is the value a per-gen [`CycleTable`](cycletable-family.md) returns from `GetCyclesForThroughput` — for Jellyfish/Dragonfish a flat read `Performance[offsetLUT[class]]`, for Pufferfish and later a `switch` that calls `Performance::GetResourceUsage(instruction_id, resource)`. (2) The **latency** of an op is a separate per-gen `Performance` array entry the `LatencyTable*` reads. Both arrays live in the same per-gen `Performance` object; the cycle constants below are the contents of those arrays.

The oldest gens (JF/DF) are fully byte-pinned here: a 33-entry `int64` offset LUT, the 16-of-33 priced subset, the seven 8-cycle MXU cells, the nine 1-cycle vector cells, and the constructor source blocks that fill them. For Pufferfish through `6acc60406`, the per-class throughput integers are pinned from the `GetResourceUsage(instr,res)` call constants in each gen's `switch`, and the per-gen latency-value *distributions* are pinned from the constructor store stream; the full per-`(instruction × resource)` 2D grids are partially extracted (flagged below).

The contract this page documents:

- **A cycle class's throughput cost is `Performance[offset]` (JF/DF) or `GetResourceUsage(instr,res)` (PF+).** The default for an unpriced class is `1` cycle.
- **The baked grid defaults to the sentinel `0x7FFFFFFF`** ("unsupported / not schedulable") and is overwritten by the per-gen `Performance` constructor with a hand-crafted broadcast/block-copy pattern.
- **Dragonfish == Jellyfish except for two cells** (`[+0x28]`, `[+0x2c]`), neither a throughput-LUT target, so the JF and DF throughput tables are byte-identical.
- **Bundle issue cost is per-lane max, not a global sum.** Per-op cycles accumulate into a `ResourceVector` keyed on `GetResource(class)`; same-lane costs add, cross-lane costs overlap.
- **Transcendentals are constants from scalar virtual overrides**, not from the grid.

| | |
|---|---|
| **Offset LUT (JF/DF)** | `qword_B438B70` @ `0xb438b70` — 33 × `int64`, class → `Performance` byte offset |
| **Resource LUT** | `dword_B438AEC` @ `0xb438aec` — 33 × `int32`, class → `ResourceVector` slot |
| **Priced mask (JF)** | `0x19FFC0821` — 16 classes priced; rest default `1` |
| **Sentinel** | `dword_84A2F60` @ `0x84a2f60` = `0x7FFFFFFF` (unsupported) |
| **Broadcast scalars** | `dword_84A2B08` = `1`, `dword_84A2854` = `2`, `dword_84A2D0C` = `8` |
| **PerformanceJf ctor** | `0x1d4930c0` — 116 stores → 419 distinct `int32` cells |
| **PerformanceDf ctor** | `0x1d493060` — JF + one `0xD00000042` store at `+0x28` |
| **Grid width** | `0xe00` bytes (`Performance::Performance` @ `0x1d492900`) |
| **Confidence** | CONFIRMED (byte-anchored) for JF/DF; per-class PF+ CONFIRMED; full 2D grids PARTIAL |

---

## Where The Constants Live

Every per-gen [`Performance`](performance-overview.md) object is one flat `0xe00`-byte allocation. The base constructor `Performance::Performance` @ `0x1d492900` broadcast-fills the entire array with the sentinel `0x7FFFFFFF` (`vbroadcastss xmm0, dword_84A2F60`), meaning "this cell is unsupported / not schedulable." The derived per-gen constructor then overwrites the supported subset with packed constants.

There are three idioms a reimplementer will see in every `Performance` constructor:

| Idiom | Disassembly | Effect |
|-------|-------------|--------|
| Broadcast fill | `vbroadcastss xmm, dword_XXX` + `vmovups [base+off], xmm` | writes 4 identical cells |
| 16-byte block copy | `vmovaps xmm, xmmword_YYY` + `vmovups [base+off], xmm` | writes 4 cells from a 4-`int32` `.rodata` block |
| scalar / movabs store | `mov [base+off], imm` / `movabs rax,imm64; mov [base+off],rax` | writes 1 or 2 cells |

The three broadcast scalars carry the common cycle values: `dword_84A2B08 = 1` (1-cycle ops), `dword_84A2854 = 2` (2-cycle bucket), `dword_84A2D0C = 8` (the 8-cycle MXU bucket). The distinctive per-instruction clusters come from 16-byte blocks; the 15 distinct blocks used by `PerformanceJf` are enumerated below.

---

## Jellyfish / Dragonfish — The Flat Offset-LUT (byte-exact)

JF/DF do not use a `(instruction, resource)` switch. `JfCycleTable::GetCyclesForThroughput` reads the `Performance` grid directly through a 33-entry `int64` offset LUT:

```c
// xla::jellyfish::JfCycleTable::GetCyclesForThroughput @ 0x1c89dce0 (decompiled, exact)
int64_t GetCyclesForThroughput(JfCycleTable *this, uint32_t cls) {
    if (((cls < 0x21) & (uint8_t)(0x19FFC0821ULL >> cls)) == 1)   // priced?
        return *(uint32_t *)(this->performance/*+0x10*/ + qword_B438B70[cls]);
    return 1;                                                      // default
}
```

The priced mask `0x19FFC0821` selects 16 of the 33 classes: `{0x00, 0x05, 0x0b, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1f, 0x20}`. The offset LUT (`qword_B438B70` @ `0xb438b70`) is `0` for every unpriced class — harmless, since the read is short-circuited. The priced rows, with the byte offset, the resolved value in the rebuilt `PerformanceJf` image, and the constructor idiom that wrote the cell:

| Class | `offsetLUT[cls]` | Resource | JF cyc | DF cyc | Source idiom | Confidence |
|------:|-----------------:|----------|-------:|-------:|--------------|------------|
| `0x00` | `0x910` | `R[1]` Matmul | 8 | 8 | block `@0xa2da220` `{8,8,8,1}[0]` | CONFIRMED |
| `0x05` | `0x92c` | `R[0]` Matpush | 8 | 8 | bcast `8` (`@0x84a2d0c`) | CONFIRMED |
| `0x0b` | `0x92c` | `R[0]` Matpush | 8 | 8 | bcast `8` (shares offset with `0x05`) | CONFIRMED |
| `0x12` | `0x33c` | `R[4]` VectorAlu1 | 1 | 1 | bcast `1` (`@0x84a2b08`) | CONFIRMED |
| `0x13` | `0x340` | `R[4]` VectorAlu1 | 1 | 1 | bcast `1` | CONFIRMED |
| `0x14` | `0x344` | `R[3]` VectorAlu0 | 1 | 1 | bcast `1` | CONFIRMED |
| `0x15` | `0x39c` | `R[5]` VectorAluAny | 1 | 1 | bcast `1` | CONFIRMED |
| `0x16` | `0x398` | `R[5]` VectorAluAny | 1 | 1 | bcast `1` | CONFIRMED |
| `0x17` | `0x954` | `R[2]` Xlu | 8 | 8 | bcast `8` | CONFIRMED |
| `0x18` | `0x3f8` | `R[6]` VectorEup | 1 | 1 | imm `1` | CONFIRMED |
| `0x19` | `0x368` | `R[5]` VectorAluAny | 1 | 1 | bcast `1` | CONFIRMED |
| `0x1a` | `0x3f4` | `R[6]` VectorEup | 1 | 1 | bcast `1` | CONFIRMED |
| `0x1b` | `0x960` | `R[2]` Xlu | 8 | 8 | bcast `8` | CONFIRMED |
| `0x1c` | `0x94c` | `R[2]` Xlu | 8 | 8 | block `@0xa2cf810` `{8,1,1,8}[3]` | CONFIRMED |
| `0x1f` | `0x958` | `R[2]` Xlu | 8 | 8 | bcast `8` | CONFIRMED |
| `0x20` | `0x39c` | `R[5]` VectorAluAny | 1 | 1 | bcast `1` (shares offset with `0x15`) | CONFIRMED |

The seven 8-cycle cells are the MXU matprep/matmul/matrix-result throughput ports (`0x00, 0x05, 0x0b, 0x17, 0x1b, 0x1c, 0x1f`); the nine 1-cycle cells are the vector/EUP/cross-lane result stages. Two pairs share an offset (`0x05`/`0x0b` → `0x92c`; `0x15`/`0x20` → `0x39c`). None of the priced offsets is `0x28` or `0x2c`, so the Dragonfish two-cell override never touches a throughput cell.

> **NOTE — these values were re-derived, not transcribed.** The full `PerformanceJf` image was rebuilt by emulating the base sentinel fill plus every constructor store (verified `vmovaps`/`vbroadcastss`/`movabs` stream from `0x1d4930c0`), then read back at `offsetLUT[cls]`. The MXU-band stores are explicit in the constructor: `[+0x910] ← xmmword_A2DA220 {8,8,8,1}`, `[+0x920..+0x980] ← bcast 8`, `[+0x940] ← xmmword_A2CF810 {8,1,1,8}`. The 16-byte source blocks were read directly from `.rodata` and all 15 match (see below).

### The 15 PerformanceJf constructor source blocks

Each is four `int32`. Read byte-exact from `.rodata`:

| Block | Value | Block | Value | Block | Value |
|-------|-------|-------|-------|-------|-------|
| `@0xa2c2f40` | `{5,1,1,1}` | `@0xa2c5b90` | `{1,1,1,4}` | `@0xa2c5ba0` | `{1,1,5,5}` |
| `@0xa2c8a30` | `{4,105,7,92}` | `@0xa2c8a40` | `{2,1,1,1}` | `@0xa2cea00` | `{2,2,1,1}` |
| `@0xa2cf810` | `{8,1,1,8}` | `@0xa2d2090` | `{1,1,4,4}` | `@0xa2d2df0` | `{1,1,1,2}` |
| `@0xa2d3c30` | `{8,1,1,1}` | `@0xa2d7660` | `{1,1,8,1}` | `@0xa2da220` | `{8,8,8,1}` |
| `@0xa2daf10` | `{4,2,1,1}` | `@0xa2db650` | `{1,1,2,2}` | `@0xa2dcd30` | `{88,8,4,1}` |

The constructor issues 116 store ops (17 block copies + 81 broadcast fills + 9 `movabs` qwords + 9 dword imms) writing exactly 419 distinct `int32` cells with no overlap; the remaining cells stay at the `0x7FFFFFFF` sentinel. Only ~31 of the 419 cells are semantically bound (16 throughput-LUT targets + ~15 latency-table cells); the rest are populated but their consumer is not yet mapped (LOW confidence on per-cell role).

### The Dragonfish delta

`PerformanceDf::PerformanceDf` @ `0x1d493060` calls `PerformanceJf::PerformanceJf`, swaps the vtable, then issues exactly one store:

```c
// PerformanceDf::PerformanceDf @ 0x1d493060 (decompiled, exact)
PerformanceJf::PerformanceJf(this, ids);
*(void **)this = /* Df vtable */;
*((uint64_t *)this + 5) = 0xD00000042ULL;             // byte offset 0x28
```

`*((uint64_t*)this + 5)` is byte offset `5 × 8 = 0x28`; the `int64` `0x0000000D_00000042` decodes to `[+0x28] = 0x42 = 66` and `[+0x2c] = 0x0d = 13` (down from JF's `88` / `8`). These two cells feed the matmul/matprep *base-latency* slots of the `LatencyTableJellyfish`, not the throughput LUT — the cost-model reflection of the v2→v3 MXU-count/clock change. Every other cell is identical (verified by diffing the two rebuilt images: exactly two cells change).

---

## Pufferfish — `switch` Over `GetResourceUsage` (per-class CONFIRMED)

From Pufferfish on, `GetCyclesForThroughput` is a `switch` over the cycle class; each case names a `Performance` instruction id and a resource and reads the grid through `GetResourceUsage`:

```c
// xla::jellyfish::PfCycleTable::GetCyclesForThroughput @ 0x1c89de60 (decompiled, exact shape)
switch (cls) {
  case 0:        return PufferfishPerformance::GetResourceUsage(perf, 125, 9);
  case 1:        return PufferfishPerformance::GetResourceUsage(perf, 137, 9);
  case 5: case 11: return PufferfishPerformance::GetResourceUsage(perf, 220, 11);
  case 6: case 12: return PufferfishPerformance::GetResourceUsage(perf, 223, 11);
  case 9: case 15: return PufferfishPerformance::GetResourceUsage(perf, 224, 11);
  case 10: case 16: LogFatal("Unsupported PushGainsS4.", /*cycle_table.cc:575*/);
  case 23:       return PufferfishPerformance::GetResourceUsage(perf, 260, 11);
  case 24:       return PufferfishPerformance::GetResourceUsage(perf, 107, 3);
  case 26:       return PufferfishPerformance::GetResourceUsage(perf, 106, 3);
  case 27:       return PufferfishPerformance::GetResourceUsage(perf, 264, 11);
  case 28:       return PufferfishPerformance::GetResourceUsage(perf, 244, 11);
  case 29:       return PufferfishPerformance::GetResourceUsage(perf, 252, 11);
  case 31:       return PufferfishPerformance::GetResourceUsage(perf, 262, 11);
  default:       return 1;
}
```

The `(instruction_id, resource)` pairs are the primary constants. Resolving each against the Pufferfish `Performance` latency array gives the per-class cost:

| Class | `(instr, res)` | Perf value | Role | Confidence |
|------:|----------------|-----------:|------|------------|
| `0x00` | `(125, 9)` | 101 | bf16 matprep | CONFIRMED |
| `0x01` | `(137, 9)` | 101 | bf16 matprep' | CONFIRMED |
| `0x05`/`0x0b` | `(220, 11)` | 7 | bf16 latch | CONFIRMED |
| `0x06`/`0x0c` | `(223, 11)` | 7 | int8 latch | CONFIRMED |
| `0x09`/`0x0f` | `(224, 11)` | 7 | fp8 latch | CONFIRMED |
| `0x0a`/`0x10` | — | fatal | `PushGainsS4` (`cycle_table.cc:575`) | CONFIRMED |
| `0x17` | `(260, 11)` | 69 | matrix-result TC | CONFIRMED |
| `0x18` | `(107, 3)` | 7 | r/w transpose | CONFIRMED |
| `0x1a` | `(106, 3)` | 7 | lane compare | CONFIRMED |
| `0x1b` | `(264, 11)` | 79 | matrix-result primary | CONFIRMED |
| `0x1c` | `(244, 11)` | 126 | matrix-result secondary | CONFIRMED |
| `0x1d` | `(252, 11)` | 126 | EUP primary | CONFIRMED |
| `0x1f` | `(262, 11)` | 69 | transcendental class | CONFIRMED |

The resource arguments expose the lane: `9` = matmul-issue (PF only — later gens use `11`), `11` = XLU MRB, `3` = XLU input slot. The `PushGainsS4` cases were declared in the enum but their cost is intentionally a fatal log, keeping those modes out of the schedulable set.

> **NOTE — the `(instr, res)` value differs from the cross-gen throughput column for the same class.** `GetResourceUsage(125, 9)` returns `101` (instruction 125's resource-`9` cell, the bf16-matprep view), whereas the [per-class throughput table](#per-class-throughput-across-generations) below lists Pufferfish class `0x00` as `79`. The two numbers are different columns of the 2D `(instruction × resource)` grid; the throughput row used by the scheduler reads a different resource slot than the `res=9` matmul-issue cell. A reimplementer must not assume the two views are the same number. Both are byte-anchored against the Pufferfish `Performance` array; the per-resource cell that the bundle cost model actually consumes is flagged PARTIAL in the [2D-grid section](#per-gen-latency-array-distributions-confirmed-counts-full-2d-partial).

---

## Per-Class Throughput Across Generations

Decoded from each gen's `GetCyclesForThroughput` (or `…Helper`) switch, cross-referenced against the per-gen `Performance` latency arrays. These are per-bundle-issue throughput cycles, not per-instruction latency.

| Class | Role | JF/DF | Puff | Vip | Glite | `6acc60406` | Confidence |
|------:|------|------:|-----:|----:|------:|---------:|------------|
| `0x00` | Vector matprep, bf16 | 8 | 79 | 131 | 192 | 212 | CONFIRMED |
| `0x05` | Latch, bf16 | 8 | 79 | 131 | 192 | 212 | CONFIRMED |
| `0x0b` | Transposed bf16 latch | 8 | 79 | 131 | 192 | 212 | CONFIRMED |
| `0x09` | Latch, fp8 | (dflt 1) | — | 114 | 192 | 204 | CONFIRMED |
| `0x12`–`0x16` | XLU rot / shuffle / bcast / reduce | 1 | 1 | 1 | 1 | 2 | CONFIRMED |
| `0x17` | Matrix-result read (TC) | 8 | 53 | 114 | 192 | 212 | CONFIRMED |
| `0x18`/`0x19`/`0x1a` | RW-xpose / cross-lane / lane-cmp | 1 | 1 | 1 | 1 | 1 | CONFIRMED |
| `0x1b` | Matrix-result, primary | 8 | 79 | 115 | 122 | 127 | CONFIRMED |
| `0x1c` | Matrix-result, secondary | 8 | 30 | 30 | 49 | 49 | CONFIRMED |
| `0x1d` | EUP unary primary | — | 1 | 1 | 13 | 10 | CONFIRMED |
| `0x1e` | EUP unary secondary | — | — | — | 13 | 11 | CONFIRMED |
| sin/cos (scalar) | transcendental estimate | 198 | 198 | 154 | 142 | 142 | CONFIRMED |
| tan (scalar) | transcendental estimate | 219 | 219 | 170 | 151 | 151 | CONFIRMED |

`—` means the gen does not implement that class (the switch falls through to default `1`). The matmul/matprep clusters match the per-format MXU cycles: **bf16** Vf=131 / Gl=192 / Gf=212; **fp8** Vf=114–115 / Gl=192 / Gf=204; **fp32** shares bf16. These cross-check against the [matmul mode modifiers](matmul-mode-modifiers.md) and the per-gen MXU latency tables ([JF/DF](mxu-latency-jf-df.md), and the per-gen `Performance` pages [PF](performance-pf.md), [VF](performance-vf.md)).

### Transcendental scalar costs (virtual overrides, CONFIRMED)

`EstimateSinCosCost`/`EstimateTanCost` are per-gen `const` virtuals returning a fixed estimate regardless of operand size:

| Gen | SinCos | Tan | Functions |
|-----|-------:|----:|-----------|
| Jellyfish / Dragonfish | 198 | 219 | `0x1c89dd20` / `0x1c89dd40` |
| Pufferfish | 198 | 219 | `0x1c89dfc0` / `0x1c89dfe0` |
| Viperfish | 154 | 170 | `0x1c89e480` / `0x1c89e4a0` |
| Ghostlite | 142 | 151 | `0x1c89ea00` / `0x1c89ea20` |
| `6acc60406` | 142 | 151 | `0x1c89f0e0` / `0x1c89f100` |

Each value was read from the function body (`return <imm>;`). The trend (sin/cos shrinking ~25 % from PF to GF) tracks the XLU pipeline / issue-throughput speedups across gens.

---

## Per-Gen Latency-Array Distributions (CONFIRMED counts; full 2D PARTIAL)

The per-gen `Performance` object also holds the *latency* array the `LatencyTable*` reads. The value histograms below were extracted by parsing every `mov dword [base+off], imm` in each constructor. Constructor addresses and array sizes:

| Gen | Performance class | Ctor | num_instr | latency bytes | resource_count | Confidence |
|-----|-------------------|------|----------:|--------------:|---------------:|------------|
| Pufferfish | `xla::pufferfish::PufferfishPerformance` | `0x1c8be080` | 336 | 1344 | 20 | CONFIRMED |
| Viperfish | `xla::viperfish::ViperfishPerformance` | `0x1c8c4840` | 384 | 1536 | 28 | CONFIRMED |
| Ghostlite | `xla::ghostlite::GhostlitePerformance` | `0x1c8cbc80` | 476 | 1904 | 31 | CONFIRMED |
| `6acc60406` | (`gxc::gfc` Performance, unnamed `sub_1C8D3740`) | `0x1c8d3740` | 465 | 1860 | 31 | CONFIRMED |

Notable clusters in the latency histograms (full per-value tables omitted for brevity): Pufferfish concentrates at `1` (144 entries), `83`/`101` (48 each), `126` (16); Viperfish at `1`/`2` (147/113), `121` (25), `131` (27); Ghostlite at `1`/`2` (149/181), `182` (24), `192` (27); `6acc60406` at `1`/`2` (154/198), `204` (12), `212` (15). The 200+ cycle entries cluster at instruction ids 289..330 — the bf16/fp8 MXU latch/matmul/matres ops.

> **PARTIAL — the full `(instruction × resource)` 2D grids are not enumerated here.** Each `Performance` constructor issues 599 (PF) / 759 (VF) / 831 (GL) / 759 (GF) dword stores split between the latency array and the 2D `resource_usage` array; this page pins the per-class throughput constants (the named switch cases, CONFIRMED) and the latency-value distributions (CONFIRMED counts) but not every per-resource cell. Extraction is mechanical against `Performance::GetResources()::kResources` and is left as a follow-up. The per-gen `Performance` pages ([PF](performance-pf.md), [VF](performance-vf.md)) track this.

---

## How The Bundle-Latency Cost Model Sums Them

The cycle constants feed a two-layer accounting (the design is detailed in [bundle-aware cost](bundle-aware-cost.md)):

1. **Op level.** For each LLO op, the emitter computes its cycle class (`CycleTableInstruction` for MXU ops; emitter `Record*` paths for vector/memory ops), reads `GetCyclesForThroughput(class)`, and adds `(double)cycles` into the op's `ResourceVector` at slot `GetResource(class)`. This is the `AccumulateInstructionUsage` → `ResourceVector::Acc` path (`[rdi + Resource*8] += cycles`).
2. **Bundle level.** A VLIW bundle is one issue slot from the rate perspective, but multiple ops share it. The scheduler combines per-op resource vectors so that **same-resource costs add** (sequential on that functional unit) and **different-resource costs overlap** (the bundle's contribution is the per-lane max). The slowest occupied lane determines the bundle's issue cost.

Memory-transfer ops are formula-based rather than a single constant: the per-byte cycle is `bytes_per_window / (lane_count × dtype_bytes) × per-lane-cycle`, composed in the `cost_model_util::Record*MemXferCycles` family. Matmul cost is `K_tile × per-(format, mxu) cycle`. Transcendentals add the scalar `EstimateSinCosCost()` / `EstimateTanCost()` on top of a per-step pipeline count. The absolute-time conversion uses `Target::TensorCoreFrequencyInMegaHertz()` (cycles → microseconds via the `1.0e6` factor at `qword_A2E0208`); the cost model itself works purely in cycles.

> **NOTE — the shipped cost model is data-table driven, not ML.** A `LearnedCostModelClientOptions` *proto* exists for configuration, but no `LearnedCostModelClient` class instance ships in this binary (verified by symbol grep — only proto message methods). Every cost number above is a static `.rodata` constant; the "learned" code paths fall back to these tables. See [learned cost-model client](learned-cost-model-client.md).

---

## Cross-References

- [CycleTable Family](cycletable-family.md) — the per-gen dispatcher that reads these constants; the cycle-class enum and resource LUT.
- [JfCycleTable](jf-cycletable.md) — the JF/DF flat offset-LUT read path in full.
- [VfCycleTable](vf-cycletable.md) — the Viperfish `switch`-over-`GetResourceUsage` path.
- [Performance Family Overview](performance-overview.md) · [Performance JF/DF](performance-jf-df.md) · [PF](performance-pf.md) · [VF](performance-vf.md) — the grids that hold these arrays.
- [MXU Latency Overview](mxu-latency-overview.md) · [MXU Latency JF/DF](mxu-latency-jf-df.md) — per-`(MatmulModifier × Resource)` matmul/matprep overrides.
- [Matmul Mode Modifiers](matmul-mode-modifiers.md) — the `MatmulDataFormat` / `GainLatchMode` modifiers that index the MXU cycle clusters.
- [Resource Enum](resource-enum.md) — the `ResourceVector` slots the per-op cycles accumulate into.
- [Bundle-Aware Cost](bundle-aware-cost.md) — the op-level vs bundle-level summation rule.
- [Learned Cost-Model Client](learned-cost-model-client.md) — why these static tables, not an ML predictor, drive the shipped cost model.
- [Bundle Model Overview](../isa/bundle-model-overview.md) — the VLIW bundle layer.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VII — Cost & Latency Model / Core model — [back to index](../index.md)
