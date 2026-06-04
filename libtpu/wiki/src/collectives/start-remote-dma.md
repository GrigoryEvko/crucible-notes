# StartRemoteDma

> *Every address, offset, operand index, and string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. Addresses are the binary's own VMA (text/rodata VMA == file offset; `.data.rel.ro` file offset = VMA − `0x200000`).*

## Abstract

A SparseCore-offloaded all-to-all needs to launch a *remote* DMA: write a local source buffer into a peer chip's buffer, on a peer SparseCore, somewhere across the slice. `libtpu` does this with one driver — `OffloadFactory::StartRemoteDma` (`0x133EBCC0`) — which is the all-to-all collective's per-target transfer launcher. This page documents the whole *StartRemoteDma launch* unit: the descriptor the driver assembles, the `SubsliceToFullSliceGlobalCoreId` producer that turns a subslice-local core id into the flat global core id the remote half consumes, and the headline negative result about the cross-chip pointer — **the remote target is *not* bit-packed into the DMA pointer; it rides the global core id as an operand.**

The transfer the driver emits is the dense [`get_remote_memref`](get-remote-memref.md) resolution plus a `sc_tpu.dma_general_start`. `StartRemoteDma`'s job is the *coordinate plumbing* around that emit: it (1) maps the per-target subslice core id to a flat global core id (`SubsliceToFullSliceGlobalCoreId`, `0x133E7900`), (2) feeds that global id into `GetRemoteMemBase` as the `remoteCoreId` operand — closing the all-to-all feed of `get_remote_memref` (the sibling of the `EnqueueDMA` path's `getRemoteDeviceAndSparseCoreIds`), (3) computes a megacore-aware destination *core index* (`ComputeRemoteCoreIndex`) and a destination *chip id* (`GlobalCoreIdToPhysicalChipId` → `SubsliceToFullSlice`), and (4) lowers the optional strides and emits `DmaGeneralStartOp::create`. The global-core-id arithmetic is closed-form: `ToGlobalCoreId(chip, localCore) = chip × SparseCoresPerLogicalDevice + localCore`, and its inverse `GlobalCoreIdToPhysicalChipId(coreId) = coreId / SparseCoresPerLogicalDevice`.

The bit-packing story is the same negative one the SparseCore pointer model establishes (see [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) and [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md)): the off-tile remote pointer the DMA engine consumes is a **32-bit word offset whose only mutation is its 8-bit address-space tag** — `tpu_addrspacecast` lowers to a value-preserving `MVT::i32` re-tag, injecting *no* core/chip bits. The 160/128/192-bit AS7/AS8/AS9 *fat-pointer* spaces the TPU DataLayout reserves are non-integral and are **not** constructed at the off-tile cast site. The peer core/chip therefore travels as the global core id (operand + DMA destination id), exactly as documented here.

For reimplementation, the contract is:

- **`StartRemoteDma` is a thin coordinate-plumbing driver over `get_remote_memref` + `dma_general_start`.** It does not move data or re-touch the descriptor base beyond what `GetRemoteMemBase` does; it computes the three flavors of "remote core id" the transfer needs — the *global* id (for the data base), the megacore-aware *core index*, and the *chip id* — and emits one `DmaGeneralStartOp`.
- **The remote core is a flat global core id, never a pointer bit-field.** Compose it as `chip × SparseCoresPerLogicalDevice + localCore`; decompose with the exact inverse divide. The off-tile `tpu_addrspacecast` re-tags only the address space (`MVT::i32`); no chip/core lands in the pointer.
- **The subslice→full-slice remap is a coordinate-offset re-linearization.** A subslice addresses peers across the full slice: take the chip id, decompose to coords, add the runtime subslice-origin coords, re-linearize against the full-slice chip bounds, then re-flatten to a global core id with the subslice-local `localCore`.
- **Reject remote `TileSpmem` data targets unless a `tile_id` is present.** The driver `RetCheckFail`s if the source memref or the dst memref is `tile_spmem` (memory space 2), and requires `tile_id.has_value()` for a remote `TileSpmem` dst.

| | |
|---|---|
| **Driver** | `OffloadFactory::StartRemoteDma` (`0x133EBCC0`, ~`0x8E0` B, `offload_collective_factory.cc`) |
| **Global-core-id producer** | `OffloadFactory::SubsliceToFullSliceGlobalCoreId` (`0x133E7900`, `0xA0` B) |
| **Flatten** | `ToGlobalCoreId` (`0x133E6880`) = `chip × SparseCoresPerLogicalDevice + localCore` |
| **Unflatten** | `GlobalCoreIdToPhysicalChipId` (`0x133E7BC0`) = `coreId / SparseCoresPerLogicalDevice` |
| **Subslice remap** | `SubsliceToFullSlice` (`0x133E79A0`) — coords + origin → re-linearize |
| **Megacore dst index** | `ComputeRemoteCoreIndex` (`0x133E7F80`) |
| **Data base** | `lowering_util::GetRemoteMemBase` (`0x13D88660`) — fed the global core id as `remoteCoreId` |
| **Emitted op** | `sc_tpu.dma_general_start` via `DmaGeneralStartOp::create` (`0x145B1880` / `0x145B16E0`) |
| **Pointer bit-packing** | **none at the off-tile path** — `tpu_addrspacecast` → value-preserving `MVT::i32` re-tag |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

---

## The launch — `OffloadFactory::StartRemoteDma`

### Purpose

`StartRemoteDma` (`0x133EBCC0`) is the SparseCore-offload all-to-all's per-target remote-transfer launcher. Given a source memref, a destination memref, a per-target subslice core id, and the bundle of sflag/offset/stride descriptor pieces, it assembles and emits one `sc_tpu.dma_general_start` that writes the local source into the peer's destination buffer, on the resolved peer SparseCore. Its demangled signature is

```text
xla::tpu::sparse_core::collective::OffloadFactory::StartRemoteDma(
    mlir::OpBuilder&, mlir::Value src, mlir::Value dst, mlir::Value remoteCoreId,
    mlir::Value, SflagAndIndex, SflagAndIndex, mlir::Value, BufferOffset, BufferOffset,
    std::optional<DmaStrides>, std::optional<mlir::Value>) const
```

sourced from `platforms/xla/sparse_core/offload_collective_factory.cc` (the location string is embedded at the body's `LogMessageFatal` / `RetCheckFail` sites). It is the all-to-all sibling of the dense `issueGeneralDma` remote arm — both terminate in a `DmaGeneralStartOp`, both resolve the same `get_remote_memref` data base, but `StartRemoteDma` produces its `remoteCoreId` from the *collective* coordinate plumbing rather than from `getRemoteDeviceAndSparseCoreIds`.

### Algorithm

The decompiled body (`0x133EBCC0`) is a guard prologue, the coordinate plumbing, and one of two `DmaGeneralStartOp::create` call shapes (with or without strides). The `Value` operands map to the decompiler's `a*` arguments as: `src = a16`, `dst = a19`, `remoteCoreId = a7` (held in `v132`/`v39`).

```c
function StartRemoteDma(b, src, dst, remoteCoreId, ...):       // 0x133EBCC0
    // [0] read the "count_dones" / strict-vs-relaxed completion mode attrs
    mode = self.flag[+1720] ? "relaxed" : "strict"             // 0x133EBDxx

    // [1] GUARD: reject remote TileSpmem data targets (memory space 2)
    if GetMemorySpace(src.type) == 2:                          // 0x133EBD75
        RetCheckFail("!src.tile_spmem()", line 1210)           //  → Status, return
    if GetMemorySpace(dst.type) == 2:                          // 0x133EBD96
        RetCheckFail("!dst.tile_spmem()", line 1212)
    if GetMemorySpace(dst.type) == 2 && !tile_id.present:      // 0x133EBDB7
        RetCheckFail("tile_id.has_value()", line 1215)
        // "tile_id must be provided for DMA to remote TileSpmem."

    // [2] local dst memref offset adjust (an AddIOp on a ConstantIndexOp(4))
    localMemref = AddIOp(dst_base, ConstantIndexOp(4))         // v33 @0x133EBE..

    // [3] subslice core id  →  flat GLOBAL core id
    gcid = SubsliceToFullSliceGlobalCoreId(b, remoteCoreId)    // 0x133E7900 @0x133EBE6A → v40

    // [4] remote DATA base: feed the GLOBAL core id as GetRemoteMemBase's remoteCoreId
    remoteBase = GetRemoteMemBase(dst /*a19*/, loc, localMemref=v33,
                                  remoteCoreId = gcid /*v40*/, ...)   // 0x13D88660 @0x133EBEB1

    // [5] megacore-aware destination CORE INDEX (routing)
    ComputeRemoteCoreIndex(b, /*v134*/, remoteCoreId /*v39*/)  // 0x133E7F80 @0x133EBEC7

    // [6] destination CHIP id  =  unflatten then full-slice remap
    chipId      = GlobalCoreIdToPhysicalChipId(b, remoteCoreId) // 0x133E7BC0 @0x133EBF14 → v45
    fullSlice   = SubsliceToFullSlice(b, chipId)                // 0x133E79A0 @0x133EBF22 → v132

    // [7] optional strides → ISA strides, then EMIT
    if dma_strides.present[+112] == 1:                          // @0x133EBF94
        isaStrides = ConvertDmaStridesToIsaStrides(b, dma_strides)   // 0x133EAFC0 @0x133EBFB0
        DmaGeneralStartOp::create(b, ..., remoteBase, ..., fullSlice, ...,
                                  isaStrides...)                // 0x145B1880 @0x133EC1DC  (with strides)
    else:
        DmaGeneralStartOp::create(b, ..., remoteBase, ..., fullSlice, ...)  // 0x145B16E0 @0x133EC41D
    return ok
```

The decisive facts, all decompile-confirmed:

- **The global core id is produced once (step 3) and consumed three ways.** It is `GetRemoteMemBase`'s `remoteCoreId` (the data base, step 4); it is `ComputeRemoteCoreIndex`'s input (the megacore-aware routing index, step 5); and it is `GlobalCoreIdToPhysicalChipId`'s input (the destination chip id, step 6). The three "remote core" quantities the descriptor needs all derive from the *same* `SubsliceToFullSliceGlobalCoreId` result — keeping the resolved data base and the routed destination consistent.
- **The remote data base comes from `GetRemoteMemBase`, not from `StartRemoteDma` itself.** Step 4 is the all-to-all feed of `get_remote_memref`: the global core id `v40` is passed as `GetRemoteMemBase`'s `remoteCoreId`, which becomes `get_remote_memref` operand 1. The base re-tag (the `tpu_addrspacecast` on the descriptor pointer) is owned by [`get_remote_memref`](get-remote-memref.md); `StartRemoteDma` only *supplies the core id*.
- **The driver emits the GeneralDma directly.** Unlike the dense path, the collective driver does not route through an `EnqueueDMAOp`; it calls `DmaGeneralStartOp::create` in-line, with the destination chip id (`fullSlice` = step 6) and the megacore core index (step 5) as the routing operands and `remoteBase` (step 4) as the target base.

### The TileSpmem rejection guard

The prologue runs three `GetMemorySpace(...) == 2` tests (memory space 2 == `tile_spmem`):

| check | @VA | line | condition | message |
|---|---|---|---|---|
| `!src.tile_spmem()` | `0x133EBD75` | 1210 | `GetMemorySpace(src) == 2` → fail | `RetCheckFail` |
| `!dst.tile_spmem()` | `0x133EBD96` | 1212 | `GetMemorySpace(dst) == 2` → fail | `RetCheckFail` |
| `tile_id.has_value()` | `0x133EBDB7` | 1215 | `GetMemorySpace(dst) == 2 && !tile_id` → fail | "tile_id must be provided for DMA to remote TileSpmem." |

> **GOTCHA —** the three checks are not redundant. The first two reject a *tile-local* `tile_spmem` (space 2) source or destination outright — a tiled SPMEM buffer is not a valid remote-DMA endpoint without a tile selector. The third is reached only when the dst is the tile-resolved `TileSpmem` *and* the optional `tile_id` (`a24`/`v.present`) is absent: a remote `TileSpmem` write is permitted, but the tile must be named explicitly. This is the same tile-id-as-operand discipline the [`get_remote_memref`](get-remote-memref.md) tile path uses (`tpu_tileid` as the cast's 2nd operand), surfaced as a launch-time precondition.

### The two emit shapes

The driver ends in one of two `DmaGeneralStartOp::create` calls, selected on the optional `DmaStrides` present byte (`*(dma_strides + 112) == 1`, `0x133EBF94`):

- **strided** (`0x145B1880`, call @`0x133EC1DC`): the longer arg list, preceded by `ConvertDmaStridesToIsaStrides` (`0x133EAFC0`) which lowers the `DmaStrides` triple to ISA stride form; the emit carries the extra stride `ValueRange`s.
- **contiguous** (`0x145B16E0`, call @`0x133EC41D`): the shorter arg list, no stride operands.

Both shapes pass `remoteBase` as the target base and `v132` (the full-slice destination chip id) into the routing slots. The per-operand role within `DmaGeneralStartOp` (which `Value` is the destination id vs the dst index vs the strides) rests on the op's ODS layout and the call-site producer order, not on a per-operand getter at this site — marked HIGH below.

> **NOTE —** the emit also carries a completion-counting mode read in the prologue: `self.flag[+1720]` selects the string `"relaxed"` vs `"strict"` written into a `count_dones`-keyed `StringAttr` (`0x133EBDxx`). This is the DMA's done-counting discipline (relaxed vs strict ordering of completion signals), threaded into the op as an attribute; its downstream semantics are on the GeneralDma emitter, not here.

---

## The producer — `SubsliceToFullSliceGlobalCoreId`

### Purpose

`SubsliceToFullSliceGlobalCoreId` (`0x133E7900`, `0xA0` B) is the all-to-all collective's `remoteCoreId` producer. An all-to-all on a *subslice* must address peers across the *full* slice; the per-target core id the collective scheduler hands `StartRemoteDma` is a **subslice-local** core id, and this function flattens it to the **global** core id the remote-base resolver and the DMA routing expect. Its demangled signature is `OffloadFactory::SubsliceToFullSliceGlobalCoreId(mlir::OpBuilder&, mlir::Value coreId) const`.

### Algorithm

```c
function SubsliceToFullSliceGlobalCoreId(b, coreId):           // 0x133E7900
    if self.target[+0x930] != 1:                               // SupportSubslices flag @0x133E7903
        return coreId                                          //   no subslice → identity

    chipId       = GlobalCoreIdToPhysicalChipId(b, coreId)     // 0x133E7BC0 @0x133E7927
    fullSliceChip = SubsliceToFullSlice(b, chipId)             // 0x133E79A0 @0x133E7935
    divisor      = LogicalDevicesPerChip(TENSOR=0)             // 0x1D615B00 @0x133E7942
                   → IdxConst(divisor)                         // 0x133E6BA0 @0x133E7950
    localCore    = arith.RemUIOp(coreId, divisor)              // 0x1CB20800 @0x133E796C
    return ToGlobalCoreId(b, fullSliceChip, localCore)         // 0x133E6880 @0x133E798C
```

The structure is decompose → remap chip → re-flatten with the original local core:

```text
   coreId  ──GlobalCoreIdToPhysicalChipId──►  chipId
                                                 │
                                          SubsliceToFullSlice (coords + subslice-origin, re-linearize)
                                                 │
                                                 ▼
                                          fullSliceChip
   coreId  ──RemUI(LogicalDevicesPerChip)──►  localCore
                                                 │
                ToGlobalCoreId(fullSliceChip, localCore)  ──►  full-slice GLOBAL core id
```

> **QUIRK —** the guard is `self.target[+0x930] != 1` (`SupportSubslices`). On a platform with no subslices, the function is the **identity** — the incoming core id is already a full-slice global id, and the whole decompose/remap/re-flatten chain is skipped. A reimplementation must not unconditionally run the remap; the no-subslice path returns the input verbatim.

### The closed-form arithmetic

The flatten/unflatten pair is closed-form, sharing the `SparseCoresPerLogicalDevice` divisor with the dense `EnqueueDMA` destination-id stride (so the two remote-DMA paths agree on the topology arithmetic):

```c
function ToGlobalCoreId(chip, localCore):                      // 0x133E6880
    stride = CoresPerChip(SPARSE=2) / LogicalDevicesPerChip(SPARSE=2)   // idiv @0x133E68D7
           = SparseCoresPerLogicalDevice
    if stride == CoresPerChip(SPARSE):                         // one logical device per chip
        return chip                                            //   → identity (no flatten)
    AlwaysAssert(localCore < LogicalDevicesPerChip(TENSOR=0),  // 0x13D7F9C0, line 651
        "Core index should be smaller than the number of logical devices per chip.")
    return arith.MulIOp(chip, IdxConst(stride)) + localCore    // MulIOp 0x1CAF0C40, AddIOp 0x1CAF0B00

function GlobalCoreIdToPhysicalChipId(coreId):                 // 0x133E7BC0
    stride = CoresPerChip(SPARSE=2) / LogicalDevicesPerChip(SPARSE=2)
    if stride == CoresPerChip(SPARSE):                         // one logical device per chip
        return coreId                                          //   → identity (no divide)
    return arith.DivUIOp(coreId, IdxConst(stride))             // 0x1CB06D00 @0x133E7C6B
```

`GlobalCoreIdToPhysicalChipId` is the exact inverse of `ToGlobalCoreId`'s chip stride. Both fold to the identity when `stride == CoresPerChip(SPARSE)` — i.e. when there is exactly one logical device per chip, the global core id *is* the chip id, so neither the multiply nor the divide is emitted.

> **CORRECTION — divisor asymmetry (HIGH).** `SubsliceToFullSliceGlobalCoreId`'s `localCore` extraction divides `coreId` by `LogicalDevicesPerChip(TENSOR=0)` (the `RemUIOp` @`0x133E796C`), while the chip flatten/unflatten uses `SparseCoresPerLogicalDevice = CoresPerChip(SPARSE=2)/LogicalDevicesPerChip(SPARSE=2)`. For the common geometry where `LogicalDevicesPerChip` is the same for `TENSOR` and `SPARSE`, these coincide. The use of the `TENSOR` (`esi=0`) divisor in the mod versus the `SPARSE` (`esi=2`) ratio in the chip divide is byte-confirmed; whether it is intended-equal or encodes a deliberate TENSOR-vs-SPARSE logical-device asymmetry is read from the arithmetic, not from a CHECK string. Marked HIGH.

### The subslice coordinate remap

`SubsliceToFullSlice` (`0x133E79A0`) is the chip-id translation an all-to-all uses to reach a peer outside its own subslice. It guards on the same `target[+0x930]` (`SupportSubslices`) bit, queries the full-slice geometry, reads the runtime subslice origin, and re-linearizes:

```c
function SubsliceToFullSlice(b, subsliceChipId):               // 0x133E79A0
    assert self.target[+0x930] == 1                            // SupportSubslices @0x133E79BA
    bounds = TpuTopology::GetFullSliceChipBoundsForSubslice()  // 0x20AD2F60 @0x133E79DE
             assert bounds.has_value()                         //   "full_slice_chip_bounds.has_value()"
    origin   = LoadSubsliceOrigin(b)                           // 0x133E7840 @0x133E7A2A  (SubsliceOriginOp)
    subCoords = ChipIdToCoordinates(b, subsliceChipId, dims)   // 0x133E7640 @0x133E7A50
    // FULL-slice chip id = linearize( subCoords + origin against bounds )
    return rowMajor(subCoords[d] + origin[d], bounds)          // 3×AddIOp + 2×MulIOp + 2×AddIOp
                                                               //   @0x133E7A77..0x133E7B70
```

`LoadSubsliceOrigin` (`0x133E7840`) materializes a `sc_tpu.subslice_origin` op (`SubsliceOriginOp::create`, `0x14610960`) — a runtime register read of *this* subslice's origin chip id — and decomposes it to coordinates. `ChipIdToCoordinates` (`0x133E7640`) is a standard radix decomposition (a `RemUIOp`/`DivUIOp` chain) of a chip id into `(x, y, z)` against the per-dimension chip bounds. The remap therefore offsets the subslice-local chip coords by the runtime origin and re-linearizes against the full-slice geometry — the full-slice chip id a cross-subslice peer occupies.

---

## The megacore destination index — `ComputeRemoteCoreIndex`

`ComputeRemoteCoreIndex` (`0x133E7F80`) produces the megacore-aware destination *core index* the DMA routing uses (distinct from the *global core id* of the data base and the *chip id* of the routing). It is gated on megacore being active:

```c
function ComputeRemoteCoreIndex(b, ..., coreId):               // 0x133E7F80
    megacore =  TpuChipConfig::Megachip()                      // 0x20AFCC00 @0x133E7FA9
             && self.target[+0x3B8][+0x94] > 0                 // TpuTopology.TpuChipConfig @0x133E7FBD
             && ( (self[+0x628] & 4) || self.target[+0x540] == 1 )   // @0x133E7FC6 / @0x133E7FCF
    if megacore:
        stride    = SparseCoresPerLogicalDevice                // idiv @0x133E8011
        localCore = arith.RemUIOp(coreId, stride)              // @0x133E8071
        pick      = arith.CmpIOp(localCore, 1)                 // 0x1CAFE620 @0x133E80AC
        return arith.SelectOp(pick, formA, formB)              // 0x1CB317A0 @0x133E814D
    else:                                                      // @0x133E8042
        return simpleIndex
```

When megacore is active, two SparseCores are fused per logical device; the `SelectOp` on `CmpIOp(localCore, 1)` chooses between the two megacore-half index forms so the DMA targets the correct physical half. Without megacore, the simpler index is returned. The `[target+0x3B8]` (`TpuTopology`, with `+0x18` → `TpuChipConfig`) and `[target+0x540]` / `[self+0x628]&4` offsets are the same megacore/SC-offload fields the SC-offload gate reads (see [On-Pod Collectives — Section Map](overview.md) §5). This is the megacore sibling of the dense destination-id imul.

---

## The remote pointer — no bit-packing at the off-tile path

The headline negative result: **the cross-chip pointer the DMA engine consumes does not encode the peer core/chip in its bits.** The remote target rides the global core id (operand → destination id), and the pointer itself is only address-space re-tagged. Three pieces of binary evidence pin this:

- **The off-tile `tpu_addrspacecast` is a value-preserving `MVT::i32` re-tag.** `GetRemoteMemRefOpLowering` casts the descriptor base into the `"_any"` space with a 1-operand `tpu_addrspacecast`, and the LLVM-backend lowering of that cast (`TPUTargetLowering::LowerADDRSPACECAST`, `0x13B70480`) emits a value-preserving SDNode (opcode `0xF3`, result `MVT::i32`) carrying the *same* 32-bit input pointer with only its result type's address-space tag changed. No new address is computed; no core/chip field is injected. The ISel mechanics (the legality matrix, the no-op-fold disable, the AA sflag group) are owned by [addrspacecast ISel](../sparsecore/addrspacecast-isel.md).
- **The backend models no pointer-bit structure for the cast.** `computeKnownBitsForTargetNode` (`0x13B7A8E0`) handles only node `0x216`; the addrspacecast node `0xF3` is absent — the backend tracks no bit-packed structure for it, treating it as an opaque value pass-through, not an address with packed sub-fields.
- **The AS7/AS8/AS9 fat-pointer spaces are reserved but unused at the cast.** The TPU DataLayout defines AS7/8/9 as 160/128/192-bit **non-integral** structured pointers (index widths 32/48/32) — the natural home for a packed `{core/chip, address-space, offset}` pointer — but the off-tile cast result is `MVT::i32` (a 32-bit word offset, AS2/3/5/6 = `p:32:32`), so the off-tile remote pointer does **not** use the fat-pointer encoding. The fat-pointer representation, the AS-id ↔ `MemorySpace` table, and why routing is operands rather than pointer bits are on [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md).

```text
   what the DMA descriptor actually carries (off-tile remote DMA):

   remote target  =  ⟨ base pointer ; offset/sizes/strides ; routing ⟩
        │                  │                  │                  │
        │           tpu_addrspacecast    UNCHANGED          DmaGeneralStart
        │           → MVT::i32 re-tag    (local values)      destination id
        │           (8-bit AS only,                          + chip id
        │            NO core/chip bits)                      (from the global
        │                                                     core id, this page)
        ▼
   the peer core/chip is the GLOBAL CORE ID — an operand/destination-id,
   never a field of the 32-bit pointer word.
```

> **QUIRK —** a reimplementer reaching for a "remote pointer = pack(chip, core, offset)" model will be wrong for the off-tile path. The DMA descriptor's *base* is the local base with one address-space tag flipped; the offset/sizes/strides are the *local* memref's, unchanged (the peer holds an identical-layout memref so the same offset hits the same element); and the routing is the global core id this page computes, threaded into `DmaGeneralStartOp` as the destination. The only path that folds an id into a pointer is the *on-tile* `TileSpmem`/`TileSmem` cast — and there it is the `tpu_tileid` as the cast's 2nd *operand*, still not an address-bit pack. The on-tile tile-id ISel is on [addrspacecast ISel](../sparsecore/addrspacecast-isel.md).

---

## Function Map

| Function | @VA | Role | Confidence |
|---|---|---|---|
| `OffloadFactory::StartRemoteDma` | `0x133EBCC0` | the per-target remote-DMA launcher (this page) | CONFIRMED |
| `SubsliceToFullSliceGlobalCoreId` | `0x133E7900` | subslice core id → flat global core id | CONFIRMED |
| `ToGlobalCoreId` | `0x133E6880` | `chip × SparseCoresPerLogicalDevice + localCore` | CONFIRMED |
| `GlobalCoreIdToPhysicalChipId` | `0x133E7BC0` | `coreId / SparseCoresPerLogicalDevice` (inverse) | CONFIRMED |
| `SubsliceToFullSlice` | `0x133E79A0` | coords + subslice-origin → full-slice chip id | CONFIRMED |
| `LoadSubsliceOrigin` | `0x133E7840` | `SubsliceOriginOp` (runtime origin chip id) → coords | CONFIRMED |
| `ChipIdToCoordinates` | `0x133E7640` | radix decompose chip id → `(x, y, z)` | CONFIRMED |
| `ComputeRemoteCoreIndex` | `0x133E7F80` | megacore-aware destination core index | CONFIRMED |
| `IdxConst` | `0x133E6BA0` | `arith.ConstantIndexOp` (with `value < kMaxNumberOfElements` = `< 2³²` check) | CONFIRMED |
| `GetRemoteMemBase` | `0x13D88660` | remote data base (fed the global core id) | CONFIRMED |
| `ConvertDmaStridesToIsaStrides` | `0x133EAFC0` | `DmaStrides` → ISA stride form | CONFIRMED |
| `DmaGeneralStartOp::create` | `0x145B1880` / `0x145B16E0` | emit `sc_tpu.dma_general_start` (strided / contiguous) | HIGH (operand→field map) |

---

## Considerations

- **`StartRemoteDma` returns a `Status`-style result.** The body returns `1` on success and a `CreateStatusAndConditionallyLog` on a `RetCheckFail`; a reimplementation must propagate the failure (the three TileSpmem preconditions) rather than asserting.
- **The optional `DmaStrides` and the optional final `Value` (tile id) gate two control flows.** The stride present byte (`+112`) selects the long/short `DmaGeneralStartOp::create`; the tile-id present byte gates the third TileSpmem precondition. Both are `std::optional`, threaded as `present`-byte + payload pairs in the ABI.
- **The destination chip id is *also* full-slice-remapped (step 6), not just the data-base core id.** Both the data base (via the global core id) and the routing (via `GlobalCoreIdToPhysicalChipId` → `SubsliceToFullSlice`) pass through the same subslice→full-slice translation, so a cross-subslice all-to-all routes correctly even though the per-target id arrived subslice-local.

---

## Related Components

| Name | Relationship |
|---|---|
| `SubsliceToFullSliceGlobalCoreId` (`0x133E7900`) | the collective `remoteCoreId` producer this driver consumes |
| `ToGlobalCoreId` / `GlobalCoreIdToPhysicalChipId` | the flatten/unflatten pair (shared `SparseCoresPerLogicalDevice` stride) |
| `SubsliceToFullSlice` (`0x133E79A0`) | the subslice coordinate-offset chip remap |
| `ComputeRemoteCoreIndex` (`0x133E7F80`) | the megacore-aware destination core index |
| `GetRemoteMemBase` (`0x13D88660`) | the remote data base; this driver supplies its `remoteCoreId` |
| `DmaGeneralStartOp::create` (`0x145B1880`/`0x145B16E0`) | the `sc_tpu.dma_general_start` the driver emits |

## Cross-References

- [On-Pod Collectives — Section Map](overview.md) — the navigational entry for Part XIII; the SC-offload substrate, the megacore/SC-offload gate fields this driver also reads.
- [get_remote_memref](get-remote-memref.md) — the remote-memref *formation* (the op, the resolver, the base re-tag); `StartRemoteDma` feeds it the global core id and consumes its base. The `EnqueueDMA` feed (`getRemoteDeviceAndSparseCoreIds`) is the sibling of this collective feed.
- [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) — the LLVM-backend lowering of `tpu_addrspacecast`: the value-preserving `MVT::i32` re-tag, the legality matrix, the no-fold/no-flatten hooks; why the off-tile pointer carries no core/chip bits.
- [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md) — the SparseCore pointer representation and the reserved 160/128/192-bit non-integral fat-pointer spaces the off-tile cast does *not* construct.
- [Intra-Chip DMA Descriptor](../dma/intra-chip-descriptor.md) — the on-chip `dma_general` descriptor the cross-chip launch is a peer of.
- [SC-Offload Config Builder](sc-offload-config-builder.md) — the SparseCore-offload collective config builder that drives the all-to-all the per-target `StartRemoteDma` calls serve.
- [AllToAll Tables](alltoall-tables.md) — the all-to-all / ragged-all-to-all link tables the collective scheduler above `StartRemoteDma` consumes.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part XIII — On-Pod Collectives & Barriers / SparseCore-offload collectives — [back to index](../index.md)
