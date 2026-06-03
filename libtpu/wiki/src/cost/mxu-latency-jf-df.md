# MXU Latency: JF / DF

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). Other versions differ. Every integer below was read out of `.rodata` by hand and cross-checked against the IDA decompile; the verification status is in the Confidence columns.*

## Abstract

This page documents the oldest-generation TensorCore MXU cost model: the **Jellyfish (TPU v2)** and **Dragonfish (TPU v3)** latency/throughput tables. Unlike every later generation, JF and DF do not use a heap-allocated `latency[]` array plus a 2-D `Instruction × Resource` reservation grid (the model that begins at Pufferfish — see [Performance: PF](performance-pf.md)). They use a single fixed-size `0xe00`-byte inline POD struct — `platforms_deepsea::jellyfish::isa::Performance` — whose per-instruction throughput is a **flat one-cell-per-`Instruction` lookup**, and a separate `LatencyTableJellyfish` whose conflict-edge model is **fifteen integers copied out of that same struct**.

The reimplementer's view is two tables driven by one struct:

- **Throughput** — `JfCycleTable::GetCyclesForThroughput(Instruction)` reads `Performance[offsetLUT[Instruction]]`, gated by a 33-bit valid-instruction mask. Sixteen of the 33 `CycleTable::Instruction` ordinals are priced from the struct; the rest return the default `1`. The MXU matprep/matmul/matrix-result ports cost **8 cycles**; the vector/EUP result stages cost **1**.
- **Latency** — `LatencyTableJellyfish` copies fifteen cells out of the singleton `Performance` into its own `[+0x18..+0x50]` field block. Two of those — the **matmul base latency** and the **matprep base latency** — are the *only* two integers DF changes relative to JF: `PerformanceDf` overwrites `Performance[+0x28] 88→66` and `[+0x2c] 8→13`. Everything else in the JF and DF cost model is byte-identical.

Both tables are served by the same `JfCycleTable` (`xla::jellyfish::JfCycleTable`, vtable `0x21c1ffb8`); `CycleTable::Create` registers one `JfCycleTable` for both v2 and v3. The `JfCycleTable` vtable carries **no** `GetLatency` slot — the latency axis lives entirely in `LatencyTableJellyfish`. The two halves join in the matmul-rewrite / MRB-accumulation consumers, which read both the throughput cell and the copied latency fields.

If you have read the LLVM scheduling itineraries (`InstrItineraryData`), the closest analog is a per-itinerary-class operand-cycle table plus a separate forwarding/bypass latency table — except here both are baked as `.rodata` constants and the "itinerary class" is the `CycleTable::Instruction` enum, an MXU-modifier expansion rather than an opcode.

| | |
|---|---|
| **Throughput reader** | `JfCycleTable::GetCyclesForThroughput(Instruction)` `0x1c89dce0` |
| **Throughput formula** | `valid = (I < 0x21) && ((0x19FFC0821 >> I) & 1)`; `valid ? Performance[offsetLUT[I]] : 1` |
| **offsetLUT** | `0xb438b70` (33 × int64, `Instruction → Performance` byte-offset) |
| **resLUT** | `0xb438aec` (33 × int32, `Instruction → Resource` column 0..6) |
| **Performance struct** | `0xe00` bytes, inline POD; base sentinel `0x7fffffff`; `PerformanceJf` ctor `0x1d4930c0`, `PerformanceDf` ctor `0x1d493060` |
| **Latency table** | `LatencyTableJellyfish` ctor `0x1c8a0c20` — copies 15 `Performance` cells into `[+0x18..+0x50]` |
| **MXU issue (throughput)** | 8 cycles/op for priced matprep/matmul/matres formats |
| **MXU matmul base latency** | JF **88** / DF **66** (`Performance[+0x28]` → `LatencyTable[+0x50]`) |
| **MXU matprep base latency** | JF **8** / DF **13** (`Performance[+0x2c]` → `LatencyTable[+0x4c]`) |
| **EUP push→pop edge** | 4 cycles (`Performance[+0x30]` → `LatencyTable[+0x1c]`) |
| **JF→DF delta** | exactly 2 int32 cells (`+0x28`, `+0x2c`); throughput table byte-identical |

---

## The Throughput Reader

### GetCyclesForThroughput

`JfCycleTable::GetCyclesForThroughput` is a four-line function. It bounds the `Instruction` ordinal, tests it against a 33-bit valid mask, and on a hit indexes the `Performance` struct (held at `JfCycleTable+0x10`) by a byte-offset taken from a 33-entry `int64` LUT. On a miss it returns the default of `1`. This is verbatim from the decompile:

```c
// xla::jellyfish::JfCycleTable::GetCyclesForThroughput  @ 0x1c89dce0  (verified)
__int64 JfCycleTable::GetCyclesForThroughput(this, unsigned int instr) {
    if ( ((instr < 0x21) & (uint8_t)(0x19FFC0821uLL >> instr)) == 1 )
        return *(uint32_t*)( *(uint64_t*)(this + 0x10)        // Performance*
                             + qword_B438B70[instr] );         // offsetLUT[instr]
    return 1;                                                   // default
}
```

The valid mask `0x19FFC0821` selects sixteen priced ordinals; the other seventeen short-circuit to `1`. The `offsetLUT` slot for every *unpriced* ordinal is literally `0x0`, which is harmless because the mask test always fails before the read is reached.

> **NOTE —** the bound is `< 0x21` (33 ordinals, `0x00..0x20`) and the mask is exactly 33 bits wide. Ordinals `≥ 0x21` are never priced here; they belong to other `CycleTable` paths. A reimplementation must apply the bound *and* the mask — relying on the offsetLUT alone would read `Performance[0]` (the vtable) for unpriced ordinals.

### The Resource Column

`CycleTable::GetResource` is a single flat lookup — each `Instruction` maps to one of seven `Resource` columns:

```c
// xla::jellyfish::CycleTable::GetResource  @ 0x1c89ce20  (verified)
__int64 CycleTable::GetResource(this, int instr) {
    return dword_B438AEC[instr];          // resLUT[instr], 33 x int32, values 0..6
}
```

The seven distinct values `0..6` are not a private enum — they are slot indices into the 23-slot `ResourceVector`. The only consumer, `AccumulateInstructionUsage`, calls `ResourceVector::Acc(GetResource(I), (double)GetCyclesForThroughput(I))`, and `Acc` indexes `[ResourceVector + Resource*8]` with a hard bound of 23:

```c
// xla::jellyfish::ResourceVector::Acc  @ 0x1c89adc0  (verified)
__int64 ResourceVector::Acc(this, unsigned int resource, double cycles) {
    if ( resource >= 0x17 ) __ud1();      // bound 0x17 = 23 ResourceVector slots
    this[resource] += cycles;             // vaddsd [rdi + resource*8]
    return resource;
}
```

So the seven JF/DF `Resource` columns are the first seven `ResourceVector` slots: `R[0]` Matpush, `R[1]` Matmul, `R[2]` Xlu, `R[3]` VectorAlu0, `R[4]` VectorAlu1, `R[5]` VectorAluAny, `R[6]` VectorEup. The JF cost model populates only the MXU/vector head of the 23-slot accumulator.

---

## The Integer Throughput Table

This is the full reconstruction of `JfCycleTable::GetCyclesForThroughput` over all 33 `CycleTable::Instruction` ordinals. The `offsetLUT` (`0xb438b70`) and `resLUT` (`0xb438aec`) columns were read byte-for-byte out of `.rodata`; the cycle value is the priced cell resolved in the reconstructed `PerformanceJf` image. JF and DF are identical for every cell (none of the priced offsets is `+0x28` or `+0x2c`, the only two DF overrides).

| `Instr` | `offsetLUT[I]` | `Res` | `ResourceVector` slot | priced | JF cyc | DF cyc | Confidence |
|---|---|---|---|---|---|---|---|
| `0x00` | `0x910` | `r1` | Matmul | yes | 8 | 8 | CERTAIN |
| `0x01` | `0x000` | `r1` | Matmul | no | 1 | 1 | CERTAIN |
| `0x02` | `0x000` | `r1` | Matmul | no | 1 | 1 | CERTAIN |
| `0x03` | `0x000` | `r1` | Matmul | no | 1 | 1 | CERTAIN |
| `0x04` | `0x000` | `r1` | Matmul | no | 1 | 1 | CERTAIN |
| `0x05` | `0x92c` | `r0` | Matpush | yes | 8 | 8 | CERTAIN |
| `0x06` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x07` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x08` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x09` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x0a` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x0b` | `0x92c` | `r0` | Matpush | yes | 8 | 8 | CERTAIN |
| `0x0c` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x0d` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x0e` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x0f` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x10` | `0x000` | `r0` | Matpush | no | 1 | 1 | CERTAIN |
| `0x11` | `0x000` | `r6` | VectorEup | no | 1 | 1 | CERTAIN |
| `0x12` | `0x33c` | `r4` | VectorAlu1 | yes | 1 | 1 | CERTAIN |
| `0x13` | `0x340` | `r4` | VectorAlu1 | yes | 1 | 1 | CERTAIN |
| `0x14` | `0x344` | `r3` | VectorAlu0 | yes | 1 | 1 | CERTAIN |
| `0x15` | `0x39c` | `r5` | VectorAluAny | yes | 1 | 1 | CERTAIN |
| `0x16` | `0x398` | `r5` | VectorAluAny | yes | 1 | 1 | CERTAIN |
| `0x17` | `0x954` | `r2` | Xlu | yes | 8 | 8 | CERTAIN |
| `0x18` | `0x3f8` | `r6` | VectorEup | yes | 1 | 1 | CERTAIN |
| `0x19` | `0x368` | `r5` | VectorAluAny | yes | 1 | 1 | CERTAIN |
| `0x1a` | `0x3f4` | `r6` | VectorEup | yes | 1 | 1 | CERTAIN |
| `0x1b` | `0x960` | `r2` | Xlu | yes | 8 | 8 | CERTAIN |
| `0x1c` | `0x94c` | `r2` | Xlu | yes | 8 | 8 | CERTAIN |
| `0x1d` | `0x000` | `r2` | Xlu | no | 1 | 1 | CERTAIN |
| `0x1e` | `0x000` | `r2` | Xlu | no | 1 | 1 | CERTAIN |
| `0x1f` | `0x958` | `r2` | Xlu | yes | 8 | 8 | CERTAIN |
| `0x20` | `0x39c` | `r5` | VectorAluAny | yes | 1 | 1 | CERTAIN |

The sixteen priced ordinals are exactly `{0x00, 0x05, 0x0b, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1f, 0x20}`. The seven **8-cycle** cells are the MXU matprep/matmul/matrix-result throughput ports (`0x00`, `0x05`, `0x0b`, `0x17`, `0x1b`, `0x1c`, `0x1f`); the nine **1-cycle** priced cells are vector-ALU / EUP result stages.

> **QUIRK — two ordinals share one cell.** `Instr 0x15` and `Instr 0x20` both read `offsetLUT = 0x39c` (VectorAluAny, value 1). The flat-cell model does not require distinct offsets per ordinal; reservation columns and throughput cells are decoupled.

The `Instr 0x00..0x10` band (the MXU ordinals) is produced by the MXU classifier `CycleTableInstruction` (`0x1c89ca80`), which maps matmul opcodes `0x8d..0x96` through a `GainLatchMode → Instruction` LUT and matprep/matpush opcodes `0x9b..0xa5` through a `MatmulDataFormat → Instruction` LUT (both decoded on [JfCycleTable](jf-cycletable.md)). The non-MXU band `0x11..0x20` is emitted by the HLO-level cost model as direct ordinal immediates — see [IARs Per TensorCore](iars-per-tensorcore.md) for the full producer breakdown.

---

## The Performance Struct and the JF→DF Delta

### Layout

`Performance` is a `0xe00`-byte (3584-byte) inline POD with the vtable at `+0x00`, `DeviceIdentifiers` at `+0x08..+0x14`, and the per-`Instruction` cost buffer over `[+0x18 .. +0xdf8]` (890 `int32` slots). The base constructor `Performance::Performance` (`0x1d492900`) `memset`-fills the whole buffer with the sentinel **`0x7fffffff`** (= `INT_MAX`, *not* `0xffffffff`), broadcast from `.rodata @0x84a2f60` (read byte-exact as `0x7fffffff`). `PerformanceJf` (`0x1d4930c0`) then overwrites cells by copying pre-baked 16-byte `.rodata` blocks, broadcast fills (scalars `@0x84a2b08 = 1`, `@0x84a2854 = 2`, `@0x84a2d0c = 8`, all read byte-exact), and immediate stores — 116 store ops touching 419 distinct `int32` slots; the other 471 stay at the sentinel.

The throughput-relevant constant blocks, read directly out of `.rodata`:

| `.rodata` block | bytes (4 × int32) | lands at | role |
|---|---|---|---|
| `@0xa2c8a30` | `{4, 105, 7, 92}` | `Performance[+0x18]` | RPU producer / matres-self conflict floors |
| `@0xa2dcd30` | `{88, 8, 4, 1}` | `Performance[+0x28]` | matmul base (`+0x28`), matprep base (`+0x2c`), EUP edge (`+0x30`) |
| `@0xa2da220` | `{8, 8, 8, 1}` | `Performance[+0x910]` | MXU throughput band head (`Instr 0x00`) |
| `@0xa2cf810` | `{8, 1, 1, 8}` | `Performance[+0x940]` | MXU throughput cell `[+0x94c]` (`Instr 0x1c`) |

### The DF Override

`PerformanceDf` is `PerformanceJf` plus a vtable swap and exactly one quadword store. From the decompile:

```c
// platforms_deepsea::jellyfish::isa::PerformanceDf::PerformanceDf  @ 0x1d493060  (verified)
PerformanceJf::PerformanceJf(this, dev);          // build the full JF image first
*(uint64_t*)this        = off_21CC7478;           // swap the vtable
*((uint64_t*)this + 5)  = 0xD00000042uLL;         // store at this+0x28 (= int32[5..6])
```

`this + 5` (qword) is `Performance[+0x28]`. The qword `0xD00000042` writes `[+0x28] = 0x42 = 66` and `[+0x2c] = 0x0D = 13`. So:

| cell | JF | DF | role | Confidence |
|---|---|---|---|---|
| `Performance[+0x28]` | 88 | **66** | MXU matmul base latency | CERTAIN |
| `Performance[+0x2c]` | 8 | **13** | MXU matprep base latency | CERTAIN |

Every other one of the 419 populated slots is byte-identical JF vs DF. Because neither `+0x28` nor `+0x2c` is an `offsetLUT` target, `GetCyclesForThroughput` returns the same value on JF and DF for all 16 priced ordinals — the entire v2→v3 cost difference is these two latency-table integers.

> **NOTE — the v2→v3 MXU change shows up only in the matmul base latency.** Dragonfish doubles the MXU count (1→2) and raises the TensorCore clock. The throughput cell stays at 8 cycles/op; the speedup is encoded as a *lower* matmul base latency (88→66) with a slightly *higher* matprep base (8→13). A reimplementation that scales the throughput cell for DF would double-count the v3 advantage.

---

## The LatencyTableJellyfish Copy Map

`LatencyTableJellyfish` is the conflict-edge / forwarding latency model. Its constructor copies fifteen cells from the singleton `Performance` (built once by `Performance::CreateTensorCore` and cached as the file-scope `TpuPerformanceTable(version)` table) into its own `[+0x18..+0x50]` field block. These fields are what `LatencyTableJellyfish::LatencyBetweenInternal` (`0x1c8a0d60`) reads as the per-edge latency floor — `r15` is the `LatencyTable*`, not the `Performance*`. The copy is verbatim from the constructor decompile; the `Performance` source is indexed as `int32[]` (`v2[k]` = `Performance[k*4]`):

```c
// xla::jellyfish::LatencyTableJellyfish::LatencyTableJellyfish  @ 0x1c8a0c20  (verified)
v2 = TpuPerformanceTable(version)::table;          // = Performance::CreateTensorCore(...)
LT[+0x18] = *(int32*)(v2+0x44); // Performance[+0x44]  (raw byte add, not v2[17])
LT[+0x1c] = v2[12];   // Performance[+0x30]   ← EUP push→pop edge
LT[+0x20] = v2[9];    // Performance[+0x24]
LT[+0x24] = v2[7];    // Performance[+0x1c]
LT[+0x28] = v2[8];    // Performance[+0x20]
LT[+0x2c] = v2[597];  // Performance[+0x954]  ← MXU-result throughput cell (Instr 0x17)
LT[+0x30] = v2[595];  // Performance[+0x94c]  ← MXU-result throughput cell (Instr 0x1c)
LT[+0x34] = v2[580];  // Performance[+0x910]  ← matprep throughput cell  (Instr 0x00)
LT[+0x38] = v2[587];  // Performance[+0x92c]  ← matmul  throughput cell  (Instr 0x05)
LT[+0x3c] = v2[456];  // Performance[+0x720]  ← xpose-result cell
LT[+0x40] = v2[455];  // Performance[+0x71c]  ← xpose-result B cell
LT[+0x44] = v2[262];  // Performance[+0x418]
LT[+0x48] = v2[264];  // Performance[+0x420]
LT[+0x4c] = v2[11];   // Performance[+0x2c]   ← MXU matprep base  (JF 8 / DF 13)
LT[+0x50] = v2[10];   // Performance[+0x28]   ← MXU matmul  base  (JF 88 / DF 66)
```

The resolved values (JF / DF), with the role inferred from the `LatencyBetweenInternal` predicate that reads each field:

| `LatencyTable` off | ← `Performance` off | JF | DF | role | Confidence |
|---|---|---|---|---|---|
| `+0x18` | `+0x44` | 1 | 1 | matres-result FIFO floor (UNVERIFIED role) | HIGH |
| `+0x1c` | `+0x30` | 4 | 4 | **EUP push→pop edge** | CERTAIN |
| `+0x20` | `+0x24` | 92 | 92 | RPU-result floor | HIGH |
| `+0x24` | `+0x1c` | 105 | 105 | RPU op→op / matres-self conflict | HIGH |
| `+0x28` | `+0x20` | 7 | 7 | UsesRpu producer floor | HIGH |
| `+0x2c` | `+0x954` | 8 | 8 | MXU-result cell (`Instr 0x17`) | CERTAIN |
| `+0x30` | `+0x94c` | 8 | 8 | MXU-result cell (`Instr 0x1c`) | CERTAIN |
| `+0x34` | `+0x910` | 8 | 8 | matprep throughput cell (`Instr 0x00`) | CERTAIN |
| `+0x38` | `+0x92c` | 8 | 8 | matmul throughput cell (`Instr 0x05`) | CERTAIN |
| `+0x3c` | `+0x720` | 8 | 8 | xpose-result cell | HIGH |
| `+0x40` | `+0x71c` | 8 | 8 | xpose-result B cell | HIGH |
| `+0x44` | `+0x418` | 8 | 8 | branch-op cell | HIGH |
| `+0x48` | `+0x420` | 8 | 8 | branch-op cell | HIGH |
| `+0x4c` | `+0x2c` | **8** | **13** | **MXU matprep base** | CERTAIN |
| `+0x50` | `+0x28` | **88** | **66** | **MXU matmul base** | CERTAIN |

Three of the first columns (`+0x20`/`+0x24`/`+0x28` ← `Performance[+0x24]`/`[+0x1c]`/`[+0x20]`) come from the head block `@0xa2c8a30 = {4,105,7,92}` (note the `92`/`105`/`7` reorder through the copy indices, with the leading `4` at `Performance[+0x18]` left uncopied); `+0x18` (← `Performance[+0x44]` = `1`) is the second cell of the `@0xa2c8a40 = {2,1,1,1}` block, not the head block. The last two (`+0x4c`/`+0x50`) come from `@0xa2dcd30 = {88,8,4,1}` and are the only two the DF override touches. The version guard at the top of the constructor `CHECK`s `tpu_version_ ∈ {kJellyfish, kDragonfish}`, confirming this table serves only v2/v3.

> **GOTCHA — the matmul base and matprep base are *swapped* in field order vs struct order.** `Performance[+0x28]` (matmul base) copies to the *higher* `LatencyTable[+0x50]`, and `Performance[+0x2c]` (matprep base) to the *lower* `LatencyTable[+0x4c]`. A reimplementation that copies the `{88,8,...}` block linearly into the latency-table tail will transpose the matmul/matprep base latencies.

### The EUP Push→Pop Edge

`Performance[+0x30] = 4` (the third element of `@0xa2dcd30 = {88,8,4,1}`) copies to `LatencyTable[+0x1c]`. `LatencyBetweenInternal` raises the edge latency to this `4` when the producer is an EUP push (opcode `0x128..0x13a`) and the consumer is the `0x14e` EUP-result pop (or a pseudo-EUP). The function also applies an *independent* hardcoded minimum floor of `4` (`mov r12d, 0x4`, raised to `5` on a `SetIar`-followed-by-indexed-load). Both `4`s coincide on JF/DF. See [EUP Latency Overview](eup-latency-overview.md) and [EUP Per-Gen Integers](eup-per-gen-integers.md) for the per-gen progression `4 (JF/DF) → 7 (PF) → 6 (VF) → 13/14 (GL)`.

---

## Transcendental Estimate Costs

The `JfCycleTable` vtable carries two transcendental estimate slots beyond the throughput reader. Both are constant-return functions, read verbatim:

| Function | vtable slot | value | Confidence |
|---|---|---|---|
| `JfCycleTable::EstimateSinCosCost` `0x1c89dd20` | `+0x18` | **198** (`0xc6`) | CERTAIN |
| `JfCycleTable::EstimateTanCost` `0x1c89dd40` | `+0x20` | **219** (`0xdb`) | CERTAIN |

These price the Payne–Hanek-style range-reduced transcendentals on JF/DF; DF inherits them (one `JfCycleTable` registration serves both gens). See [EUP Payne–Hanek](eup-paynehanek.md).

---

## How JF Differs From Every Later Gen

The flat one-cell-per-`Instruction` model is unique to v2/v3. From Pufferfish onward, `Performance` becomes a heap `latency[]` array plus a 2-D `GetResourceUsage(Instruction, Resource)` grid, and `PfCycleTable::GetCyclesForThroughput` (`0x1c89de60`) wraps `GetResourceUsage` calls rather than a flat offset-LUT read. Viperfish goes further: `VfCycleTable::GetCyclesForThroughput` (`0x1c89e2c0`) dispatches into `viperfish::MxuLatencyTable::GetResourceUsage` (confirmed in the decompile), a `FlatHashMap<Modifier, array<int,19>>` reservation table keyed by `{MatmulDataFormat, is_transpose, Msr}` — the matprep stages there are not flat cells at all but four of nineteen `MxuResource` ports.

| Gen | Codename | TpuVer | throughput model | resource cols | matmul base | matprep base | EUP edge | Confidence |
|---|---|---|---|---|---|---|---|---|
| JF | Jellyfish | v2 | flat inline POD + offset-LUT | 7 | 88 | 8 | 4 | CERTAIN |
| DF | Dragonfish | v3 | = JF + 2 cells | 7 | 66 | 13 | 4 | CERTAIN |
| PF | Pufferfish | v4 | heap `latency[]` + 2-D grid | 20 | (grid) | (grid) | 7 | HIGH |
| VF | Viperfish | v5p | `Modifier → array<19>` reservation | 28 | 131 | reservation | 6 | HIGH |
| GL | Ghostlite | v6e | `Modifier → array<11>` reservation | 31 | (grid) | fixed rows | 13/14 | HIGH |
| GF | 6acc60406 | v7 | `array<11>` reservation | 31 | (grid) | fixed rows | 12 | HIGH |

The systolic contract — weight-stationary 128×128 array, matpush 8×128/4×256 tiles, latch / push / matmul / matres sequence — never changes; only the cost-model encoding widens. See [MXU Latency Overview](mxu-latency-overview.md) for the cross-gen reservation-matrix concept and the per-gen pages [MXU Latency: PF](mxu-latency-pf.md), [VF](mxu-latency-vf.md), [GL](mxu-latency-gl.md), [GF](mxu-latency-gf.md).

---

## Related Components

| Component | Relationship |
|---|---|
| `JfCycleTable::GetCyclesForThroughput` `0x1c89dce0` | the throughput reader (offsetLUT → `Performance` cell) |
| `CycleTable::GetResource` `0x1c89ce20` | the `Instruction → Resource` column LUT (`0xb438aec`) |
| `ResourceVector::Acc` `0x1c89adc0` | the consumer that accumulates throughput into the 23-slot vector |
| `PerformanceJf` ctor `0x1d4930c0` / `PerformanceDf` ctor `0x1d493060` | the inline-POD fill and the 2-cell DF override |
| `LatencyTableJellyfish` ctor `0x1c8a0c20` | the 15-field `Performance → LatencyTable` copy map |
| `LatencyTableJellyfish::LatencyBetweenInternal` `0x1c8a0d60` | the per-edge latency floor reader (EUP edge, matmul/matprep base) |
| `CycleTableInstruction` `0x1c89ca80` | the MXU classifier that produces `Instr 0x00..0x10` |

## Cross-References

- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuResource` enum and the cross-gen reservation-matrix concept this page is the JF/DF specialization of.
- [JfCycleTable](jf-cycletable.md) — the full `offsetLUT`/`resLUT` byte transcription and the MXU-modifier `GainLatchMode`/`MatmulDataFormat → Instruction` classifier LUTs.
- [Performance: JF / DF](performance-jf-df.md) — the full 419-slot `Performance` constant source-block dump and the inline-POD layout.
- [CycleTable Family](cycletable-family.md) — the `CycleTable::Create(TpuVersion)` factory that registers one `JfCycleTable` for v2/v3.
- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector` whose first 7 slots are the JF/DF `Resource` columns.
- [IARs Per TensorCore](iars-per-tensorcore.md) — the non-MXU `Instr 0x11..0x20` band producer and the per-gen register/IAR counts.
- [EUP Latency Overview](eup-latency-overview.md) and [EUP Per-Gen Integers](eup-per-gen-integers.md) — the EUP push→pop edge and the per-gen progression.
- [MXU Latency: PF](mxu-latency-pf.md), [VF](mxu-latency-vf.md), [GL](mxu-latency-gl.md), [GF](mxu-latency-gf.md) — the later-gen reservation models JF/DF predate.
- [../isa/slot-mxu.md](../isa/slot-mxu.md) — the MXU slot whose opcodes feed the `CycleTableInstruction` classifier.
- [../isa/slot-matprep-iar-latch.md](../isa/slot-matprep-iar-latch.md) — the matprep/latch/IAR sub-slots and the per-gen matprep cost divergence.
