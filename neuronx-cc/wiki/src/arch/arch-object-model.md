# The arch Object Model

> *All symbols and addresses on this page are read from the **cp310** wheel of `neuronx_cc` 2.24.5133.0+58f8de22. `libwalrus.so` BuildID `92b4d331…`; `libBIR.so` BuildID `a9b1ea38…`. The C++ libraries are rebuilt per wheel (cp310 and cp311 share a size but not a SHA; cp312 differs in size), so re-confirm addresses against cp311/cp312 — see [Version Provenance](../reference/versions.md). For both binaries, `.text`/`.rodata` VMA equals file offset (`libwalrus` `.text` @ `0x62d660`, `.rodata` @ `0x1c72000`; `libBIR` `.text` @ `0x1820c0`, `.rodata` @ `0x708000`). Treat every address as version-pinned.*

## Abstract

Every hardware constant the compiler needs — the SBUF byte budget, the number of PSUM banks, the systolic-array width, the partition count, the engine clock, the per-core HBM window — is read through a single four-hop accessor chain: `getArchModel(codename)` returns a `Board*`, `Board+0x8` is a `Device*`, `Device+0x10` is a `Core*`, and the `Core` holds six engine sub-object pointers plus a flat run of scalar ISA parameters. There is no flat `.rodata` table of constants and no runtime device probe; the geometry is a tree of statically-constructed C++ objects, built once at library load by four per-architecture constructors, and handed out by a string-compare dispatch over the arch codename. This page is the spine of Part 1: every other geometry page reaches its constant by walking this same chain to a different offset.

The class family is not inferred. A real C++ header ships in the wheel — `data/include/hwm/ctm/ctm.hpp` — and declares `Board`, `Device`, `Core`, `CoreParamSet`, and the six engine classes (`Pe`, `Pool`, `Act`, `Dve`, `Psumbuf`, `Statebuf`) with their exact field names and doc comments. The header is the source-of-truth for *what each offset means*; the disassembled constructors are the source-of-truth for *what value each field holds per arch*. Where the two agree, a field is CONFIRMED in both name and value.

The decisive structural fact is that the same chain is implemented **twice, independently** — once in `libwalrus.so` (the backend) and once in `libBIR.so` (the IR library) — with the same class layout, the same accessor offsets, and byte-identical per-arch immediates, but distinct entry-point addresses and distinct PLT thunks. Two separately-compiled binaries agreeing to the byte is the strongest evidence available short of source, and this page makes that cross-confirmation explicit at every step. The body walks the chain hop by hop, then shows the four real read-sites (SBUF, PSUM, DRAM, engine count) that drive the allocators, and closes with the per-arch constant matrix.

For reimplementation, the contract is:

- **The accessor chain** — `getArchModel` → `Board+0x8` → `Device+0x10` → `Core+{sub-object offset}` → field — and the exact byte offset of every field a consumer reads.
- **The object layout** — the field-by-field map of `Board`/`Device`/`Core` and the six engine sub-objects, named from `ctm.hpp` and offset-pinned from the constructors.
- **The construction model** — how each per-arch `<Arch>Core` ctor fills a `CoreParamSet` with immediates and copies it into the `Core`, so a reader can read any constant straight off the ctor body.
- **The dual-source invariant** — that `libwalrus` and `libBIR` carry independent copies of the identical tree, and why a reimplementer can trust the layout because two binaries confirm it.

| | |
|---|---|
| **Dispatch entry (libwalrus)** | `getArchModel(string) ` @ `0x17344c0` |
| **Dispatch entry (libBIR)** | `getArchModel(string) ` @ `0x478f90` (independent copy) |
| **Accessor chain** | `Board+0x8`(Device) → `Device+0x10`(Core) → `Core+{0x00..0x30}` |
| **Class header (in-wheel)** | `data/include/hwm/ctm/ctm.hpp` — names every field |
| **Per-arch ctors (libwalrus)** | `InferentiaCore` `0x1734720` · `SundaCore` `0x1734b10` · `CaymanCore` `0x1734f00` · `CoreV4Core` `0x1735300` |
| **Static singletons (.bss)** | `_inferentia_arch_model` `0x3e05800` · `_sunda_arch_model` `0x3e05980` · `_cayman_arch_model` `0x3e05b00` · `_core_v4_arch_model` `0x3e05c80` |
| **Engine sub-objects** | `Pe`@`Core+0x00` · `Pool`@`+0x08` · `Act`@`+0x10` · `Dve`@`+0x18` · `Psumbuf`@`+0x20` · `Statebuf`@`+0x28` |
| **Scalar block** | `Core+0x30` (`dramSizeGb`) … `Core+0x88` (`AllEngineCount`) |

---

## The accessor chain

### Purpose

Every hardware constant lives behind one indirection chain. A consumer that wants SBUF size, PSUM bank count, partition count, or HBM window does not look up a table keyed by a constant name; it calls `getArchModel` with the module's arch codename, walks three fixed pointer hops to the `Core`, and reads a field at a fixed offset. Learning this chain once is the whole point of the page — every Part-1 geometry constant is the same walk to a different terminal offset.

### Entry Point

The chain begins at a `bir::Module`. The module carries its arch codename (`bir::Module::getArch()` @ `libBIR 0x354ef0`) and a cached `Hwm` cost oracle (`bir::Module::getHwm()` @ `libBIR 0x3555a0`); the codename string is what feeds `getArchModel`.

```text
bir::Module
  ├─ getArch()  (libBIR 0x354ef0)   ── arch codename string ("sunda", "core_v4", …)
  └─ getHwm()   (libBIR 0x3555a0)   ── per-arch cost model (separate page)
        │
        ▼  codename string
  getArchModel(codename)            ── libwalrus 0x17344c0 / libBIR 0x478f90
        │  returns const Board&     (a static singleton, not a fresh alloc)
        ▼
  Board   +0x08 ── const Device&
        ▼
  Device  +0x10 ── const Core&
        ▼
  Core    +0x00 Pe*   +0x08 Pool*  +0x10 Act*  +0x18 Dve*
          +0x20 Psumbuf*  +0x28 Statebuf*       ── engine sub-objects
          +0x30 dramSizeGb … +0x88 AllEngineCount ── 24 scalar ISA params
```

### Algorithm

The chain is the same three instructions everywhere a constant is read; only the terminal offset and the field-read width change. This is the canonical SBUF read, lifted from the coloring allocator (`SB_Allocator` ctor) and confirmed byte-for-byte at `libwalrus 0xa97b82`:

```c
// SB_Allocator::SB_Allocator (libwalrus 0xa97750), the SB_SIZE read @0xa97b82..0xa97bbd
uint32_t read_sbuf_size(Module *m):
    Board  *board  = getArchModel(m->getArch());   // 0xa97b82  call getArchModel
    Device *dev    = *(Board  + 0x08);              // 0xa97baa  mov 0x8(%rbx),%rax
    Core   *core   = *(Device + 0x10);              // 0xa97bb3  mov 0x10(%rax),%rax
    Statebuf *sb   = *(Core   + 0x28);              // 0xa97bb7  mov 0x28(%rax),%rax
    uint32_t SB_SIZE = *(sb + 0x00);                // 0xa97bbb  mov (%rax),%eax  = partitionSize
    allocator[0x2b8] = SB_SIZE;                     // 0xa97bbd  mov %eax,0x2b8(%rcx)   (=a1+696)
    if (m->archLevel == 10):                        // 0xa97bc3  cmp $0xa,%r14d  (gen1 byte-budget fork)
        ... Inferentia special case ...
    return SB_SIZE
```

Three observations make this the template for the whole part. First, the value stored at `allocator+0x2b8` (decimal 696) is the field that prior reports named `a1+696` — it is definitively `Statebuf+0x00` (`partitionSize`), the per-partition byte budget, **not** the partition count; the partition count is the separate field `Statebuf+0x08` (`numPartitions`). Second, the `cmp $0xa,%r14d` immediately after the store is a generation gate — `ArchLevel 10` is Inferentia — and is a *consumer-side* fork, not part of the geometry read. Third, the four-hop shape (`getArchModel` → `+0x8` → `+0x10` → sub-object → field) is invariant; every other reader on this page is this same code with a different final offset.

> **NOTE —** `getArchModel` returns a `const Board&` to a **static singleton**, not a freshly-allocated object. The four singletons live in `.bss` (`_inferentia_arch_model` @ `0x3e05800`, `_sunda_arch_model` @ `0x3e05980`, `_cayman_arch_model` @ `0x3e05b00`, `_core_v4_arch_model` @ `0x3e05c80`) and are filled by the per-arch constructors at load time. The deprecated `Pacific()` accessor (`ctm.hpp:263`, `libwalrus 0x17345d0`) is a one-instruction stub that returns the Inferentia singleton through its GOT slot — the legacy default. A reimplementer should build the tree once and hand out references, never re-run the ctor per query.

### Considerations

The terminal field read is a 4-byte (`mov …,%eax`) load for every scalar geometry field — `unsigned`/`uint32_t` in `ctm.hpp`. The one structural subtlety is that `Psumbuf` and `Statebuf` are reached as *pointers* stored in the `Core` (`Core+0x20`, `Core+0x28`), whereas `dramSizeGb` and the rest of the scalar block are stored *inline* in the `Core` (`Core+0x30`…). A reader after a memory-pool geometry field does one extra dereference (`Core → sub-object → field`); a reader after a scalar ISA param reads `Core+offset` directly.

---

## The dispatch: codename → Board

### Purpose

`getArchModel` is the only door into the tree. It is a forgiving input-alias parser: many codename spellings collapse onto four `Board` singletons (a fifth, gen5/`core_v5`, is a forward stub with no `Board`, so it falls through to `__assert_fail`).

### Algorithm

The body is a linear chain of `std::string::compare(const char*)` calls, one per accepted alias, each arm returning the address of a static `Board` on match. Confirmed at `libwalrus 0x17344c0`: ten `compare` call-sites (`0x17344cb`, `0x17344ea`, … `0x173458e`) terminating in `__assert_fail` @ `0x17345c3`.

```c
// getArchModel (libwalrus 0x17344c0 / libBIR 0x478f90)
const Board& getArchModel(const string &codename):
    if compare(codename, "tonga")      == 0: return _inferentia_arch_model   // gen1
    if compare(codename, "inferentia") == 0: return _inferentia_arch_model
    if compare(codename, "inf1")       == 0: return _inferentia_arch_model
    if compare(codename, "sunda")      == 0: return _sunda_arch_model         // gen2
    if compare(codename, "trainium")   == 0: return _sunda_arch_model
    if compare(codename, "trn1")       == 0: return _sunda_arch_model
    if compare(codename, "inf2")       == 0: return _sunda_arch_model
    if compare(codename, "cayman")     == 0: return _cayman_arch_model        // gen3
    if compare(codename, "gen3")       == 0: return _cayman_arch_model
    if compare(codename, "core_v4")    == 0: return _core_v4_arch_model       // gen4
    __assert_fail("0 && \"Unknown architecture\"")                            // gen5/unknown
```

### Function Map

| Symbol | Binary | Address | Role | Confidence |
|---|---|---|---|---|
| `getArchModel(string)` | libwalrus | `0x17344c0` | Codename→`Board*` dispatch | CONFIRMED |
| `getArchModel(string)` | libBIR | `0x478f90` | Independent copy of the same dispatch | CONFIRMED |
| `Pacific()` | libwalrus | `0x17345d0` | Deprecated default → Inferentia singleton | CONFIRMED |
| `_inferentia_arch_model` | libwalrus | `0x3e05800` (.bss) | gen1 `Board` singleton | CONFIRMED |
| `_sunda_arch_model` | libwalrus | `0x3e05980` (.bss) | gen2 `Board` singleton | CONFIRMED |
| `_cayman_arch_model` | libwalrus | `0x3e05b00` (.bss) | gen3 `Board` singleton | CONFIRMED |
| `_core_v4_arch_model` | libwalrus | `0x3e05c80` (.bss) | gen4 `Board` singleton | CONFIRMED |

> **GOTCHA — the input aliases are not the canonical names.** `getArchModel` accepts `{tonga, inferentia, inf1}`→gen1, `{sunda, trainium, trn1, inf2}`→gen2, `{cayman, gen3}`→gen3, `{core_v4}`→gen4. The *canonical* internal codename (what `bir::ArchLevel2string` emits) is the narrower set `{inferentia, sunda, gen3, core_v4, core_v5}`. A reimplementation that drives `getArchModel` only off the canonical names will reject the legacy `tonga`/`trainium`/`trn1`/`inf2`/`cayman` spellings that real modules carry. Build the parser from the full alias set, the canonical mapper from the narrow one. The codename↔generation↔CoreVN↔device bijection is owned by the [codename matrix](hardware-constant-matrix.md).

---

## The object layout

### Purpose

This is the field map every consumer indexes into. Names come from `ctm.hpp` (authoritative); offsets come from the `libwalrus` constructors (`Board::Board` @ `0x17344a0`, `Device::Device` @ `0x1734480`, `Core::Core` @ `0x1734220`). The header and the disassembly agree, so each row is CONFIRMED in both name and offset.

### Board, Device

`Board` and `Device` are thin: a count or two plus the next pointer. Each carries a `tpb`/`tonga` alias field that is a duplicate reference, retained for backward compatibility (`ctm.hpp:232`, `:249`).

| Struct | Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|---|
| `Board` | `numDevices` | `+0x00` | `unsigned` | Devices per board (=2 all arches) | CONFIRMED |
| `Board` | `numTonga` | `+0x04` | `unsigned` | Alias of `numDevices` | CONFIRMED |
| `Board` | `device` | `+0x08` | `Device*` | **The Device hop** | CONFIRMED |
| `Board` | `tonga` | `+0x10` | `Device*` | Alias of `device` | CONFIRMED |
| `Device` | `numCores` | `+0x00` | `unsigned` | NeuronCores per device | CONFIRMED |
| `Device` | `numTpb` | `+0x04` | `unsigned` | Alias of `numCores` | CONFIRMED |
| `Device` | `dramSizeGb` | `+0x08` | `unsigned` | Whole-device HBM, GiB | CONFIRMED |
| `Device` | `core` | `+0x10` | `Core*` | **The Core hop** | CONFIRMED |
| `Device` | `tpb` | `+0x18` | `Core*` | Alias of `core` | CONFIRMED |

The two hops the accessor chain takes — `Board+0x8` and `Device+0x10` — are the real `device` and `core` references; the `+0x10`/`+0x18` aliases sit one slot past them and are never read by the geometry consumers on this page.

### Core

`Core` is the destination. Its first six 8-byte slots are pointers to the engine sub-objects; everything from `+0x30` on is a flat run of 24 scalar ISA parameters copied out of the `CoreParamSet`.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `pe` | `+0x00` | `Pe*` | Systolic-array geometry | CONFIRMED |
| `pool` | `+0x08` | `Pool*` | Pool-engine geometry | CONFIRMED |
| `act` | `+0x10` | `Act*` | Activation-engine geometry | CONFIRMED |
| `dve` | `+0x18` | `Dve*` | Vector-engine geometry | CONFIRMED |
| `psumbuf` | `+0x20` | `Psumbuf*` | **PSUM pool geometry** | CONFIRMED |
| `statebuf` | `+0x28` | `Statebuf*` | **SBUF pool geometry** | CONFIRMED |
| `dramSizeGb` | `+0x30` | `unsigned` | **Per-core HBM window, GiB** | CONFIRMED |
| `NumSemaphores` | `+0x34` | `uint32` | `NEURON_ISA_TPB_NUM_SEMAPHORES` (=256) | CONFIRMED |
| `IsaAddrMarkerMask` | `+0x38` | `uint8` | `NEURON_ISA_TPB_ADDR8_MARKER_MASK` | CONFIRMED (name) |
| `MaxRegNumPerEngine` | `+0x3c` | `uint32` | `NEURON_ISA_TPB_NUM_REGISTERS − 2` (=62) | CONFIRMED |
| `DmaMaxDescCountPerPacket` | `+0x40` | `uint32` | DMA descriptors per packet (=64) | CONFIRMED (name) |
| `SmallDescSize` | `+0x44` | `uint32` | DMA max transfer KiB/desc (=64) | CONFIRMED (name) |
| `MaxDmaEngineTile` | `+0x48` | `uint32` | DMA engine tile bytes (=4096) | CONFIRMED (name) |
| `MaxActTableNum` | `+0x4c` | `uint32` | `NEURON_ISA_TPB_ACTIVATION_NUM_TABLES` (=8) | CONFIRMED (name) |
| `MaxCceDmaSource` | `+0x50` | `uint32` | `NEURON_ISA_TPB_DMA_MAX_CCE_SOURCE` (=16) | CONFIRMED (name) |
| `RtReservedSemNum` … `MemTensorIndirectEngPartitionGroupSize` | `+0x54`…`+0x68` | `uint32`×6 | Runtime-shared limits | CONFIRMED (name) |
| `UnassignedEngineCount` | `+0x6c` | `uint32` | Per-engine count (=1) | CONFIRMED |
| `PeEngineCount` | `+0x70` | `uint32` | (=1) | CONFIRMED |
| `PoolEngineCount` | `+0x74` | `uint32` | (=1) | CONFIRMED |
| `ActEngineCount` | `+0x78` | `uint32` | (=1) | CONFIRMED |
| `DveEngineCount` | `+0x7c` | `uint32` | (=1) | CONFIRMED |
| `DmaEngineCount` | `+0x80` | `uint32` | (=1) | CONFIRMED |
| `SpEngineCount` | `+0x84` | `uint32` | (=1) | CONFIRMED |
| `AllEngineCount` | `+0x88` | `uint32` | (=1) | CONFIRMED |

> **CORRECTION — `Core+0x30` is `dramSizeGb`, not a ring/queue count.** An earlier reading of the bare immediates labelled `Core+0x30` (= `CoreParamSet+0x60`) a "per-Core ring/queue count" with values 16/16/24/36. The shipped header settles it: `ctm.hpp:187` declares `const unsigned dramSizeGb; // dram/hbm per core`. The values 16/16/24/36 are **GiB of HBM per NeuronCore**, and the chain `getArchModel → Board+0x8 → Device+0x10 → Core+0x30` is the per-core HBM window the budget check reads (see [§The four read-sites](#the-four-read-sites)). Likewise `Device+0x8` is `Device::dramSizeGb` (whole-device HBM), `ctm.hpp:229`.

The scalar names mapped to `NEURON_ISA_TPB_*` constants are CONFIRMED-by-name from `ctm.hpp` comments but their exact per-arch *values* (other than the few cross-checked above) were read as raw immediates; the offset/name binding is firm, the numeric value of each is read straight off the ctor body. The exact semantics behind `IsaAddrMarkerMask`, `MaxDmaEngineTile`, and the `Rt*` runtime-shared block are taken from the header's comments and are not independently traced to a consumer here.

### The six engine sub-objects

Each `Core+0xNN` pointer targets a small object constructed in-place inside the per-arch `<Arch>Core` ctor (the ctor `lea`s a stack/`.bss` slot, runs the base ctor, and stores the address into the `CoreParamSet`). Fields, names from `ctm.hpp`, offsets from the base ctors `Statebuf::Statebuf` (@ `0x17341e0`) and `Psumbuf::Psumbuf` (@ `0x17341b0`):

| Sub-object | Field | Offset | Meaning | Confidence |
|---|---|---|---|---|
| `Statebuf` | `partitionSize` | `+0x00` | **SBUF bytes per partition (SB_SIZE)** | CONFIRMED |
| `Statebuf` | `PartitionSOCStepSize` | `+0x04` | Addressable partition span (=`0x40000`) | CONFIRMED (name) |
| `Statebuf` | `numPartitions` | `+0x08` | **SBUF partitions (=128)** | CONFIRMED |
| `Statebuf` | `midPartition` | `+0x0c` | Sub-band / quadrant granule (=64) | CONFIRMED (name) |
| `Statebuf` | `align` | `+0x10` | `Align*` alignment descriptor | CONFIRMED |
| `Statebuf` | `reservedSize` | `+0x18` | Reserved bytes (default 16384=`0x4000`) | CONFIRMED |
| `Psumbuf` | `numBanks` | `+0x00` | **PSUM banks** | CONFIRMED |
| `Psumbuf` | `numPartitions` | `+0x04` | **PSUM partitions** | CONFIRMED |
| `Psumbuf` | `partSize` | `+0x08` | **PSUM bank size (=2048)** | CONFIRMED |
| `Psumbuf` | `bufLen64` | `+0x0c` | `partSize>>3` (=256), deprecated helper | CONFIRMED |
| `Psumbuf` | `bufLen32` | `+0x10` | `partSize>>2` (=512), deprecated helper | CONFIRMED |
| `PeDimensionsForDtype` | `numRows` | `+0x00` | Systolic rows (=128) | CONFIRMED |
| `PeDimensionsForDtype` | `numCols` | `+0x04` | Systolic cols (=64 gen1, 128 gen2+) | CONFIRMED |
| `PeDimensionsForDtype` | `maxWeightStep` | `+0x08` | Dtype byte-class factor (=2) | CONFIRMED |
| `PeDimensionsForDtype` | `minWave` | `+0x0c` | Secondary dim (=128) | CONFIRMED |
| `Pe` | `m16` | `+0x08` | `PeDimensionsForDtype&` (fp16/bf16 dims) | CONFIRMED |
| `Pool` / `Act` | `numChannels` | `+0x08` | Engine partition count (64 gen1, 128 gen2+) | CONFIRMED |

`Pe`, `Pool`, `Act`, and `Dve` are abstract in the header (`getReorderWindowSize()` is pure-virtual); the per-arch reorder window is an override, not a stored field — `InferentiaPe::getReorderWindowSize` (@ `0x17345e0`) is `mov $0x40; ret` (=64), the shared Inferentia `Pool`/`Act`/`Dve` body (@ `0x17345f0`) is `mov $0x8; ret` (=8), and the gen4 `CoreV4Act`/`CoreV4Dve` body (@ `0x17351d0`) is `mov $0x10; ret` (=16) — the only generation that widens the activation/vector reorder window.

> **NOTE — the `Pe` dtype table and MX.** `Pe::getDimensionsForDtype(dtype)` selects a `PeDimensionsForDtype` per element type; the header declares one stored member, `m16` (`Pe+0x08`), the fp16/bf16 dims. The systolic dimensions themselves are identical 128×128 across gen2–gen4 in the `Pe` ctor bodies (gen1 is 128×64). A reported gen4-only extra `0x10`-element FP4/MX dtype slot was **not** reproduced by a direct read of the `CoreV4Pe` ctor: the `lea 0x10(%rdi)` at its entry is the generic embedded-`Pe` member base, present in all four arch ctors, not a CoreV4-specific dtype variant (INFERRED→demoted; the MX datapath's geometry source is not located in this ctor). MX support is a real gen4 divergence but its evidence lives elsewhere; treat the `Pe`-ctor "third dtype slot" claim as unconfirmed and see [SBUF/PSUM geometry](sbuf-psum-geometry.md).

---

## The construction model

### Purpose

A reimplementer needs to read any constant off the binary. The construction model is what makes that possible: each per-arch `<Arch>Core` ctor builds a `CoreParamSet` on the stack — a struct of `std::optional` fields, each an immediate `movl $imm, off(%rsp)` — then calls `Core::Core(const CoreParamSet&)`, which copies the optionals into the `Core`'s inline fields. Read the immediate, know the constant.

### Algorithm

The `CoreParamSet` (`ctm.hpp:147`) lays its engine pointers at `+0x00`…`+0x50` (8-byte stride, each with a 1-byte "engaged" flag) and its scalar block starting at `+0x60` (`dramSizeGb`), `+0x68` (`NumSemaphores`), … `+0x10c` (`AllEngineCount`). `Core::Core` copies each scalar with a `+0x08`-stride-in / `+0x04`-stride-out pattern, confirmed at `0x1734294`:

```c
// Core::Core(const CoreParamSet&)  (libwalrus 0x1734220)
function Core_ctor(Core *this /*rdi*/, CoreParamSet *p /*rsi*/):
    this[0x00] = *(p + 0x00);   // pe*       (0x1734278 region)
    this[0x08] = *(p + 0x10);   // pool*
    this[0x10] = *(p + 0x20);   // act*
    this[0x18] = *(p + 0x30);   // dve*
    this[0x20] = *(p + 0x40);   // psumbuf*  (0x1734278  mov %rcx,0x20(%rdi))
    this[0x28] = *(p + 0x50);   // statebuf* (0x173428a  mov %rcx,0x28(%rdi))
    this[0x30] = *(p + 0x60);   // dramSizeGb (0x1734294 mov 0x60(%rsi),%ecx; 0x173429b mov %ecx,0x30(%rdi))
    ... 23 more scalar copies, CoreParamSet+0x68.. → Core+0x34.. ...
```

So `CoreParamSet+0x60` → `Core+0x30` is the `dramSizeGb` copy. The per-arch ctor writes `dramSizeGb` as a single `movl $imm,0x60(%rsp)`; reading that immediate gives the per-core HBM window directly.

### How each per-arch ctor writes the divergent fields

The four `<Arch>Core` ctors differ only in a handful of immediates; the bulk of the scalar block (`NumSemaphores`=256, the granules, the per-engine counts=1) is identical across all four. The divergent writes, all CONFIRMED off the ctor bodies:

```text
InferentiaCore (0x1734720)  movl $0x10,0x60(%rsp)  ; dramSizeGb = 16 GiB   @0x17347b6
                            mov  $0x20,%ecx        ; Device dramSizeGb=32  @0x1734995
                            mov  $0x4,%edx         ; numCores = 4          @0x173499a
                            mov  $0x2,%edx         ; numDevices = 2        @0x17349b5
SundaCore      (0x1734b10)  movl $0x10,0x60(%rsp)  ; dramSizeGb = 16 GiB   @0x1734ba6
                            mov  $0x20,%ecx        ; Device dramSizeGb=32  @0x1734d85
                            mov  $0x2,%edx         ; numCores = 2          @0x1734d8a
CaymanCore     (0x1734f00)  movl $0x18,0x60(%rsp)  ; dramSizeGb = 24 GiB   @0x1734f96
                            mov  $0x18,%ecx        ; Device dramSizeGb=24  @0x1735175
                            mov  $0x2,%edx         ; numCores = 2          @0x173517a
CoreV4Core     (0x1735300)  movl $0x24,0x60(%rsp)  ; dramSizeGb = 36 GiB   @0x1735396
                            mov  $0x24,%ecx        ; Device dramSizeGb=36  @0x1735575
                            mov  $0x2,%edx         ; numCores = 2          @0x173557a
```

> **QUIRK — per-core HBM and whole-device HBM are independent immediates.** On Inferentia, `numCores`=4 and `Core::dramSizeGb`=16 GiB, but `Device::dramSizeGb`=32 GiB — *not* 4×16=64. The two `dramSizeGb` fields are written by separate `mov`s and need not be consistent. The budget check that actually gates compilation reads the **per-core** field (`Core+0x30`), not the device field. A reimplementer must not compute one from the other.

The Inferentia ctor also exposes the half-width nature of gen1: its `Statebuf` is full-width (128 partitions) but its `Psumbuf` (64 partitions), `Pe` cols (64), and `Pool`/`Act` `numChannels` (64) are half what gen2+ carry. "128 partitions everywhere" is a gen2+ fact, not a universal one.

---

## The four read-sites

### Purpose

Four real consumers prove the chain is the *only* path to a constant and pin the terminal offsets. Each is the SB_SIZE template (`getArchModel → +0x8 → +0x10 → terminal`) with a different last hop. All four are byte-confirmed.

### The read-sites

```c
// (1) SBUF size — SB_Allocator (libwalrus 0xa97b82..0xa97bbd)
board = getArchModel(arch); sb = *(*(*(board+0x8))+0x10) + 0x28); SB_SIZE = *(sb+0x00);
//     Board+0x8 → Device+0x10 → Core+0x28(statebuf) → Statebuf+0x00 = partitionSize

// (2) PSUM banks — PSUM_Allocator (libwalrus 0xada125..0xada13b)
board = getArchModel(arch); ps = *(*(*(board+0x8))+0x10) + 0x20); NUM_BANKS = *(ps+0x00);
//     Board+0x8 → Device+0x10 → Core+0x20(psumbuf) → Psumbuf+0x00 = numBanks

// (3) HBM window — HBMUsage::run (libwalrus 0x16ba31f..0x16ba32a)
board = getArchModel(arch); HBMLimit = (uint64)(*(*(*(board+0x8))+0x10)+0x30)) << 30;
//     Board+0x8 → Device+0x10 → Core+0x30 = dramSizeGb (GiB), <<30 → bytes

// (4) Engine count — bir::getEngineCount (libBIR 0x47d851..0x47d908)
board = getArchModel(arch); core = *(*(board+0x8))+0x10);
count = *(core + {Unassigned:0x6c, PE:0x70, Pool:0x74, Act:0x78,
                  DVE:0x7c, DMA:0x80, SP:0x84, ALL:0x88}[engineType]);
//     Board+0x8 → Device+0x10 → Core+0x6c.. = per-engine count (jump-table on EngineType)
```

> **CORRECTION — the `getEngineCount` offset/engine map (supersedes a prior mis-pairing).** An earlier reading paired the count offsets as `{Pool:0x6c, Act:0x74, PE:0x78, DMA:0x84, ALL:0x88}`. The actual `getEngineCount` jump table (`libBIR 0x47d820`, the per-`EngineType` `mov eax,[rbx+off]` arms) pairs them differently: `Unassigned→0x6c` (`@0x47d8d8`), `PE→0x70` (`@0x47d920`), `Pool→0x74` (`@0x47d8f0`), `Act→0x78` (`@0x47d908`), `DVE→0x7c` (`@0x47d950`), `DMA→0x80` (`@0x47d938`), `SP→0x84` (`@0x47d8a8`), `ALL→0x88` (`@0x47d8c0`). The struct order of the count block is PE, Pool, Act, DVE, DMA, SP, ALL (`0x70`…`0x88`) with `Unassigned` at `0x6c` just below — matching the `Core+0x6c`…`Core+0x88` scalar tail in [§The Core layout](#core). [CONFIRMED — `getEngineCount @0x47d820` jump-table arms]

Read-site (3) is the per-core HBM budget. `HBMUsage::run(vector<...>)` (`libwalrus 0x16b94b0`) reads `Core+0x30`, shifts left 30 (`shl $0x1e`) to get bytes, multiplies the limit by a soft `1.1×` margin (the IEEE-754 double `0x3FF199999999999A`=1.1 at `.rodata 0x1dbf5b0`, confirmed `9a999999 9999f13f`), and warns if estimated usage exceeds `1.1×HBMLimit` while a hard assert flags `usage > HBMLimit`. Gated by the `--hbm-usage-check` flag (`DisableHBMUsageCheck` cl::opt). The full DRAM allocator and split-DRAM machinery are owned by the [DRAM/HBM geometry page](dram-hbm-geometry.md).

> **NOTE — `getEngineCount` reads counts, not geometry.** Read-site (4) reaches `Core+0x6c`…`Core+0x88` — the per-engine *count* block (all =1 here: one Pool, one Act, one PE, … per NeuronCore), not the engine *geometry* sub-objects at `Core+0x00`…`Core+0x18`. Engine *multiplicity* (how many of each engine) and engine *geometry* (how wide each one is) are distinct axes living at distinct offsets. The `bir::EngineInfo` handle (`EngineInfo2string` @ `libBIR 0x47c310`) is itself an 8-byte `{EngineType, engineId}` pair with **no** geometry — geometry is keyed by `EngineType` ordinal through the `Core` sub-object table, never indexed off an `EngineInfo`.

### The EngineType ordinal map

The `EngineType` ordinal is the key every engine page binds to. It is an 8-value enum, byte-pinned from `bir::EngineType2string` (`libBIR 0x47fa80`, internal names) and `bir::EngineType2ExternalName` (`libBIR 0x47fca0`, external/profile names), cross-confirmed by the `getEngineCount` jump table (`0x47d820`) and the `getValidEngines` per-op legality maps. Both name tables decode from inline `mov`-immediate literals (e.g. case 4 = `0x4D44`+`A` = "DMA", case 5 = `0x5644`+`E` = "DVE", external case 4 = `0x636E7953`+… = "SyncDMA"); the default arm throws `runtime_error("Unknown EngineType '<n>'")`.

| Ordinal | Internal (`EngineType2string`) | External (`EngineType2ExternalName`) | Role | Datapath? (`0x6E`) |
|---|---|---|---|---|
| 0 | `Unassigned` | `Unassigned` | pseudo / ctor default | no |
| 1 | `Pool` | `GPSIMD` | pooling / reduce leg (the GPSIMD external alias) | **yes** |
| 2 | `Activation` | `Scalar` | scalar / activation (PWP LUT) | **yes** |
| 3 | `PE` | `Tensor` | systolic matmul array | **yes** |
| 4 | `DMA` | `SyncDMA` | DMA engine | no |
| 5 | `DVE` | `Vector` | data-movement / vector | **yes** |
| 6 | `SP` | `Sync` | sync / control sequencer | **yes** |
| 7 | `ALL` | `All` | pseudo / all-engine barrier | no |

*Confidence: **CONFIRMED** — internal and external names byte-read off both `2string` bodies, ordinals cross-confirmed three ways (`EngineType2string`, `getEngineCount`, `getValidEngines`).* The datapath column is the `bir::isDataPathEngine` mask `0x6E` = `{1,2,3,5,6}` — the five engines an all-engine barrier fans out across ([execution & sync model](execution-sync-model.md)); ordinals 0 (Unassigned), 4 (DMA), 7 (ALL) are excluded. The internal→external alias `Pool→GPSIMD` is the source of the "GPSIMD is not a separate engine" finding ([GPSIMD engine](gpsimd-engine.md)). Every engine page (1.08–1.14) keys to this map: PE=3, Pool=1, Activation=2, DVE=5, SP=6, DMA=4.

### Function Map

| Symbol | Binary | Address | Terminal hop | Reads | Confidence |
|---|---|---|---|---|---|
| `SB_Allocator::SB_Allocator` | libwalrus | `0xa97750` (read @`0xa97b82`) | `Core+0x28` → `Statebuf+0x00` | SBUF bytes/partition | CONFIRMED |
| `PSUM_Allocator::PSUM_Allocator` | libwalrus | `0xad9970` (read @`0xada120`) | `Core+0x20` → `Psumbuf+0x00` | PSUM bank count | CONFIRMED |
| `HBMUsage::run(vector<...>&)` | libwalrus | `0x16b94b0` (read @`0x16ba2f7`) | `Core+0x30` | HBM window (GiB) | CONFIRMED |
| `bir::getEngineCount(EngineType,ArchLevel)` | libBIR | `0x47d820` | `Core+0x6c..0x88` | Per-engine count | CONFIRMED |

---

## The dual-source invariant

### Purpose

The single most important reason to trust this layout is that two independently-compiled binaries carry it identically. This section makes the cross-confirmation explicit so a reimplementer knows the layout is not a one-binary artifact.

### The two trees

`libwalrus.so` (the backend) and `libBIR.so` (the IR library) each define the full `getArchModel`/`Board`/`Device`/`Core` family, with distinct entry-point addresses and distinct PLT thunks, but **byte-identical** layout and immediates:

| Element | libwalrus (`92b4d331…`) | libBIR (`a9b1ea38…`) | Agreement |
|---|---|---|---|
| `getArchModel(string)` | `0x17344c0` | `0x478f90` | same dispatch shape, ends in `__assert_fail` |
| `Board+0x8` → Device hop | `mov 0x8(%rax),%rax` | `mov 0x8(%rax),%rax` (`0x47d851`) | identical |
| `Device+0x10` → Core hop | `mov 0x10(%rax),%rax` | `mov 0x10(%rax),%rbx` (`0x47d85c`) | identical offset |
| `Statebuf::Statebuf` | `0x17341e0` (6-arg) | `0x478cb0` (6-arg) | same signature |
| `Psumbuf::Psumbuf` | `0x17341b0` (3-arg) | `0x478c80` (3-arg) | same signature |
| Sunda `Statebuf` immediates | `esi=0x30000, ecx=0x80, r8d=0x40, push=0x4000` | `esi=0x30000, ecx=0x80, r8d=0x40, push=0x4000` (`0x4e1985`) | byte-identical |
| Sunda `Psumbuf` immediates | `esi=8, edx=0x80, ecx=0x800` | `esi=8, edx=0x80, ecx=0x800` (`0x4e195a`) | byte-identical |

The PLT thunks differ — `libwalrus`'s `Statebuf::Statebuf` thunk resolves through one slot, `libBIR`'s through another (`0x178c00`) — proving these are genuinely separate compilation units sharing the same `ctm` source, not the same object linked twice. The `libBIR getEngineCount` chain (`0x47d851` `Board+0x8`, `0x47d85c` `Device+0x10`, then `Core+0x6c`…) is the same `getArchModel → +0x8 → +0x10 → Core` walk that drives the `libwalrus` allocators.

> **NOTE — which binary owns which.** Both libraries carry the geometry tree, but the live consumers differ. `libwalrus` houses the allocators (`SB_Allocator`, `PSUM_Allocator`, `HBMUsage`) and the per-arch cost `Hwm` classes that read geometry during backend codegen. `libBIR` houses `getEngineCount`, `EngineInfo`, and the `ArchLevel*` codename mappers. When a constant must be cited, prefer the `libwalrus` address for backend-consumed geometry and the `libBIR` address for IR-level enums; either binary is authoritative for the layout itself, since they agree.

### Considerations

The per-arch immediates are byte-identical across both binaries for SBUF, PSUM, PE, Pool/Act, the `dramSizeGb` window, and the scalar ISA block. The one thing that is *not* carried in either compiler binary is the marketed NeuronCores-per-chip count and any HBM/ICI link topology beyond the per-core window — those are runtime concerns (the `ctm.cpython-*.so` `_<codename>_arch_model` objects are populated at runtime). The compiler's view is exactly the on-NeuronCore TPB resources: SBUF, PSUM, the compute engines, the semaphore count, and the per-core HBM window. A reimplementer's compiler does not need (and cannot statically obtain from this wheel) the chip-level core count.

---

## The per-arch constant matrix

The terminal values, read off the per-arch ctor immediates in both binaries. Every cell is CONFIRMED unless tagged. Generations: gen1 Inferentia/Tonga, gen2 Sunda/Trn1, gen3 Cayman/Trn2, gen4 CoreV4/Mariana/Trn3.

| Constant | Field (accessor terminal) | gen1 | gen2 | gen3 | gen4 |
|---|---|---|---|---|---|
| SBUF bytes/partition | `Statebuf+0x00` `partitionSize` | 96 KiB | 192 KiB | 224 KiB | 256 KiB |
| SBUF partitions | `Statebuf+0x08` `numPartitions` | 128 | 128 | 128 | 128 |
| SBUF reserved | `Statebuf+0x18` `reservedSize` | 16384 | 16384 | 16384 | 16384 |
| PSUM banks | `Psumbuf+0x00` `numBanks` | 4 | 8 | 8 | 8 |
| PSUM partitions | `Psumbuf+0x04` `numPartitions` | 64 | 128 | 128 | 128 |
| PSUM bank size | `Psumbuf+0x08` `partSize` | 2048 | 2048 | 2048 | 2048 |
| PE rows × cols | `PeDim+0x00`/`+0x04` | 128×64 | 128×128 | 128×128 | 128×128 |
| Pool/Act partitions | `Pool/Act+0x08` `numChannels` | 64 | 128 | 128 | 128 |
| PE reorder window | `Pe::getReorderWindowSize` | 64 | 64 | 64 | 64 |
| Act/Dve reorder window | `Act/Dve::getReorderWindowSize` | 8 | 8 | 8 | **16** |
| Per-core HBM | `Core+0x30` `dramSizeGb` | 16 GiB | 16 GiB | 24 GiB | 36 GiB |
| Per-device HBM | `Device+0x08` `dramSizeGb` | 32 GiB | 32 GiB | 24 GiB | 36 GiB |
| NeuronCores/device | `Device+0x00` `numCores` | 4 | 2 | 2 | 2 |
| Devices/board | `Board+0x00` `numDevices` | 2 | 2 | 2 | 2 |
| Semaphores/core | `Core+0x34` `NumSemaphores` | 256 | 256 | 256 | 256 |

The numeric values, their per-arch immediate addresses, and the cross-binary confirmation are owned in full by the [hardware constant matrix](hardware-constant-matrix.md); this row set is the orientation summary, included so a reader who has walked the chain can immediately see what each terminal offset yields.

---

## Cross-References

- [Hardware Constant Matrix](hardware-constant-matrix.md) — the full per-arch value table with every immediate address and the dual-binary confirmation, keyed off the offsets named here.
- [SBUF & PSUM Geometry](sbuf-psum-geometry.md) — the `Statebuf` and `Psumbuf` sub-objects in depth, the allocators that read `Core+0x28`/`Core+0x20`, and the MX dtype slot.
- [DRAM & HBM Geometry](dram-hbm-geometry.md) — `Core::dramSizeGb` (`Core+0x30`) consumed by `HBMUsage`, the split-DRAM passes, and why per-core ≠ per-device HBM.
- [The libwalrus Backend](../walrus/) — the allocators (`SB_Allocator`, `PSUM_Allocator`, coloring/linear-scan) and `Hwm` cost classes that walk this chain during codegen.
- [Methodology & the Confidence Model](../methodology.md) — the four-tier ladder and why a recovered `ctm.hpp` field name and a byte-pinned ctor immediate are both binary-derived evidence.
