# CycleTable Family

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.rodata` addresses are virtual addresses; for this binary `.rodata` VMA == file offset (section `[11]` at `0x84a0000`), and `.text` VMA == file offset.*

## Abstract

`xla::jellyfish::CycleTable` is the abstract base of the per-generation **throughput** half of the TensorCore cost model. Its one job is to translate a *cycle class* — an opaque `CycleTable::Instruction` enum value, not an LLO opcode — into the bundle-issue cycle cost of the slowest functional unit that class occupies. A reimplementer should picture two collaborating objects: the `CycleTable` (this family), which answers "how many issue cycles does cycle-class `I` cost, and which resource lane does it block?", and a per-gen [`Performance`](performance-overview.md) grid, which is the flat `.rodata`-baked array the `CycleTable` actually reads. The `CycleTable` owns no cycle numbers itself; it holds a `Performance*` at `+0x10` and indexes into it.

There is exactly one subclass per `tpu::TpuVersion`. The six registered factories (`TpuVersion` `0..5`) collapse to four distinct read strategies: a flat offset-LUT (`JfCycleTable`, shared by the two oldest gens), and three switch-over-`Performance::GetResourceUsage` forms (`PfCycleTable`, `VfCycleTable`, and the helper-wrapped `GlcCycleTable`/`GfcCycleTable`). The cycle-class enumeration is a dense `0x00..0x20` (33 values) shared across all gens; each gen prices a subset and returns the default `1` cycle for the rest.

This page is the framing reference for the family: it gives the class hierarchy, the registration/dispatch path, the structure of the 33-value cycle-class enum, and how the family sits next to the [`Performance`](performance-overview.md) grids and the [MXU latency](mxu-latency-overview.md) reservation matrices. The per-gen read paths and the baked constants live in [JfCycleTable](jf-cycletable.md), [VfCycleTable](vf-cycletable.md), and [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md).

The contract a reimplementer must honor:

- **One `CycleTable` per `TpuVersion`, selected by a factory registry**, not by `if`-chains. `CycleTable::Create(const Target&)` reads `target.tpu_version()` and invokes the registered lambda; an unregistered version is fatal.
- **The `Instruction` argument is a cycle *class*, not an opcode.** LLO opcodes are first folded into one of 33 classes by `CycleTableInstruction(LloInstruction*)`; only MXU matmul/latch/matprep opcodes are classified there.
- **`GetCyclesForThroughput(I)` is bundle-issue throughput, not dependency latency.** The latency axis is a separate object (`LatencyTable*`); the `JfCycleTable` vtable has *no* latency slot.
- **`GetResource(I)` names the lane the class blocks.** The scheduler adds `(double)cycles` into `ResourceVector[GetResource(I)]`; a bundle's contribution is the max over its slots' lane costs, not the sum.
- **Transcendentals are priced by scalar virtual overrides** (`EstimateSinCosCost`, `EstimateTanCost`), not by the cycle-class LUT.

| | |
|---|---|
| **Abstract base** | `xla::jellyfish::CycleTable` (pure-virtual `GetCyclesForThroughput`) |
| **`JfCycleTable` vtable** | `0x21c1ffb8` — slots: dtor pair, `GetCyclesForThroughput` @ `0x1c89dce0`, `EstimateSinCosCost`, `EstimateTanCost` |
| **Factory** | `CycleTable::Create(const Target&)` @ `0x1c89cc00` — dispatches on `target.tpu_version()` |
| **Registration** | `_GLOBAL__sub_I_cycle_table.cc` @ `0x21353460` — 6 × `FunctionRegistry::Register(version, λ)` |
| **Cycle-class enum** | `CycleTable::Instruction` — dense `0x00..0x20` (33 values); per-gen priced subset |
| **Opcode → class** | `CycleTableInstruction(LloInstruction*)` @ `0x1c89ca80` — MXU band only |
| **Throughput accessor** | `GetCyclesForThroughput(Instruction)` — per-gen virtual; default `1` |
| **Resource accessor** | `CycleTable::GetResource(Instruction)` @ `0x1c89ce20` — flat LUT `dword_B438AEC` |
| **Underlying grid** | per-gen [`Performance`](performance-overview.md) object held at `CycleTable+0x10` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## What A CycleTable Is

The cost model is split into two strictly separated levels (the split is the central design fact of this subsystem):

1. **`CycleTable`** — a thin per-gen virtual dispatcher keyed on a *cycle class*. It answers two questions about a class `I`: `GetCyclesForThroughput(I)` (issue cycles) and `GetResource(I)` (which functional-unit lane). It stores no cycle numbers of its own; it owns a `Performance*`.
2. **[`Performance`](performance-overview.md)** — the per-gen flat array of baked cycle constants, allocated `0xe00` bytes wide and zero-cost to query (`Performance[byte_offset]`). The `CycleTable` is the only thing that knows how to map a class to a byte offset (JF/DF) or an instruction/resource pair (PF and later).

Both the throughput cycle (this family) and the dependency latency (the `LatencyTable*` family) ultimately read the *same* per-gen `Performance` object; they are just different accessor methods over it (`GetCyclesForThroughput`/`GetResourceUsage` vs `GetLatency`/`GetLatencyBetween`). The `JfCycleTable` vtable carries no `GetLatency` slot at all — its slots are throughput (`+0x10`), `EstimateSinCos` (`+0x18`), `EstimateTan` (`+0x20`), and two device-detail accessors. On Jellyfish/Dragonfish the latency axis is supplied entirely by a separate `LatencyTableJellyfish`; see [bundle-aware cost](bundle-aware-cost.md).

A reimplementer should not try to merge the two levels. The `Performance` grid is gen-private data; the `CycleTable` is the gen-private *access pattern* over it. The 33-class enum is the shared vocabulary between them.

---

## The Class Hierarchy And Per-Version Dispatch

```text
                 xla::jellyfish::CycleTable          (abstract base; JfCycleTable vtable @ 0x21c1ffb8)
                          |
   +----------+-----------+-----------+-------------+--------------+
   |          |           |           |             |              |
JfCycleTable (TpuVersion 0 + 1)   PfCycleTable     VfCycleTable   GlcCycleTable   GfcCycleTable
  flat offset-LUT                  (TpuVersion 2)   (TpuVersion 3) (TpuVersion 4)  (TpuVersion 5)
  jellyfish v2 / dragonfish v3     pufferfish v4    viperfish v5   ghostlite v6L   6acc60406 (TPU7x)
```

The six factories are registered once by the `cycle_table.cc` static initializer (`_GLOBAL__sub_I_cycle_table.cc` @ `0x21353460`), each as a `FunctionRegistry<TpuVersion, unique_ptr<CycleTable>(const Target&)>` entry. Selection happens in `CycleTable::Create` @ `0x1c89cc00`, which reads the version off the `Target`, looks up the lambda, and invokes it; a missing registration is a fatal log (`"No cycle table registered for platform: "`, `cycle_table.cc:960`).

| `TpuVersion` | Codename            | Subclass        | `GetCyclesForThroughput` | Read strategy | Confidence |
|-------------:|---------------------|-----------------|--------------------------|---------------|------------|
| 0            | jellyfish (v2)      | `JfCycleTable`  | `0x1c89dce0`             | flat offset-LUT (`kUnusedRegisterJfCycleTable`) | CONFIRMED |
| 1            | dragonfish (v3)     | `JfCycleTable`  | `0x1c89dce0`             | flat offset-LUT (`kUnusedRegisterDfCycleTable`) | CONFIRMED |
| 2            | pufferfish (v4)     | `PfCycleTable`  | `0x1c89de60`             | `switch` → `GetResourceUsage(instr,res)` | CONFIRMED |
| 3            | viperfish (v5)      | `VfCycleTable`  | `0x1c89e2c0`             | `switch` → `GetResourceUsage(instr,res)` | CONFIRMED |
| 4            | ghostlite (v6 lite) | `GlcCycleTable` | `0x1c89e980` (wrapper)   | helper `0x1c89ed20` + `CHECK(ok)` | CONFIRMED |
| 5            | `6acc60406` (TPU7x) | `GfcCycleTable` | `0x1c89f060` (wrapper)   | helper `0x1c89f400` + `CHECK(ok)` | CONFIRMED |

> **NOTE — `TpuVersion` 0 and 1 share one class.** Both the jellyfish (v0) and dragonfish (v1) factories produce a `JfCycleTable` — the two registry entries bind the symbols `kUnusedRegisterJfCycleTable` and `kUnusedRegisterDfCycleTable` respectively, both to the same `JfCycleTable` factory lambda. Both read `GetCyclesForThroughput` @ `0x1c89dce0` over the same throughput offset-LUT; the gens differ only in their `Performance` grids, and the cells that differ are not throughput-LUT targets — so `GetCyclesForThroughput` is identical for JF and DF. See [JfCycleTable](jf-cycletable.md).

The two later gens (`Glc`, `Gfc`) wrap a `…Helper` that returns an `absl::Status`-style result; the public `GetCyclesForThroughput` calls the helper and `CHECK`s success (`MakeCheckFailString(..., "cycles is OK")`, fatal at `cycle_table.cc:817`) before returning the cycle integer. A reimplementation can flatten this into a direct return; the wrapper exists only to surface "unschedulable class" as a fatal rather than a silent default.

---

## The Cycle-Class Enumeration (`CycleTable::Instruction`)

`CycleTable::Instruction` is a dense enum `0x00..0x20` (33 values). It is **not** an LLO opcode and **not** the `Performance`/`GhPerf::Instruction` grid index — it is a coarse bucketing of MXU and vector functional behavior, shared verbatim across all six gens. The role each class plays is stable across gens even though the cycle integers differ.

| Class | Role | JF/DF | Confidence |
|------:|------|------:|------------|
| `0x00` | Vector matprep, bf16 | 8 | CONFIRMED |
| `0x01`–`0x04` | matprep variants (bf16/fp8 family) | default 1 | CONFIRMED |
| `0x05` | Latch / push gains, bf16 | 8 | CONFIRMED |
| `0x06` | Latch, int4 | default 1 | CONFIRMED |
| `0x07`,`0x08` | `6acc60406` new latch paths (latch_mode 48/50) | default 1 | CONFIRMED |
| `0x09` | Latch, fp8 | default 1 | CONFIRMED |
| `0x0a` | `PushGainsS4` — fatal on PF/VF (`"Unsupported PushGainsS4."`, `cycle_table.cc:682`) | default 1 | CONFIRMED |
| `0x0b` | Transposed bf16 latch | 8 | CONFIRMED |
| `0x0c` | Transposed int8 latch | default 1 | CONFIRMED |
| `0x0d`,`0x0e` | `6acc60406` transposed latch (latch_mode 49/51) | default 1 | CONFIRMED |
| `0x0f` | Transposed fp8 latch | default 1 | CONFIRMED |
| `0x10` | transposed `PushGainsS4` — fatal on PF/VF, same `"Unsupported PushGainsS4."` string as `0x0a` | default 1 | CONFIRMED |
| `0x11` | Vector EUP class | default 1 | CONFIRMED |
| `0x12`,`0x13` | XLU rotate in/out (RotIn/RotOut) | 1 | CONFIRMED |
| `0x14` | Shuffle / permute | 1 | CONFIRMED |
| `0x15`,`0x16` | Broadcast / reduce | 1 | CONFIRMED |
| `0x17` | Matrix-result read (TC) | 8 | CONFIRMED |
| `0x18` | Read/write transpose register | 1 | CONFIRMED |
| `0x19` | Cross-lane reduction | 1 | CONFIRMED |
| `0x1a` | Lane comparison / EUP edge | 1 | CONFIRMED |
| `0x1b` | Matrix-result read, primary | 8 | CONFIRMED |
| `0x1c` | Matrix-result read, secondary | 8 | CONFIRMED |
| `0x1d`,`0x1e` | EUP unary primary/secondary | default 1 | CONFIRMED |
| `0x1f` | Matrix-result read | 8 | CONFIRMED |
| `0x20` | Transcendental class (vector ALU "any") | 1 | CONFIRMED |

> **QUIRK — "33 classes" vs "16 priced."** The enum spans `0x00..0x20` but `JfCycleTable` prices only 16 of them (the rest fall through to the default `1`). The priced set is pinned by the literal mask `0x19FFC0821` (see [JfCycleTable](jf-cycletable.md)); later gens price additional bf16/fp8 latch and matprep variants. The JF/DF column above shows `8` for the seven MXU classes and `1` for the nine priced vector classes; "default 1" marks the classes the JF mask leaves unpriced. The *role* labels are stable across gens — a reimplementer ports the role-to-class map once and re-prices per gen.

### Folding opcodes into classes — `CycleTableInstruction`

`xla::jellyfish::CycleTableInstruction(LloInstruction*)` @ `0x1c89ca80` is the only producer of MXU cycle classes. It classifies exactly two opcode bands and is fatal on anything else:

```c
// xla::jellyfish::CycleTableInstruction @ 0x1c89ca80 (decompiled, exact shape)
uint32_t CycleTableInstruction(const LloInstruction *insn) {
    uint32_t op = insn->opcode;
    if ((uint16_t)(op - 141) <= 9) {                 // opcodes 141..150 = matmul/latch band
        uint8_t lm = insn->latch_mode();
        if (lm >= 0x34 || !bittest64(0xF000003FFFC3F, lm))
            LogFatal("Unsupported gain latch mode ", /*cycle_table.cc:431*/);
        return unk_B4389F4[lm];                       // latchLUT @ 0xb4389f4, 52 × int32
    }
    if ((uint16_t)(op - 155) <= 0xA) {                // opcodes 155..165 = matprep/matpush band
        uint8_t f = insn->matmul_data_format() - 1;
        if (f >= 0xA)
            LogFatal("Unsupported matmul data format ", /*cycle_table.cc:464*/);
        return unk_B438AC4[f];                         // fmtLUT @ 0xb438ac4, indices 0..9 read
    }
    LogFatal("Unsupported instruction ", /*cycle_table.cc:470*/);
}
```

Two `.rodata` lookup tables turn the MXU modifier into a cycle class:

| Table | Address | Shape | Maps | Confidence |
|-------|---------|-------|------|------------|
| `latchLUT` (`unk_B4389F4`) | `0xb4389f4` | 52 × `int32`, valid mask `0xF000003FFFC3F` | `GainLatchMode` → `Instruction` | CONFIRMED |
| `fmtLUT` (`unk_B438AC4`) | `0xb438ac4` | `int32[]`, indexed by `matmul_data_format()-1`; classifier reads indices `0..9` only (`< 0xA` guard) | `MatmulDataFormat` → `Instruction` | CONFIRMED |

The latch LUT (mask bits verified against the raw table): `0x00/0x02/0x04 → 5`, `0x01/0x03/0x05 → 11`, `0x0a → 12`, `0x0b/0x0e/0x10 → 6`, `0x0c → 9`, `0x0d → 15`, `0x0f/0x11 → 12`, `0x12/0x14/0x16/0x18 → 9`, `0x13/0x15/0x17/0x19 → 15`, `0x30 → 7`, `0x31 → 13`, `0x32 → 8`, `0x33 → 14`. The fmt LUT (index = `format-1`) reads `[0,1,1,1,4,4,4,4,2,3,1]`; the `< 0xA` guard means only indices `0..9` are reachable, i.e. `fmt 1 → 0`, `fmt 2/3/4 → 1`, `fmt 5/6/7/8 → 4`, `fmt 9 → 2`, `fmt 10 → 3`. The 11th entry (`fmt 11 → 1`) exists in `.rodata` but is rejected by this classifier as a fatal `"Unsupported matmul data format "` (`cycle_table.cc:464`); it is read only by later-gen paths. `CycleTableInstruction` itself is gen-independent — the same classifier produces MXU cycle classes for every gen. The vector/EUP/matrix-result classes (`0x11`..`0x20`) are produced by non-MXU emitter paths, not by `CycleTableInstruction`.

> **NOTE — the format LUT is wider than the classifier reads.** The `matmul_data_format()-1` validity check is `< 0xA`, so `CycleTableInstruction` reads only the first 10 fmt entries (formats 1..10). The 11th-and-beyond format values (packed int8/int4) are used by later-gen `Performance`/`MxuLatency` paths and are documented with [matmul mode modifiers](matmul-mode-modifiers.md); the shared classifier rejects them as fatal.

---

## The Resource Side — `CycleTable::GetResource`

`CycleTable::GetResource(Instruction)` @ `0x1c89ce20` is a single flat lookup, shared by all gens:

```c
// xla::jellyfish::CycleTable::GetResource @ 0x1c89ce20 (decompiled, exact)
int GetResource(int instruction) {
    return dword_B438AEC[instruction];               // resLUT @ 0xb438aec, 33 × int32
}
```

The returned value is *directly* the slot index into a per-op `ResourceVector` — `AccumulateInstructionUsage` does `ResourceVector::Acc(GetResource(I), (double)GetCyclesForThroughput(I))`, and `ResourceVector::Acc` (`0x1c89adc0`) is `[rdi + Resource*8] += cycles` with a `cmp esi, 0x17` bound (23 slots). The JF/DF resource LUT emits only the values `0..6` — the MXU/vector head of the 23-slot accumulator (see the [resource enum](resource-enum.md)):

| `GetResource` value | `ResourceVector` slot | Name | JF/DF occupant classes |
|--------------------:|-----------------------|------|------------------------|
| 0 | `R[0]` | Matpush | `0x05`..`0x10` (latch band) |
| 1 | `R[1]` | Matmul | `0x00`..`0x04` (matprep band) |
| 2 | `R[2]` | Xlu | `0x17`, `0x1b`..`0x1f` (matrix-result / cross-lane) |
| 3 | `R[3]` | VectorAlu0 | `0x14` |
| 4 | `R[4]` | VectorAlu1 | `0x12`, `0x13` |
| 5 | `R[5]` | VectorAluAny | `0x15`, `0x16`, `0x19`, `0x20` |
| 6 | `R[6]` | VectorEup | `0x11`, `0x18`, `0x1a` |

This is the mechanism by which the cost model models *resource conflict*: two classes that map to the same lane add (sequential on that unit); two that map to different lanes overlap (the scheduler takes the per-lane max across a bundle). The semantic micro-port mapping under each `R[k]` name is INFERRED beyond the binding-confirmed `ResourceVector` enum names.

---

## How This Relates To Performance And MxuLatency

| Table family | What it answers | Accessor | Page |
|--------------|-----------------|----------|------|
| **CycleTable** (this page) | issue cycles + lane for a cycle class | `GetCyclesForThroughput`, `GetResource` | here |
| **[Performance](performance-overview.md)** | the baked per-gen cycle grid the CycleTable reads | `GetResourceUsage(instr,res)`, `GetLatency` | [overview](performance-overview.md) |
| **[MxuLatency](mxu-latency-overview.md)** | per-`(MatmulModifier × Resource)` matmul/matprep cycles | `GetResourceUsage` (keyed map) | [overview](mxu-latency-overview.md) |
| **`LatencyTable*`** | read-after-write dependency latency | `GetLatency`, `GetLatencyBetween` | [bundle-aware cost](bundle-aware-cost.md) |

The clean way to read the picture: the `CycleTable` is the *index logic*; the `Performance` grid is the *data*; the `MxuLatency` map is a per-gen *override* of the matmul/matprep cells when the simple `(instruction, resource)` lookup is too coarse (matmul cost depends on `(format, transpose-flag, MSR/MRB target)`, not a single per-opcode constant). The `LatencyTable*` is an orthogonal axis the scheduler combines with the throughput cycles via the per-op `ResourceVector`. The concrete per-gen integers — including the seven JF/DF 8-cycle MXU cells and the matmul-latency clusters (bf16 = 131/192/212, fp8 = 114/192/204 on Vf/Gl/Gf) — are tabulated in [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md).

---

## Cross-References

- [JfCycleTable](jf-cycletable.md) — the flat offset-LUT read path, the 16-of-33 priced subset, and the 7-column resource naming for the oldest gens.
- [VfCycleTable](vf-cycletable.md) — the Viperfish `switch`-over-`GetResourceUsage` read path.
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the baked `.rodata` cycle tables grouped by gen/engine, and how the bundle-latency cost model sums them.
- [Performance Family Overview](performance-overview.md) — the per-gen `Performance<gen>` grid the CycleTable indexes into.
- [MXU Latency Overview](mxu-latency-overview.md) — the per-`(MatmulModifier × Resource)` reservation matrices that override the simple matmul cells.
- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector` whose head `R[0]..R[6]` the JF/DF resource LUT emits.
- [Bundle-Aware Cost](bundle-aware-cost.md) — how per-op throughput cycles and per-op resource vectors combine into a bundle issue cost.
- [Bundle Model Overview](../isa/bundle-model-overview.md) — the VLIW bundle layer the cost model attributes cycles to.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VII — Cost & Latency Model / CycleTable — [back to index](../index.md)
