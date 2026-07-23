# SBUF / PSUM Bank Geometry

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The geometry constants live in `neuronxcc/starfish/lib/libwalrus.so`; the engine handle is in `libBIR.so`. For `.text`/`.rodata` the virtual address equals the file offset. Other wheels differ — treat every address as version-pinned.*

## Abstract

Every on-chip address the backend computes — every SBUF partition offset, every PSUM bank index — is bounded by a handful of compile-time constants baked into `libwalrus.so`. They describe two physically distinct memories: **SBUF** (the State Buffer, the 128-partition scratchpad the engines read and write) and **PSUM** (the Partial-SUM accumulator, a small array of fixed 2 KiB banks the PE array drains into). This page recovers those constants for all four hardware generations, the struct that holds each one, and the accessor chain the allocators walk to read them. The bar is that a reimplementer can reconstruct the SBUF and PSUM address spaces — partition count, per-partition byte budget, bank count, bank size — for Inferentia, Trainium (gen2), gen3, and gen4.

The geometry is **not** carried on the BIR `bir::EngineInfo` object. That object is an 8-byte `{EngineType, id}` handle with no size fields ([1.01](arch-object-model.md)); the numbers live one indirection deeper, in a static singleton object tree `Board → Device → Core` built once at library load. The `Core` holds six engine sub-objects, and two of them — `Statebuf` (SBUF) and `Psumbuf` (PSUM) — are pure geometry records. An allocator reads a field by calling `getArchModel(codename)` and walking `Board+0x8 → Device+0x10 → Core+0x{20,28} → field`. This page documents that walk, the per-generation immediates the per-arch constructors stamp into those records, and the one structural surprise: **Inferentia is a half-width part** — 64 in the engines' second dimension where gen2+ are uniformly 128.

The numbers in this build are corroborated two ways. The constructor immediates are read directly off the `<Arch>Statebuf` / `<Arch>Psumbuf` ctor bodies (`InferentiaPsumbuf`, `SundaStatebuf`, …), and the field **names** come from a real C++ header shipped in the wheel, `data/include/hwm/ctm/ctm.hpp`, which declares `class Statebuf`, `class Psumbuf`, `class Core` with their member layout and doc comments. The two sources agree field-for-field, so every number here rests on both an observed immediate and a named declaration.

For reimplementation, the contract is:

- The `Board → Device → Core` singleton tree and the two accessor offsets (`Core+0x20` = `Psumbuf*`, `Core+0x28` = `Statebuf*`) that key every geometry read.
- The `Statebuf` and `Psumbuf` record layouts and the four per-generation immediate sets that fill them.
- Partition-axis vs free-axis addressing: why SBUF is a 2-D (partition × byte) space and PSUM is a 1-D (bank) space, and what an address means in each.
- The half-width Inferentia case, so a reimplementer does not hard-code 128 into the engine second dimension.

| | |
|---|---|
| **Geometry tree root** | `getArchModel(std::string const&)` @ `0x17344c0` |
| **Accessor walk** | `getArchModel → [Board+0x8]=Device → [Device+0x10]=Core → Core+0x{20,28}` |
| **SBUF record** | `Statebuf` — ctor `0x17341e0`; per-arch `Inferentia/Sunda/Cayman/CoreV4Statebuf` @ `0x17346d0/0x1734ac0/0x1734eb0/0x17352b0` |
| **PSUM record** | `Psumbuf` — ctor `0x17341b0`; per-arch `…Psumbuf` @ `0x17346b0/0x1734aa0/0x1734e90/0x1735290` |
| **SB_SIZE / partition** | 96 / 192 / 224 / 256 KiB (gen1/2/3/4); 128 partitions all gens |
| **PSUM banks × size** | 4 / 8 / 8 / 8 banks × 2048 B; 64/128/128/128 partitions |
| **Field-name source** | `data/include/hwm/ctm/ctm.hpp` (shipped header — the source of the field names) |
| **SBUF read-chain** | `SB_Allocator` ctor @ `0xa97b82`–`0xa97bbd` (→ allocator+0x2b8) |
| **PSUM read-chain** | `PSUM_Allocator` ctor @ `0xada120`–`0xada13b` (→ allocator+0x0) |

---

## The geometry object tree

### Purpose

The hardware model is a static singleton tree, constructed once when `libwalrus.so` loads and shared by every pass that needs a hardware constant. A consumer never instantiates it — it asks `getArchModel` for the `Board` belonging to a codename and walks down to the field it wants. The tree exists so that the dozens of allocators, schedulers, and legalizers that depend on geometry all read **one** authoritative copy keyed by the compile target, rather than each carrying its own constants.

### Entry Point

```text
getArchModel(codename)           @0x17344c0   ── string-compare dispatch over 10 codenames → Board*
  └─ returns one of 4 static Board singletons (one per generation)
       Board   @ +0x08 → Device*
       Device  @ +0x10 → Core*
       Core    @ +0x20 → Psumbuf*   (PSUM geometry)
       Core    @ +0x28 → Statebuf*  (SBUF geometry)
       Core    @ +0x00..0x18 → Pe*, Pool*, Act*, Dve*  (compute-engine geometry)
       Core    @ +0x30 → dramSizeGb (HBM window — see 1.06)
```

The four `Board` objects are selected by codename. `getArchModel` clusters ten device aliases onto four generations; `core_v5` (gen5, ArchLevel 50) has no `Board` and aborts with `assert("0 && Unknown architecture")` at `ctm.cpp:0x5F` — gen5 is a forward stub in this build. The clustering appears on both binaries (the `libBIR` side via [`getArchModel` @0x478f90](arch-object-model.md), the `libwalrus` side here):

> **GOTCHA — there are ten aliases, and `core_v5` is not one of them.** The compare chain has exactly ten arms: `tonga`/`inf1`/`inferentia` (3) + `sunda`/`trainium`/`trn1`/`inf2` (4) + `cayman`/`gen3` (2) + `core_v4` (1). The `core_v5` literal sits *past* the `__assert_fail` string in `.rodata`, so grepping the region turns it up without it ever being compared.

| Generation | ArchLevel | `<Arch>Core` ctor | Codename aliases |
|---|---|---|---|
| gen1 Inferentia | 10 | `InferentiaCore` @ `0x1734720` | `tonga`, `inf1`, `inferentia` |
| gen2 Sunda/Trainium | 20 | `SundaCore` @ `0x1734b10` | `sunda`, `trainium`, `trn1`, `inf2` |
| gen3 Cayman | 30 | `CaymanCore` @ `0x1734f00` | `cayman`, `gen3` |
| gen4 CoreV4 | 40 | `CoreV4Core` @ `0x1735300` | `core_v4` |

### Algorithm

The walk is the same for every geometry field; only the terminal offset differs. This is the SBUF-size read, lifted verbatim from the loop-aware `SB_Allocator` constructor (the PSUM read is identical but ends at `Core+0x20`, `Psumbuf+0`):

```c
// neuronxcc::backend::ColoringAllocatorWithLoop::Rep::SB_Allocator::SB_Allocator @0xa97750
function read_SB_SIZE(module, allocator):
    codename = module.getArch()                       // 0xa97b7a → ArchLevel2string
    board    = getArchModel(codename)                 // 0xa97b82, rbx = Board*
    device   = *(Device**)(board  + 0x08)             // 0xa97baa, Board+0x8
    core     = *(Core**)  (device + 0x10)             // 0xa97bb3, Device+0x10
    statebuf = *(Statebuf**)(core + 0x28)             // 0xa97bb7, Core+0x28
    sb_size  = *(uint32*)(statebuf + 0x00)            // 0xa97bbb, Statebuf+0 = partitionSize
    allocator[0x2b8] = sb_size                        // 0xa97bbd, cached at allocator+696
    // 0xa97bc3: a follow-on `cmp $0xa,%r14d` (ArchLevel 10 = Inferentia) forks the
    //           gen1 byte budget; the stored value itself is the Statebuf+0 immediate.
```

> **NOTE —** the cache slot `allocator+0x2b8` (= 696 decimal) is the field earlier reports called "a1+696". It holds `SB_SIZE` **bytes** (`Statebuf+0`), not a partition count; the 128-partition figure is the separate `Statebuf+0x8` field. A reimplementer must keep the two distinct — they are two fields of one record, never one overloaded slot.

### Considerations

The geometry is a **baked compile-time constant per codename**, not device-config or runtime-probe driven. The compiler decides the SBUF/PSUM budget purely from the target string. The class family is defined once by the shipped `ctm` hardware-model library (`ctm.hpp` header + `ctm.cpython-3xx.so`); the per-arch constructors that hold the immediates are compiled into **both** `libwalrus.so` and `libBIR.so`, which carry the geometry tree **byte-identically** — a cross-binary consistency check (two compilations of one `ctm` source agree), not an independent derivation ([1.01](arch-object-model.md) makes that explicit). `libBIR` additionally exposes the engine-*multiplicity* counts (`getEngineCount`), a different facet. The genuinely independent corroborator of specific *values* — the 2048-byte bank size, the clock frequencies — is the cost model (`bir::Hwm`, e.g. `getPsumBankSize`), a separate code path documented in [1.04](hardware-constant-matrix.md). The geometry on this page is read from the `libwalrus` copy.

---

## The Statebuf record — SBUF geometry

### Purpose

`Statebuf` describes the State Buffer: the engines' primary on-chip scratchpad, organized as 128 independent **partitions**, each a flat per-partition byte budget. It is a 2-D address space — `(partition, byte-offset)` — and the SBUF allocator (`SB_Allocator`, [Part 8](../walrus/)) colors tensors into partition × byte rectangles. The record holds the two numbers that bound that rectangle (per-partition size, partition count) plus alignment and a reserved-region size.

### Record layout

The base constructor `Statebuf::Statebuf` (@`0x17341e0`) takes six arguments. The names come from `ctm.hpp:129`'s `class Statebuf` declaration; the offsets are read off the constructor's store sequence:

| Field | Offset | Type | ctm.hpp name | Meaning |
|---|---|---|---|---|
| partitionSize | `+0x00` | `uint32` | `partitionSize` | **SB_SIZE** — per-partition byte budget (the allocator field) |
| SOC step | `+0x04` | `uint32` | `PartitionSOCStepSize` | addressable/aligned partition span (the 2nd size field) |
| numPartitions | `+0x08` | `uint32` | `numPartitions` | SBUF partition count (= 128 all gens) |
| midPartition | `+0x0C` | `uint32` | `midPartition` | partition-quadrant midpoint (= 64 all gens) |
| align | `+0x10` | `Align*` | `align` | alignment descriptor (correct / basicPerf / highPerf triple) |
| reservedSize | `+0x18` | `uint32` | `reservedSize` | reserved-region size (default 16384; gen-specific 0x4000/0x4008) |

Two of these are easy to mistake for allocation granules. `+0xC` is `midPartition` — the partition-quadrant midpoint, `0x40` (=64) on every generation — not a sub-bank size. `+0x18` is `reservedSize`, the `0x4000`/`0x4008` value pushed by each subclass ctor, not a partition-alignment quantum; `ctm.hpp` gives it a default of 16384.

### Per-generation immediates

Each `<Arch>Statebuf` constructor sets up the six arguments inline and tail-calls the base `Statebuf` ctor. The immediates below were read off the ctor bodies and independently re-disassembled; **all four sets match byte-for-byte**:

| Arch | SB_SIZE (`esi`, `+0x00`) | hex | SOC step (`edx`, `+0x04`) | partitions (`ecx`, `+0x08`) | midPartition (`r8d`, `+0x0C`) | reservedSize (push, `+0x18`) | Confidence |
|---|---|---|---|---|---|---|---|
| gen1 Inferentia | 96 KiB | `0x18000` | `0x20000` | `0x80` (128) | `0x40` (64) | `0x4000` | CERTAIN |
| gen2 Sunda | 192 KiB | `0x30000` | `0x40000` | `0x80` (128) | `0x40` (64) | `0x4000` | CERTAIN |
| gen3 Cayman | 224 KiB | `0x38000` | `0x40000` | `0x80` (128) | `0x40` (64) | `0x4008` | CERTAIN |
| gen4 CoreV4 | 256 KiB | `0x40000` | `0x40000` | `0x80` (128) | `0x40` (64) | `0x4008` | CERTAIN |

The total SBUF capacity per generation is `SB_SIZE × 128 partitions`: 12 MiB (gen1), 24 MiB (gen2), 28 MiB (gen3), 32 MiB (gen4). A reimplementer rarely needs the total — the allocator's budget is **per partition**, because every engine accesses all 128 partitions in lockstep and a tensor's footprint is bounded by its largest per-partition slice.

> **QUIRK —** `numPartitions = 128` is constant across *all four* generations, including Inferentia. The half-width nature of Inferentia (next section) does **not** touch SBUF — its 128 partitions are full-width. The "64" on Inferentia appears only in the compute engines and PSUM. A reimplementer who assumes "Inferentia is 64 everywhere" will mis-size SBUF by 2×.

### Considerations

The function map below ties the SBUF symbols to addresses. The `<Arch>Statebuf` ctors are full bodies (~0x4d bytes), not thunks — they construct the `Align` descriptor inline and reach the base ctor via the PLT stub `_ZN8StatebufC2EjjjjR5Align_j@plt` (`0x5f3870`). The in-binary `Statebuf::Statebuf` @`0x17341e0` is the inlinable copy.

| Function | Address | Role | Confidence |
|---|---|---|---|
| `Statebuf::Statebuf(uint,uint,uint,uint,Align&,uint)` | `0x17341e0` | base ctor — stores the six fields | CERTAIN |
| `InferentiaStatebuf::InferentiaStatebuf` | `0x17346d0` | gen1 immediates | CERTAIN |
| `SundaStatebuf::SundaStatebuf` | `0x1734ac0` | gen2 immediates | CERTAIN |
| `CaymanStatebuf::CaymanStatebuf` | `0x1734eb0` | gen3 immediates | CERTAIN |
| `CoreV4Statebuf::CoreV4Statebuf` | `0x17352b0` | gen4 immediates | CERTAIN |
| `SB_Allocator::SB_Allocator` (with-loop) | `0xa97750` | reads `Statebuf+0` → allocator+0x2b8 | CERTAIN |

---

## The Psumbuf record — PSUM geometry

### Purpose

`Psumbuf` describes the Partial-SUM buffer: the matmul accumulator space the PE systolic array drains into (`MemoryType::PSUM` = 32, [1.01](arch-object-model.md)). Physically it is a small array of fixed-size **banks**, each 2 KiB. Unlike SBUF, PSUM has no addressable byte axis the allocator searches — a tensor occupies a whole number of banks, and the PSUM allocator is a **1-D bank-interval** colorer ([Part 8](../walrus/)). The record holds the bank count, the partition count, and the bank size.

### Record layout

The base constructor `Psumbuf::Psumbuf` (@`0x17341b0`) takes three arguments. Names come from `ctm.hpp:111`'s `class Psumbuf`:

| Field | Offset | Type | ctm.hpp name | Meaning |
|---|---|---|---|---|
| numBanks | `+0x00` | `uint32` | `numBanks` | **NUM_PSUM_BANKS** — bank count (the allocator field) |
| numPartitions | `+0x04` | `uint32` | `numPartitions` | PSUM partition count |
| partSize | `+0x08` | `uint32` | `partSize` | bank size in bytes (= 2048 all gens) |
| bufLen64 | `+0x0C` | `uint32` | `bufLen64` | `partSize >> 3` = 256 — element count, FP64 stride (deprecated) |
| bufLen32 | `+0x10` | `uint32` | `bufLen32` | `partSize >> 2` = 512 — element count, FP32 stride (deprecated) |

`bufLen64`/`bufLen32` are derived element-count helpers the header marks "unnecessary - deprecate these"; the allocator uses only `numBanks` and `partSize`.

### Per-generation immediates

Each `<Arch>Psumbuf` ctor is a **5-instruction thunk**: load the three args, tail-`jmp` to the base ctor via PLT (`_ZN7PsumbufC2Ejjj@plt`, `0x5ff360`). Immediates re-disassembled and independently confirmed:

| Arch | banks (`esi`, `+0x00`) | partitions (`edx`, `+0x04`) | bank size (`ecx`, `+0x08`) | per-partition PSUM window | Confidence |
|---|---|---|---|---|---|
| gen1 Inferentia | `0x04` (4) | `0x40` (64) | `0x800` (2048) | 8 KiB | CERTAIN |
| gen2 Sunda | `0x08` (8) | `0x80` (128) | `0x800` (2048) | 16 KiB | CERTAIN |
| gen3 Cayman | `0x08` (8) | `0x80` (128) | `0x800` (2048) | 16 KiB | CERTAIN |
| gen4 CoreV4 | `0x08` (8) | `0x80` (128) | `0x800` (2048) | 16 KiB | CERTAIN |

> **NOTE — where the bank count actually lives.** The PSUM allocator caches `numBanks` at its `this+0` from a `getArchModel` walk, so the colorer never holds the integer as a literal and reading the allocator alone leaves the value implicit (recoverable only by dividing the 16 KiB PSUM window). The literal is in the four `<Arch>Psumbuf` ctor bodies quoted above: `numBanks = {4, 8, 8, 8}` for gen1/2/3/4. Inferentia is the only 4-bank part; gen2+ are uniformly 8.

The bank **size** 2048 is corroborated three independent ways: (1) the `Psumbuf+0x08` immediate above; (2) three per-arch getter bodies that each compile to a literal return —

```asm
; TrainiumHwm::getPsumBankSize @0x1851900   (Gen3Hwm @0x1857cf0, CoreV4Hwm @0x185e570 identical)
1851900:  b8 00 08 00 00   mov    $0x800,%eax        ; 2048
1851905:  c3               ret
```

— and (3) `getPsumBankNum` (@`0x1088bd0`) computing a tensor's bank footprint as `ceil(tensorBytes / getPsumBankSize())`, i.e. `ceil(bytes / 2048)`.

### The PSUM read-chain

The PSUM allocator's constructor reads `numBanks` with the same `getArchModel` walk as SBUF, ending one offset earlier (`Core+0x20` = `Psumbuf*` rather than `Core+0x28` = `Statebuf*`):

```c
// neuronxcc::backend::ColoringAllocatorWithLoop::Rep::PSUM_Allocator::PSUM_Allocator @0xad9970
function read_NUM_PSUM_BANKS(module, allocator):
    board    = getArchModel(module.getArch())         // 0xada120
    device   = *(Device**)(board  + 0x08)             // 0xada125, Board+0x8
    core     = *(Core**)  (device + 0x10)             // 0xada131, Device+0x10
    psumbuf  = *(Psumbuf**)(core  + 0x20)             // 0xada135, Core+0x20
    allocator[0x00] = *(uint32*)(psumbuf + 0x00)      // 0xada139→0xada13b, Psumbuf+0 = numBanks
```

The colorer then guards every tensor: if `numBanks < tensor.liveBanks`, it aborts with `"<name> is too big for PSUM, requires <N> banks with PSUM size <M>"` ([Part 8](../walrus/)). That `M` is the cached `numBanks`, so the constant on this page is exactly the budget a reimplementer's PSUM allocator must enforce.

### Considerations

| Function | Address | Role | Confidence |
|---|---|---|---|
| `Psumbuf::Psumbuf(uint,uint,uint)` | `0x17341b0` | base ctor — stores the three fields | CERTAIN |
| `InferentiaPsumbuf::InferentiaPsumbuf` | `0x17346b0` | gen1 immediates (4 banks, 64 parts) | CERTAIN |
| `SundaPsumbuf::SundaPsumbuf` | `0x1734aa0` | gen2 immediates (8 banks, 128 parts) | CERTAIN |
| `CaymanPsumbuf::CaymanPsumbuf` | `0x1734e90` | gen3 immediates (8 banks, 128 parts) | CERTAIN |
| `CoreV4Psumbuf::CoreV4Psumbuf` | `0x1735290` | gen4 immediates (8 banks, 128 parts) | CERTAIN |
| `TrainiumHwm::getPsumBankSize` | `0x1851900` | returns 2048 | CERTAIN |
| `Gen3Hwm::getPsumBankSize` | `0x1857cf0` | returns 2048 | CERTAIN |
| `CoreV4Hwm::getPsumBankSize` | `0x185e570` | returns 2048 | CERTAIN |
| `getPsumBankNum(MemoryLocation*)` | `0x1088bd0` | `ceil(bytes / 2048)` | CERTAIN |
| `PSUM_Allocator::PSUM_Allocator` (with-loop) | `0xad9970` | reads `Psumbuf+0` → allocator+0x0 | CERTAIN |

---

## Partition-axis vs free-axis addressing

A reimplementer reasoning about on-chip addresses needs the shape of each address space, not the full allocator. The two memories are shaped differently, and the difference is the single most useful thing on this page.

```text
  SBUF — 2-D address space  (partition, byte)              PSUM — 1-D address space  (bank)
  ┌───────────────────────────────────────────┐           ┌─────┬─────┬─────┬─────┬───┐
  │ part 0  │ 0 .................... SB_SIZE-1 │           │bank0│bank1│bank2│ ... │ N │   N = numBanks-1
  │ part 1  │ 0 .................... SB_SIZE-1 │           └─────┴─────┴─────┴─────┴───┘
  │   ...   │ ...                              │            each bank = partSize (2048) bytes,
  │ part 127│ 0 .................... SB_SIZE-1 │            spanning ALL numPartitions at once
  └───────────────────────────────────────────┘
  free axis = byte offset within a partition                free axis = bank index
  partition axis = which of 128 partitions                  partition axis = implicit (a bank is full-width)
```

**SBUF — partition axis × free axis.** An SBUF address is a pair: a *partition* (0…`numPartitions`-1) on one axis and a *byte offset* (0…`SB_SIZE`-1) within that partition on the **free axis**. The engines operate partition-parallel — one instruction touches a strip across many partitions at the same free offset — so the allocator searches a 2-D rectangle: how many partitions tall (the partition dimension of the tensor) by how many bytes wide. The per-partition budget `SB_SIZE` bounds the free axis; `numPartitions` (128) bounds the partition axis. A reimplementer's SBUF allocator must place a tensor as a `partitions × bytes` rectangle inside the `128 × SB_SIZE` grid.

**PSUM — bank axis only.** A PSUM address is a single **bank index** (0…`numBanks`-1). There is no byte axis the allocator searches: a bank is an indivisible 2048-byte unit that already spans all `numPartitions` partitions. A tensor occupies `ceil(bytes / 2048)` consecutive banks (`getPsumBankNum`), and "where it lives" is just its base bank. The partition axis is *implicit* — every bank is full-width — which is why the PSUM colorer is 1-D where the SBUF colorer is 2-D. A reimplementer's PSUM allocator is a bank-interval first-fit over `numBanks` slots, nothing more.

> **GOTCHA — `numPartitions` differs between the two records on Inferentia.** `Statebuf.numPartitions` is 128 on every generation, but `Psumbuf.numPartitions` is **64** on Inferentia (128 on gen2+). They are not the same field and not the same number on gen1. The PSUM partition count matters for how many PE columns a bank serves, not for the bank-index arithmetic above — but a reimplementer copying "128 partitions" across both records will be wrong on Inferentia's PSUM.

---

## The half-width Inferentia case

Across the four generations, gen2/3/4 are uniformly 128 in every "second dimension", but **Inferentia (gen1) is half-width — 64** in the compute-engine and PSUM second dimension while keeping a full-width 128-partition SBUF. This is the one place a reimplementer cannot assume 128. The pattern, read off the per-arch compute-engine and PSUM ctors:

| Geometry dimension | Record field | gen1 Inferentia | gen2/3/4 | Confidence |
|---|---|---|---|---|
| SBUF partitions | `Statebuf.numPartitions` | **128** | 128 | CERTAIN |
| PE systolic rows | `Pe.numRows` | **128** | 128 | CERTAIN |
| PE systolic columns | `Pe.numCols` | **64** | 128 | CERTAIN |
| PSUM partitions | `Psumbuf.numPartitions` | **64** | 128 | CERTAIN |
| PSUM banks | `Psumbuf.numBanks` | **4** | 8 | CERTAIN |
| Pool / Act channels | `Pool.numChannels` / `Act.numChannels` | **64** | 128 | CERTAIN |

The reading is that Inferentia's systolic array is a 128×64 PE grid (half the columns of gen2+'s 128×128), its PSUM is half the banks and half the partitions, and its pooling/activation engines process 64 channels. Only the SBUF stays full-width at 128 partitions. Every other generation is the uniform 128 a naive reimplementation would assume.

> **GOTCHA — the "128 partitions" constant is not an `EngineInfo` field.** `bir::EngineInfo` is an 8-byte `{EngineType, id}` handle carrying no geometry at all, so there is nothing at `EngineInfo+100` to read. The 128 lives in the `Core` sub-object geometry fields (`Statebuf+0x8`, `Psumbuf+0x4`, `Pe+0x4`, …), several of which are 64 rather than 128 on Inferentia. Geometry is keyed by `EngineType` *through the Core sub-object table*, never by an `EngineInfo`-indexed array.

---

## At-a-glance: the full geometry matrix

The consolidated per-generation table. Every cell is read from a constructor immediate or a literal getter body.

| Geometry | gen1 Inferentia | gen2 Sunda | gen3 Cayman | gen4 CoreV4 |
|---|---|---|---|---|
| **SBUF** SB_SIZE / partition | 96 KiB (`0x18000`) | 192 KiB (`0x30000`) | 224 KiB (`0x38000`) | 256 KiB (`0x40000`) |
| SBUF partitions | 128 | 128 | 128 | 128 |
| SBUF total capacity | 12 MiB | 24 MiB | 28 MiB | 32 MiB |
| **PSUM** banks | 4 | 8 | 8 | 8 |
| PSUM bank size | 2048 B | 2048 B | 2048 B | 2048 B |
| PSUM partitions | 64 | 128 | 128 | 128 |
| PSUM window / partition | 8 KiB | 16 KiB | 16 KiB | 16 KiB |
| PE systolic rows × cols | 128 × 64 | 128 × 128 | 128 × 128 | 128 × 128 |
| Pool / Act channels | 64 | 128 | 128 | 128 |
| HBM per core ([1.06](dram-hbm-geometry.md)) | 16 GiB | 16 GiB | 24 GiB | 36 GiB |

---

## Related Components

| Name | Relationship |
|---|---|
| `bir::EngineInfo` (libBIR) | the 8-byte `{EngineType, id}` handle that does *not* carry this geometry; keys the Core sub-object table |
| `Core` sub-objects (`Pe/Pool/Act/Dve`) | sibling geometry records in the same `Core`; supply PE/Pool/Act dimensions |
| `Core.dramSizeGb` | the per-core HBM window, read through the same `Board→Device→Core` walk at `Core+0x30` ([1.06](dram-hbm-geometry.md)) |
| `SB_Allocator` / `PSUM_Allocator` (Part 8) | the consumers — color tensors into the SBUF rectangle / PSUM bank array bounded by these constants |

## Cross-References

- [1.01 Arch Object Model](arch-object-model.md) — the `bir::EngineInfo` handle and `EngineType` enum that key the Core sub-object table; PSUM = `MemoryType` 32, SBUF = 16.
- [1.04 Hardware Constant Matrix](hardware-constant-matrix.md) — the full per-generation constant table this page's SBUF/PSUM rows feed into.
- [1.06 DRAM / HBM Geometry](dram-hbm-geometry.md) — the off-chip side: `Core.dramSizeGb` read through the identical `getArchModel` walk, and the HBM budget check.
- [SBUF allocator (Part 8)](../walrus/) — consumes `SB_SIZE` and the 128-partition layout to 2-D color State-Buffer tensors.
- [PSUM allocator (Part 8)](../walrus/) — consumes `numBanks` and the 2048-byte bank size to 1-D color the partial-sum accumulator.
