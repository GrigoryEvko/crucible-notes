# IarsPerTensorCore and the Non-MXU Cost Band

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). Other versions differ. Every integer below was read out of `.rodata` (LUTs and embedded chip-parts protos) by hand and cross-checked against the IDA decompile; status is in the Confidence columns.*

## Abstract

This page consolidates three per-generation cost-model quantities that share one root: a count and a classifier surface, all keyed by the same `CycleTable::Instruction` enum the MXU throughput tables use. First, the **`IarsPerTensorCore`** value — the count of Index-Add Registers (IAR0/IAR1) per TensorCore that bounds the matpush/indexed-load addressing path and feeds the cost model. Second, the **non-MXU `CycleTable` band** (`Instruction 0x11..0x20`) on Jellyfish/Dragonfish — the vector-ALU, cross-lane (Xlu), and EUP cost cells whose *values* are pinned (on [MXU Latency: JF / DF](mxu-latency-jf-df.md)) but whose *producer* is documented here. Third, the **Viperfish matprep-stage ordinal binding** — the mechanism that turns a matprep opcode into a reservation array.

The unifying fact is that `IarsPerTensorCore` is **not a code constant**. It is `DWORD[Target+0x4a8]`, written once by the shared `Target::Init` from a `VectorIsa` field of the embedded per-generation `*_chip_parts.binarypb` proto, loaded at runtime via the `embed://` filesystem. Extracted from the embedded blobs, the value is **2 on every generation** — the IAR file is gen-stable (IAR0/IAR1), matching the 1-bit `IarField` ceiling in the ISA encoding. The version pin above is therefore necessary but not sufficient: a chip whose proto differs would change the count.

For reimplementation, the contract is:

- **`IarsPerTensorCore`** = `DWORD[Target+0x4a8]`, accessor `Target::IarsPerTensorCore` (`0x1d617280`); sole writer `Target::Init` (`0x1d60fc20`) from `vector_isa()` field 7; value **2** all gens.
- **The JF/DF non-MXU band** `Instruction 0x11..0x20` is emitted by the HLO-level cost model (`CostModel::RecordHloCycles` + `cost_model_util::Record*`) as *direct ordinal immediates* keyed on HLO opcode / `PrimitiveType` / transfer role — there is no LLO→`Instruction` classifier for it (the MXU classifier `CycleTableInstruction` covers only `0x00..0x10`).
- **The VF matprep stages** do not carry standalone classifier ordinals: the matprep opcodes `0x97..0x9a` `FATAL` in `GetViperfishInstruction`, and the reservation is produced by `MxuLatencyTable::GetResourceUsage` keyed on a `MatpushModifier{MatmulDataFormat, is_transpose, Msr}`.

| | |
|---|---|
| **`IarsPerTensorCore`** | `DWORD[Target+0x4a8]` = **2** all gens (IAR0/IAR1, gen-stable) |
| **Accessor** | `Target::IarsPerTensorCore` `0x1d617280` (`return *(uint32*)(this+0x4a8)`) |
| **Sole writer** | `Target::Init` `0x1d60fc20` from `TpuSequencerParts::vector_isa()` field 7 |
| **Proto source** | `embed://tpu_chip_parts/<version>_chip_parts.binarypb`, `VectorIsa.f7` |
| **ISA ceiling** | 1-bit `IarField` (`0x1ee3b380`, `>>13 & 1`) ⇒ 2 IARs |
| **Non-MXU band** | `CycleTable::Instruction 0x11..0x20` (vector-ALU / Xlu / EUP) |
| **Non-MXU producer** | `CostModel::RecordHloCycles` `0x130bbfe0` + `cost_model_util::Record*` (direct ordinal immediates) |
| **VF matprep binding** | `MxuLatencyTable::GetResourceUsage` `0x1c8ae5c0`, `FlatHashMap<MatpushModifier, array<int,19>>` |
| **VF matprep opcodes** | `0x97..0x9a` `FATAL` in `GetViperfishInstruction` `0x1c8a3300` |

---

## IarsPerTensorCore — Source and Per-Gen Value

### The Accessor and the Sole Writer

`IarsPerTensorCore` is a one-instruction accessor reading offset `0x4a8` of the `Target` object (`298 * 4 = 0x4a8`):

```c
// xla::jellyfish::Target::IarsPerTensorCore  @ 0x1d617280  (verified)
__int64 Target::IarsPerTensorCore(this) {
    return *((unsigned int*)this + 298);          // DWORD[Target + 0x4a8]
}
```

The sole writer of `Target+0x4a8` is the **shared** `Target::Init` — no per-generation `*Target` constructor writes it. In the register-count block of `Init`, the field is read from the `VectorIsa` C++ sub-struct (embedded at `TpuSequencerParts+0x1c`), gated by the VectorIsa-present byte:

```c
// xla::jellyfish::Target::Init  @ 0x1d60fc20  (verified — the +0x4a8 store)
v93 = TpuSequencerParts::vector_isa(seqparts);    // = &seqparts[+0x1c]
if ( *(uint8_t*)(v93 + 0x18) == 1 ) {             // VectorIsa-present gate (seqparts+0x34)
    v94 = *(uint32_t*)(v93 + 0x14);               // IarsPerTensorCore  (seqparts+0x30 = VectorIsa.f7)
    *((uint32_t*)Target + 298) = v94;             // store at Target+0x4a8   ← the sole writer
    *((uint64_t*)Target + ...) = *(uint64_t*)(v93 + 0xc);   // adjacent MxusPerTensorCore qword
}
```

`TpuSequencerParts::FromProto` packs the proto `VectorIsa` submessage (six `int32` fields, proto field numbers 2..7) into the C++ struct, with field 7 landing at struct `+0x30` → `Target+0x4a8`. So `IarsPerTensorCore` is the `VectorIsa` field-7 proto value, loaded at runtime by `TpuChipParts::DefaultsForVersion` via `tsl::ReadBinaryProto("embed://tpu_chip_parts/<version>_chip_parts.binarypb")`.

### The Extracted Per-Gen Values

The per-gen blobs are embedded **uncompressed** in `.rodata`. Parsing the `VectorIsa` submessage out of each blob (the `f2=128, f3=8` prefix locates it; `f7` is the IAR count) gives, byte-exact:

| Gen (codename) | `lane_count` f2 | `sublane_count` f3 | `mxu_count` f5 | `xlu_count` f6 | `iar_count` f7 = `IarsPerTensorCore` | Confidence |
|---|---|---|---|---|---|---|
| v2 Jellyfish | 128 | 8 | 1 | 1 | **2** | CERTAIN |
| v3 Dragonfish | 128 | 8 | 2 | 1 | **2** | CERTAIN |
| v4 Pufferfish | 128 | 8 | 4 | 2 | **2** | CERTAIN |
| v5p Viperfish | 128 | 8 | 4 | 3 | **2** | CERTAIN |
| v6e Ghostlite | 128 | 8 | 2 | 2 | **2** | CERTAIN |
| v7 6acc60406 | 128 | 8 | 2 | 2 | **2** | CERTAIN |

So `IarsPerTensorCore = 2` on every generation — the IAR file does not grow v2→v7. This is exactly consistent with the ISA-encoding ceiling: the `SetIar` slot's `IarField` accessor (`0x1ee3b380`) is `>>13 & 1`, a single bit, which can address only IAR0/IAR1. The `VectorIsa` field names are not inferred — `TpuSequencerParts::FromProto` (`0x20b30700`) validates each by name (`"Invalid lane_count in vector_isa field"`, `"Invalid sublane_count …"`, `"Invalid mxu_count …"`, `"Invalid xlu_count …"`, `"Invalid iar_count …"`), so f2=`lane_count`=128 (constant across all gens), f5=`mxu_count` (1/2/4/4/2/2), and f6=`xlu_count` (1/1/2/3/2/2) are CONFIRMED, not merely positional.

> **NOTE — this value is data, not code.** The accessor and the 1-bit `IarField` are immutable code; the *value* is a runtime proto field. The "2 all gens" claim holds for this binary's embedded chip-parts blobs only. A reimplementation must read it from the per-gen proto, not bake it in. See [Chip-Parts Binarypb](../targets/chip-parts-binarypb.md) and [../isa/slot-matprep-iar-latch.md](../isa/slot-matprep-iar-latch.md) for the IAR value-field layout.

### The Consolidated Per-Gen Register-File Table

`IarsPerTensorCore` is the last column of the per-gen register-file block at `Target+0x498..+0x4a8`, all written by the same `Target::Init` from the same chip-parts blobs. The four register counts are re-derived byte-exact from the embedded protos:

| Gen | `SREG` `+0x498` | `VREG` `+0x49c` | `VMREG` `+0x4a0` | `PREG` `+0x4a4` | `IARS` `+0x4a8` | Confidence |
|---|---|---|---|---|---|---|
| v2 Jellyfish | 32 | 32 | 8 | 15 | 2 | HIGH |
| v3 Dragonfish | 32 | 32 | 8 | 15 | 2 | HIGH |
| v4 Pufferfish | 32 | 32 | 8 | 15 | 2 | HIGH |
| v5p Viperfish | 32 | 64 | 16 | 14 | 2 | HIGH |
| v6e Ghostlite | 32 | 64 | 16 | 14 | 2 | HIGH |
| v7 6acc60406 | 32 | 64 | 16 | 14 | 2 | HIGH |

(Field offsets byte-exact from `Target::Init` + `RegisterCount`; values from the embedded chip-parts protos. The `SREG`/`VREG`/`VMREG`/`PREG` order follows `Init`'s seq-0 type dispatch.)

---

## The JF/DF Non-MXU CycleTable Band (Instruction 0x11..0x20)

### The Producer

The MXU classifier `CycleTableInstruction` (`0x1c89ca80`) maps **only** the MXU band — matmul opcodes `0x8d..0x96` and matprep/matpush opcodes `0x9b..0xa5` — into `CycleTable::Instruction 0x00..0x10`. There is no LLO→`Instruction` classifier for ordinals `0x11..0x20`. Those ordinals are emitted by the HLO-level cost model as **direct ordinal immediates** (`mov esi, 0xNN`) keyed on HLO opcode, `PrimitiveType`, or memory-transfer role, then passed to `GetCyclesForThroughput` (`JfCycleTable` vtable `+0x10`) for the cycle value and `ResourceVector::Acc(Resource, cycles)` for the accumulator slot:

- `CostModel::RecordHloCycles` (`0x130bbfe0`) — per-HLO entry; switches on `HloOpcode` and emits `0x12`/`0x13`/`0x14` for elementwise vector ops.
- `cost_model_util::Record*` — `RecordPackAndStCycles` (`0x13844740`, `0x16`), `RecordBroadcastSublaneChunkCycles` (`0x138448c0`, `0x17`/`0x1b`), `RecordMemXferCyclesImpl` (`0x13844e80`); `Broadcast::RecordCostCycles` (`0x136e6940`, `0x17`); the `Record{Conv,DepthwiseConv}KernelCycles` / `RecordConvolutionCycles` / `SpatialMajorConvolution::CalculateWindowMxuCycles` emitters (`0x16`/`0x19`/`0x1c`/`0x1f`).

### The Band Table

The `offsetLUT` (`0xb438b70`) and `resLUT` (`0xb438aec`) were re-read byte-exact; the cycle value is the priced `PerformanceJf` cell under the valid mask `0x19FFC0821`. JF == DF for the entire band (none of these offsets is the DF-override `+0x28`/`+0x2c`).

| `Instr` | `Res` | `ResourceVector` | priced | PerfOff | JF/DF cyc | emitting context | Confidence |
|---|---|---|---|---|---|---|---|
| `0x11` | `r6` | VectorEup | no | `0x0` | 1 | unpriced (SparseCore decomposer) | CERTAIN |
| `0x12` | `r4` | VectorAlu1 | yes | `0x33c` | 1 | elementwise vector-ALU (`RecordHloCycles`) | CERTAIN |
| `0x13` | `r4` | VectorAlu1 | yes | `0x340` | 1 | elementwise vector-ALU (`RecordHloCycles`) | CERTAIN |
| `0x14` | `r3` | VectorAlu0 | yes | `0x344` | 1 | vector-ALU lane0 (reduction-fn) | CERTAIN |
| `0x15` | `r5` | VectorAluAny | yes | `0x39c` | 1 | vector-ALU any (sublane reduce-window) | CERTAIN |
| `0x16` | `r5` | VectorAluAny | yes | `0x398` | 1 | pack-and-store / reduce-window / conv-kernel | CERTAIN |
| `0x17` | `r2` | Xlu | yes | `0x954` | 8 | broadcast / cross-lane (`Broadcast::RecordCostCycles`) | CERTAIN |
| `0x18` | `r6` | VectorEup | yes | `0x3f8` | 1 | EUP / transcendental (`RecordHloCycles`) | CERTAIN |
| `0x19` | `r5` | VectorAluAny | yes | `0x368` | 1 | vector-ALU any (`RecordConvolutionCycles`) | CERTAIN |
| `0x1a` | `r6` | VectorEup | yes | `0x3f4` | 1 | EUP / transcendental (`RecordHloCycles`) | CERTAIN |
| `0x1b` | `r2` | Xlu | yes | `0x960` | 8 | cross-lane reduce-window | CERTAIN |
| `0x1c` | `r2` | Xlu | yes | `0x94c` | 8 | conv-kernel cross-lane | CERTAIN |
| `0x1d` | `r2` | Xlu | no | `0x0` | 1 | unpriced | CERTAIN |
| `0x1e` | `r2` | Xlu | no | `0x0` | 1 | unpriced | CERTAIN |
| `0x1f` | `r2` | Xlu | yes | `0x958` | 8 | spatial-conv window-MXU | CERTAIN |
| `0x20` | `r5` | VectorAluAny | yes | `0x39c` | 1 | vector-ALU any (shares `0x39c` with `0x15`) | CERTAIN |

The four 8-cycle Xlu cells (`0x17`/`0x1b`/`0x1c`/`0x1f`) are the cross-lane throughput ports (broadcast-sublane, reduce-window, conv-kernel-window, spatial-conv-window). The 1-cycle cells are vector-ALU (`0x12..0x16`, `0x19`, `0x20`) or VectorEup (`0x18`/`0x1a`). The `Res` column is the `ResourceVector` slot R[2]..R[6] (Xlu / VectorAlu0 / VectorAlu1 / VectorAluAny / VectorEup).

> **QUIRK — `Instr 0x15` and `0x20` share offset `0x39c`.** Both read the same VectorAluAny cell (value 1); the producer chooses which ordinal to emit (sublane-reduce vs reshape/sparse-core) but they price identically. A reimplementation that assumes distinct offsets per ordinal will allocate a redundant cell.

---

## The Viperfish Matprep-Stage Ordinal Binding

### Matprep Opcodes FATAL in the Classifier

On Viperfish the matprep opcodes (`0x97..0x9a`) are **not** classified into a standalone `Instruction` ordinal — they hit the `FATAL` arm of `GetViperfishInstruction` (`0x1c8a3300`, opcode jump table `0xb43a104`, default arm `0x1c8a3e6a`). The matmul opcode `0x9b` instead reads `matmul_data_format()` and indexes the VF matmul-format WORD table `@0xa2d05c0`, read byte-exact as `{0xd4, 0xda, 0xf8, 0xfe, 0xe0, 0xe6, 0xec, 0xf2}` (fmt 1..8 = f32/bf16/fp8e5m2/fp8e4m3/u8/s8/u4/s4):

| `MatmulDataFormat` | dtype | VFinstr ordinal (`@0xa2d05c0`) | Confidence |
|---|---|---|---|
| 1 | f32 | `0xd4` | CERTAIN |
| 2 | bf16 | `0xda` | CERTAIN |
| 3 | f8e5m2→bf16 | `0xf8` | CERTAIN |
| 4 | f8e4m3→bf16 | `0xfe` | CERTAIN |
| 5 | u8 | `0xe0` | CERTAIN |
| 6 | s8 | `0xe6` | CERTAIN |
| 7 | u4 | `0xec` | CERTAIN |
| 8 | s4 | `0xf2` | CERTAIN |

### The Reservation-Table Producer

The matprep stages are produced by `MxuLatencyTable::GetResourceUsage` (`0x1c8ae5c0`), which `VfCycleTable::GetCyclesForThroughput` (`0x1c89e2c0`) wraps — confirmed in the decompile, where `case 0` calls `viperfish::MxuLatencyTable::GetResourceUsage(&result, ..., 212, 3, 0)`. `GetResourceUsage` dispatches on the `Resource` arg and the matmul ordinal, builds a **Modifier** key, and looks it up in a `FlatHashMap<Modifier, std::array<int,19>>` — the 19-entry array is indexed by `MxuResource` (`SetReservations<...>` writes `array[MxuResource]=val` with bound `0x13 = 19`). The matprep 4-stage systolic-feed occupancy (`r4..r7`) is four of those nineteen `MxuResource` ports.

The four modifier keys, each built by `SetReservations<...>` in the `ViperfishPerformance` constructor:

| Modifier | key fields | role | Confidence |
|---|---|---|---|
| `MatmulModifier` | `{MatmulDataFormat}` | the pure matmul step | HIGH |
| `MatpushModifier` | `{MatmulDataFormat, is_transpose, Msr}` | the matprep/matpush stages | HIGH |
| `MatresModifier` | (matmul-result key) | matmul-result stages | HIGH |
| `VlxmrModifier` | (vector-load matrix-result key) | vector-load matrix-result | HIGH |

The `MatpushModifier` is the matprep-opcode → reservation binding: a matprep stage's reservation is keyed by `{MatmulDataFormat × transpose-of-gains × matpush-Msr-stage}` → a 19-entry `MxuResource` reservation array. The matprep opcode carries a Modifier that indexes the table, not a standalone ordinal. This is the VF realization of the same job JF folds into a flat offset-LUT cell. See [Matmul-Mode Modifiers](matmul-mode-modifiers.md) and [MXU Latency: VF](mxu-latency-vf.md).

> **QUIRK — the matprep cost representation migrated across gens.** JF folds the transpose-of-gains into the matmul opcode (flat cell); PF folds matprep into the Latch ops; VF FATALs the matprep opcodes and uses the `MatpushModifier`-keyed `array<19>` reservation; GL/GF give each matprep variant a fixed binary-search perf row (with a smaller `array<11>` reservation). A single matprep cost model across gens mis-prices four of the five. See [../isa/slot-matprep-iar-latch.md](../isa/slot-matprep-iar-latch.md).

---

## Worked Examples

Three end-to-end traces, each exercising one of the three surfaces above.

### IarsPerTensorCore on Viperfish

A Viperfish device constructs a `ViperfishTarget`, whose base chain calls the shared `Target::Init` (`0x1d60fc20`). `Init` has already obtained the `viperfish_chip_parts.binarypb` `TpuSequencerParts` (loaded via `embed://`). It reads `vector_isa()+0x18` (the present byte) — `1` — then `vector_isa()+0x14` = `VectorIsa.f7` = **2**, and stores it at `Target+0x4a8`. A later `IarsPerTensorCore()` returns `2`; the IAR-setter helper bounds `iar_value < 2` (IAR0/IAR1). No `ViperfishTarget` constructor writes `+0x4a8` itself — the value is purely the proto field.

### A JF/DF Broadcast Cost

An HLO `broadcast` routes through `CostModel::RecordHloCycles` to `Broadcast::RecordCostCycles` (`0x136e6940`), which emits the ordinal immediate `Instr 0x17` and calls `GetCyclesForThroughput(0x17)`. That reads `PerformanceJf[offsetLUT[0x17]] = PerformanceJf[0x954] = 8` cycles, then `ResourceVector::Acc(r2 = Xlu, 8.0)`. So a JF/DF broadcast costs **8 cross-lane (Xlu) cycles**. An elementwise add instead emits `Instr 0x12` → `PerformanceJf[0x33c] = 1` cycle on `r4 = VectorAlu1`.

### A VF bf16 Matprep Stage

A bf16 `kVectorMatmul` (opcode `0x9b`, `MatmulDataFormat = 2`) routes through `GetViperfishInstruction`, whose `0x9b` arm reads `WORD[0xa2d05c0 + 1*2] = 0xda` (the bf16 pure-matmul ordinal). Its matprep companion is produced by `MxuLatencyTable::GetResourceUsage(0xda, Resource, gains)` (`0x1c8ae5c0`): the `0xda` branch builds `MatmulModifier{DataFormat = 2}` for the matmul and `MatpushModifier{DataFormat, is_transpose, Msr = LatchOpcodeToMsr(0x97..0x9a)}` for the prep stages, then `FlatHashMap<MatpushModifier, array<int,19>>::find` returns the 19-entry `MxuResource` reservation. The matprep-stage columns `r4..r7` = `{4, 12, 20, 28}` — the four systolic-feed stages. The bf16 matprep stage's cost is the `MatpushModifier`-keyed `array<19>`, never a standalone ordinal.

---

## Related Components

| Component | Relationship |
|---|---|
| `Target::IarsPerTensorCore` `0x1d617280` | reads `DWORD[Target+0x4a8]` (= 2 all gens) |
| `Target::Init` `0x1d60fc20` | sole writer of `+0x4a8` from `VectorIsa.f7` |
| `TpuChipParts::DefaultsForVersion` `0x20b1b040` | loads `embed://...<version>_chip_parts.binarypb` |
| `CostModel::RecordHloCycles` `0x130bbfe0` | emits the non-MXU `Instr 0x11..0x20` ordinals |
| `CycleTableInstruction` `0x1c89ca80` | the MXU-only classifier (`Instr 0x00..0x10`) |
| `GetViperfishInstruction` `0x1c8a3300` | matprep opcodes `0x97..0x9a` FATAL here |
| `MxuLatencyTable::GetResourceUsage` `0x1c8ae5c0` | the VF `Modifier → array<19>` matprep reservation |

## Cross-References

- [MXU Latency: JF / DF](mxu-latency-jf-df.md) — the JF/DF throughput table that prices this non-MXU band, and the `LatencyTableJellyfish` copy map.
- [JfCycleTable](jf-cycletable.md) — the full `offsetLUT`/`resLUT` transcription and the MXU classifier LUTs.
- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector` whose slots R[2]..R[6] this band occupies.
- [Matmul-Mode Modifiers](matmul-mode-modifiers.md) — the VF `MatpushModifier`-keyed `array<int,19>` reservation table in full.
- [MXU Latency: VF](mxu-latency-vf.md) — the Viperfish reservation model that wraps `GetResourceUsage`.
- [VfCycleTable](vf-cycletable.md) — the VF throughput reader that dispatches into `MxuLatencyTable::GetResourceUsage`.
- [../isa/slot-matprep-iar-latch.md](../isa/slot-matprep-iar-latch.md) — the IAR value-field layout, the 1-bit `IarField` encoding, and the per-gen matprep cost divergence.
- [../targets/chip-parts-binarypb.md](../targets/chip-parts-binarypb.md) — the embedded per-gen `chip_parts.binarypb` proto and the `embed://` load path that supplies `IarsPerTensorCore`.
- [Consolidated Per-Gen Counts (overview)](overview.md) — the cost-model index that links the per-gen Performance/CycleTable pages.
