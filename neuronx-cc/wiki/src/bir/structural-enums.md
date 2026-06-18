# Structural Enums — Engine, Arch, Accumulation, Axis, Indirect, Transpose

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The six enums and their `2string`/`string2` (de)serializers live in `neuronxcc/starfish/lib/libBIR.so` (GNU build-id `a9b1ea38c47e579178b179fd445aa8edd593f206`, md5 `12bb979f7ca41248252abb0f16b2da98`). For `.text` (`0x1820c0`–`0x707a44`) and `.rodata` (`0x708000`–`0x7a118c`), virtual address equals file offset; `.data` is `+0x400000` and `.bss` holds the static `Board` objects. The per-arch engine *counts* and PSUM geometry are runtime-built by the hardware-model side in `libwalrus.so` (build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`) — see [Build & Version Provenance](../reference/versions.md). cp311/cp312 share the logic but not the addresses; re-confirm against the target wheel.*

## Abstract

Six enums in `libBIR` classify *where* a BIR instruction runs, *which silicon generation* it targets, and *how* it reshapes the dimensions of a tensor access pattern. They are the structural vocabulary every BIR instruction is built on: an instruction names an **`EngineType`** (which of the eight engine slots), an **`ArchLevel`** (which of five generations), and — depending on opcode — an **`EngineAccumulationType`** (the PSUM read-modify-write command), an **`AxisListType`** (how many innermost AP dims a reduce collapses), an **`IndirectDim`** (which AP axis a gather indirects), or a **`TransposeOps`** (a 4-axis DMA-transpose permutation). None of these is hand-rolled. Every one is an InstaBrew-generated (`…/neuronxcc/instabrew/brewer.py`) round-trip pair: a forward `<Enum>2string` dispatch and an inverse `string2<Enum>` parser, with `to_json` emitting the *name* string and `from_json` reading it back, and a uniform fatal default arm. There is no numeric remap anywhere — the ordinal stored in the struct *is* the wire value, and the name is purely for the human-readable JSON.

Two of these enums carry a trap that a reimplementer must reproduce exactly. **`EngineType` has two name tables, not one**: `EngineType2string` prints the *internal* name (`Pool`, `Activation`, `PE`, …) and `EngineType2ExternalName` prints a *runtime/profile-facing* alias for the same eight ordinals (`GPSIMD`, `Scalar`, `Tensor`, …). The external alias `GPSIMD` is just the internal `Pool(1)` engine seen from outside — this resolves the long-standing question of whether GPSIMD is a separate engine ordinal (it is not; see [GPSIMD Engine](../arch/gpsimd-engine.md)). And **`ArchLevel` knows generation 5 but the hardware-model dispatcher does not**: `ArchLevel2string` happily returns `"core_v5"` for ordinal `50`, yet `getArchModel` has no `Board` for it and asserts `"Unknown architecture"` — gen-5 is a forward stub in this build.

Every claim below is read directly off the binary: the switch-bound (`cmp esi, N`), the jump-table targets, and the inline / `.rodata` name literals. Five of the six enums are CERTAIN at the byte level; the per-arch engine-*count* numbers are the one thing `libBIR` does not hold statically (they live in `.bss` `Board` objects filled by `libwalrus`).

For reimplementation, the contract is:

- **The exact ordinal→name maps** for all six enums, both name tables for `EngineType`, and the inverse parsers — the wire contract for BIR-on-disk JSON.
- **The `GPSIMD ≡ Pool(1)` aliasing rule** and the `EngineType2ExternalName` table — get this wrong and every profile/runtime engine name is mislabeled.
- **The `ArchLevel = generation × 10` ladder** and the `core_v5(50)`-has-no-`Board` dormancy.
- **The AP axis-letter physical key** (`[W,Z,Y,X]` = AP index `[0,1,2,3]`, outermost→innermost) that gives `AxisListType`, `IndirectDim`, and `TransposeOps` their meaning, plus the consumers that store/read each field.

| | |
|---|---|
| **`EngineType2string`** | `0x47fa80` (443 B) — 8 cases, jump table `@0x7974bc` |
| **`EngineType2ExternalName`** | `0x47fca0` (438 B) — same 8 ordinals → profile aliases |
| **`ArchLevel2string`** | `0x479490` — 5 cases `{10,20,30,40,50}` |
| **`EngineAccumulationType2string`** | `0x400990` (194 B) — 6 cases, jump table `@0x78ad20` |
| **`AxisListType2string`** | `0x4011b0` — 6 cases `{X..C}` |
| **`IndirectDim2string`** | `0x4021a0` — 4 cases `{W,Z,Y,X}` |
| **`TransposeOps2string`** | `0x401500` — 14 cases (1 `None` + 13 permutations) |
| **Fatal default arm** | `sub_679F20("Unknown <Enum>", instabrew/brewer.py)` |

---

## EngineType — the eight engine slots and the GPSIMD alias

### Purpose

`EngineType` names which of the Trainium/Inferentia engine slots an instruction is bound to. It is the inner key of the per-op `getValidEngines()` legality map (`Instruction::isValidEngine @0x2d6b10`) and the type half of every `EngineInfo` handle. Eight ordinals, two of which (`Unassigned`, `ALL`) are pseudo-engines rather than physical hardware.

### Algorithm

`EngineType2string @0x47fa80` is a jump-table switch. The bound is `cmp esi, 7` (8 cases); the table at `.rodata 0x7974bc` holds eight `int32` self-relative offsets. Each arm writes the name into the returned `std::string` SSO buffer — short names are stored as inline `mov`-immediate word/byte literals, decoded little-endian.

```c
// bir::EngineType2string @0x47fa80  — verified byte-exact
std::string EngineType2string(EngineType e):     // arg in esi
    if (unsigned)e > 7:                           // 0x47fa8a  cmp esi,7 / ja default
        throw runtime_error("Unknown EngineType '<n>'")   // 0x47fa8d
    switch jumptable[e] @0x7974bc:                // 0x47faa6  jmp *rdx
        case 1: write "Pool"   // 0x47fb54  movl $0x6c6f6f50 ('looP' LE), len 4
        case 3: write "PE"     // 0x47fbb4  mov  $0x4550    ('EP'   LE), len 2
        ...                                        // remaining arms identical shape
```

### Data Tables

Both name tables, read off the switch arms (internal) and the `.rodata` diagnostic strings (external). The external table is `EngineType2ExternalName @0x47fca0`; the alias relationship is also spelled out verbatim in eight `.rodata` strings of the form `ExternalEngineType used as EngineType. External: <ext> Internal: <int>` (present at `strings` scan; e.g. `External: GPSIMD Internal: Pool`).

| Ord | Internal (`2string`) | External (`2ExternalName`) | Physical role | Confidence |
|---|---|---|---|---|
| 0 | `Unassigned` | `Unassigned` | pseudo / default sentinel | CERTAIN |
| 1 | `Pool` | `GPSIMD` | pooling / reduce engine | CERTAIN |
| 2 | `Activation` | `Scalar` | scalar/activation (PWP LUT) | CERTAIN |
| 3 | `PE` | `Tensor` | systolic matmul array | CERTAIN |
| 4 | `DMA` | `SyncDMA` | DMA engine | CERTAIN |
| 5 | `DVE` | `Vector` | data-movement / vector engine | CERTAIN |
| 6 | `SP` | `Sync` | sync / control engine | CERTAIN |
| 7 | `ALL` | `All` | pseudo / all-engine barrier | CERTAIN |

The inverse `string2EngineType @0x47fe60` is the bijective parser; `operator<<(ostream&, EngineType) @0x47fc40` delegates to `EngineType2string`, so there is a single source of truth for the printed name.

> **GOTCHA — `EngineType` has two name tables, and they disagree on every physical engine.** `Pool(1)` prints as `Pool` internally but `GPSIMD` to the outside; `Activation(2)` is `Scalar` externally; `PE(3)` is `Tensor`; `DVE(5)` is `Vector`. A reimplementation that uses one table for both directions will mislabel profiles or fail to parse runtime-emitted engine names. The two pseudo-ordinals (`Unassigned(0)`, `ALL(7)`) are the only ones whose internal and external names coincide (modulo `ALL`→`All` case).

> **NOTE — `GPSIMD` is not a ninth engine ordinal, and `PWP` is not an engine at all.** `GPSIMD` is the external alias of internal `Pool(1)`, proven both by `EngineType2ExternalName` case 1 and by the diagnostic string `ExternalEngineType used as EngineType. External: GPSIMD Internal: Pool`. `PWP` (seen in the `libwalrus` codegen) is the LUT subsystem name of the `Activation`/`Scalar` engine, not a distinct `EngineType`. See [GPSIMD Engine](../arch/gpsimd-engine.md) for the single machine op (`InstGPSIMDSB2SB`) that actually runs on this slot.

### EngineInfo — the per-engine handle

`EngineInfo` is *not* a geometry table — it is a lightweight `{EngineType ordinal, uint32 id}` pair. `EngineInfo2string @0x47c310` stringifies the `EngineType` (via `EngineType2string`) followed by the decimal `id`, producing tokens like `"PE0"`, `"Pool1"`; `string2EngineInfo @0x47f710` splits the trailing digits as the `id` and parses the prefix via `string2EngineType`.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `engineType` | `+0x00` | `uint32` | `EngineType` ordinal `0..7` | HIGH |
| `engineId` | `+0x04` | `uint32` | per-type instance index (the decimal in the token) | HIGH |

(IDA recovered no formal `EngineInfo` struct; the two-field layout is implicit from the `2string`/`string2` accessors — they touch only the first two `uint32`. If `EngineInfo` carries further fields, the round-trip serializer does not exercise them.)

### Considerations

The per-arch *count* of each engine is a separate lookup. `getEngineCount(EngineType, ArchLevel) @0x47d820` resolves `ArchLevel2string → getArchModel → Board*`, then reads `rbx = *(*(Board+8)+0x10)` (a `uint32*` engine-count array) and indexes it by `EngineType` ordinal:

| `EngineType` | byte offset | disasm site |
|---|---|---|
| `Unassigned(0)` | `rbx+0x6C` | `mov eax,[rbx+6Ch] @0x47d8d8` |
| `PE(3)` | `rbx+0x70` | `@0x47d920` |
| `Pool(1)` | `rbx+0x74` | `@0x47d8f0` |
| `Activation(2)` | `rbx+0x78` | `@0x47d908` |
| `DVE(5)` | `rbx+0x7C` | `@0x47d950` |
| `DMA(4)` | `rbx+0x80` | `@0x47d938` |
| `SP(6)` | `rbx+0x84` | `@0x47d8a8` |
| `ALL(7)` | `rbx+0x88` | `@0x47d8c0` |

> **GOTCHA — the engine-count array is laid out in a different order than the enum.** The fields run `Unassigned, PE, Pool, Act, DVE, DMA, SP, ALL` at `+0x6C..+0x88` — i.e. *not* in ordinal order. Index by the offset table above, never by `ordinal*4`. The numeric counts themselves are runtime-constructed in `.bss` (the `Board` objects `unk_91DE40`/`91DFC0`/`91E140`/`91E2C0`, filled by an external `hwm/ctm.cpp` static ctor reached via the `0x478f70` thunk), so `libBIR` holds *no* static `#DMA`/`#DVE` literals — pinning actual per-gen counts needs the `libwalrus`/hardware-model side (GAP G1).

---

## ArchLevel — the five-generation ladder

### Purpose

`ArchLevel` enumerates the silicon generation. The numeric code is `generation × 10` — `{10, 20, 30, 40, 50}` for generations 1–5 — and each code maps to a codename string (`ArchLevel2string`) and, for the live generations, to a `Board` hardware-model singleton (`getArchModel`).

### Algorithm

`ArchLevel2string @0x479490` is a compare-chain switch over the five `× 10` codes; each arm `lea`s a codename string from `.rodata`. Verified directly from the disasm:

```c
// bir::ArchLevel2string @0x479490  — verified byte-exact
std::string ArchLevel2string(ArchLevel a):        // arg in esi
    if a == 30:  return "gen3"        // 0x479495  cmp esi,1Eh  -> .rodata 0x70c71f
    if a == 10:  return "inferentia"  // 0x47949c  cmp esi,0Ah  -> .rodata 0x70a339
    if a == 20:  return "sunda"       // 0x4794a1  cmp esi,14h
    if a == 40:  return "core_v4"     // 0x4794d0  cmp esi,28h
    if a == 50:  return "core_v5"     // 0x4794d5  cmp esi,32h  -> .rodata 0x70c748
    // default: out-of-range falls through to the getArchModel-side error
```

### Data Tables

| Code | Codename | Gen | `Board` (`getArchModel`) | Device aliases folded onto the `Board` | Confidence |
|---|---|---|---|---|---|
| 10 | `inferentia` | gen1 | `unk_91DE40` | `{tonga, inferentia, inf1}` | CERTAIN (code+name); HIGH (alias cluster) |
| 20 | `sunda` | gen2 | `unk_91DFC0` | `{sunda, trainium, trn1, inf2}` | CERTAIN / HIGH |
| 30 | `gen3` | gen3 | `unk_91E140` | `{cayman, gen3}` | CERTAIN / HIGH |
| 40 | `core_v4` | gen4 | `unk_91E2C0` | `{core_v4}` | CERTAIN / HIGH |
| 50 | `core_v5` | gen5 | **(none)** | — asserts `"Unknown architecture"` | CERTAIN |

`getArchModel @0x478f90` is the single in-binary device-alias table: it folds the nine codename spellings above onto exactly four `Board` singletons. `string2ArchLevel @0x479720` is the inverse parser; a sibling `ArchLevel2RuntimeTarget @0x4795xx` projects the same ordinal into a third (cost-model) name space — see [Codename ↔ Device ↔ Generation Taxonomy](../arch/codename-taxonomy.md) for the full three-name-space crosswalk.

> **QUIRK — `ArchLevel2string` knows `core_v5`, but `getArchModel` does not.** The name-mapper returns `"core_v5"` for ordinal `50` (`.rodata 0x70c748`), so a reader who only checks the string table would conclude gen-5 is fully supported. It is not: `getArchModel` has no `Board` for it and the chain ends in `assert(0 && "Unknown architecture")` (string `.rodata 0x70c750`). Generation 5 is a **forward stub** in this build — its name is enumerated, its hardware model is absent. See [Vestigial Generations](../arch/vestigial-generations.md).

> **GOTCHA — `Trn1` is gen2 (`sunda`/20), not gen1.** The device→generation folding is the classic off-by-one trap: `inf1`/`tonga` cluster under `inferentia(10)`, but `trn1`/`inf2`/`trainium` all cluster under `sunda(20)`. The marketed "1" in `Trn1` does not mean generation 1.

---

## EngineAccumulationType — the PSUM accumulator command

### Purpose

`EngineAccumulationType` is the PSUM read-modify-write command code: it tells the engine whether to clear, add into, or load-then-accumulate the PSUM bank for a result. Six ordinals. The enum value *is* the command code — `libBIR` does no numeric remap; it owns only the enum and its wire (de)serialization. The actual RMW arithmetic per command is applied in `libBIRSimulator`, not here.

### Algorithm

`EngineAccumulationType2string @0x400990` is a jump-table switch (`cmp esi, 5`, table at `.rodata 0x78ad20`), each arm calling the SSO string writer `sub_3F0770(out, "<name>")`. The ordinal→name binding was traced through the jump table to the `lea`'d name pointer at each target — byte-exact:

```c
// bir::EngineAccumulationType2string @0x400990  — verified via jumptable @0x78ad20
// case 0 -> 0x4009e0 -> "Idle"            (.rodata 0x70bad7)
// case 1 -> 0x4009f8 -> "Zero"            (.rodata 0x70bf0d)
// case 2 -> 0x400a10 -> "Accumulate"      (.rodata 0x70bafd)
// case 3 -> 0x400a28 -> "ZeroAccumulate"  (.rodata 0x70badc)
// case 4 -> 0x4009b0 -> "AddAccumulate"   (.rodata 0x70baeb)
// case 5 -> 0x4009c8 -> "LoadAccumulate"  (.rodata 0x70baf9)
```

### Data Tables

| Value | Name | PSUM accumulator-command semantics (RMW on PSUM bank) | Confidence |
|---|---|---|---|
| 0 | `Idle` | no PSUM accumulator op (engine idle / passthrough) | CERTAIN (name); MEDIUM (RMW detail) |
| 1 | `Zero` | zero the PSUM region (clear, no add) | CERTAIN / MEDIUM |
| 2 | `Accumulate` | `PSUM += result` (read-modify-write add) | CERTAIN / MEDIUM |
| 3 | `ZeroAccumulate` | `PSUM = result` (zero then write = start-of-accumulation) | CERTAIN / MEDIUM |
| 4 | `AddAccumulate` | `PSUM += result` (explicit add variant) | CERTAIN / MEDIUM |
| 5 | `LoadAccumulate` | `PSUM = load-then-accumulate` (load operand into accumulator) | CERTAIN / MEDIUM |

### Wire binding — two JSON keys, one enum

This enum is the deserialization target of **two distinct BIR JSON keys carried on different instructions**, both verified from disassembly:

- **`accumulator_cmd`** (`.rodata 0x70c217`) on `InstCopyPredicated`. `toJson @0x439a00` reads the `uint32` field at `+0x144` (`mov 0x144(%rbx),%eax @0x439a7d`) and serializes it by name via `to_json(json&, EngineAccumulationType) @0x41bc50`; `readFieldsFromJson @0x419470` parses the key back into `+0x144`. The sibling `+0x140` field on the same instruction is an `AluOpType`.
- **`reduce_cmd`** (`.rodata 0x70c18b`) on `InstExponential` and `InstRangeSelect`. `InstExponential::readFieldsFromJson` reads `.json.at("reduce_cmd")` then `lea 0xf0(%r12)` / `jmp from_json(…EngineAccumulationType&)` (`@0x417cdb`–`0x417cf0`), so `reduce_cmd` deserializes to an `EngineAccumulationType` at `+0xF0`.

> **NOTE — the same `EngineAccumulationType` enum is reached through two different JSON key names.** `accumulator_cmd` (on `InstCopyPredicated`, field `+0x144`) and `reduce_cmd` (on `InstExponential`/`InstRangeSelect`, field `+0xF0`) both deserialize to this one enum. A reimplementer reading the wire format must wire *both* key names to the same six-name parser. This is the same `reduce_cmd`→`EngineAccumulationType` binding that [AluOpType + Mode Enums](aluop-modes.md) recovered from the opposite (mode-enum) side — and it is why `reduce_cmd` is *not* a `ReduceCmdType` despite the name; `bir::ReduceCmdType` is a dormant sim/runtime enum with no JSON round-trip in this build.

> **GOTCHA — the companion `accumulation_flag` is a different field on a different instruction.** The bool `accumulation_flag` (`.rodata` present) lives on `InstMatmultBase`, driven by `getAccumulationFlagEvalIfNeeded @0x4030e0` — it is matmul's own start/stop-tensor-calc accumulate flag, *not* the six-valued `EngineAccumulationType`. Do not collapse the two.

---

## The AP axis-letter key — `[W, Z, Y, X]`

The next three enums all name axes of a TPB access pattern (AP) by single letters. The physical meaning of those letters is fixed and proven by two independent witnesses inside `libBIR`:

```text
W = AP dim 0  (outermost / partition-side)   = Pattern[0]
Z = AP dim 1                                  = Pattern[1]
Y = AP dim 2                                  = Pattern[2]
X = AP dim 3  (innermost free dim)            = Pattern[3]
C = the extra channel / "collapse-all" token  (AxisListType only; not a positional axis)
```

- **Direction witness 1:** `AccessPattern::getStepBytesPerHighestDim @0x203170` reads `Pattern[0].getStep()` and asserts `"Negative step in Pattern[0] not supported"` — AP index `[0]` *is* the highest/outermost dim (the partition-step entry).
- **Direction witness 2:** `get_reduce_ops @0x49d190` collapses from `ap[size-1]` inward, naming the innermost reduced dim `X`, the next `Y`, then `Z`, then `W`.
- **Identity witness:** the canonical identity permutation `"WZYX"` is *absent* from `TransposeOps` (it is `None=0`), which pins the canonical source axis order to `[W,Z,Y,X]` = AP index `[0,1,2,3]`.

`W` corresponds to the SBUF/PSUM partition axis (128 lanes). `partition_dim` is a *stored* index (`MemoryLocationSet::getPartitionDim @0x343d60`, offset `+0x334`), so "`W` = AP[0] = partition" is the canonical/default layout; the field exists so a non-zero AP entry can be flagged the partition axis (GAP G4). The 16-byte `TENSOR3D` AP descriptor carries `(stride,num)` pairs for axis0/1/2; the 4-D `TENSOR4D` variant spills a fourth pair, giving the full `{W,Z,Y,X}` that `TransposeOps` permutes and `AxisListType=XYZW` reduces.

---

## AxisListType — the reduce/free-axis selector

### Purpose

`AxisListType` selects how many innermost AP dims a `TensorReduce`/`Pool` collapses. Six values. The name lists the axis letters that *are* reduced, innermost-first.

### Algorithm

`AxisListType2string @0x4011b0` is a switch with bound `cmp esi, 5` (6 cases; prologue bytes `41 54 83 fe 05` = `push r12 / cmp esi,5`). The reduce semantics live in the consumer `get_reduce_ops @0x49d190`, which reads the stored `AxisListType` from `Instruction+0xF4` — exactly where `InstBuilder::addTensorReduce @0x2b3670` stored it.

```c
// consumer: StaticProfiler::get_reduce_ops @0x49d190  — for TensorReduce (opcode 27)
N = AxisListType_value + 1                 // count of INNERMOST AP dims reduced
assert(AxisListType_value <= 3 && ap_size > 2)
v = ap[size-1].num                         // X  (innermost, always reduced)
if N >= 2: v *= ap[size-2].num             // Y
if N >= 3: v *= ap[size-3].num             // Z
if N == 4: v *= ap[size-4].num             // W
// the outer entries ap[0 .. size-N-1] are the FREE (kept) loop dims
```

### Data Tables

| Val | Name | Reduced (innermost-first) | Confidence |
|---|---|---|---|
| 0 | `X` | `{X}` — innermost dim only | CERTAIN |
| 1 | `XY` | `{X, Y}` | CERTAIN |
| 2 | `XYZ` | `{X, Y, Z}` | CERTAIN |
| 3 | `XYZW` | `{X, Y, Z, W}` — all four free dims | CERTAIN |
| 4 | `XYZWC` | 5-axis / channel variant (not on the `get_reduce_ops` path) | CERTAIN (name); GAP (consumer) |
| 5 | `C` | channel / "collapse-all" selector | CERTAIN (name); GAP (consumer) |

`string2AxisListType @0x40e630` is the bijective inverse (`"X"→0 … "C"→5`).

> **QUIRK — `Pool(20)` ignores the stored `AxisListType`; it hard-codes a reduce count of 2.** On the `get_reduce_ops` path, the `Pool` opcode always collapses the two innermost dims `{X,Y}` regardless of the `AxisListType` value stored on the instruction. Only `TensorReduce(27)` uses `N = value + 1`. A reimplementation that drives `Pool`'s collapse width from the enum will be wrong for any `Pool` whose stored `AxisListType != XY(1)`.

> **NOTE — `XYZWC(4)` and `C(5)` never reach `get_reduce_ops` (it asserts `value <= 3`).** The trailing `C` is the only letter outside the `W/Z/Y/X` positional set; it denotes a 5th/channel axis or a collapse-all-including-channel selector. These two values belong to wider AP variants (4-D BN / channel-wise reduce) whose consumer is not resolvable from `libBIR` alone (GAP G3).

---

## IndirectDim — the gather/scatter dimension selector

### Purpose

`IndirectDim` names which AP axis is *indirected* — whose index comes from an offset/index vector rather than a static stride. Four values. The enum value equals the axis's canonical dim ordinal: `W=0, Z=1, Y=2, X=3`.

### Algorithm

`IndirectDim2string @0x4021a0` is a small branch tree (`cmp esi,2 / jg …`, then `cmp esi,1`), four arms writing single-letter names. The value is stored in `DynamicAPINFO+0x74` (116) and serialized by `to_json @0x268c00` under the key `indirect_dim`.

### Data Tables

| Val | Name | Gathers along | Confidence |
|---|---|---|---|
| 0 | `W` | AP dim 0 — outermost / partition-side | CERTAIN |
| 1 | `Z` | AP dim 1 | CERTAIN |
| 2 | `Y` | AP dim 2 | CERTAIN |
| 3 | `X` | AP dim 3 — innermost free dim | CERTAIN |

`string2IndirectDim @0x40f290` is the bijective inverse; `from_json @0x41a600` reads the JSON string and parses it. Companion JSON keys on the same descriptor: `indirect_dim_max_index`, `indirect_dims`, `num_tensor_indirect_indices`.

> **NOTE — `libBIR` only (de)serializes/prints `IndirectDim`; the descriptor binding is in `libwalrus`.** The AP-index → wire-descriptor binding (the bit-29 "tensor-indirect" mode, the `INDIRECT16B`/`MXINDIRECT16B` field computation) is performed in `libwalrus` CoreVN codegen, not here (GAP G1). `IndirectDim` value `X(3)` indirects the innermost free dim; `W(0)` the outermost.

---

## TransposeOps — the 4-axis DMA-transpose permutation

### Purpose

`TransposeOps` encodes a permutation of the four AP axes for a DMA transpose. Fourteen values: `None` (identity) plus 13 non-trivial permutations of `{W,Z,Y,X}`. The name is the *destination* axis order; the canonical source order is `[W,Z,Y,X]` = AP index `[0,1,2,3]`.

### Algorithm

`TransposeOps2string @0x401500` is a switch with bound `cmp esi, 0xd` (14 cases). The consumer is `InstBuilder::addDMATranspose @0x2b4390`, whose dynsym signature carries the `TransposeOps` argument and a `SmallVector<APPair, 4>` — and which asserts both src and dst APs are 4-D:

```c
// consumer: InstBuilder::addDMATranspose @0x2b4390
assert(src_ap.size() == 4 && dst_ap.size() == 4)  // "Source and destination access pattern must be 4-D"
assert(dst.isSB())                                 // "Destination must be ... a SB location"
inst[+0x158] = transpose_ops_value                 // store TransposeOps
inst[+0x128] = CopyMode::Transpose (1)             // mark the InstDMACopy a transpose
```

### Data Tables

The 14 codes. "src AP idx" lists, for destination position `[0,1,2,3]`, which source AP index is placed there (source order `[W,Z,Y,X]`). All names verified byte-exact off the switch arms; `string2TransposeOps @0x40e860` is the bijective inverse over all 14.

| Val | Name | Dst order | src AP idx `[d0,d1,d2,d3]` | Character |
|---|---|---|---|---|
| 0 | `None` | W Z Y X | `[0,1,2,3]` | identity (`WZYX` implicit) |
| 1 | `WZXY` | W Z X Y | `[0,1,3,2]` | swap two innermost (X↔Y) |
| 2 | `WXZY` | W X Z Y | `[0,3,1,2]` | rotate inner-3 |
| 3 | `WYXZ` | W Y X Z | `[0,2,3,1]` | rotate inner-3 |
| 4 | `ZWYX` | Z W Y X | `[1,0,2,3]` | swap two outermost (W↔Z) |
| 5 | `ZYWX` | Z Y W X | `[1,2,0,3]` | 3-cycle on outer-3 |
| 6 | `ZYXW` | Z Y X W | `[1,2,3,0]` | left-rotate all 4 |
| 7 | `YXWZ` | Y X W Z | `[2,3,0,1]` | swap halves |
| 8 | `YXZW` | Y X Z W | `[2,3,1,0]` | X,Y to front; Z,W reversed |
| 9 | `YWZX` | Y W Z X | `[2,0,1,3]` | 3-cycle on outer-3 |
| 10 | `XWZY` | X W Z Y | `[3,0,1,2]` | right-rotate all 4 |
| 11 | `XZYW` | X Z Y W | `[3,1,2,0]` | swap W↔X |
| 12 | `XYZW` | X Y Z W | `[3,2,1,0]` | full reversal (the canonical transpose) |
| 13 | `XYWZ` | X Y W Z | `[3,2,0,1]` | reverse, then swap trailing pair |

> **QUIRK — the identity permutation is `None`, not `WZYX`; and only 13 of the 24 possible 4-axis permutations are enumerated.** `WZYX` is absent precisely because it is the identity (`None=0`). Code 12 `XYZW` is the *full* reversal — `dst[0]=src[3] … dst[3]=src[0]` — i.e. the canonical N-D transpose. The 10 unlisted permutations are not expressible as a single `TransposeOps` and are not emitted by `addDMATranspose` (GAP G2). `TransposeOps` is meaningful only on a 4-axis AP; on shorter patterns the assert fires.

---

## Verification Summary

Five of the six enums are CERTAIN at the byte level; the one gap is the per-arch engine *count* numbers.

| Claim | Method | Result |
|---|---|---|
| Binary identity | `md5 = 12bb979f7ca41248252abb0f16b2da98`, build-id `a9b1ea38…` | matches both backing reports |
| All six `2string` addresses + sizes | `readelf -sW` / `nm -D` exported symbols | every address matched exactly |
| `EngineType` ordinals + both name tables | jump table `@0x7974bc` → `movl $0x6c6f6f50`("Pool" case 1), `mov $0x4550`("PE" case 3); `.rodata` `External: GPSIMD Internal: Pool` strings | CERTAIN |
| `GPSIMD ≡ Pool(1)` alias | `EngineType2ExternalName` case 1 + diagnostic string | CERTAIN |
| `EngineAccumulationType` ordinal→name | jump table `@0x78ad20` traced to each `lea` name ptr | CERTAIN (6/6 names) |
| `ArchLevel` codes/codenames | `cmp esi` against `{0Ah,14h,1Eh,28h,32h}`; codename `.rodata` resolved | CERTAIN; `core_v5(50)` no-`Board` confirmed |
| `AxisListType`/`IndirectDim`/`TransposeOps` bounds | switch prologues `cmp esi,{5,2,0Dh}` | CERTAIN (6/4/14 cases) |
| `reduce_cmd`/`accumulator_cmd` → `EngineAccumulationType` | disasm `from_json` tail at `+0xF0` / field read at `+0x144` | CERTAIN |

**Re-verify ceiling.** The ordinal sets, the name tables, the `GPSIMD≡Pool(1)` alias, the wire-key bindings, and the switch bounds are all byte-confirmed against the binary's exported symbols and disassembly. What is *not* pinnable from `libBIR` alone: the numeric per-arch engine counts (runtime-built in `.bss` `Board` objects via `libwalrus`; GAP G1), the exact PSUM RMW micro-op per `EngineAccumulationType` ordinal (applied in `libBIRSimulator`; MEDIUM on silicon detail), the consumers of `AxisListType {XYZWC, C}` (GAP G3), the `TransposeOps` HW-realizable selection rule (GAP G2), and the `IndirectDim`→wire-descriptor binding (GAP G1).

## Related Components

| Name | Relationship |
|---|---|
| `EngineType2ExternalName @0x47fca0` | the second `EngineType` name table — internal→external alias |
| `getArchModel @0x478f90` | the in-binary codename→`Board` alias-cluster table (9 spellings → 4 `Board`s) |
| `getEngineCount @0x47d820` | per-arch engine multiplicity (reads `Board` engine-count array `+0x6C..+0x88`) |
| `get_reduce_ops @0x49d190` | `AxisListType` consumer; collapses `value+1` innermost dims (`Pool` hard-codes 2) |
| `addDMATranspose @0x2b4390` | `TransposeOps` consumer; asserts 4-D src/dst, stores at `InstDMACopy+0x158` |
| `libBIRSimulator.so` | applies the `EngineAccumulationType` PSUM RMW arithmetic (reference datapath) |
| `libwalrus.so` (CoreVN codegen) | builds engine counts, the `IndirectDim`/`TransposeOps` wire descriptors |

## Cross-References

- [GPSIMD Engine — the Pool-Alias Cross-Core SB2SB Mover](../arch/gpsimd-engine.md) — 1.13, the single machine op that runs on `EngineType Pool(1)`; the same `GPSIMD≡Pool` alias from the engine side
- [Codename ↔ Device ↔ Generation Taxonomy](../arch/codename-taxonomy.md) — 1.02, the full `ArchLevel`↔codename↔CoreV↔device crosswalk and the three name spaces per ordinal
- [Vestigial Generations](../arch/vestigial-generations.md) — `core_v5(50)` and the gen-5 forward-stub status
- [AluOpType + the Mode Enums](aluop-modes.md) — 7.7, the `reduce_cmd`→`EngineAccumulationType@+0xF0` binding recovered from the mode-enum side; the dormant `ReduceCmdType`
- [InstructionType: the 110-Opcode Enum](instruction-type.md) — `TensorReduce(27)`/`Pool(20)`/`InstCopyPredicated` opcodes that carry these fields
- [The `bir::Instruction` Base Struct](instruction-base.md) — the polymorphic root whose subclasses store these enum fields
- [Build & Version Provenance](../reference/versions.md) — md5/build-id pins for `libBIR.so`, `libBIRSimulator.so`, `libwalrus.so`
