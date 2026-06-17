# Per-Generation Hardware-Constant Matrix

> *All symbols, addresses, and field names on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310/cp311/cp312 wheels — the geometry is byte-identical across the three). Addresses are virtual addresses; for `.text`/`.rodata` they equal the file offset. Other wheels differ; treat every address as version-pinned.*

## Abstract

The compiler's idea of "what the chip is" is a small tree of statically-constructed C++ objects, one tree per architecture generation. Every per-engine size a downstream allocator, scheduler, or encoder needs — SBUF bytes per partition, PSUM bank count and bank size, the PE systolic array dimensions, the semaphore count, the partition count, the per-core HBM budget — is a literal immediate baked into a per-generation constructor at library-build time. This page is the consolidated table of those literals: every geometry constant, for every generation the compiler can target, each grounded in the exact constructor it is read from.

There is a rare luxury here. The wheel ships the **source header** for the model — `data/include/hwm/ctm/ctm.hpp` — which declares the `Board`/`Device`/`Core`/`Psumbuf`/`Statebuf`/`Pe`/`Pool`/`Act`/`Dve` classes with their real field names and, in comments, the `NEURON_ISA_TPB_*` ISA constant each scalar is sourced from. So unlike most pages in this book, the field *names* here are authoritative, not inferred; only a handful of derived semantics remain inferred. The numeric *values* are read off the four per-arch constructors in `ctm.cpython-3xx.so` — the **single source** of the geometry constants. (The arch-ctor symbols that appear in `libwalrus.so` and the tool binaries are all `// attributes: thunk` tail-calls back into `ctm.cpython`; there is no second, independent copy of the geometry to corroborate against.) What *is* a genuinely independent second source is the **cost model** — `TrainiumHwm` / `Gen3Hwm` / `CoreV4Hwm` in `walrus_driver` — which re-derives a few of the same numbers (notably the 2048-byte PSUM bank) from its own constants; that agreement is called out where it holds.

The model spans five generations but ships geometry for four. `getArchModel(string)` dispatches a device-alias string to one of four `Board*` singletons — gen1 Inferentia (Tonga / Inf1), gen2 Sunda (Trn1), gen3 Cayman (Trn2), gen4 CoreV4 / "mariana" (Trn3) — and a fifth ladder rung (gen5 `core_v5`) is a forward stub with no `Board` and an `assert("Unknown architecture")`. The silicon-codename decode (which internal name maps to which marketed part) is owned by [arch/codename-taxonomy.md](codename-taxonomy.md); this page names the codename per row and otherwise stays quantitative. The structural object graph that holds these constants — `getArchModel → Board → Device → Core → {Pe, Pool, Act, Dve, Psumbuf, Statebuf}` — is owned by [arch/arch-object-model.md](arch-object-model.md); here it is the skeleton the table hangs on.

For reimplementation, the contract is:

- The **four constructor families** and the literal arguments each passes — these *are* the geometry; there is no config file or device probe.
- The **invariants** (128 partitions, 128×128 PE, 256 semaphores, 2048-byte PSUM bank, one unit per engine) versus the **divergent axes** (SBUF size, gen1's half-width PSUM/Pool/Act, DVE opcode count, cost-model frequency, per-core HBM).
- The **cross-source check**: the geometry is single-sourced in the CTM ctors, but the `walrus_driver` cost model is an independent binary that re-derives the PSUM bank size (2048) and supplies per-engine frequencies. One claimed third source — a gen4 PE dtype slot — does **not** exist in the geometry and is corrected below.

| | |
|---|---|
| **Geometry source (runtime)** | `ctm.cpython-3xx.so` — per-arch `<Arch>Core` / `<Arch>{Psumbuf,Statebuf,Pe,Pool,Act,Dve}` ctors |
| **Field-name source-of-truth** | `data/include/hwm/ctm/ctm.hpp` (shipped C++ header) |
| **Dispatch entry** | `getArchModel(const string&)` — alias-string → `Board*` (4 arms + assert) |
| **Object graph** | `Board → Device → Core → {Pe,Pool,Act,Dve,Psumbuf,Statebuf}` |
| **Cost model (cross-check)** | `TrainiumHwm` / `Gen3Hwm` / `CoreV4Hwm` in `walrus_driver` / `libwalrus.so` |
| **Generations shipped** | gen1 Inferentia · gen2 Sunda · gen3 Cayman · gen4 CoreV4 (gen5 = stub) |

## How to read the matrix

The geometry is **not** a flat `.rodata` table. It is a tree of objects built once at library load. `getArchModel` returns a `const Board&` for the requested device alias; `Board` holds a `Device&`, `Device` holds a `Core&`, and `Core` holds six engine/memory sub-objects (`Pe`, `Pool`, `Act`, `Dve`, `Psumbuf`, `Statebuf`) plus 24 scalar ISA parameters. Each generation has its own `<Arch>Core` constructor that builds a `CoreParamSet` on the stack — filling it with that generation's immediates — and then constructs its six `<Arch>`-typed sub-objects, each of which is a one-line constructor that forwards literal arguments to the base-class constructor.

So every constant on this page is sourced the same way: find the `<Arch><SubObject>` constructor, read the immediate it passes. For example `SundaPsumbuf::SundaPsumbuf` is, in its entirety, `Psumbuf::Psumbuf(this, 8u, 0x80u, 0x800u)` — eight banks, 128 partitions, a 2048-byte bank. The base constructor signatures come from `ctm.hpp`, so the argument *positions* have real names.

```text
getArchModel("trn1")
  └─ Board   { numDevices, &Device }
       └─ Device { numCores, dramSizeGb(device), &Core }
            └─ Core { &Pe, &Pool, &Act, &Dve, &Psumbuf, &Statebuf,
                      dramSizeGb(per-core), NumSemaphores, … 23 more scalars }
                 ├─ Pe       (PeDimensionsForDtype& m16)   ── numRows×numCols
                 ├─ Pool     (numChannels)
                 ├─ Act      (numChannels)
                 ├─ Dve      ()
                 ├─ Psumbuf  (numBanks, numPartitions, partSize)
                 └─ Statebuf (partitionSize, SOCStepSize, numPartitions,
                              midPartition, &align, reservedSize)
```

The base-class field layouts, from `ctm.hpp`:

| Class | Constructor signature (`ctm.hpp`) | Fields that matter here |
|---|---|---|
| `Statebuf` | `(partitionSize, PartitionSOCStepSize, numPartitions, midPartition, Align&, reservedSize=16384)` | SBUF bytes/partition, partition count |
| `Psumbuf` | `(numBanks, numPartitions, partSize)` | PSUM bank count, bank partitions, bank bytes |
| `PeDimensionsForDtype` | `(numRows, numCols, maxWeightStep, minWave)` | systolic array rows × cols |
| `Pool` / `Act` | `(numChannels)` | engine width (partitions) |
| `Device` | `(Core&, numCores, dramSizeGb)` | NeuronCores per device, device HBM |
| `Board` | `(Device&, numDevices)` | devices per board |

> **NOTE —** `Pe` holds exactly **one** `const PeDimensionsForDtype &m16` (the comment in `ctm.hpp` reads `// fp16/bf16`). `Pe::getDimensionsForDtype(string)` accepts two dtype spellings before it `assert`s `"Dtype not available in PE array"` — there is no per-dtype array of dimensions in the CTM model. This bears on the MX question below.

---

## The consolidated matrix

The table below is the deliverable: every per-generation geometry constant, with the constructor and value it is read from, and a confidence tag per row. gen1 Inferentia is included for reference (it is the only **half-width** part — see the dedicated callout). The axes are the rows; the four generation columns carry the literal values; the **Source** column names the exact constructor; **Conf** is the four-tier ladder ([methodology](../methodology.md)).

| Constant | gen1 Inferentia | gen2 Sunda | gen3 Cayman | gen4 CoreV4 | Source (ctor → arg) | Conf |
|---|---|---|---|---|---|---|
| ArchLevel ordinal | 10 | 20 | 30 | 40 | `ArchLevel2string` switch | CONFIRMED |
| Internal codename | `inferentia` | `sunda` | `gen3` | `core_v4` | `getArchModel` aliases | CONFIRMED |
| Runtime codename | inferentia | sunda | cayman | **mariana** | `ArchLevel2RuntimeTarget` | CONFIRMED |
| Marketed device | Inf1 | Trn1 | Trn2 | Trn3 | `ArchLevel2ExternalString` | CONFIRMED |
| CoreVN / codegen | CoreV1 (legacy) | CoreV2 | CoreV3 | CoreV4 | `initCodegen` variant | CONFIRMED |
| **SBUF bytes/partition** | 0x18000 = 96 KiB | 0x30000 = 192 KiB | 0x38000 = 224 KiB | 0x40000 = 256 KiB | `Statebuf` arg0 `partitionSize` | CONFIRMED |
| **SBUF total** (×128) | 12 MiB | 24 MiB | 28 MiB | 32 MiB | arg0 × 128 | CONFIRMED |
| SBUF partitions | 128 | 128 | 128 | 128 | `Statebuf` arg2 `numPartitions` = 0x80 | CONFIRMED |
| SBUF SOC step size | 0x20000 = 128 KiB | 0x40000 = 256 KiB | 0x40000 = 256 KiB | 0x40000 = 256 KiB | `Statebuf` arg1 `PartitionSOCStepSize` | CONFIRMED |
| SBUF mid-partition | 64 | 64 | 64 | 64 | `Statebuf` arg3 `midPartition` | CONFIRMED |
| SBUF reservedSize | 0x4000 = 16384 | 0x4000 = 16384 | 0x4008 = 16392 | 0x4008 = 16392 | `Statebuf` arg5 `reservedSize` | CONFIRMED |
| **PSUM bank count** | 4 | 8 | 8 | 8 | `Psumbuf` arg0 `numBanks` | CONFIRMED |
| **PSUM bank size** | 2048 B | 2048 B | 2048 B | 2048 B | `Psumbuf` arg2 `partSize` = 0x800 | CONFIRMED |
| PSUM partitions | 64 | 128 | 128 | 128 | `Psumbuf` arg1 `numPartitions` | CONFIRMED |
| PSUM phys/partition | 8 KiB (4×2K) | 16 KiB (8×2K) | 16 KiB | 16 KiB | banks × partSize | CONFIRMED |
| PSUM phys total | 0.5 MiB | 2 MiB | 2 MiB | 2 MiB | banks × partSize × parts | CONFIRMED |
| **PE array (rows×cols)** | 128 × 64 | 128 × 128 | 128 × 128 | 128 × 128 | `PeDimensionsForDtype` arg0×arg1 | CONFIRMED |
| PE maxWeightStep | 2 | 2 | 2 | 2 | `PeDimensionsForDtype` arg2 | CONFIRMED |
| PE minWave | 128 | 128 | 128 | 128 | `PeDimensionsForDtype` arg3 = 0x80 | CONFIRMED |
| PE reorder window | 64 | 64 | 64 | 64 | `<Arch>Pe::getReorderWindowSize` | CONFIRMED |
| Pool channels (parts) | 64 | 128 | 128 | 128 | `Pool` arg0 `numChannels` | CONFIRMED |
| Act channels (parts) | 64 | 128 | 128 | 128 | `Act` arg0 `numChannels` | CONFIRMED |
| Pool reorder window | 8 | 8 | 8 | 8 | `<Arch>Pool::getReorderWindowSize` | CONFIRMED |
| Act reorder window | 8 | 8 | 8 | 16 | `<Arch>Act::getReorderWindowSize` | CONFIRMED |
| Dve reorder window | 8 | 8 | 8 | 16 | `<Arch>Dve::getReorderWindowSize` | CONFIRMED (gen4) |
| **Semaphores / core** | 256 | 256 | 256 | 256 | `CoreParamSet.NumSemaphores` = 0x100 | CONFIRMED |
| **Per-core HBM** | 16 GiB | 16 GiB | 24 GiB | 36 GiB | `Core::dramSizeGb` (CoreParamSet) | CONFIRMED |
| Device HBM | 32 GiB | 32 GiB | 24 GiB | 36 GiB | `Device::dramSizeGb` (arg2) | CONFIRMED |
| NeuronCores / device | 4 | 2 | 2 | 2 | `Device` arg1 `numCores` | CONFIRMED |
| Devices / board | 2 | 2 | 2 | 2 | `Board` arg1 `numDevices` | CONFIRMED |
| Per-engine count (each) | 1 | 1 | 1 | 1 | `CoreParamSet.*EngineCount` | CONFIRMED |
| **Cost-model freq** PE/Pool/Act | (140)¹ | 140 | 120 | 120 | `<Hwm>::getEngineFrequency` ord 1–3 | CONFIRMED |
| **Cost-model freq** SP | (112)¹ | 112 | 96 | 120 | `<Hwm>::getEngineFrequency` ord 5 | CONFIRMED |
| DVE default-set opcodes | — | 46 | 52 | 59 | `dve_bin_gen{2,3,4}` opcode_table | CONFIRMED |
| MX / FP4 microscaling | NO | NO | NO | feature, not geometry | see §"MX is not a PE dimension" | STRONG |

¹ gen1 has no distinct `Hwm` subclass; `TrainiumHwm` (the gen2/Sunda cost model) is the lowest cost model that ships. gen1 latency uses the base perf-sim path. See §"Cost-model frequencies".

> **NOTE — these are baked compile-time constants.** Every value above is an immediate in a constructor, fixed per codename at library-build time. There is no device probe, no config file, no environment override that changes the SBUF size or PSUM bank count. A reimplementation hard-codes the same four constructor families; it does not read geometry from the runtime.

---

## SBUF — the state buffer

SBUF is the on-chip working memory, modeled by `Statebuf`. The one divergent value is `partitionSize` (arg0): the bytes available per partition. Multiply by the 128-partition count (`numPartitions`, arg2 = `0x80`, gen-invariant) for the total. The four constructors, read in full from `ctm.cpython`:

```c
// _ZN18InferentiaStatebufC1Ev  (ctm.cpython)
Statebuf::Statebuf(this, 98304 /*0x18000=96KiB*/, 0x20000, 128, 64, &align, 0x4000 /*16384*/);
// _ZN13SundaStatebufC1Ev
Statebuf::Statebuf(this, 196608 /*0x30000=192KiB*/, 0x40000, 128, 64, &align, 0x4000);
// _ZN14CaymanStatebufC1Ev
Statebuf::Statebuf(this, 229376 /*0x38000=224KiB*/, 0x40000, 128, 64, &align, 16392 /*0x4008*/);
// _ZN14CoreV4StatebufC1Ev
Statebuf::Statebuf(this, 0x40000 /*256KiB*/,        0x40000, 128, 64, &align, 16392);
```

So per partition: **96 / 192 / 224 / 256 KiB** for gen1/2/3/4, i.e. **12 / 24 / 28 / 32 MiB** total across 128 partitions. The trailing `reservedSize` (arg5, header default `16384`) flips from `0x4000` to `0x4008` (bit 3 set) starting with Cayman; the byte value is CONFIRMED, but `ctm.hpp` names it only `reservedSize`, so the meaning of the extra bit is INFERRED. The second argument `PartitionSOCStepSize` is `0x20000` (128 KiB) on Inferentia and `0x40000` (256 KiB) on gen2+; `midPartition` (arg3) is `64` on every generation.

The allocator reads `partitionSize` through the standard object chain. In the SBUF coloring allocator (`SB_Allocator`), the read is `getArchModel → Board+8(Device) → Device+0x10(Core) → Core+0x28(Statebuf) → Statebuf+0`, then stored into the allocator's per-partition byte budget. That `Statebuf+0` is exactly arg0 — the per-partition SBUF size. The 128-partition count is a *separate* field (`Statebuf+8`), so the two are never conflated. The geometry of how the allocator carves this budget is owned by [arch/sbuf-psum-geometry.md](sbuf-psum-geometry.md).

---

## PSUM — the matmul accumulator

PSUM is the systolic-array accumulator, modeled by `Psumbuf(numBanks, numPartitions, partSize)`. The interesting divergence is on gen1 alone: Inferentia has **4** banks over **64** partitions; gen2/3/4 all have **8** banks over **128** partitions. The bank size is a flat 2048 bytes everywhere.

```c
// _ZN17InferentiaPsumbufC1Ev
Psumbuf::Psumbuf(this, 4u,  0x40u /*64*/,  0x800u /*2048*/);
// _ZN12SundaPsumbufC1Ev   /  _ZN13CaymanPsumbufC1Ev  /  _ZN13CoreV4PsumbufC1Ev
Psumbuf::Psumbuf(this, 8u,  0x80u /*128*/, 0x800u);
```

Physical PSUM is therefore `numBanks × partSize × numPartitions` = **0.5 MiB** on gen1 and **2 MiB** on gen2/3/4 (8 banks × 2 KiB × 128). The per-partition window is `numBanks × partSize` = 8 KiB (gen1) or 16 KiB (gen2+).

The PSUM allocator reads `numBanks` through `getArchModel → Board+8 → Device+0x10 → Core+0x20(Psumbuf) → Psumbuf+0`. The 2048-byte bank size is corroborated by a second, independent source — the cost model — in three sibling functions:

```c
TrainiumHwm::getPsumBankSize() → return 2048;   // gen2
Gen3Hwm::getPsumBankSize()     → return 2048;   // gen3
CoreV4Hwm::getPsumBankSize()   → return 2048;   // gen4
```

> **CORRECTION (M13/K10) —** an earlier reading inferred `NUM_PSUM_BANKS = 8` from the *size of the PSUM address window* rather than from a constructor. It is a literal: `Psumbuf` arg0, directly read as `8` on gen2/3/4 and `4` on gen1. The inferred-then-confirmed value happens to agree, but the grounding is now the constructor argument, not the window size.

> **GOTCHA — physical PSUM (2 MiB) is not the PSUM *address window* (4 MiB).** The encoder reserves a 4 MiB address region at `0x2000000` for PSUM in the 64-byte compute word (bit-25 base, mask `0x1E000000`). That is address space, not bank capacity (8 banks × 2 KiB × 128 partitions = 2 MiB physical). A reimplementer who conflates the two will mis-size either the encoder's address fields or the allocator's bank budget. They describe different axes; both are correct.

---

## PE — the systolic array

The PE array is modeled by a single `PeDimensionsForDtype(numRows, numCols, maxWeightStep, minWave)`. gen1 is **128 × 64** (half-width in the column dimension); gen2/3/4 are all **128 × 128**, with identical maxWeightStep (2) and minWave (128):

```c
// _ZN12InferentiaPeC1Ev
PeDimensionsForDtype::PeDimensionsForDtype(&this->m16, 0x80 /*128 rows*/, 0x40 /*64 cols*/, 2, 0x80);
// _ZN7SundaPeC1Ev  /  _ZN8CaymanPeC1Ev  /  _ZN8CoreV4PeC1Ev   — all identical:
PeDimensionsForDtype::PeDimensionsForDtype(&this->m16, 0x80, 0x80, 2, 0x80);
```

`getReorderWindowSize()` returns 64 on every generation. The full PE datapath is owned by [arch/pe-engine.md](pe-engine.md); this page only pins the array dimensions.

### MX is not a PE dimension

> **CORRECTION (M13) —** an earlier report read a *third* `PeDimensionsForDtype` dtype slot on gen4 (an extra `0x10 = 16` immediate) from a libBIR-side PE constructor and concluded "MX / microscaling is a gen4-only PE geometry dimension." The authoritative `ctm.hpp` header declares exactly one `const PeDimensionsForDtype &m16` on `Pe`, and the runtime `CoreV4Pe` constructor builds exactly **one** `PeDimensionsForDtype(128, 128, 2, 128)` — byte-identical to `SundaPe` and `CaymanPe`. `Pe::getDimensionsForDtype` accepts only two dtype spellings before asserting `"Dtype not available in PE array"`. The CTM PE geometry therefore shows **no** gen4-specific dtype slot.

MX / FP4 microscaling is unambiguously a real gen4-era compiler feature — but it lives at the HLO, BIR, and *cost-model* layers, not in the PE geometry tree. It is implemented by the HLO legalize passes `legalize-quantize-mx` and `legalize-scaled-matmul` (which recognize the `QuantizeMX` and `__op$block_scaled_dot` custom-calls, with `block_size` defaulting to 32 = `0x20`) and lowered to the BIR opcodes `InstMatmultMx` / `InstQuantizeMx`. Where the gen4-specificity *does* surface as a concrete per-generation override is the cost model: `CoreV4Hwm::getLatency(const bir::InstMatmultMx&)` exists as a CoreV4-only latency override (`@0x483de0`), with **no** matching `Gen3Hwm` or `TrainiumHwm` override — so the MX matmul has a cost-model handler only on gen4, while the base `bir::Hwm` handles it (by default) on the other generations. The "MX = gen4" association is correct; its mechanism is the HLO/BIR pipeline plus the gen4 `Hwm` latency override, not an extra column in the systolic array. A reimplementer building the geometry tree from `ctm.hpp` should give gen4 the same 128×128 PE as gen2/gen3 and look for MX support in the legalize passes ([hlo-opt](../hlo-opt/)) and the `CoreV4Hwm` latency overrides.

---

## Pool, Act, DVE — the vector/scalar engines

`Pool` and `Act` each take a single `numChannels`. Both are **64** on Inferentia (half-width) and **128** on gen2+:

```c
InferentiaPool: Pool::Pool(this, 0x40 /*64*/);   SundaPool/CoreV4Pool: Pool::Pool(this, 0x80 /*128*/);
SundaAct:       Act::Act(this, 0x80 /*128*/);     // (Inferentia Act = 64, half-width)
```

The reorder-window getters diverge only on gen4: `Pool` is 8 on every generation; `Act` and `Dve` are 8 on gen1/2/3 and **16** on gen4. On gen1/2/3 a *single* getter body returns 8 and is shared by `Pool`, `Act`, and `Dve` (the decompiled symbol carries the others as alternative names). On gen4 the split is real: `CoreV4Pool::getReorderWindowSize` returns 8 as a standalone body, while one shared body returns **16** for *both* `CoreV4Act` and `CoreV4Dve` (the gen4 `Dve` getter's alternative name is `_ZNK9CoreV4Act20getReorderWindowSizeEv`). So gen4 Act = gen4 Dve = 16 is read directly, not inferred.

The DVE *instruction set* itself is the other per-generation divergence on the vector axis: the default microcode opcode-table count is **46 / 52 / 59** for gen2/3/4 (the gen2→gen3 delta is +9 added, −3 removed = net +6; gen3→gen4 adds 7). These counts are read from the shipped `dve/dve_bin_gen{2,3,4}/…_opcode_table.bin` data and are orthogonal to the geometry constants here — they describe the ISA breadth, not engine dimensions. The full DVE ISA roster is its own subject.

> **QUIRK — Inferentia is the only half-width part.** SBUF is 128-partition on *every* generation, including gen1. But gen1's PSUM partitions, PE columns, Pool channels, and Act channels are all **64** — half the gen2+ width. So "128 partitions everywhere" is true for SBUF and PE *rows*, but the PSUM/Pool/Act "second dimension" is 64 on Inferentia. A reimplementer who hard-codes 128 across the board will over-size every gen1 engine that is not SBUF or PE-rows.

---

## The 24 scalar ISA parameters

Beyond the six sub-objects, `Core` carries 24 scalar parameters, filled from the per-arch `CoreParamSet` and copied into `Core+0x30…+0x88` by `Core::Core`. `ctm.hpp` names every one and, for the ISA-sourced ones, comments the `NEURON_ISA_TPB_*` constant it derives from. The values are **uniform across all four generations except `dramSizeGb`** (the first scalar). The decoded `SundaCore` constructor maps cleanly onto the header:

| `Core` field (`ctm.hpp`) | Source / ISA constant | gen1 | gen2 | gen3 | gen4 |
|---|---|---|---|---|---|
| `dramSizeGb` | per-core HBM, GiB | 16 | 16 | 24 | 36 |
| `NumSemaphores` | `NEURON_ISA_TPB_NUM_SEMAPHORES` | 256 | 256 | 256 | 256 |
| `MaxRegNumPerEngine` | `NUM_REGISTERS − 2` (62; reg 62/63 reserved for call-return) | 62 | 62 | 62 | 62 |
| `IsaAddrMarkerMask` | `NEURON_ISA_TPB_ADDR8_MARKER_MASK` | — | (set) | — | — |
| `DmaMaxDescCountPerPacket` | `DMA_MAX_DESC_COUNT_PER_PACKET` | 64 | 64 | 64 | 64 |
| `SmallDescSize` | `DMA_MAX_TRANSFER_SIZE_PER_DESC_KIB` | 64 | 64 | 64 | 64 |
| `MaxDmaEngineTile` | `DMA_ENGINE_DATA_WIDTH_BYTES × MAX_CCE_SOURCE` | 4096 | 4096 | 4096 | 4096 |
| `MaxActTableNum` | `ACTIVATION_NUM_TABLES` | 8 | 8 | 8 | 8 |
| `MaxCceDmaSource` | `NEURON_ISA_TPB_DMA_MAX_CCE_SOURCE` | 16 | 16 | 16 | 16 |
| `RtReservedSemNum` | shared RT constant | 3 | 3 | 3 | 3 |
| `RtDmaQueueLimit` | shared RT constant | 176 | 176 | 176 | 176 |
| `RtHwdgeQueueLimit` | shared RT constant | 2 | 2 | 2 | 2 |
| `RtReservedEventsNum` | shared RT constant | 12 | 12 | 12 | 12 |
| `NumPartPerQuadrant` | shared RT constant | 32 | 32 | 32 | 32 |
| `MemTensorIndirectEngPartitionGroupSize` | shared RT constant | 16 | 16 | 16 | 16 |
| `{Unassigned,Pe,Pool,Act,Dve,Dma,Sp,All}EngineCount` | per-engine multiplicity | 1 each | 1 each | 1 each | 1 each |

The `SundaCore` constructor, decoded, sets `CoreParamSet` slot 12 (= `Core+0x30`) to `16`, slot 13 (= `NumSemaphores`) to `256`, then `62, 64, 64, 4096, 8, 16, 3, 176, 2, 12, 32, 16`, then nine engine-count slots each `1`. `CaymanCore` and `CoreV4Core` differ from `SundaCore` *only* in slot 12: `24` and `36` respectively. The semaphore count `256` (8-bit index, `< 256`) is therefore CONFIRMED-uniform.

> **CORRECTION (M12/M13) —** the first scalar (`Core+0x30`, values 16/16/24/36) was originally read as an inferred "per-core DMA-ring / queue count." The shipped `ctm.hpp` names it `Core::dramSizeGb` — the **per-core HBM window in GiB**, with the comment `// dram/hbm per core`. The HBM budget check (`HBMUsage`) reads exactly this field and shifts it left by 30 to get the byte limit. The 16/16/24/36 are GiB, not ring counts.

### Per-core HBM and the budget check

The per-core HBM window (`Core::dramSizeGb`) is the value the compiler's DRAM budget actually gates against. `HBMUsage::run` reads `Core+0x30`, shifts left 30 (`<< 30` → bytes), and compares accumulated DRAM-tensor usage against it (with a soft 1.1× warning margin and a hard `usage ≤ limit` assert). The per-*device* `dramSizeGb` (`Device` arg2) is a separate immediate — 32/32/24/36 GiB — and is *not* the gate. Note gen1: 4 cores × 16 GiB ≠ the 32 GiB device value; the two need not agree (and only coincide on gen2 by arithmetic, on gen3/gen4 because the device immediate was set equal to the per-core window). HBM/DRAM is the one geometry axis the *compiler* models that the runtime also cares about; the per-generation HBM detail is owned by the DRAM-geometry analysis, but the per-core/per-device GiB constants are pinned here.

---

## Cost-model frequencies

The three `Hwm` ("hardware/cost model", **not** a memory high-water-mark) subclasses in `walrus_driver` / `libwalrus.so` supply per-engine throughput divisors for the perf simulator. These are cost-model cycle figures (used as `floor((NEP·mult + base)·100 / freq)`), **not MHz**. Read directly from the `getEngineFrequency(EngineType)` bodies:

```c
TrainiumHwm::getEngineFrequency(t):  // gen2 / Sunda
    if (1 <= t <= 3) return 140;     // PE, Pool, Act
    if (t == 5)      return 112;     // SP
Gen3Hwm::getEngineFrequency(t):      // gen3 / Cayman
    if (1 <= t <= 3) return 120;
    if (t == 5)      return 96;
CoreV4Hwm::getEngineFrequency(t):    // gen4 / CoreV4
    if (1 <= t <= 3) return 120;
    if (t == 5)      return 120;
    else → getEngineCyclesPerElement();   // alt path
```

> **CORRECTION (K20) —** the `140 / 112` pair was originally attributed to Tonga (gen1). It belongs to **`TrainiumHwm`, the gen2/Sunda cost model** — `getArchModel` groups `{sunda, trainium, trn1, inf2}` onto Sunda, so "Trainium" is gen2, not gen1. gen1 ships *no* distinct `Hwm` subclass (only `TrainiumHwm`, `Gen3Hwm`, `CoreV4Hwm` exist); gen1 latency falls through to the base perf-sim path. PE is the *slower* clock domain on gen2/gen3 (PE 140-vs-SP-112, PE 120-vs-SP-96) and equal to SP on gen4 (both 120).

> **NOTE — the perf-sim `Hwm` families are a coarser 3-tier grouping.** The cost model collapses the four codegen tiers into three latency buckets, with `Gen3Hwm` spanning both Sunda (gen2) and Cayman (gen3). This is a perf-model simplification; the *codegen* keeps the generations distinct (`CoreV2Gen` vs `CoreV3Gen`). Do not read the shared `Hwm` family as a generation merge.

---

## What is *not* in the compiler's geometry

Three things a reimplementer might expect in the per-generation matrix are structurally absent from the compiler binaries, because they are runtime/driver concerns:

- **ICI / inter-chip link count** — no `getIci`-style symbol exists in either binary's dynamic symbol table. The CTM model covers only on-NeuronCore TPB resources.
- **Marketed NeuronCores-per-chip** — the compiler pins `numCores` *per device* (4 / 2 / 2 / 2) and `numDevices` *per board* (2 everywhere), plus the in-binary truth that a logical NeuronCore spans 1–2 physical cores (`assert("LNC_SIZE <= 2")`, default `lnc_size = 1`). The full marketed per-chip core count is a public-spec figure, not a binary literal, and is SPECULATIVE if treated as exact.
- **DMA queue count** — queues are allocated dynamically per-`Module` (`bir::Module::addQueue`), clamped by `optNumDMAEngines`, not a fixed per-generation immediate.

These absences are confirmed by symbol-table search, not assumed; a page that listed them with concrete per-gen numbers would be inventing them.

---

## Related Components

| Name | Relationship |
|---|---|
| `getArchModel` | The dispatch that selects which of the four `Board` trees this page tabulates |
| `<Arch>Core` ctors | The four constructor families that *are* the geometry |
| `ctm.hpp` | The shipped header that names every field the matrix grounds |
| `HBMUsage` | The one consumer that gates compilation on a geometry value (`Core::dramSizeGb`) |
| `TrainiumHwm` / `Gen3Hwm` / `CoreV4Hwm` | The independent cost-model source that corroborates bank size and supplies frequencies |

## Cross-References

- [arch/arch-object-model.md](arch-object-model.md) — the `Board → Device → Core` object graph these constants live in; this page is its quantitative companion.
- [arch/codename-taxonomy.md](codename-taxonomy.md) — the full codename ↔ ArchLevel ↔ marketed-device decode named-but-deferred in the matrix's first rows.
- [arch/sbuf-psum-geometry.md](sbuf-psum-geometry.md) — how the SBUF/PSUM byte budgets recorded here are carved by the allocators.
- [arch/pe-engine.md](pe-engine.md) — the systolic-array datapath whose 128×128 dimensions are pinned here.
- [appendix/arch-constant-matrix.md](../appendix/arch-constant-matrix.md) — the appendix-level consolidated constant ledger this table feeds.
- [Methodology & the Confidence Model](../methodology.md) — the four-tier ladder used in the matrix's Confidence column.
