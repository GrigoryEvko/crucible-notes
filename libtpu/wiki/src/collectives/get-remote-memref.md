# get_remote_memref

> *Every address, offset, operand index, and string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. Addresses are the binary's own VMA (text/rodata VMA == file offset; `.data.rel.ro` file offset = VMA − `0x200000`).*

## Abstract

A SparseCore collective that touches a peer chip's memory needs a memref that *names* the remote data without recomputing an absolute address. `libtpu` builds that remote memref with one MLIR op — `sc_tpu.get_remote_memref` — produced by one resolver — `lowering_util::GetRemoteMemBase`. This page documents the whole *get-remote-memref* unit: the op's signature and verifier, the resolver that constructs it from a local memref and a peer core-id, and the remote re-tagging the op's LLVM lowering performs.

The headline result is the same negative one the SparseCore pointer model implies (see [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md)): **a cross-chip remote memref is the *local* memref with only its base pointer's address space re-tagged.** `GetRemoteMemBase` reads the local memref's `MemorySpace`, promotes it to the remote-reachable `"_any"` superset (`MemorySpaceToAny`), and rebuilds an otherwise byte-identical `MemRefType` — same shape, same element type, same layout, only the `MemorySpaceAttr` changed. The op's LLVM lowering then unpacks the local `MemRefDescriptor`, runs a single `tpu_addrspacecast` on the aligned base pointer, writes the re-tagged pointer back into descriptor fields `[0]` and `[1]`, and **leaves the offset, sizes, and strides untouched**. The peer core holds an identical-layout memref, so the same offset addresses the same logical element on the peer. The cross-chip address is therefore a `{core-id, address-space, local-offset}` *tuple*, not a recomputed flat address — and of those three, only the address-space integer rides inside the pointer; the core-id rides as a separate SSA operand (the `remoteCoreId` operand for the off-tile path, or `tpu_tileid` for the on-tile TEC path).

The page is three units. First, **the op** — `sc_tpu.get_remote_memref`: its operand order (`localMemref`, `remoteCoreId`, `v3`, optional `v4`), its `OpTraits`, and the verifier's per-operand type constraints, from `build` / `create` / `verifyInvariantsImpl`. Second, **the resolver** — `GetRemoteMemBase`: the local-memref → `"_any"` promotion that filters which memories are remotely addressable, the remote-`MemRefType` rebuild, and the `GetRemoteMemRef::create` emit, recovered from the full 86-line decompile. Third, **the remote re-tag** — `GetRemoteMemRefOpLowering::matchAndRewrite`: the descriptor unpack, the source-space branch (tile vs generic), the `"_any"` re-tag (with the Vmem skip), and the descriptor re-pack. The on-tile `addrspacecast` ISel mechanics live on [addrspacecast ISel](../sparsecore/addrspacecast-isel.md); the AS-id table on [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md); the actual remote DMA *transfer* that consumes this base on [StartRemoteDma](start-remote-dma.md) — this page links them and owns only the op, the resolver, and the re-tag.

For reimplementation, the contract is:

- **The remote memref is the local memref, base-pointer re-tagged only.** Do not recompute an absolute cross-chip address. Rebuild the `MemRefType` with the promoted `"_any"` `MemorySpaceAttr`; copy shape/elem/layout verbatim; at lowering, address-space-cast the aligned base and leave offset/sizes/strides at their local values.
- **`MemorySpaceToAny` is the remote-addressability filter.** Only the data memories (`smem`, `tile_spmem`/`spmem`, `hbm`, `vmem`, and the `smem_tile`/`smem_scs` aliases) promote; every control/scratch space (`sflag`, `dreg`, `timem`, `simem`, `iova`, `mar`, the `*_cb` and `sflag_*` banks) `LOG(FATAL)`s. Remote data is reachable; remote control flags are not re-addressed through this path.
- **The op is a pure 3-or-4-operand SSA op with one typed result.** `AtLeastNOperands<3>`; operand 0 is a `MemRefType` (constraint `sc_ops12`), operands 1 & 2 are index-like (constraint `sc_ops2`), operand 3 is the optional 4th. The result is `OneTypedResult<MemRefType>` — the remote base. No inherent attributes.
- **Carry the peer core-id as the `remoteCoreId` operand, never in the pointer.** For the off-tile generic path the lowering folds *no* core-id into the pointer; the routing rides the separate operand (and downstream the DMA destination-id). For the on-tile TEC path the `tpu_tileid` is the cast's 2nd operand.

| | |
|---|---|
| **Op name** | `"sc_tpu.get_remote_memref"` @ `0x866A17F` (len `0x18`) |
| **OpTraits** | `ZeroRegions, OneResult, OneTypedResult<MemRefType>, ZeroSuccessors, AtLeastNOperands<3u>, OpInvariants` |
| **Resolver** | `lowering_util::GetRemoteMemBase` (`0x13D88660`, `lowering_util.cc:5356`) |
| **build** | `0x1459AB40` — op0=localMemref, op1=remoteCoreId, op2=v3, op3=v4 (optional), result Type last |
| **create (3-op / 4-op)** | `0x145C64E0` / `0x145C6600` (the 4-op is the one `GetRemoteMemBase` calls) |
| **verifyInvariantsImpl** | `0x145C66A0` — op0 → `sc_ops12` (MemRefType); op1,op2 → `sc_ops2` (index-like) |
| **getLocalMemrefMemorySpace** | `0x146B0000` — operand 0's MemRefType → `GetMemorySpace` |
| **Remote re-tag (lowering)** | `GetRemoteMemRefOpLowering::matchAndRewrite` (`0x1357AE40`) |
| **The `"_any"` filter** | `MemorySpaceToAny` (`0x14B786E0`, table `0xAF36B84`); FATAL for control spaces |
| **The cast** | `tpu_addrspacecast::create` (`0x146D5EA0`); skipped when the `"_any"` space is Vmem (205) |
| **Peer-id producer** | `getRemoteDeviceAndSparseCoreIds<EnqueueDMAOp>` (`0x13511660`) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

---

## The op — `sc_tpu.get_remote_memref`

### Purpose

`sc_tpu.get_remote_memref` is the first-class SSA carrier of a cross-chip remote memref. Its single typed result (`OneTypedResult<MemRefType>`) is the remote base that the GeneralDma assembler stores and forwards as the DMA destination/target-semaphore base, and that the all-to-all collective driver hands to the remote transfer. The op carries the data base only; the synchronization core is named separately by the DMA destination-id (see §3 and [StartRemoteDma](start-remote-dma.md)).

### Operands and result

`build` (`0x1459AB40`) is small enough to confirm operand-for-operand. It writes the three mandatory operand `Value`s to a stack array and adds them in order, adds the optional fourth only when non-null, then appends the result `Type` to the op's result-type vector last:

```text
GetRemoteMemRef::build(OpBuilder, OperationState &st, Type resultTy,
                       Value v0, Value v1, Value v2, Value v3 /*optional, may be null*/)

   addOperands(st, &v0, 1)        ; operand 0 = localMemref
   addOperands(st, &v1, 1)        ; operand 1 = remoteCoreId
   addOperands(st, &v2, 1)        ; operand 2 = v3
   if (v3 != null)                ; the 4th operand is added ONLY when present
       addOperands(st, &v3, 1)    ; operand 3 = v4   (optional)
   st.types[st.numResults++] = resultTy   ; result Type appended last (op+64 SmallVector)
```

There are **no** `Properties` writes — the op carries no inherent attributes. `getLocalMemrefMemorySpace` (`0x146B0000`) confirms operand 0 is the local memref: it reads operand 0's `MemRefType` (`[impl+8] & ~7`) and returns its `GetMemorySpace`.

| Operand | Role | Type class | Confidence |
|---|---|---|---|
| 0 | `localMemref` — the data base whose type is read and promoted | `MemRefType` (constraint `sc_ops12`) | CONFIRMED |
| 1 | `remoteCoreId` — a peer core-id component | index-like (constraint `sc_ops2`) | CONFIRMED |
| 2 | `v3` — the second id component (device vs core) | index-like (constraint `sc_ops2`) | CONFIRMED |
| 3 | `v4` — optional 4th operand (a remote sflag/offset component) | added only when non-null | HIGH |
| result | the remote `MemRefType` base | `OneTypedResult<MemRefType>` | CONFIRMED |

> **GOTCHA —** the *semantic* split of operands 1 (`remoteCoreId`) and 2 (`v3`) between the remote **device** (chip) id and the remote **core** id within the chip is HIGH, not CONFIRMED. The verifier type-checks both with the *same* constraint (`sc_ops2`), so they are two interchangeable index-like ids; the device-vs-core assignment is read from the `GetRemoteMemBase` call-site marshalling order (§3), not from any per-operand check string. A reimplementation must fix the order at the call site, not infer it from the op.

### Traits and verifier

The trait set comes from the `Model<>` trait-template instantiation: `ZeroRegions`, `OneResult`, `OneTypedResult<MemRefType>`, `ZeroSuccessors`, `AtLeastNOperands<3u>`, `OpInvariants`. The `AtLeastNOperands<3u>` is exactly the 3 mandatory + 1 optional shape `build` produces.

`verifyInvariantsImpl` (`0x145C66A0`) checks, against the op's operand list (stride 32, first operand at op-impl+24):

```text
operand 0  (impl+24)  → __mlir_ods_local_type_constraint_sc_ops12   "operand" idx 0   ; MemRefType
operand 1  (impl+56)  → __mlir_ods_local_type_constraint_sc_ops2    "operand" idx 1   ; index-like
operand 2  (impl+88)  → __mlir_ods_local_type_constraint_sc_ops2    "operand" idx 2   ; index-like
result   0            → __mlir_ods_local_type_constraint_sc_ops12   "result"  idx 0   ; MemRefType
```

So the verifier enforces the asymmetry the op semantics need: operand 0 and the result are `MemRefType`s (the local base in, the remote base out); the two id operands are index-like. The `create` overloads (`0x145C64E0` 3-op / `0x145C6600` 4-op) are thin wrappers that build an `OperationState` over the op name at `0x866A17F` and forward positionally into `build` then `OpBuilder::create`.

---

## The resolver — `lowering_util::GetRemoteMemBase`

### Purpose

`GetRemoteMemBase` is the cross-chip remote-base *resolver*: given the local memref `Value`, a peer core-id, a second id component, and an optional fourth, it produces the `sc_tpu.get_remote_memref` op whose result is the remote base. Its demangled signature is `xla::tpu::sparse_core::lowering_util::GetRemoteMemBase(mlir::OpBuilder, LocationGenerator, mlir::Value, mlir::Value, mlir::Value, std::optional<mlir::Value>)`, at `0x13D88660`, sourced from `platforms/xla/sparse_core/lowering_util.cc:5356` (the location string is embedded in the body).

### What it does

The decompiled body is a four-step pipeline. It does **not** compute an absolute address:

```text
GetRemoteMemBase(builder, locGen, v1=localMemref, v2=remoteCoreId, v3, opt v4):

  [1]  localType = v1.getType()                       ; [v1.impl + 8] & ~7  → MemRefType
       srcSpace  = GetMemorySpace(localType)           ; 0x1459C7E0
       anySpace  = MemorySpaceToAny(srcSpace)          ; 0x14B786E0  — the remote-addressability filter

  [2]  remoteType = MemRefType::get(                   ; 0x1D897680
                      localType.getShape(),            ; 0x1D8921E0   ← copied verbatim
                      localType.getElementType(),      ; 0x1D892200   ← copied verbatim
                      localType.getLayout(),           ; 0x1D892220   ← copied verbatim
                      MemorySpaceAttr::get(ctx, anySpace))   ; 0x1458FF20  ← ONLY the space changes

  [3]  loc = locGen.visit()                            ; std::variant Visitor dispatch; lowering_util.cc:5356
                                                        ; variant index == 0xff → __throw_bad_variant_access

  [4]  optV4 = (presentByte & 1) ? v4 : null
       return GetRemoteMemRef::create(builder, loc, remoteType,
                                      v1 /*localMemref*/, v2 /*remoteCoreId*/, v3, optV4) - 16
```

Steps 1–2 are the whole "remote-ness" mechanism: the remote `MemRefType` differs from the local one *only* in its `MemorySpaceAttr`, set to the `"_any"` promotion of the local space. The cross-chip address is the local `{shape, elem, layout, offset}` plus a remote address space plus the `remoteCoreId` operand — a tuple, not a recomputed flat address. The `- 16` on the return converts the produced `Op*` into its `OneTypedResult<MemRefType>` `Value`.

### `MemorySpaceToAny` — the remote-addressability filter

`MemorySpaceToAny` (`0x14B786E0`) is a jump table (rodata `0xAF36B84`, indexed `MemorySpace − 1`, span `0x15`) whose fall-through `LOG(FATAL)`s. It admits only the data memories; every control/scratch space traps. This is the gate that decides what a remote DMA can target:

| Local `MemorySpace` | promotes to | reachable remotely? |
|---|---|---|
| 1 `smem` | 9 `smem_any` | yes |
| 2 `tile_spmem` | 15 `spmem_any` | yes |
| 3 `spmem` | 15 `spmem_any` | yes |
| 4 `hbm` | 10 `hbm_any` | yes |
| 6 `vmem` | 6 `vmem` (self; already global) | yes |
| 16 `smem_tile` | 9 `smem_any` | yes |
| 21 `smem_scs` | 9 `smem_any` | yes |
| 5 `sflag`, 7 `dreg`, 8 (gap), 11 `timem`, 12 `simem`, 13 `iova`, 14 `sflag_tile`, 15 `spmem_any`, 17 `mar`, 18 `tile_spmem_cb`, 19 `smem_cb`, 20 `sflag_scs` | **FATAL** | no |

> **NOTE —** the consequence: a remote GeneralDma can target a peer core's HBM / VMEM / SMEM / SPMEM *data*, but the two-sided sync's *remote semaphore* does **not** go through `GetRemoteMemBase` — `sflag` FATALs here. The peer sync flag is named instead by the DMA destination-id and the `dest_sync_flags` slot fields. `GetRemoteMemBase` resolves the data base; the remote sync core is named by the destination-id topology arithmetic (§3). The `MemorySpace` enum values and the AS-id band these promote into are tabulated on [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md).

### Callers

`GetRemoteMemBase` has four call sites, all in the cross-chip remote-DMA stack:

| Caller | @VA | what it passes |
|---|---|---|
| `issueGeneralDma` | `0x1350B4A1` | the remote arm of the GeneralDma assembler (§3) |
| `OffloadFactory::StartRemoteDma` | `0x133EBEB1` | `rdx` = `SubsliceToFullSliceGlobalCoreId` result (the flattened global core id) — see [StartRemoteDma](start-remote-dma.md) |
| `InitiateAsynchronousAllToAllDynamic` | `0x13D8C38E` | the dynamic all-to-all collective driver |
| `InitiateAsynchronousAllToAllDense` | `0x13DB2178` | the dense all-to-all collective driver |

The all-to-all collective drivers are the primary consumers — the SparseCore-offload remote half of the producer/consumer handshake.

---

## The remote re-tag — `GetRemoteMemRefOpLowering`

### Purpose

`sc_tpu.get_remote_memref` is a high-level op; the physical re-tag happens at its LLVM lowering, `GetRemoteMemRefOpLowering::matchAndRewrite` (`0x1357AE40`, run in the `ExpandTiledMemRefs` / ConvertToLLVM pass). This is where the `{core-id, address-space, local-offset}` tuple becomes an actual LLVM pointer: the lowering unpacks the local `MemRefDescriptor`, address-space-casts the base pointer into the `"_any"` (or tile-peer) space, writes it back, and re-packs.

### The lowering body

The decompiled `matchAndRewrite` is a single pass over the local descriptor:

```text
[a]  localType = operand0.getType()                    ; getODSOperandIndexAndLength(0) → impl[+8]&~7
     srcSpace  = MemorySpaceToAddressSpace(            ; 0x14B78780 — the LLVM addr-space integer (v19)
                   GetMemorySpace(localType))          ;   e.g. TileSpmem=201, TileSmem=219, Vmem=205

[b]  asid = typeConverter.getMemRefAddressSpace(localType)
     if (!present)  →  notifyMatchFailure("Failed to get memref address space")  →  return failure

[c]  unpack(localDescriptor)                            ; MemRefDescriptor::unpack 0x171BD620
     base = descriptor.field[1]   (aligned ptr)         ; → v87

[d]  BRANCH on srcSpace:
       srcSpace == 219 (TileSmem)  → CastTileSmemPointerToSmem (0x135B86E0)   ; reads operand 3 as tile-id source
       srcSpace == 201 (TileSpmem) → CastTileSpmemPointerToSpmem (0x135B8400) ;   "
         (both NORMALISE the tile pointer: inject tpu_tileid, re-tag to the non-tile peer
          Smem/Spmem space, update srcSpace by-ref, set the flag v86=1, then fall through)

[e]  GENERIC path (LABEL_19):
       anySpace = GetAnyTypeFromAddressSpace(srcSpace)  ; 0x1357B400  — the "_any" canonicalisation
       remoteType = MemRefType::get(localType.shape, .elem, .layout,
                       MemorySpaceAttr::get(ctx, AddressSpaceToMemorySpace(anySpace)))
       anyPtrTy = LLVMPointerType::get(ctx, anySpace)   ; 0x1746EB40
       if (anySpace != 205 /*Vmem*/)                    ;  Vmem is already global → cast SKIPPED
           base = tpu_addrspacecast::create(builder, loc, anyPtrTy, base) - 16   ; 0x146D5EA0

[f]  descriptor.field[0] = descriptor.field[1] = base   ; allocated + aligned ptr; OFFSET/SIZES/STRIDES untouched
     pack(descriptor) → newValue                        ; MemRefDescriptor::pack 0x171BD460
     replaceOp(op, newValue)                            ; 0x1C951540
```

The decisive facts, all decompile-confirmed:

- **Only the base pointer changes.** The lowering writes the cast result into descriptor fields `[0]` and `[1]` (allocated + aligned pointer) and re-packs the *rest of the local descriptor verbatim* — offset, sizes, strides stay at their local values. The peer holds an identical-layout memref, so the same offset/strides address the same logical element.
- **The generic re-tag is `tpu_addrspacecast` with one operand** (the base ptr). The `"_any"` destination space comes from `GetAnyTypeFromAddressSpace`. The cast is the *only* address-space change.
- **Vmem (205) skips the cast.** `if (AnyTypeFromAddressSpace != 205)` gates the `tpu_addrspacecast::create` — Vmem is already a global handoff space, so no re-tag is needed.
- **The tile path injects the core-id as an operand, not a pointer bit.** For on-tile `TileSpmem` (201) / `TileSmem` (219) sources, the lowering calls the tile-cast helpers, which read the parent function's `sc.sequencer` attribute and (in the TEC "execute" context) build a `tpu_tileid` and emit a 2-operand `tpu_addrspacecast_{spmem,smem}` with the tile-id as the second operand. A dynamic-shape guard (`hasRank` + a shape-walk) `emitOpError`s "Dynamic shapes are not supported when TileS(p)mem -> S(p)mem conversion is used" if the tile→non-tile path is used on a dynamic shape.

> **QUIRK —** the off-tile generic remote pointer carries **no** core-id. `matchAndRewrite` re-tags only the 8-bit address space; the peer core/chip routing rides the separate `remoteCoreId` operand (and downstream the DMA destination-id slot). Only the *on-tile* path folds an id into the cast — the `tpu_tileid` second operand. Whether the hardware "_any" pointer additionally encodes a core/chip field in upper address bits is below the LLVM-IR surface the binary exposes; at the IR level the routing is purely the operand. The `addrspacecast` ISel — which intrinsic each cast becomes and how it matches in the SelectionDAG — is on [addrspacecast ISel](../sparsecore/addrspacecast-isel.md).

### The cross-chip address composition

Putting the three units together, the physical remote pointer the DMA engine consumes is a three-part tuple, and the lowering only ever touches one part of it:

```text
        remote memref  =  ⟨ address-space ; local-offset/sizes/strides ; core-id ⟩

   address-space  : the LLVM pointer's address-space integer (re-tagged by matchAndRewrite to the
                    "_any"/peer ID — SpmemAny 0xDA / HBMAny 0xD5 / SflagAny 0xD3 / SmemAny 0xD4 /
                    Vmem 0xCD self / or the tile-peer Spmem 0xCA / Smem on the tile path)

   local-offset   : the UNCHANGED descriptor offset + sizes + strides — NOT recomputed; the peer
                    holds an identical-layout memref

   core-id        : NOT folded into the off-tile pointer — it rides the separate remoteCoreId operand
                    (→ DMA destination-id); on the tile path it IS the tpu_tileid 2nd cast operand
```

---

## §3 — the peer core-id: shared `{device, core}` source

The `remoteCoreId` operand `GetRemoteMemBase` consumes and the GeneralDma `destination_id` (the 5-bit cross-core routing slot) both originate from the **same** `{device, core}` computation — `getRemoteDeviceAndSparseCoreIds<EnqueueDMAOp>` (`0x13511660`). This binding is what keeps the resolved data base and the routed sync core consistent.

`getRemoteDeviceAndSparseCoreIds` index-casts the EnqueueDMAOp's two id `Value`s (`arith::IndexCastOp`), reads `EnqueueDMAOp::getTargetCoreType`, and for a **SPARSE** target (core-type 2) divides the core id by the per-SparseCore TEC count before returning the `{device, core}` pair:

```text
getRemoteDeviceAndSparseCoreIds<EnqueueDMAOp>(builder, deviceId, coreId):
   deviceId = arith::IndexCastOp::create(... deviceId)     ; 0x13511722
   coreId   = arith::IndexCastOp::create(... coreId)       ; 0x135117C3
   switch (EnqueueDMAOp::getTargetCoreType()):              ; 0x14AF8B20
       case 0 (TENSOR):  r13 = 0
       case 1:           r13 = 2
       case 2 (SPARSE):  tec  = *(int*)(*(QWORD*)(target + 0x948) + 0x90)   ; per-SC TEC count
                         coreId = DivUIOp(coreId, ConstantIndexOp(tec))     ; the SPARSE sub-core divide
                         r13 = 4
   return FailureOr<DeviceAndCoreIds>{ deviceId, coreId }
```

The `[target+0x948]+0x90` field is the per-SparseCore TEC-sequencer count (16 on v5p/v6e/v7x), and the `DivUIOp` here is the **exact inverse** of the destination-id SPARSE stride (`SparseCoresPerLogicalDevice × TEC_count`). At the call site, `issueGeneralDma`'s remote arm (`0x1350B4A1`) hands the two `DeviceAndCoreIds` fields to `GetRemoteMemBase` as `v2` (`remoteCoreId`) and `v3`, which become `get_remote_memref` operands 1 & 2.

| stage | fn (@VA) | action |
|---|---|---|
| 0. device/core id operands | `getRemoteDeviceAndSparseCoreIds` `0x13511660` | `arith::IndexCastOp` each id |
| 1. target core type | `EnqueueDMAOp::getTargetCoreType` `0x14AF8B20` | 0 TENSOR / 1 / 2 SPARSE (memref `MemorySpace` → CoreType) |
| 2. SPARSE sub-core divide | `[target+0x948]+0x90` (TEC count) | `DivUIOp(coreId, TEC_count)` |
| 3. build `DeviceAndCoreIds` | (return `FailureOr<>`) | `{deviceId, coreId}` |
| 4a. → DMA `destination_id` | `issueGeneralDma` (P-3-327/335) | same ids → topology divisor → 5-bit slot |
| 4b. → `get_remote_memref` | `issueGeneralDma` remote arm `0x1350B4A1` → `GetRemoteMemBase` `0x13D88660` | same ids → op1=`remoteCoreId`, op2=`v3` |

Both 4a and 4b consume the stage-3 `{device, core}`; the SPARSE TEC-count divisor `[target+0x948]+0x90` is the same field in stage 2 as in the destination-id SPARSE stride. The data base is resolved here; the routed sync core is named by the destination-id — two consumers of one `{device, core}` computation. The destination-id topology arithmetic and the GeneralDma assembler that routes it are on [StartRemoteDma](start-remote-dma.md) and the GeneralDma emitter.

> **GOTCHA —** the optional 4th operand (`v4`) of `get_remote_memref` is left **null** by the remote-DMA path: `issueGeneralDma` passes `rcx = 0 / r8d = 0` (the present byte is clear). Its role — a remote sflag/offset second component — is HIGH, observed only from the optional operand existing in `build` and from the collective drivers' richer call shape, not CHECK-pinned.

---

## Related Components

| Name | Relationship |
|---|---|
| `GetRemoteMemBase` (`0x13D88660`) | the resolver that builds the op from `{localMemref, remoteCoreId, …}` |
| `MemorySpaceToAny` (`0x14B786E0`) | the remote-addressability filter (data memories promote; control spaces FATAL) |
| `GetRemoteMemRefOpLowering::matchAndRewrite` (`0x1357AE40`) | the LLVM lowering that re-tags the base pointer |
| `GetAnyTypeFromAddressSpace` (`0x1357B400`) | concrete AS-id → `"_any"` superset (the generic re-tag target) |
| `tpu_addrspacecast::create` (`0x146D5EA0`) | the 1-operand cast that performs the generic re-tag |
| `getRemoteDeviceAndSparseCoreIds` (`0x13511660`) | the shared `{device, core}` producer for `remoteCoreId` and the DMA destination-id |

## Cross-References

- [On-Pod Collectives — Section Map](overview.md) — the navigational entry for Part XIII; the SparseCore-offload substrate this remote-memref unit sits on.
- [StartRemoteDma](start-remote-dma.md) — the all-to-all producer + `SubsliceToFullSliceGlobalCoreId`, and the GeneralDma transfer that consumes the remote base resolved here.
- [SC-Offload Config Builder](sc-offload-config-builder.md) — the SparseCore-offload collective config builder that drives the remote-DMA collective drivers.
- [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md) — the SparseCore pointer representation and the AS-id ↔ `MemorySpace` table the `"_any"` promotion lands in; why routing is operands, not pointer bits.
- [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) — how the `tpu_addrspacecast` family selects in the SelectionDAG (the on-tile cast ISel mechanics).
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part XIII — On-Pod Collectives & Barriers / SparseCore-offload collectives — [back to index](../index.md)
