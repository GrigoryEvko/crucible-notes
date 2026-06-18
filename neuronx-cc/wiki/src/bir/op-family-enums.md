# Op-Family Enums and the L1/L2/L3 Crosswalk

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the cp310 build of `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`). The L3 silicon bytes come from the sibling `libwalrus.so` (cp310, md5 `1d93972b81e619ce6d178a0e4b9003b3`); for both libraries `.text` (`libBIR` `0x1820c0`–`0x707a44`) and `.rodata` (`0x708000`–`0x7a118c`) have virtual address equal to file offset. The cp311/cp312 builds carry the identical ABI but their addresses drift; the ordinals, names, and the L1≠L3 mapping are version-stable.*

## Abstract

This page closes the BIR op-family enum catalog. It transcribes the enums that select the *operation* of the matmul, branch, collective, RNG, semaphore, DMA, and activation/pool instruction families — `MatmultPerfMode`, `BranchCompareOp`, `BranchOutcomeHint`, `CollectiveKind`/`CollectiveDimension`/`CollectiveComputeTypeHint`, `RandomAlgorithmKind`/`RandomDistributionKind`/`EventSemaphoreClearMode`, `DGEType`/`DMAQoSClass`/`DMAQueueAttribute`, `ActivationFunctionType` (31 members) and `PoolFunctionType` — plus the remaining metadata enums that round out the 45-symbol `<Enum>2string` census. It then assembles the master **L1/L2/L3 crosswalk** and classifies the four families where the numbering diverges across layers.

Every BIR enum has up to three on-the-wire representations, and confusing them is the single most expensive mistake a reimplementer can make. **L1** is the C++ ordinal stored in the `bir::Inst*` object — the integer the `<Enum>2string` switch indexes. **L2** is the BIR-JSON wire form: with one exception (`MemoryType`), every enum serializes *by name*, `toJson → bir::to_json(Enum) → <Enum>2string(int) → json["key"]`, and parses *by name* through `from_json → string2<Enum>`. So the L2 name equals the L1 spelling, 1:1. **L3** is the silicon byte `libwalrus`'s CoreV{2,3,4} `visitInst<Op>` emitter stamps into the 64-byte TPB instruction bundle. L3 exists only for the handful of enums that map onto a silicon opcode or field — and for four of them **the L3 byte is not the L1 ordinal**.

The four mismatch families are the gravitational center of the page: (M1) `InstructionType` → CoreV* `setupHeader` opcode word (`QuantizeMx` is L1 `96` but L3 byte `0xE3`); (M2) `AluOpType` → CoreV4 `ALU_OP` byte, where the comparison predicates are renumbered; (M3) `Dtype` → `NEURON_ISA_TPB_DTYPE` wire-tag, a full remap; and (M4) `DMAQoSClass` → encoded descriptor byte `class − 1`. The detailed M2 comparison-reorder switch is owned by the sibling page [AluOpType and the BIR Mode Enums](aluop-modes.md); this page situates it inside the broader four-family taxonomy and supplies the op-family enums that the crosswalk needs.

For reimplementation, the contract is:

- The exact ordinals and verbatim spellings of every op-family enum, recovered from both the `<Enum>2string` forward body and the `string2<Enum>` inverse — including the corrections where the public guesswork had the order or membership wrong.
- The opcode that *carries* each enum, the JSON key, and the struct offset it deserializes into (so a reimplemented parser writes the right field).
- The three-layer model and the four L1≠L3 families, with a representative each reproduced from the `libwalrus` emitter, so a backend never reads an L1 ordinal as a silicon byte or vice-versa.
- The handful of structural quirks: one opcode multiplexes all 11 `CollectiveKind`s; `ActivationFunctionType` routes 31 variants through one `InstActivation` opcode; `EventSemaphoreClearMode` lives on `GroupResetSemaphores`, not `EventSemaphore`; `DMAQueueAttribute` has no statically recoverable member names.

| | |
|---|---|
| **Enum codegen** | InstaBrew `brewer.py` — every default arm cites `…/neuronxcc/instabrew/brewer.py`; the hand-written ones cite `walrus/ir/lib/IR/*.cpp` |
| **L1→L2 (serialize)** | `Inst::toJson` → `bir::to_json(json, Enum&)` → `<Enum>2string(int)` → `json["<key>"]` |
| **L2→L1 (parse)** | `Inst::readFieldsFromJson` → `json.at("<key>")` → `bir::from_json(json, Enum&)` → `string2<Enum>(str)` → field at `Inst+0xNN` |
| **L3 emitter (libwalrus)** | CoreV{2,3,4} `visitInst<Op>` → `setupHeader` 16-bit opcode word + per-field encoders |
| **Census** | 45 distinct `<Enum>2string` bodies; 44 enumerated (DMAQueueAttribute has no static members) |
| **L1≠L3 families** | M1 `InstructionType`, M2 `AluOpType`, M3 `Dtype`, M4 `DMAQoSClass` |

> **NOTE —** every `0x40xxxx`/`0x2dxxxx`/`0x3cxxxx` body cited here has a `0x17xxxx`/`0x18xxxx` twin in the IDA listing. The twins are the lazy-binding PLT stubs (second VA frame for the same symbol); the real switch bodies are the high-band addresses. Cite the high-band frame.

---

## The Three-Layer Model

### Purpose

Before any single enum, fix the vocabulary. A BIR enum is one value seen three ways, and the layers do not share a numbering. The model below is the contract that prevents the cross-layer confusion the rest of the page repeatedly flags.

### Layers

```text
L1  libBIR INTEGER     the C++ enum ordinal in the bir::Inst* object
                       (e.g. InstructionType @Inst+0x58); the index the
                       <Enum>2string switch consumes.   Source: this page + D01-D13.

L2  BIR-JSON NAME      how the enum is serialized in BIR-JSON. EVERY enum but one
                       is emitted AS ITS NAME STRING (L2 name == L1 spelling, 1:1).
                       Chain:  toJson -> to_json(Enum) -> <Enum>2string(int) -> json["key"]
                               readFields -> from_json -> string2<Enum>(str) -> field
                       EXCEPTION: MemoryType -> the numeric bit-flag (1/2/4/.../128).

L3  libwalrus BYTE     the integer field the CoreV{2,3,4} visitInst<Op> encoder stamps
                       into the 64-byte TPB bundle (setupHeader writes the 16-bit opcode
                       WORD; per-field encoders write dtype/ALU/QoS BYTES). Exists ONLY
                       for enums that map onto a silicon opcode/field. Produced DOWNSTREAM
                       in libwalrus, NOT in libBIR.
```

### The decisive consequence

For an enum with both an L1 ordinal and an L3 byte, the two are generally **not** the same number. There are exactly four such families (§ The Four Mismatch Families). For every other enum, L3 is absent — the enum either rides inside an opcode already counted under `InstructionType`, selects a `libwalrus` emitter codepath with no standalone byte, or is consumed only by passes and the verifier. The **L2 name is the only representation stable across all three layers**; it is the join key.

> **GOTCHA —** the canonical trap, reproduced verbatim from the binary: `bir::InstructionType::QuantizeMx` is L1 `96`, BIR-JSON `"type":"QuantizeMx"`, and L3 silicon byte `0xE3` (`227`). Reading `96` as a TPB opcode, or `0xE3` as a BIR class id, is wrong in both directions. Only the name `"QuantizeMx"` survives the journey.

---

## Matmul — MatmultPerfMode

### Purpose

`MatmultPerfMode` (5 values, `0..4`) selects how operand/result lanes are *packed* into the 128×128 systolic PE array to raise throughput when the native element is narrower than the array cell. It is the `perf_mode` field of `InstMatmultBase`, inherited by every matmul leaf opcode. The matmul runs on `EngineType::PE` (`InstMatmult::getDefaultEngine @0x3e2cb0 → 3`).

### Encoding

`MatmultPerfMode2string @0x400be0` is a 5-arm switch; `string2MatmultPerfMode @0x40e150` is the inverse cascade against the same five literals; both are byte-exact, both anchored to `brewer.py`.

| L1 | Name (verbatim) | Meaning | Conf |
|---|---|---|---|
| 0 | `None` | standard 1-pass matmul, no PE-array packing | CERTAIN |
| 1 | `DoubleRow` | fold two contraction rows (partitions) into one array pass — ~2× on the K/partition axis | CERTAIN |
| 2 | `DoubleColumn` | pack two PSUM output columns per pass — ~2× on the output-column axis | HIGH |
| 3 | `DoublePixel` | pack two free-axis fmap pixels per pass — ~2× on the free/spatial axis | HIGH |
| 4 | `DoubleRowSwInterleave` | `DoubleRow` with software-interleaved row pairs (when the AP stride cannot satisfy the hardware even-pair constraint) | HIGH |

The packing meaning for modes `1` and `4` is grounded in the `libwalrus` matmul emitter: `DoubleRow` is the `float32r` (`bir::Dtype` idx `17`) path that halves the K-axis lengths and rewrites the source access-pattern so two K-rows stream per cycle, asserting "DoubleRow AP's first F dim must be 2". Modes `2`/`3` are HIGH (inferred from the name and the proven `DoubleRow` K-fold); a CoreV* `perf_mode` switch decode would raise them to CERTAIN.

### Wire

`InstMatmultBase::readFieldsFromJson @0x41f600` parses JSON key `"perf_mode"` into the field at `InstMatmultBase+0x2D0` (`lea rsi, aPerfMode` @`0x41fc0f`; `lea rsi, [r14+2D0h]` @`0x41fc1b`; `call from_json(…, MatmultPerfMode&)` @`0x41fc25`). The field is a `MaybeAffine`-wrapped enum. At L3, `MatmultPerfMode` has **no standalone silicon byte** — it folds an array axis inside the PE emitter codepath (the chosen variant rides in the matmul opcode bundle, counted under `InstructionType`).

---

## Branch — BranchCompareOp and BranchOutcomeHint

### Purpose

Two enums govern conditional control flow. `BranchCompareOp` (13 values) is the comparison predicate of `InstCompareAndBranch` (IT `78`); `BranchOutcomeHint` (2 values) is the static branch-prediction hint of `InstBranchHint` (IT `80`). All control-flow ops share `getDefaultEngine → 7` (`EngineType::ALL`) and default `InstSyncType=1` (sequencer): branches are sequenced on the control path.

### BranchCompareOp — encoding

`BranchCompareOp2string @0x401ff0` has **13 arms (`0..12`), not 12** — the public roster stopped at the reg block. `string2BranchCompareOp @0x40f090` is the 13-branch inverse cascade in the same order. The 12 relational predicates form a 6×2 grid (predicate × rhs-source); value `12` is a guard sentinel.

| L1 | Name | Encoding | Conf |
|---|---|---|---|
| 0–5 | `IS_LTIMM` `IS_LEIMM` `IS_EQIMM` `IS_NEIMM` `IS_GEIMM` `IS_GTIMM` | `if (lhs <pred> <imm>) goto on_true` — rhs is an immediate baked into the branch | CERTAIN |
| 6–11 | `IS_LTREG` `IS_LEREG` `IS_EQREG` `IS_NEREG` `IS_GEREG` `IS_GTREG` | `if (lhs <pred> <reg>) goto on_true` — rhs is a scalar register / runtime value | CERTAIN |
| 12 | `Unsupported` | guard sentinel for a predicate the codegen target cannot encode (kept so round-trip never silently drops) | CERTAIN |

Predicate order within each block is `LT(0) LE(1) EQ(2) NE(3) GE(4) GT(5)`; the block (`val / 6`) selects the rhs source: block 0 = `IMM`, block 1 = `REG`. `InstCompareAndBranch::readFieldsFromJson @0x416f80` parses key `"comp_op"` into `+0xF0`; branch targets are the separate keys `"on_true"`/`"on_false"`. The producer of value `12` (`Unsupported`) is a `libwalrus` codegen guard, not visible in `libBIR`.

> **NOTE —** `BranchCompareOp` is the antisymmetric cousin of `AluOpType`'s comparison family. Where the [AluOpType](aluop-modes.md) predicates get *reordered* on the CoreV4 wire so an operand swap maps `gt↔lt`, `BranchCompareOp` instead doubles the predicate set into `IMM`/`REG` blocks. Both designs exist to avoid re-encoding a swapped comparison; they solve it differently.

### BranchOutcomeHint — encoding

`BranchOutcomeHint2string @0x4022a0` strcpy's two literals inline (no jump table); `string2BranchOutcomeHint @0x40f340` compares them in order. The verbatim spellings are `LikelyTaken`/`LikelyNotTaken` (the public "taken/not-taken" was the gloss, not the enumerator).

| L1 | Name | Meaning | Conf |
|---|---|---|---|
| 0 | `LikelyTaken` | static branch-prediction hint: predict taken | CERTAIN |
| 1 | `LikelyNotTaken` | static branch-prediction hint: predict fall-through | CERTAIN |

`InstBranchHint::readFieldsFromJson @0x41af70` parses key `"hint"` into `+0xF0`. IT `80` `BranchHint` is a *separate* opcode from IT `78` `CompareAndBranch`: the Terminator base accept-mask `@byte_7858CE` rejects `80`, so `BranchHint` is excluded from structural Terminator subsumption — it is a hint annotation, not a branch.

---

## Collective — Kind, Dimension, ComputeTypeHint

### Purpose

Three enums parametrize collective communication, and all three live on a single opcode, `InstCollectiveCompute` (IT `48`). `CollectiveKind` (11 values) is the algorithm; `CollectiveDimension` (2 values) is the axis; `CollectiveComputeTypeHint` (3 values) is the parallelism-strategy classifier. `InstCollectiveCompute::getDefaultEngine @0x3e2cf0 → 1` (`EngineType::Pool`).

> **QUIRK —** `CollectiveKind` is **not** 1:1 with an `InstructionType`. The IT field (`47`–`50`) is the top-level opcode discriminator; `CollectiveKind` is an orthogonal sub-field stored on `InstCollectiveCompute` (IT `48`). So one opcode (`48`) multiplexes all 11 kinds `SendRecv..PermuteReduceImplicit`. The point-to-point primitives `InstCollectiveSend` (IT `49`) and `InstCollectiveRecv` (IT `50`) carry only `peer_id` — they are *not* selected by `CollectiveKind==SendRecv` (that kind rides on IT `48`).

### CollectiveKind — encoding

`CollectiveKind2string @0x4016c0` is a jump-table switch (`ja` bound rejects `>10`); `string2CollectiveKind @0x40ea80` is 11 ordered `std::string::compare` arms. The contiguous string pool at `.rodata 0x70bc94` reads byte-exact (verified by `xxd` at the VA): `SendRecv·SendRecvCCE·AllReduce·ReduceScatter·AllGather·AllToAll·AllToAllV·Permute·PermuteReduce·PermuteImplicit·PermuteReduceImplicit·"Unknown CollectiveKind"`.

| L1 | Name | Reduce-op (`op`, AluOpType) | Conf |
|---|---|---|---|
| 0 | `SendRecv` | n/a (move) | CERTAIN |
| 1 | `SendRecvCCE` | n/a (collective-comm-engine) | CERTAIN |
| 2 | `AllReduce` | add/max/min/… (required) | CERTAIN |
| 3 | `ReduceScatter` | add/max/min/… (required) | CERTAIN |
| 4 | `AllGather` | n/a | CERTAIN |
| 5 | `AllToAll` | n/a | CERTAIN |
| 6 | `AllToAllV` | n/a | CERTAIN |
| 7 | `Permute` | n/a (uses `permute_chain`/`src_target_pairs`) | CERTAIN |
| 8 | `PermuteReduce` | add/max/min/… (required) | CERTAIN |
| 9 | `PermuteImplicit` | n/a | CERTAIN |
| 10 | `PermuteReduceImplicit` | add/max/min/… (required) | CERTAIN |

> **GOTCHA —** the reduction operation of a reducing collective is **not** part of `CollectiveKind` and **not** the compute-type hint. It is a separate field, `op`, typed `bir::AluOpType` (`add=4`, `max=8`, `min=9`, …), parsed inside the nested `cc_channel` object at `InstCollectiveCompute+0x180`. The per-kind legality of `op` (which kinds require a non-`bypass` reduce-op) is enforced in the `libwalrus` `birverifier`, not in `libBIR`; the "required" column is HIGH-confidence semantics, not a `libBIR` gate.

### CollectiveDimension and CollectiveComputeTypeHint

`CollectiveDimension2string @0x401a30`: `0=Partition`, `1=Free` (parsed at `cc_dim`, field `+0x27C`). `CollectiveComputeTypeHint2string @0x401840`: `0=TP`, `1=FSDP`, `2=None` (parsed at `cc_type_hint`, field `+0x244`).

| Enum | L1 values | JSON key | Conf |
|---|---|---|---|
| `CollectiveDimension` | `0:Partition` `1:Free` | `cc_dim` | CERTAIN |
| `CollectiveComputeTypeHint` | `0:TP` `1:FSDP` `2:None` | `cc_type_hint` | CERTAIN |

> **CORRECTION (D-D07) —** `CollectiveComputeTypeHint` is a **tensor/data-parallelism-strategy classifier** (Tensor-Parallel vs Fully-Sharded-Data-Parallel vs unspecified), *not* a "sum/max/min reduction-op hint" as earlier rosters supposed. The upstream is the HLO stream-id injector (`stream_id "0"`=TP, `"1"`=FSDP). Both directions of the enum body and the read/write key↔offset wiring confirm it.

The constructor (`InstCollectiveCompute C1 @0x4106c0`) defaults `kind = AllReduce(2)` and `cc_type_hint = None(2)`; `toJson` *suppresses* emitting `cc_type_hint` when it equals `2` (`cmp [rbx+244h],2; jz` @`0x4395b2`) — `None` is the silent default.

---

## RNG and Semaphore — Random* and EventSemaphoreClearMode

### Purpose

`RandomAlgorithmKind` (3) and `RandomDistributionKind` (4) are the PRNG selectors; both live on exactly one opcode, `InstRand` (IT `60`). `EventSemaphoreClearMode` (2) is the semaphore-group reset mode; it lives on `InstGroupResetSemaphores` (IT `14`) — **not** `EventSemaphore` (IT `13`).

### Random enums — encoding

`RandomAlgorithmKind2string @0x401de0` and `RandomDistributionKind2string @0x401ee0` are byte-exact both directions; the `Uniform` arm is built inline from immediates (`'Unif'`/`'or'`/`'m'`), decoded and confirmed by the `string2` comparand order.

| Enum | L1 values | JSON key | Conf |
|---|---|---|---|
| `RandomAlgorithmKind` | `0:LFSR` `1:PCG32` `2:PHILOX_1` | `random_algorithm` (`InstRand+240`) | CERTAIN |
| `RandomDistributionKind` | `0:Raw` `1:Uniform` `2:Normal` `3:Binomial` | `distribution` (`InstRand+288`) | CERTAIN |

> **CORRECTION (D-D10) —** earlier rosters had `RandomAlgorithmKind` as `PCG32=0` (the order was wrong; the body's `if(a2) fatal` guard proves only `a2==0` reaches `LFSR`, so `LFSR=0`), and `RandomDistributionKind` listed only `{Normal,Binomial,Raw}` — **missing `Uniform`**. The true sets are above, byte-verified both directions.

`InstRand::readFieldsFromJson @0x420ab0` is the *sole* caller of both `from_json<Random*>` overloads — proven by the xrefs census. The near-duplicate RNG ops carry neither enum: `InstRand2` (IT `97`), `InstRng` (IT `100`), `InstGetRandState` (`58`), `InstSetRandState` (`59`), `InstRandGetState` (`99`), `InstRandSetState` (`98`) all have degenerate (`retn`) `readFieldsFromJson` — their data flows through tensor operands. The RNG state itself resides in `MemoryType RNGSTATE`, which is the **bit flag `64` (`0x40`)**, not ordinal `6` (`MemoryType` is a power-of-two bitmask; see § Master Crosswalk).

### EventSemaphoreClearMode — encoding

`EventSemaphoreClearMode2string @0x400ac0` has exactly two arms; value `1` is the 20-byte string assembled from `xmmword_7813B0` (`"SemaphoreZeroBit"`) + `"mask"`.

| L1 | Name | Clear behavior of the `sema_group` | Conf |
|---|---|---|---|
| 0 | `SemaphoreZero` | reset each semaphore in the group to value `0` (full counter zero) | CERTAIN |
| 1 | `SemaphoreZeroBitmask` | zero only the masked bits of each semaphore (selective clear; value preserved outside the mask) | CERTAIN |

> **CORRECTION (D-D10) —** `EventSemaphoreClearMode` is the `"mode"` field of `InstGroupResetSemaphores` (IT `14`), parsed at `+240` by `readFieldsFromJson @0x4307e0` (alongside the optional `sema_group` vector). `InstEventSemaphore` (IT `13`) does **not** carry it — its `readFieldsFromJson @0x405090` is a bare `retn`, and the only `ClearMode` (de)serializer callers are `GroupResetSemaphores` methods. Earlier rosters that pinned the clear behavior on op `13` were wrong.

---

## DMA — DGEType, DMAQoSClass, DMAQueueAttribute

### Purpose

Three enums describe DMA descriptor generation and priority. `DGEType` (4) selects the descriptor-generation *engine*; `DMAQoSClass` (17) is the priority level; `DMAQueueAttribute` is a property-bag key whose member names are not statically recoverable from this build.

### DGEType — encoding

`DGEType2string @0x400dc0`: `0=None`, `1=SWDGE`, `2=HWDGE`, `3=Unassigned` (`string2DGEType @0x40e290` inverse). The field is `InstDMA+0xF8`, constructor default `3` (`Unassigned`). SW-DGE = the compiler emits an explicit descriptor table in the stream; HW-DGE = the DMA engine generates descriptors on-device from a compact offset-register-driven access-pattern (gated by `isHWDGEDMAWithEngineSet @0x30f240`: `HWDGE && engine-id-set`). `DGEType` selects an emitter codepath at L3, not a standalone byte.

### DMAQoSClass — encoding and the L1≠L3 offset

`DMAQoSClass2string @0x400ed0` is a 17-arm switch; `P_n = n + 2`, so `P0=2 … P14=16`, plus `Unassigned=0`, `Default=1`. The "numbered class" used throughout the QoS path is `class − 2`.

| L1 | Name | L3 encoded byte | Conf |
|---|---|---|---|
| 0 | `Unassigned` | `0` (disengaged) | CERTAIN |
| 1 | `Default` | `0` | CERTAIN |
| 2..16 | `P0 … P14` | `class − 1` (`1..15`) | CERTAIN |

The wire byte is produced by `encodeISAOrRuntimeDMAQoS @0x3128c0`: `class ≤ 1 → 0`; else `getNumberedClassFromDMAQoS(class) + 1 = class − 1`. The byte lands in DMA descriptor `byte[12]` bits `[0:2]`, **only** when `supportsDMAQoSOnISA(arch)` (`arch > 29` = gen3/core_v4/core_v5); on inferentia/sunda no QoS byte is emitted. This is the M4 mismatch family (§ The Four Mismatch Families).

> **GOTCHA —** the encode/decode pair is **asymmetric by design**. `getDMAQoSFromNumberedClass @0x30fc40` is *not* the exact inverse of `getNumberedClassFromDMAQoS @0x30fc20`: it guards `(j − 2) ≤ 14` and returns `j + 2`, so a numbered class in `{0,1}` (P0/P1) is rejected and would FATAL on decode. The binary never round-trips encode→decode on the same value — encode feeds a `DMAQoSClass`, decode feeds a wire-byte−1, and the verifier feeds a class to `getNumberedClassFromDMAQoS` only. Treat the two directions as independent; the canonical mapping is the encode table above.

The descriptor QoS field is 3 bits, so only `P0..P6` (wire `1..7`) fit a single descriptor byte; how `P7..P14` are carried on the core_v5 runtime-raw path (`byte = *(uint*)(inst+0xF0)`, read raw, no width check) is unresolved (gap).

### DMAQueueAttribute — the no-member gap

`DMAQueueAttribute2string @0x25b2f0` is a **FATAL stub**: its entire body throws `"Unknown DMAQueueAttribute"` (`DMAQueueAttributes.cpp:17`); the switch was optimized to the default arm only, with no name string baked in. `string2DMAQueueAttribute @0x25b310` lazily builds an *empty* `unordered_map<string, DMAQueueAttribute>` (bucket_count `1`, size `0`), so any lookup throws `out_of_range`.

> **NOTE —** the enum is *live* — `DMAQueue::{get,set}Attribute` (`@0x257bc0`/`@0x258910`) key a `boost::variant` value map by the 4-byte enum — but its member names are **not byte-recoverable from `libBIR.so` cp310**. The populating TU is not linked into this object. Candidate names (`num_queues`, `num_active_channels`, `sync_type`, `priority_class`) appear only as parser-dumper data-strings, not proven bound to this enum. INT→NAME UNRECOVERABLE from this binary.

---

## Activation and Pool — ActivationFunctionType and PoolFunctionType

### Purpose

`ActivationFunctionType` (**31 members**, `0..30`) is the activation-function selector; all 31 variants route through a single opcode, `InstActivation` (IT `4`), parametrized by a PWP polynomial table. `PoolFunctionType` (2 members) is the pooling-function selector on `InstPool` (IT `20`). Both serialize on BIR-JSON under the key `"func"`. The Activation engine's external name is `Scalar`; Pool's is `GPSIMD`.

### ActivationFunctionType — the 31-member set

`ActivationFunctionType2string @0x4002a0` is a 31-arm switch (`cases 0..30`, the `default` arm being the FATAL `"Unknown ActivationFunctionType"`); `string2ActivationFunctionType @0x40d710` is the matching 31-entry inverse. Verified directly: the switch body has cases `0..30` with case `30 → "Unknown"` an explicit member, and the default arm is the brewer.py FATAL — so the member count is exactly **31**, not open-ended.

| L1 | Name | PWP? | L1 | Name | PWP? |
|---|---|---|---|---|---|
| 0 | `Identity` | H | 16 | `Gelu` | T |
| 1 | `Square` | H | 17 | `Mish` | T |
| 2 | `Relu` | H | 18 | `Gelu_apprx_tanh` | T |
| 3 | `Lrelu` | H | 19 | `Gelu_apprx_sigmoid` | T |
| 4 | `Prelu` | H | 20 | `Derivative_Gelu_apprx_sigmoid` | T |
| 5 | `Sigmoid` | T | 21 | `Derivative_Gelu` | T |
| 6 | `Tanh` | T | 22 | `Derivative_Erf` | T |
| 7 | `Exp` | T | 23 | `Copy` | H |
| 8 | `Softplus` | T | 24 | `Abs` | H |
| 9 | `Sqrt` | T | 25 | `Reciprocal` | T |
| 10 | `Rsqrt` | T | 26 | `Abs_reciprocal_sqrt` | T |
| 11 | `Ln` | T | 27 | `Silu` | T |
| 12 | `Erf` | T | 28 | `Derivative_silu` | T |
| 13 | `Sin` | T | 29 | `Is_finite` | H |
| 14 | `Arctan` | T | 30 | `Unknown` | — |
| 15 | `Sign` | H | | | |

`PWP?` distinguishes the realization: **H** = hardwired elementwise op (`lut_size = 0`, always resident in every activation func-set); **T** = piecewise-polynomial, loaded into the Activation LUT via `InstLoadActFuncSet` (IT `6`) and selected by a per-codename PWP id (a *separate* id space from this enum — e.g. `Silu` is BIR `27` but PWP `neuron_id 36`). The H/T split is from the wheel-side `pwp_jsons` `lut_size` column; the BIR ordinals are CERTAIN from the switch.

> **NOTE — three distinct id spaces.** Do not conflate (a) the BIR `ActivationFunctionType` ordinal (`0..30`, this table), (b) the PWP `neuron_id` the Activation engine dispatches on (the real silicon "func id"; ids ≥ 128 are a reserved high band for `copy`/`reciprocal`/`memset_zero`), and (c) the `act_info.json` per-func-set pass budget (a different number again — e.g. `exp` `act_info=400` but `pwp lut_size=777`). The wire key `"func"` carries the BIR name; the PWP id is baked into the L3 activation bundle, counted under `InstActivation`.

`Exp(7)` and `Reciprocal(25)` are special: each has *both* a value in this enum *and* a dedicated hardwired opcode (`InstExponential` IT `103`, `InstReciprocal` IT `21`). The dedicated ops carry **no** `ActivationFunctionType` field — the func is implicit in the opcode (`InstExponential::toJson @0x43dcf0` emits only `"reduce_cmd"`). The compiler may emit either form for the same silicon function.

### PoolFunctionType — exactly Max and Avg

`PoolFunctionType2string @0x4010e0` builds the SSO string inline (no table): case `0` → `"Max"` (`word 0x614D + byte 0x78`), case `1` → `"Avg"` (`word 0x7641 + byte 0x67`), and FATALs on any third value (no `"Sum"`). `string2PoolFunctionType @0x40e5c0` compares `aMax → 0`, `aAvg → 1`.

| L1 | Name | BIR-op | Conf |
|---|---|---|---|
| 0 | `Max` | `InstPool` (IT `20`), key `"func"` | CERTAIN |
| 1 | `Avg` | `InstPool` (IT `20`), key `"func"` | CERTAIN |

> **GOTCHA —** there is **no Sum or Min pool variant**. `PoolFunctionType` is strictly `{Max, Avg}` — the `PoolFunctionType2string @0x4010e0` body decodes only `0→Max`, `1→Avg` and FATALs on a third ordinal (no `"Sum"`). Sum/Min-style reductions are expressed by *other* opcodes (`InstTensorReduce`, `InstMax`, `InstMaxIndex`, `InstRangeSelect`), not by a pool-function value. Any reimplementation that adds a `Sum` pool mode will diverge from the wire format.

---

## The Remaining-Enum Sweep

### Purpose

To close the 45-symbol census, the metadata/structural enums not covered above are transcribed here in brief. Each is `brewer.py`- or `walrus/ir`-generated, byte-exact from its `2string` body, and serialized as its name (no L3 byte). The four-column entry is L1 ordinals → name set, the carrying op/field, and any correction.

| Enum | `2string @` | L1 values (verbatim) | Carried by / note | Conf |
|---|---|---|---|---|
| `UniqueTensorsType` | `0x401930` | `0:Unknown 1:No 2:Yes` | `unique_tensors` key; tri-state | CERTAIN |
| `AddressRotationScope` | `0x401b00` | `0:None 1:Kernel 2:Global` | SB double-buffer scope; **corrects** the public `0:Kernel/1:Global/2:None` | CERTAIN |
| `KernelSource` | `0x401c10` | `0:Beta1 1:Beta2 2:Beta3` | which kernel-ISA generation a BIRKernel op was built against | CERTAIN |
| `DebugDeviceBuffer` | `0x401d10` | `0:stdout 1:stderr` | `InstDevicePrint` (IT `57`) target stream | CERTAIN |
| `TensorCompletionMode` | `0x402390` | `0:Default 1:DebugTensorRead` | `InstTensorCompletion` (IT `94`) | CERTAIN |
| `InstSyncType` | `0x2d63c0` | `0:datapath 1:sequencer 2:dma` | sync path; **corrects** the public `0:sequencer/1:dma/2:datapath`; default table `byte_785860`, per-instance only on InlineASMBytes (IT `93`) | CERTAIN |
| `EdgeKind` | `0x26aba0` | `0:Invalid 1:Ordered 2:Anti 3:Output 4:Flow` | dep-edge kind (`Flow=RAW`, `Anti=WAR`, `Output=WAW`) | CERTAIN |
| `NamedObjectOrigin` | `0x39ff50` | `0:Internal 1:Penguin 2:NKI` | provenance tag | CERTAIN |
| `ArchRevision` | `0x479860` | `0:v1 1:v2` | minor stepping; pairs with `ArchLevel` | CERTAIN |
| `Argument::ArgumentKind` | `0x233ae0` | sparse `1:PhysicalAccessPattern … 12:RegSet` (`0,4,5,9,10` reserved) | operand classifier; L2 uses 8 *string* spellings, not the C++ name | CERTAIN |
| `InstructionArgumentType` | `0x2e4a80` | `0:Argument 1:IndirectionArgument 2:Output` | operand role; selects 1 of 3 Instruction lists | CERTAIN |
| `FunctionAttribute` | `0x26c2a0` | `0:no_spill … 24:ready_for_codegen` (25 vals) | Function-level pass-progress flags | CERTAIN |
| `ModuleAttribute` | `0x39e3e0` | `0:previous_pass … 22:neff_feature_large_tensor_support` (23 vals) | Module-level NEFF feature gates | CERTAIN |

> **CORRECTION (D-D13) —** three orderings in the public roster were inverted, all proven from the `2string` body: `AddressRotationScope` is `None=0/Kernel=1/Global=2` (not `Kernel=0`); `InstSyncType` is `datapath=0/sequencer=1/dma=2` (not `sequencer=0`); and `RandomAlgorithmKind` is `LFSR=0` (covered above). A consumer that hard-coded the old orderings is mis-decoding the wire.

`FunctionAttribute` and `ModuleAttribute` are large boolean/flag sets reconstructed from SSE-loaded `.rodata` prefixes plus packed tail immediates; the member meaning is in the name (pass-progress bookkeeping and per-NEFF capability gates), and they never reach L3 as a byte. The full 45-symbol completion checklist (every enum → covering source report) is the BIR enum appendix's job; this page owns the op-family subset and the crosswalk.

---

## The Four Mismatch Families

### Purpose

This is the section the rest of the page exists to support. Of the 44 enumerated BIR enums, exactly four reach L3 as a silicon byte that **differs from the L1 ordinal**. Any consumer that hard-codes an L1 ordinal where an L3 byte is expected, or vice-versa, is wrong. Each family is named with a representative reproduced from the binary.

### M1 — InstructionType → setupHeader opcode word

The IT field (`@Inst+0x58`, `0..109`) is the BIR opcode discriminator. At L3, each CoreV* `visitInst<Op>` stamps its *own* ISA opcode word via `setupHeader` — generally not the L1 ordinal. The canonical example, re-verified in `libwalrus` disasm:

```c
// libwalrus  visitInstQuantizeMx  @0x143dc60   — L1 96  -> L3 byte 0xE3 / word 0x10E3
function visitInstQuantizeMx(inst):
    ...
    descriptor.opcode_byte = 0xE3;        // mov byte [rbp-0D0h], 0E3h   @0x143ddd2
    setupHeader(hdr, /*opcode word*/ 0x10E3);  // mov esi, 10E3h          @0x143ddea
    //                       ^ 96 (0x60) != 0xE3 (227)
```

`MatmultMx` (L1 `95`) is worse — a 1→2 expansion: `visitInstMatmultMx` calls `generateLdweightMx` (opcode `0x1009`, `mov esi, 1009h` @`0x143e4cc`) **and** `generateMatmultMx` (opcode `0x100A`). One L1 opcode → two L3 opcodes, neither equal to `95`. The MX class uses the prefix `0x10__`; the low byte (`0x09`/`0x0A`/`0xE3`) is the per-op opcode, so "byte `0xE3`" and "word `0x10E3`" are the same fact viewed two ways.

> **GOTCHA —** only the CoreV4 MX overrides are byte-pinned here; the other ~104 opcodes each stamp their own `setupHeader` word in the matching `visitInst<Op>`, and the full 110→opcode table lives in `libwalrus` (a backend deliverable). The L1 int and L2 name for all 110 are owned by [InstructionType: the 110-Opcode Enum](instruction-type.md). **Never** read an `InstructionType` int as a TPB opcode, or an opcode word as a BIR class id — the L2 name `"type"` is the only stable key.

### M2 — AluOpType → CoreV4 ALU_OP byte (comparison reorder)

`AluOpType` is identity `0x00..0x12` for `bypass..is_equal`, then the comparison family is **renumbered** on the CoreV4 wire so an operand swap maps `gt↔lt`/`ge↔le` onto adjacent codes without re-encoding. The converter is `libwalrus sub_142E030` (jump table base `0x1dfb5e8`); the reorder is re-verified this pass:

```text
not_equal  19 -> 0x18      (mov eax,18h @0x142e110)
is_gt      20 -> 0x13      (mov eax,13h @0x142e100)
is_ge      21 -> 0x14
is_lt      22 -> 0x16      (mov eax,16h @0x142e0e0)
is_le      23 -> 0x15
mod        27 -> 0x1B      (mov eax,1Bh @0x142e0b0)
rsqrt      28 -> 0x1D      (mov eax,1Dh @0x142e0a0)
abs        29 -> 0x19      (mov eax,19h @0x142e090)
mod_int    32 -> 0xC8      (int32 extended band 0xC4..0xE1)
average    24 -> ERR       (dedicated 1/N op — no generic ALU byte)
elemwise_mul 25 -> ERR     (dedicated multiply op)
```

The detailed switch and the full 33-row L1→L3 table are owned by the sibling [AluOpType and the BIR Mode Enums](aluop-modes.md). The structural point for the crosswalk: comparison predicates are antisymmetric, so the wire groups them so the swapped form is one code away; and two ordinals (`average`, `elemwise_mul`) fall to a `reportError` default because silicon handles them with dedicated opcodes, not the generic ALU path.

### M3 — Dtype → NEURON_ISA_TPB_DTYPE wire-tag

The L3 dtype wire-tag (`byte_1DFBAD0`, read by CoreV4 `sub_14347C0`) is a **full remap** ordered by container-*size* class, so almost no entry equals its L1 ordinal: `uint8 0→3`, `int8 1→2`, `bfloat16 12→6`, `float16 13→7`, `int32 15→8`, `uint32 14→9`, `float32 16→10`, `uint64 18→1`, `int64 19→12`. The fp8 family `3/4/5/8` collapse onto tags `13/14/14/14` (e4m3fn_x4 shares e4m3fn's tag — the `×4`-ness rides on the access-pattern, not the tag). The 20-byte LUT and the stride/align tables are owned by [Dtype Tables & Wire-Tag LUTs](dtype-tables.md); the crosswalk fact is: **every `Dtype` ordinal differs from its wire-tag**.

### M4 — DMAQoSClass → encoded descriptor byte

`DMAQoSClass` → `class − 1` for `P0..P14`; `0` for `Unassigned`/`Default` (`encodeISAOrRuntimeDMAQoS @0x3128c0`, into descriptor `byte[12]` bits `[0:2]`). So `Default 1→0`, `P0 2→1`, … `P14 16→15`. On core_v5 the runtime-raw path reads the byte directly from `inst+0xF0`, still not the L1 ordinal. (Full encoder above, § DMA.)

### The L2-numeric exception — MemoryType

Not a mismatch family, but the one enum whose **L2 itself is a number, not a name**: `MemoryLocation::toJson` writes the `"type"` key as the bit-flag *integer* (`Unallocated=1`, `Input=2`, `Output=4`, `DRAM=8`, `SB=16`, `PSUM=32`, `RNGSTATE=64`, `REG=128`), via the numeric adl_serializer. Everywhere else `"type"` would be a name; here it is a power-of-two flag. `MemoryType` has no L3 silicon byte (it gates allocation/addressing).

---

## Master Crosswalk

### The table

The columns are: **L1** (libBIR ordinal); **L2** (BIR-JSON form — a *name* unless marked `num`); **L3** (silicon byte, `—` if absent, `codepath` if it selects a `libwalrus` emitter branch with no standalone byte); **Δ** (`=` L1==L3, `✗` L1≠L3, `·` no L3, `name`/`num` for the L2 form). Only the four ✗ families and `MemoryType`-`num` carry a non-name wire surprise.

| Enum | `2string @` | members (L1:name) | L2 / L3 | Δ |
|---|---|---|---|---|
| `InstructionType` | `0x2d5bf0` | 110 vals `0..109` | name / per-op `setupHeader` opcode word | ✗ (M1) |
| `AluOpType` | `0x400600` | 33 vals `0..32` | name / CoreV4 `ALU_OP` byte | ✗ (M2) |
| `Dtype` | `0x2641e0` | 20 vals `0..19` | name / `NEURON_ISA_TPB_DTYPE` tag | ✗ (M3) |
| `DMAQoSClass` | `0x400ed0` | 17 vals (`Unassigned,Default,P0..P14`) | name / `class−1` descriptor byte | ✗ (M4) |
| `MemoryType` | `0x3ca040` | 8 bit-flags `1..128` | **num** / — | num |
| `MatmultPerfMode` | `0x400be0` | `0:None…4:DoubleRowSwInterleave` | name / codepath (PE axis fold) | · |
| `BranchCompareOp` | `0x401ff0` | `0:IS_LTIMM…12:Unsupported` (13) | name (`comp_op`) / — | · |
| `BranchOutcomeHint` | `0x4022a0` | `0:LikelyTaken 1:LikelyNotTaken` | name (`hint`) / — | · |
| `CollectiveKind` | `0x4016c0` | `0:SendRecv…10:PermuteReduceImplicit` (11) | name (`cc_channel.kind`) / IT48 opcode word | · |
| `CollectiveDimension` | `0x401a30` | `0:Partition 1:Free` | name (`cc_dim`) / — | · |
| `CollectiveComputeTypeHint` | `0x401840` | `0:TP 1:FSDP 2:None` | name (`cc_type_hint`) / — | · |
| `RandomAlgorithmKind` | `0x401de0` | `0:LFSR 1:PCG32 2:PHILOX_1` | name (`random_algorithm`) / — | · |
| `RandomDistributionKind` | `0x401ee0` | `0:Raw 1:Uniform 2:Normal 3:Binomial` | name (`distribution`) / — | · |
| `EventSemaphoreClearMode` | `0x400ac0` | `0:SemaphoreZero 1:SemaphoreZeroBitmask` | name (`mode`, on IT14) / — | · |
| `DGEType` | `0x400dc0` | `0:None 1:SWDGE 2:HWDGE 3:Unassigned` | name / codepath (descriptor-gen engine) | · |
| `ActivationFunctionType` | `0x4002a0` | `0:Identity…30:Unknown` (31) | name (`func`) / PWP id in IT4 bundle | · |
| `PoolFunctionType` | `0x4010e0` | `0:Max 1:Avg` | name (`func`) / — | · |
| `InstSyncType` | `0x2d63c0` | `0:datapath 1:sequencer 2:dma` | name (`sync_type`) / default table | · |
| `EdgeKind` | `0x26aba0` | `0:Invalid 1:Ordered 2:Anti 3:Output 4:Flow` | name / — | · |
| `DMAQueueAttribute` | `0x25b2f0` | (no static members) | — / — | gap |

(Enums fully owned by sibling pages — `EngineType`, `ArchLevel`, the mode enums, `TensorClass`/`TensorKind`, `AxisListType`, etc. — are omitted here; the [AluOpType page](aluop-modes.md) and the BIR enum appendix carry them. The names/ordinals above are CERTAIN from the cited `2string` bodies and their `string2` inverses.)

### Reading the table

- A `·` row never reaches silicon as a standalone byte: it either rides inside an opcode (counted under `InstructionType`, e.g. `CollectiveKind`/`ActivationFunctionType`), or selects an emitter codepath (`DGEType`/`MatmultPerfMode`), or is consumed only by passes/the verifier.
- A `✗` row is a do-not-confuse case (the four families above).
- The one `num` row (`MemoryType`) is the only place the BIR-JSON `"type"` key is a number, not a name.
- The single `gap` row (`DMAQueueAttribute`) has live machinery but no statically recoverable names.

---

## Adversarial Self-Verification

Five strongest claims, re-checked against the cp310 binaries this pass:

| Claim | Method | Result |
|---|---|---|
| `ActivationFunctionType` = 31 members | read `2string @0x4002a0` decompiled body | cases `0..30` present, case `30="Unknown"` an explicit member, `default` = brewer.py FATAL — **31, CERTAIN** |
| `CollectiveKind` = 11 members | `xxd` of `.rodata 0x70bc94` (VA==fileoff) | byte-exact pool `SendRecv … PermuteReduceImplicit` + `"Unknown CollectiveKind"` — **11, CERTAIN** |
| M1 `QuantizeMx` 96→`0xE3` | `libwalrus` disasm `visitInstQuantizeMx @0x143dc60` | `mov byte [rbp-0D0h], 0E3h` @`0x143ddd2` + `mov esi, 10E3h` @`0x143ddea` — **CERTAIN** |
| M2 AluOp comparison reorder | `libwalrus` disasm `sub_142E030` | case 29 abs→`0x19`, 28 rsqrt→`0x1D`, 27 mod→`0x1B`, 22 is_lt→`0x16`, 20 is_gt→`0x13`, 19 not_equal→`0x18` — **CERTAIN** |
| md5 + VA==fileoff | `md5sum` + `readelf -S` | `libBIR` `12bb979f…`, `libwalrus` `1d93972b…`; `.text @0x1820c0`, `.rodata @0x708000` addr==offset — **CERTAIN** |

Re-verification ceiling (honest gaps):

- **M3/M4 not re-disassembled this pass.** The `Dtype` wire-tag LUT and the `DMAQoSClass` `class−1` encoder are taken from the sibling pages' verified derivations ([dtype-tables](dtype-tables.md), [aluop-modes](aluop-modes.md)) and D-D09/D-D14, not re-walked here. CERTAIN at the source, cited not re-proven.
- **The full 110→opcode L3 table is not claimed.** Only the CoreV4 MX overrides (`QuantizeMx`, `MatmultMx`) are byte-pinned; the remaining ~104 `setupHeader` words are a `libwalrus` backend deliverable.
- **Per-kind/per-engine legality is not in `libBIR`.** The "required reduce-op" column for collectives, the per-engine legal-activation set, and the per-target QoS budget are all enforced in the `libwalrus` `birverifier`; `libBIR` holds the data, not the gate.
- **`DMAQueueAttribute` names are unrecoverable** from this build (FATAL-stub `2string` + empty `string2` map). The enum is live but its members are not statically present.
- **No NEFF/BIR-JSON fixture byte-diff** was performed; the L2 name↔L1 int binding is proven from the (de)serializer bodies and the L3 bytes from emitter constants — an end-to-end hexdiff would upgrade the L2→L3 join from "emitter-constant CERTAIN" to "wire-observed".

---

## Related Components

| Component | Relationship |
|---|---|
| `InstructionType` (IT enum) | the M1 family; the top-level opcode discriminator that every op-family enum hangs off |
| `isa/pe-matmul-encoding` | the L3 silicon encoding for MX/Quantize ops — where `QuantizeMx`'s `0xE3`/`0x10E3` opcode word is stamped |
| `AluOpType` + mode enums | the M2 family; owns the CoreV4 comparison-reorder switch in detail |
| `libwalrus` CoreV* emitters | produce every L3 byte; `setupHeader` opcode words, `ALU_OP`, dtype tag, QoS descriptor byte |
| `birverifier` (libwalrus) | per-kind/per-engine/per-QoS legality gates; the data lives in BIR, the gate lives here |

## Cross-References

- [InstructionType: the 110-Opcode Enum & sameInst Family Masks](instruction-type.md) — the M1 mismatch family; carries the L1 int + L2 name for all 110 opcodes
- [AluOpType and the BIR Mode Enums](aluop-modes.md) — the M2 family; the full 33-row CoreV4 `ALU_OP` map and the comparison-reorder switch
- [Dtype Tables & Wire-Tag / Stride / Alignment LUTs](dtype-tables.md) — the M3 family; the `NEURON_ISA_TPB_DTYPE` 20-entry remap
- [The bir::Instruction Base Struct & the +0xD0 Sched/Dep Block](instruction-base.md) — the `Inst+0x58` IT field and the per-op struct frames the enums deserialize into
- [Argument / AccessPattern / Immediate / Register Value Model](value-model.md) — `Argument::ArgumentKind` / `InstructionArgumentType`, the operand-model enums in the sweep
- [Activation Encoding](../isa/activation-encoding.md) — the L3 encoding for `InstActivation`; the PWP id space behind the 31-member `ActivationFunctionType`
- [PE Matmul Encoding — Dense / Sparse / MX & Quantize](../isa/pe-matmul-encoding.md) — the L3 `setupHeader` opcode word format; where `QuantizeMx` 96→`0xE3` and `MatmultMx` 95→`0x1009`/`0x100A` are stamped
