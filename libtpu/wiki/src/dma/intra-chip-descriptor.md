# Intra-Chip DMA Descriptor

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`); `.data.rel.ro` carries a `0x200000` VMA→file delta. Other versions will differ.*

## Abstract

Every on-chip DMA a TensorCore sequencer issues — VMEM↔HBM staging, scalar memsets, the local leg of a collective, host infeed/outfeed — is described by one record: `OciDescriptorCommonIssuedFromTcs` ("OCI descriptor common, issued from the TensorCore Sequencer"). It is a 17-field, bit-packed structure whose two memory endpoints are each named by a `(mem_id, core_id, opcode)` triple, whose transfer class is a `dma_type` tag, and whose size is a `(length, length_granule)` pair. The same descriptor family is instantiated per silicon generation — `pxc` (Pufferfish/BarnaCore), `vfc`/`glc`/`gfc` (SparseCore), and `vlc` (no SparseCore) — and the per-generation classes differ only in the composite names attached to the memory-space enum values; the field layout, the opcode enums, and the `dma_type`/`length_granule` enums are identical across all of them.

This descriptor is the *intra-chip* counterpart of the cross-chip ICI wire descriptor: where the ICI builder (`JellyfishDmaDescriptorState` / `PufferfishDmaDescriptorState`) stages an 8- or 24-word array in SMEM and stamps a remote chip id and a remote-sync-flag address into it, the intra-chip descriptor never leaves the chip — both endpoints resolve to local tiers (HBM, VMEM, SMEM, CMEM, IMEM, BMEM, SPMEM). The reader who already understands a DMA engine's "source descriptor / destination descriptor / length / completion-flag" quartet will recognize the shape immediately; the two surprises this page documents are (1) the memory endpoint is *polymorphic* — a single 2-bit `mem_id` value resolves to a different physical tier depending on the companion 3-bit `core_id` — and (2) there are **two unrelated `DmaType` enumerations** in the binary, one carried by the LLO/runtime layer and one in the profiler descriptor, with different value counts and orderings.

This page owns three things: the `SrcMem`/`DstMem` memory-space enums (`MemMemId` + `MemCoreId`) and how a `(mem_id, core_id)` pair renders to a physical tier; the `Src`/`Dst` `Opcode` enums and the string-valued opcode attributes the LLO DMA ops actually carry; and the descriptor field layout (offsets `+0x18`..`+0x5c`) that the LLO DMA-start ops fill. The *Simple-vs-Strided* `DmaParameters` selector is on [DmaParameters Selector](dma-parameters-selector.md); the rolled/strided/general transfer-body emitters are on [Rolled / Strided / General Emitters](rolled-strided-general.md); the tile-index→flat-offset algebra that produces the address operands is on [Tile-Index Expansion](tile-index-expansion.md).

For reimplementation, the contract is:

- **The two memory-space enums** — the 4-value `MemMemId` and the 8-value `MemCoreId`, and the segment-selection rule that turns a `(mem_id, core_id)` pair into a named physical tier.
- **The two opcode enums** — `SrcOpcode {READ, …}` and `DstOpcode {WRITE, …}`, and the string-valued `dst_opcode` attribute (`write_4b` / `read_and_add` / `atomic_add`) the LLO `DmaSimpleStartOp` / `DmaGeneralStartOp` carry, plus the memory-space gates that restrict each opcode.
- **The endpoint rendering** — `MemorySpaceToDriverResource(MemorySpace)` (the LLO `MemorySpace` → driver-resource id used to stamp the descriptor address word) and how `(mem_id, core_id, opcode, addr)` compose into a source / destination endpoint.
- **The field layout** — the 17 fields at `+0x18`..`+0x5c`, the `(length, length_granule)` size pair, and which fields the LLO DMA-start ops write vs. which the profiler descriptor parses.

| | |
|---|---|
| **Descriptor (profiler/wire)** | `asic_sw::driver::deepsea::<gen>::profiler::OciDescriptorCommonIssuedFromTcs` |
| **pxc ctor** | `…pxc::profiler::OciDescriptorCommonIssuedFromTcs::OciDescriptorCommonIssuedFromTcs` @ `0x1cf1b620` |
| **Per-gen decode** | `…<gen>::profiler::DecodeOciDescriptorCommonIssuedFromTcs` (pxc @ `0xf5bace0`, vlc @ `0xf5e29a0`, vfc @ `0xf607b20`, glc @ `0xf63a140`, gfc @ `0xf66fba0`) |
| **Per-gen encode** | `…<gen>::profiler::EncodeOciDescriptorCommonIssuedFromTcs` (pxc @ `0xf5cc700`) |
| **Endpoint render** | `xla::jellyfish::MemorySpaceToDriverResource(MemorySpace)` @ `0x1d6223e0` |
| **LLO dst-opcode reader** | `xla::tpu::sparse_core::GetDstOpcode<DmaSimpleStartOp>` @ `0x135aaa60`, `<DmaGeneralStartOp>` @ `0x135b2be0` |
| **Runtime `DmaType` enum** | `xla::jellyfish::DmaType` — 3 values (`<<` operator @ `0x1d5ae080`) |
| **Field span** | `+0x18` (trace_id_header) … `+0x5c` (length_granule); size word at `+0x58`/`+0x5c` |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile + the FDP descriptor pool |

---

## 1. The Two Memory-Space Enums (`SrcMem` / `DstMem`)

### Purpose

A DMA endpoint is *not* named by a single memory-space integer. The descriptor splits each endpoint's space into two fields — a 2-bit `mem_id` (`src_mem_mem_id` @ `+0x24`, `dst_mem_mem_id` @ `+0x30`) and a 3-bit `core_id` (`src_mem_core_id` @ `+0x28`, `dst_mem_core_id` @ `+0x34`). The pair is *polymorphic*: the same `mem_id` value resolves to a different physical tier depending on which core class the companion `core_id` selects. This is how four 2-bit codes cover the full HBM / VMEM / SMEM / IMEM / CMEM / BMEM / SPMEM tier space without a wider field.

### Encoding

The two enums, byte-verified from the FDP descriptor pool (the `enum_type` blocks of the carved `OciDescriptorCommonIssuedFromTcs` `DescriptorProto`), are identical between source (`SRC_*`) and destination (`DST_*`):

| `MemMemId` (2-bit) | pxc composite name (`SRC_MEM_MEM_ID_*`) | Confidence |
|---:|---|---|
| 0 | `HBM_TCVMEM_BCBMEM` | CONFIRMED |
| 1 | `RSVD_TCSMEM_BCSMEM` | CONFIRMED |
| 2 | `CMEM_TCIMEM_BCBIMEM` | CONFIRMED |
| 3 | `RSVD_RSVD_BCVIMEM` | CONFIRMED |

| `MemCoreId` (3-bit) | name (`SRC_MEM_CORE_ID_*`) | selects segment | Confidence |
|---:|---|---|---|
| 0 | `RESERVED` | — | CONFIRMED |
| 1 | `NONCORE` | 1st (`HBM` / `CMEM` / …) | CONFIRMED |
| 2 | `TC0` | 2nd (`TCVMEM` / `TCSMEM` / `TCIMEM`) | CONFIRMED |
| 3 | `TC1` | 2nd | CONFIRMED |
| 4 | `BC0` | 3rd (`BCBMEM` / `BCSMEM` / `BCBIMEM`) | CONFIRMED |
| 5 | `BC1` | 3rd | CONFIRMED |
| 6 | `BC2` | 3rd | CONFIRMED |
| 7 | `BC3` | 3rd | CONFIRMED |

The composite name is a `_`-joined triple `<noncore>_<tensorcore>_<thirdcore>`. To resolve a `(mem_id, core_id)` pair to a physical tier, pick the `mem_id` row, then select the segment named by the `core_id` class:

```text
mem_id = 0, core_id = NONCORE  ->  HBM      (HBM_TCVMEM_BCBMEM, 1st segment)
mem_id = 0, core_id = TC0/TC1  ->  TCVMEM   (2nd segment)
mem_id = 0, core_id = BC0..3   ->  BCBMEM   (3rd segment)
mem_id = 2, core_id = NONCORE  ->  CMEM
mem_id = 2, core_id = TC0      ->  TC IMEM
mem_id = 1, core_id = TC0      ->  TC SMEM
```

> **NOTE —** the segment-selection rule (NONCORE→1st, TC*→2nd, BC*/SC*→3rd) is **INFERRED** from the composite-name structure, not byte-proven from a renderer: no `MemMemId × MemCoreId → tier` symbolizer is linked into `libtpu.so`. The decode functions (`DecodeOciDescriptorCommonIssuedFromTcs`) parse `mem_id`/`core_id` into the proto, but the only intra-chip DMA-timeline pass that consumes the descriptor (`ConvertDmaTransfersToXPlane` @ `0xf254bc0`) **drops both endpoints** — see [§5](#5-what-the-renderer-keeps-vs-drops). The `mem_id`/`core_id` value→name bindings are CONFIRMED; the tier resolution they imply is INFERRED.

### The cross-generation rename

The `MemMemId` *value set* is fixed at four; only the composite names change per generation, tracking each gen's third core class:

| Gen / family | `mem_id=0` | `mem_id=1` | `mem_id=2` | `mem_id=3` |
|---|---|---|---|---|
| `pxc` (BarnaCore) | `HBM_TCVMEM_BCBMEM` | `RSVD_TCSMEM_BCSMEM` | `CMEM_TCIMEM_BCBIMEM` | `RSVD_RSVD_BCVIMEM` |
| `vfc`/`glc`/`gfc` (SparseCore) | `HBM_TCVMEM_SCSPMEM` | `HOST_TCSMEM_SCSMEM` | `VMEMALL_TCIMEM_SCSIMEM` | `NONCORERESERVEDMEM0_TCRESERVEDMEM_SCTIMEM` |
| `vlc` (2-name, no SparseCore) | `HBM_TCVMEM` | `HOST_TCSMEM` | `NONCORERESERVEDMEM0_TCIMEM` | `NONCORERESERVEDMEM0_TCRESERVEDMEM` |

`vlc` names are 2-segment (no third core), so its `core_id` only meaningfully selects NONCORE vs TC*. All five gens declare the `OciDescriptorCommonIssuedFromTcs` class with the same field layout and the same 4-value `mem_id` enum — confirmed by the five per-gen `Decode…` / class-data symbols (`…vfc…OciDescriptorCommonIssuedFromTcs::GetClassData` @ `0x1cf23e40`, `…vlc…` @ `0x1cf2cf80`, etc.).

### The LLO-side `MemorySpace`

The descriptor's `(mem_id, core_id)` pair is the *wire/profiler* view of the endpoint. Inside the compiler, an LLO DMA operand carries an `xla::jellyfish::MemorySpace` — the 17-value runtime enum documented on [MemorySpace Enum](../isa/memory-space-enum.md). The descriptor builder turns that `MemorySpace` into a hardware endpoint via `MemorySpaceToDriverResource` ([§3](#3-endpoint-rendering)); the profiler descriptor's `mem_id`/`core_id` is the post-hardware-encoding readout of the same endpoint. A reimplementer carries `MemorySpace` end to end and maps to `(mem_id, core_id)` only at the descriptor boundary; do not confuse the 17-value LLO enum with the 4-value `mem_id`.

---

## 2. The Opcode Enums (`SrcOpcode` / `DstOpcode`)

### Purpose

Each endpoint carries a 2-bit opcode (`src_opcode` @ `+0x2c`, `dst_opcode` @ `+0x38`) that selects what the DMA engine *does* at that endpoint, beyond plain move. The source opcode chooses between a normal read and the two memset modes; the destination opcode chooses between a plain write and the two special-write / atomic modes.

### Encoding

Both enums are 2-bit, byte-verified from the FDP `enum_type` blocks:

| `SrcOpcode` (`+0x2c`) | name | meaning | Confidence |
|---:|---|---|---|
| 0 | `READ` | normal source read | CONFIRMED |
| 1 | `RESERVED` | — | CONFIRMED |
| 2 | `INSTRUCTIONMEMSET` | fill IMEM (no source read) | CONFIRMED |
| 3 | `DATAMEMSET` | fill data memory (no source read) | CONFIRMED |

| `DstOpcode` (`+0x38`) | name | meaning | Confidence |
|---:|---|---|---|
| 0 | `WRITE` | normal destination write | CONFIRMED |
| 1 | `RESERVED` | — | CONFIRMED |
| 2 | `WRITESPECIAL0` | special write mode 0 | CONFIRMED |
| 3 | `WRITESPECIAL1` | special write mode 1 | CONFIRMED |

### The LLO-side string opcode

The LLO SparseCore DMA-start ops carry the destination opcode as a **string-valued MLIR attribute**, not the 2-bit code; the lowering maps the string to the enum. `GetDstOpcode<DmaSimpleStartOp>` @ `0x135aaa60` (and the `DmaGeneralStartOp` twin @ `0x135b2be0`, byte-identical) compare the attribute's bytes against fixed little-endian constants:

```c
// xla::tpu::sparse_core::GetDstOpcode<DmaSimpleStartOp>   sub_135AAA60
function GetDstOpcode(op, ms):
    s = op.getDstOpcode()                       // string attr @ this+0x48 (sub_145B9700)
    if  len(s)==8  && s == "write_4b":          // 0x62345F6574697277 LE -> "write_4b"
        code = 1
    elif len(s)==16 && s == <16-char write attr>:  // vptest vs xmmword_A2D00C0
        code = 2
    elif len(s)==12 && s == "read_and_add":     // 0x646E615F64616572 | 0x6464615F
        code = 3
    elif len(s)==10 && s == "atomic_add":       // 0x615F63696D6F7461 | 0x6464
        code = atomic-add path
    else:
        code = 0                                // default: plain "write"
    // memory-space gates (emitError on violation):
    if  code != default && ms != Smem:
        error("dst_opcode is only supported for Smem.")
    if  atomic_add && ms != Spmem:
        error("Atomic add dst_opcode is only supported for Spmem.")
    return code
```

The string→code table the LLO ops emit:

| LLO `dst_opcode` string | Length | Maps to `DstOpcode` | MS gate | Confidence |
|---|---:|---|---|---|
| (absent) | 0 | `WRITE` (0) | — | HIGH |
| `write_4b` | 8 | `WRITESPECIAL0`/1 | `Smem` only | CONFIRMED |
| (16-char write attr) | 16 | special write | `Smem` only | HIGH |
| `read_and_add` | 12 | special / atomic | `Smem` only | CONFIRMED |
| `atomic_add` | 10 | atomic-add (element-typed) | `Spmem` only | CONFIRMED |

> **GOTCHA —** the opcode is a string at the LLO/MLIR layer and a 2-bit enum on the wire. A reimplementer that drives the descriptor straight off the 2-bit enum misses the *gating*: the non-default opcodes are rejected unless the destination memory space is `Smem` (or `Spmem` for `atomic_add`). The `getDstOpcode` accessor reads the attribute at `this+0x48` with a bit-test on the property word (`(*(this+44) >> 19) & 0x10`), so the attribute is optional — absent means the plain `WRITE` opcode. The 16-char variant's exact spelling lives in `xmmword_A2D00C0` and was not string-decoded (HIGH, not CONFIRMED).

The `atomic_add` branch additionally inspects the destination memref's element type (`isF32` → code 1, `isBF16` → 2, `Float8E4M3FN` → 3, else "Unsupported element type for atomic add."), so the atomic-add opcode is further specialized by element width inside the Spmem path.

---

## 3. Endpoint Rendering

### Purpose

"Rendering" an endpoint means turning an LLO `(MemorySpace, byte-offset)` operand into the descriptor's address word(s). The memory space becomes a *driver resource id* placed in the address word's high field; the offset is scaled to the address granule and OR'd in. The same render path serves source and destination; the descriptor's `mem_id`/`core_id` fields are the hardware's readback of this encoding.

### Algorithm

The space→resource mapping is `MemorySpaceToDriverResource`, a dense switch over the **LLO `MemorySpace` enum** (the 17-value runtime enum, *not* the proto). It is the single byte-anchored ground truth for which tiers are DMA-addressable and what resource id each gets:

```c
// xla::jellyfish::MemorySpaceToDriverResource(MemorySpace ms)   sub_1D6223E0
function MemorySpaceToDriverResource(ms):
    switch ms:                       // ms = the 17-value LLO MemorySpace enum
        case 0  (<no memory space>):  return 10
        case 1  (hbm):                return 2
        case 2  (hib):                return 3
        case 3  (vmem):               return 4
        case 4  (cmem):               FATAL("Unsupported memory space")   // memory_space.cc:31
        case 5  (smem):               return 6
        case 6  (sflag):              return 0
        case 7  (imem):               return 5
        case 8  (barna_core_bmem):    return 7
        case 9  (barna_core_smem):    return 9
        case 10 (barna_core_sflag):   return 1
        case 11 (barna_core_imem):    return 8
        case 12..16:                  FATAL("Unsupported memory space")   // memory_space.cc:49
```

The returned resource id is stamped into the descriptor's address word at bit 40 (`SetSourceAddress(resource << 0x28)` in the ICI builder ctor; the same `<< 0x28` shift renders the intra-chip endpoint), and the within-tier offset is OR'd into the low bits after granule scaling (`EncodeDmaAddressForGranule` @ `0x1d5402c0`, which additionally OR's bit 31 = `0x80000000` for HBM/external-resource operands as the external-address marker).

> **CORRECTION (DMA-1) —** a sibling analysis (the ICI cross-chip descriptor work) summarized this map as "hbm→3, vmem→9, smem→6, sflag→0xa". Decompilation of `MemorySpaceToDriverResource` @ `0x1d6223e0` shows the actual returns are **hbm→2, hib→3, vmem→4, smem→6, sflag→0** (and `cmem` is a hard `FATAL`, never DMA-addressable here). The `smem→6` value is the only one that matched. The earlier "hbm→3, vmem→9, sflag→0xa" figures are the *resource ids of the wrong spaces* (hib→3, barna_core_smem→9, …) — a one-row shift, the classic off-by-one of reading the wrong switch arm. Use the switch above.

### The resource-id table

| LLO `MemorySpace` | Enum# | Driver resource id | Confidence |
|---|---:|---:|---|
| `<no memory space>` | 0 | 10 | CONFIRMED |
| `hbm` | 1 | 2 | CONFIRMED |
| `hib` | 2 | 3 | CONFIRMED |
| `vmem` | 3 | 4 | CONFIRMED |
| `cmem` | 4 | — (FATAL) | CONFIRMED |
| `smem` | 5 | 6 | CONFIRMED |
| `sflag` | 6 | 0 | CONFIRMED |
| `imem` | 7 | 5 | CONFIRMED |
| `barna_core_bmem` | 8 | 7 | CONFIRMED |
| `barna_core_smem` | 9 | 9 | CONFIRMED |
| `barna_core_sflag` | 10 | 1 | CONFIRMED |
| `barna_core_imem` | 11 | 8 | CONFIRMED |
| `sparse_core_*` (12..16) | 12..16 | — (FATAL) | CONFIRMED |

> **QUIRK —** the resource-id assignment is *not* the identity of the `MemorySpace` enum, and it is *not* monotone: `sflag`→0, `barna_core_sflag`→1, `hbm`→2, `hib`→3, `vmem`→4, `imem`→5, `smem`→6, `barna_core_bmem`→7, `barna_core_imem`→8, `barna_core_smem`→9, NONE→10. A reimplementer must use the explicit table; deriving the resource id from the space integer is wrong for every row. The `cmem` and `sparse_core_*` cases trap at `LogMessageFatal` — those spaces are addressable by LLO loads/stores but **not** as a DMA endpoint on this gen's resource model, so a DMA targeting them is a compile-time fatal, not a silent miss.

---

## 4. The Descriptor Field Layout

### Purpose

`OciDescriptorCommonIssuedFromTcs` is a 17-field, bit-packed record. The fields the LLO DMA-start ops fill, and the offsets the per-gen `Decode…` functions read back, are fixed across all generations.

### Layout

Field offsets are byte-verified from the carved `DescriptorProto` (the FDP message body) and the pxc producer store. The structure is bit-packed — these are the *parsed-into-proto* member offsets, not raw bit positions:

| # | Field | Offset | Width / enum | Filled by | Confidence |
|---:|---|---:|---|---|---|
| 1 | `trace_id_header` | `+0x18` | u64 | DMA-id producer (pairing key) | CONFIRMED |
| 2 | `dma_type` | `+0x20` | `DmaTypeValues` (4) | DMA lowering | CONFIRMED |
| 3 | `src_mem_mem_id` | `+0x24` | `MemMemId` (2-bit) | source endpoint render | CONFIRMED |
| 4 | `src_mem_core_id` | `+0x28` | `MemCoreId` (3-bit) | source endpoint render | CONFIRMED |
| 5 | `src_opcode` | `+0x2c` | `SrcOpcode` (2-bit) | source opcode | CONFIRMED |
| 6 | `dst_mem_mem_id` | `+0x30` | `MemMemId` (2-bit) | dest endpoint render | CONFIRMED |
| 7 | `dst_mem_core_id` | `+0x34` | `MemCoreId` (3-bit) | dest endpoint render | CONFIRMED |
| 8 | `dst_opcode` | `+0x38` | `DstOpcode` (2-bit) | `GetDstOpcode<>` | CONFIRMED |
| 9 | `src_sync_flag_id` | `+0x3c` | u32 | sync-flag binding | CONFIRMED |
| 10 | `src_sync_flag_core_id` | `+0x40` | `SyncFlagCoreId` (8) | sync-flag binding | CONFIRMED |
| 11 | `dst_sync_flag_0_id` | `+0x44` | u32 | completion flag 0 | CONFIRMED |
| 12 | `dst_sync_flag_0_core_id` | `+0x48` | `SyncFlagCoreId` (8) | completion flag 0 | CONFIRMED |
| 13 | `dst_sync_flag_1_id` | `+0x4c` | u32 | completion flag 1 | CONFIRMED |
| 14 | `dst_sync_flag_1_core_id` | `+0x50` | `SyncFlagCoreId` (8) | completion flag 1 | CONFIRMED |
| 15 | `program_counter` | `+0x54` | u32 | trace/PC | CONFIRMED |
| 16 | `length` | `+0x58` | u32 | size operand | CONFIRMED |
| 17 | `length_granule` | `+0x5c` | `LengthGranule` (1-bit) | size operand | CONFIRMED |

The three `SyncFlagCoreId` enums (`+0x40`, `+0x48`, `+0x50`) share the same 8-value set as `MemCoreId` (`RESERVED`/`NONCORE`/`TC0`/`TC1`/`BC0`..`BC3`) — the sync-flag target core mirrors the memory-target core. The dual `dst_sync_flag_{0,1}` pair is the dual-channel completion that the V2 ICI descriptor also exposes (via `set_dst_sync_flag_mem_offset(idx 0..1)`); on the intra-chip descriptor both are present in the layout.

### The size pair (`length` × `length_granule`)

The transfer byte count is `length << granule_shift`, where the shift is chosen by the 1-bit `length_granule`:

| `LengthGranule` (`+0x5c`) | name | shift | bytes per unit | Confidence |
|---:|---|---:|---:|---|
| 0 | `512B` | `<< 9` | 512 | CONFIRMED |
| 1 | `4B` | `<< 2` | 4 | CONFIRMED |

The pxc DMA-timeline producer (the `ConvertTpuTraceToXPlane<pxc>` lambda, store block @ `0xf26c865`) is the byte-exact proof of the size decode and of *which* fields survive into a rendered span:

```c
// producer id-91 store   @0xf26c865
span.begin_gtc     = begin_gtc;            // [r15+0x8]
span.begin_present = 1;                    // [r15+0x10]
shift  = (descr.length_granule == 0) ? 9 : 2;   // cmp [r14+0x5c],0 ; mov ecx,2 ; mov eax,9 ; cmove
bytes  = descr.length << shift;            // eax = [r14+0x58] ; shl rax, cl
span.byte_count    = bytes;                // [r15+0x28]
span.kind_tag      = 3;                    // [r15+0x40]
// NO read of descr[+0x24 .. +0x50]  -> src/dst endpoints + sync flags are NOT copied
```

### How LLO ops fill it

The descriptor fields are populated from an LLO `Dma*StartOp` op's operands and attributes ([Tile-Index Expansion](tile-index-expansion.md) produces the address operands; [DmaParameters Selector](dma-parameters-selector.md) chooses the Simple vs Strided op shape):

- `src_mem_*` / `dst_mem_*` ← the source / destination operand's `MemorySpace`, rendered via `MemorySpaceToDriverResource` ([§3](#3-endpoint-rendering)).
- `dst_opcode` ← the op's string `dst_opcode` attribute via `GetDstOpcode<>` ([§2](#2-the-opcode-enums-srcopcode--dstopcode)); `src_opcode` defaults to `READ` unless a memset op selects `INSTRUCTIONMEMSET`/`DATAMEMSET`.
- `length` / `length_granule` ← the size operand, with the granule chosen so the byte count fits the size field (the ICI builder's `WriteSize` enforces `granule_bytes <= 1024`, i.e. a 512-B granule caps a single descriptor's size word at the 10-bit-equivalent field).
- `dst_sync_flag_*` ← the completion sync flag the DMA bumps when the last byte lands (the on-chip analogue of the ICI receive-side auto-increment).
- `enable_trace` ← the op's `getEnableTrace` attribute (`DmaSimpleStartOp::getEnableTrace` @ `0x145b9620`), which toggles whether the descriptor emits a profiler trace entry.

---

## 5. What the Renderer Keeps vs Drops

The single intra-chip DMA-timeline pass, `xprof::tpu::ConvertDmaTransfersToXPlane` @ `0xf254bc0`, renders one paired DMA into one device `XEvent`. It is the only consumer that reads the descriptor for display, and it **decodes the endpoints into the proto but never copies them into the rendered span**. The eight stats it attaches per span are:

| XStat | StatType | Source | Confidence |
|---|---:|---|---|
| `device_offset_ps` | 147 | `round(begin_gtc & ~0xf · 1e9 / (clk<<4))` | CONFIRMED |
| `device_duration_ps` | 148 | `round((end−begin)&~0xf · 1e9 / (clk<<4))` | CONFIRMED |
| `bytes_transferred` | 78 | `length << granule_shift` (`+0x58`/`+0x5c`) | CONFIRMED |
| `queue` | 79 | string @ span`+0x28` — **empty on pxc** | CONFIRMED |
| `details` | (string) | std::string @ span`+0x40` — **empty on pxc** | CONFIRMED |
| `_a` | 42 | constant 1 (per-DMA aggregate marker) | CONFIRMED |
| `flow` | 56 | `XFlow::next_flow_id_` (begin↔end arrow) | CONFIRMED |
| `bandwidth` | (string) | `StrFormat(unit, bytes/(dur_ps/1e12))`, 5-rung B/KB/MB/GB/TB ladder | CONFIRMED |

> **GOTCHA —** the `src_mem_*` / `dst_mem_*` / `*_opcode` / `*_sync_flag_*` fields (descriptor `+0x24`..`+0x50`) are parsed by `DecodeOciDescriptorCommonIssuedFromTcs` but the rendering pass reads **only** `length` (`+0x58`), `length_granule` (`+0x5c`), and the `trace_id_header` (`+0x18`, for begin/end pairing). The `queue` and `details` string stats are attached on every span but their backing strings are zero-initialized and never written by the pxc producer, so a captured pxc trace shows them empty. An `nm -C` scan finds **no** `MemMemId/MemCoreId → tier` or endpoint→stat symbolizer in `libtpu.so`. So the endpoint enums of [§1](#1-the-two-memory-space-enums-srcmem--dstmem) / [§2](#2-the-opcode-enums-srcopcode--dstopcode) are fully decodable but have no display renderer inside this unit — a downstream xprof/TensorBoard symbolizer, if any, would have to re-read the proto fields the pass discards.

---

## 6. The `DmaType` Enums — Two Unrelated Enumerations

The transfer class appears under the name `DmaType` in **two** places with **different value sets**, and conflating them is a trap.

The **runtime / LLO** enum `xla::jellyfish::DmaType` is recovered from its `absl` log operator (`operator<<<DmaType>` @ `0x1d5ae080`) — a dense 3-value switch:

| `xla::jellyfish::DmaType` | Value | Confidence |
|---|---:|---|
| `DMA_TYPE_CHIP_TO_HOST` | 0 | CONFIRMED |
| `DMA_TYPE_LOCAL_OR_HOST` | 1 | CONFIRMED |
| `DMA_TYPE_REMOTE_WRITE_UNICAST` | 2 | CONFIRMED |

The **profiler descriptor** enum `DmaTypeValues` (`dma_type` @ `+0x20`) is a 4-value set with a *different ordering*:

| `DmaTypeValues` (pxc) | Value | Confidence |
|---|---:|---|
| `DMA_TYPE_LOCAL` | 0 | CONFIRMED |
| `DMA_TYPE_CHIP2HOST` | 1 | CONFIRMED |
| `DMA_TYPE_REMOTEUNICAST` | 2 | CONFIRMED |
| `DMA_TYPE_REMOTEMULTICAST` | 3 | CONFIRMED |

On the SparseCore gens (`vfc`/`vlc`/`glc`/`gfc`) the descriptor `DmaTypeValues` collapses to two values `{LOCALORHOST=0, REMOTEUNICAST=1}`. The runtime layer additionally names a wider set of `.rodata` strings (`DMA_TYPE_LOCAL`, `DMA_TYPE_LOCAL_OR_HOST`, `DMA_TYPE_CHIP_TO_HOST`, `DMA_TYPE_REMOTE_UNICAST`, `DMA_TYPE_REMOTE_WRITE_UNICAST`, `DMA_TYPE_REMOTE_MULTICAST`) selected by `BuildDmaOverrides(srcMS, dstMS, isRemote, DmaType, …)`.

> **QUIRK —** `DMA_TYPE_LOCAL` is the value `0` of the *profiler* enum (the intra-chip case) but is **not** a value of the 3-value runtime `xla::jellyfish::DmaType` operator (which starts at `DMA_TYPE_CHIP_TO_HOST=0`). The two enums share spelling fragments but disagree on count, order, and the value of `0`. A reimplementer must keep them in separate namespaces: the LLO/runtime `DmaType` drives the `BuildDmaOverrides` registry that picks the control-word override; the descriptor `DmaTypeValues` is the wire/profiler tag the hardware emits. For the intra-chip descriptor this page owns, the relevant `dma_type` is the profiler `DMA_TYPE_LOCAL=0`.

---

## Cross-References

- [DmaParameters Selector](dma-parameters-selector.md) — the Simple-vs-SingleStrided op-shape selector and dim-coalescing that decides which LLO DMA-start op fills this descriptor
- [Rolled / Strided / General Emitters](rolled-strided-general.md) — the transfer-body emitters that issue the descriptor once its fields are filled
- [Tile-Index Expansion](tile-index-expansion.md) — `ExpandTiledMemRefs` / `expandTiledIndices`, which produces the address operands rendered into the source/destination endpoints
- [MemorySpace Enum](../isa/memory-space-enum.md) — the 17-value LLO `MemorySpace` enum whose values `MemorySpaceToDriverResource` maps to driver resource ids
- [Memory-Load Slot](../isa/slot-memory-load.md) / [Memory-Store Slot](../isa/slot-memory-store.md) — the VLIW slots that encode a `MemorySpace` tag for the operands a DMA endpoint resolves
- [Host↔Device DMA](host-device-dma.md) — the `DeriveHostDmaTransfers` / tag-6/7 host path (`DMA_TYPE_CHIP_TO_HOST` / `DMA_TYPE_LOCAL_OR_HOST`)
- [OCI Command DMA-ID](oci-command-dma-id.md) — the `trace_id_header` (`+0x18`) DMA-id that pairs a descriptor's begin/end trace points
- [The net_router Emitter Pipeline](../routing/net-router-pipeline.md) — the collective router whose local leg issues intra-chip descriptors; the cross-chip remote-endpoint encoding is its counterpart
- [Address-Space IDs](../targets/address-space-ids.md) — the SparseCore `mlir::sparse_core::MemorySpace` AS-ID enum, the unrelated sibling numbering at the SC boundary
