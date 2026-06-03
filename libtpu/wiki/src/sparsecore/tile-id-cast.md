# Tile-ID Cast

> *Every address, opcode value, operand index, and string literal on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`). `.text` VA equals file offset at `0xe63c000`, `.rodata` at `0x84a0000` — both identity-mapped. Address-space integers are decimal/hex as the binary uses them (`0xc9`=201 TileSpmem, `0xca`=202 Spmem). Other versions differ.*

## Abstract

On the SparseCore, a pointer into **TileSpmem** (LLVM address space `0xc9`/201) is *on-tile* — it names a word offset inside one tile's private scratch, with no encoding of *which* tile. The hardware EXECUTE lane (TEC) addresses that tile through a runtime register, not through pointer bits. The **tile-id addrspacecast** is the MLIR-dialect op family that bridges the two: it re-tags an on-tile TileSpmem pointer to the peer off-tile-addressable **Spmem** space (`0xca`/202) *while pairing it with the tile-id read from the `STILEID` hardware register*. This page documents the **on-tile 2-operand cast lowering body** — the `CastTileSpmemPointerToSpmem` driver — the **tile-id composition** (how a tile-id and a base pointer become the two operands of one cast op), and the **emitted node sequence** the lowering produces.

The central, instruction-grade finding: **the tile id is NOT packed into pointer bits.** The cast is value-preserving on the pointer half (the result carries the same 32-bit word offset), and the tile id rides as the cast op's *second SSA operand* — an `i32` produced by `tpu_tileid` (`llvm_tpu.tileid`, the `STILEID.VRES` register read). Downstream, the lowered TileSpmem load/store consumes the base as a scalar base-register `MCOperand` and the tile context as the sequencer-lane bundle it sits in. The "fat pointer" the data-layout reserves for AS7/8/9 plays no part here — see [Fat Pointers (AS7/8/9)](fat-pointers-as789.md) for why that reserve is dead AMDGPU boilerplate and not a TPU structured pointer.

For reimplementation, the contract is:

- **Two operands, one result.** A TEC-lane addrspacecast is `{base : !ptr<src_as>, tileId : i32} -> !ptr<dst_as>`. Trait set `ZeroRegions, OneResult, OneTypedResult<Type>, ZeroSuccessors, NOperands<2u>, MemoryEffectOpInterface`. Operand 0 is always the base pointer; operand 1 is always the tile id.
- **The tile id is a register read, not a constant.** Operand 1 is the `i32` SSA result of `tpu_tileid` (`STILEID.VRES` / `stileid.u32`). The lowering synthesises it on demand if the call site does not already supply one.
- **The sequencer attribute is the gate.** The lowering only injects a tile id when the enclosing `LLVMFuncOp` carries `sc.sequencer == "execute"` (TEC). On any other sequencer (SCS / TC / TAC) the cast is the 1-operand form and the lowering rejects a TileSpmem source with a hard diagnostic.
- **The composition is operand pairing, not arithmetic.** No add, no shift, no multiply combines the tile id and the base. "Composition" here means *the two are bound side-by-side as the cast's operands*; the pointer value is re-tagged unchanged and the tile id is threaded separately to the consuming load/store.

| | |
|---|---|
| **Lowering driver** | `xla::tpu::sparse_core::CastTileSpmemPointerToSpmem` @ `0x135b8400` |
| **Emitted cast op** | `tpu_addrspacecast_spmem::create` @ `0x146d67e0` (TileSpmem `0xc9` → Spmem `0xca`) |
| **Tile-id source** | `tpu_tileid::create` @ `0x149883a0` — `llvm_tpu.tileid`, 0-operand / 1-result `i32` |
| **Tile-id MLIR origin** | `TileIdOp` → `ReadIntegerRegisterOpLowering<TileIdOp,tpu_tileid>::matchAndRewrite` @ `0x1359c7c0` |
| **HW register** | `STILEID.VRES` / `stileid.u32` (`SparseCoreScalarAlu1ReadRegisterTileid`; vfc/glc/gfc) |
| **Operand order** | op0 = base pointer · op1 = tile-id `i32` (two `addOperands(...,1,...)` calls) |
| **Sequencer gate** | `sc.sequencer` inherent attr == `"execute"` (immediates `0x63657865`/`0x65747563`) |
| **On-tile predicate** | `IsOffTileMemory` @ `0x13d7ac00` = `(ms & ~0x10) != 2` — on-tile iff MS∈{2, 0x12} |
| **Dst address space** | `0xca` (202, Spmem) set into the out result-AS field |
| **Pointer-bit pack?** | **NO** — value-preserving re-tag; tile id is a paired operand (see [addrspacecast ISel](addrspacecast-isel.md)) |
| **Confidence** | CONFIRMED (lowering body, operand order, arity split, sequencer gate, on-tile predicate read from decompile) unless a row says otherwise |

---

## The On-Tile Addressing Problem

### Purpose

This unit states *why* the cast exists before how it lowers, so a reimplementer understands what the tile id is for.

SparseCore memory spaces split into on-tile and off-tile. The on-tile predicate is exact and tiny:

```c
// xla::tpu::sparse_core::lowering_util::IsOffTileMemory @ 0x13d7ac00
bool IsOffTileMemory(MemorySpace ms) {
    return (ms & 0xFFFFFFEF) != 2;     // (ms & ~0x10) != 2
}
```

Off-tile is *everything except* MS `2` (`tile_spmem`) and MS `0x12` (`tile_spmem_cb`) — the `& ~0x10` folds the circular-buffer variant onto the same on-tile class. A TileSpmem pointer (LLVM AS `0xc9`) is therefore on-tile: it is a word offset inside *the current tile's* scratch, and it carries no tile index. The TEC EXECUTE lane runs one logical program across many tiles; to address a specific tile's TileSpmem from a peer-addressable space it must supply the runtime tile select. That select is the `STILEID` register value, and the tile-id addrspacecast is the IR construct that attaches it.

> **NOTE — TileSpmem ⇄ Spmem is the on-tile/off-tile boundary.** `0xc9` (TileSpmem) is the on-tile view; `0xca` (Spmem) is the off-tile-addressable peer. The cast converts the former to the latter so the EXECUTE lane can name the tile explicitly. The two address-space integers, and the full SC AS roster, are owned by the address-space pages; this page only uses the `0xc9`→`0xca` pair.

### A Note on Names

Two name strings appear for the same op and must not be confused:

| Name | String (decompile) | Where it appears |
|---|---|---|
| MLIR op registration name | `llvm_tpu.addrspacecast.spmem` (underscore `llvm_tpu`) | `OperationState` ctor in `::create` |
| MLIR op registration name | `llvm_tpu.tileid` | `tpu_tileid::create` `OperationState` |
| Printed / intrinsic name | `llvm.tpu.addrspacecast.spmem` (dot `llvm.tpu`) | the LLVM intrinsic the op lowers to |
| Printed / intrinsic name | `llvm.tpu.tileid` / `llvm.tpu.tileid.lock` | the printed intrinsic / op-name |

The `OperationState` ctor strings read from the decompiled `::create` bodies are the `llvm_tpu.` (underscore) registration names; the `llvm.tpu.` (dotted) names are the printed-intrinsic forms. A reimplementer registering the dialect uses the underscore name; a reimplementer printing IR sees the dotted one.

---

## The 2-Operand Cast Lowering Body

### Purpose

`CastTileSpmemPointerToSpmem` is the lowering driver that performs the tile-id composition and emits the cast. Its body is short and fully decoded; this is the authoritative description of the on-tile cast lowering.

### Signature and Control Flow

```c
// xla::tpu::sparse_core::CastTileSpmemPointerToSpmem @ 0x135b8400
//   (ConversionPatternRewriter &rw, Operation *op, Value base, Value &outPtr, uint &outAS)
// Returns LogicalResult; on success writes outPtr (the re-tagged Spmem ptr) and outAS=202.

LogicalResult CastTileSpmemPointerToSpmem(rw, op, base, /*out*/ outPtr, /*out*/ outAS) {

  // (1) Walk block parents up to the enclosing LLVM::LLVMFuncOp.
  Operation *fn = op;
  for (;;) {
      Block *b = fn->block;                       // *(op+16)
      if (!b) { fn = nullptr; break; }
      fn = b->getParentOp();
      if (!fn) { fn = nullptr; break; }
      if (typeID(fn) == TypeID<LLVM::LLVMFuncOp>)  // *(*(fn+48)+16) == &LLVMFuncOp::id
          break;
  }

  // (2) Read the sequencer attribute off that function.
  Attribute seq = (fn && fn->numAttrs >= 0x1000000)
                    ? fn->getInherentAttr("sc.sequencer", 12)   // 0x1d8cc800
                    : DictionaryAttr::get(op+56, "sc.sequencer", 12);
  StringRef s = StringAttr::getValue(seq);

  // (3) Gate on "execute" (TEC): length 7 AND bytes == "execute".
  if (s.size() == 7 &&
      (s[0..3] == 0x63657865 /*"exec"*/) && (s[3..6] == 0x65747563 /*"cute"*/)) {

      // (4) Compose the tile id: synthesise tpu_tileid if the caller supplied none.
      Value tileId = providedTileId;               // a3
      if (!tileId) {
          Type i32 = rw.getI32Type();               // 0x1d853c40
          tileId   = tpu_tileid::create(rw, loc, i32);   // 0x149883a0  (STILEID read)
      }

      // (5) Build the destination pointer type in Spmem (0xca).
      Type spmemPtr = LLVM::LLVMPointerType::get(ctx, /*AS=*/0xca);   // 0x1746eb40

      // (6) Emit the 2-operand cast: {base, tileId} -> spmemPtr.
      outPtr = tpu_addrspacecast_spmem::create(rw, loc, spmemPtr, base, tileId); // 0x146d67e0
      outAS  = 202;                                 // 0xca, the result address space
      return success();
  }

  // (7) Not TEC: hard error — TileSpmem DMA is execute-only.
  return op->emitError("DMA to or from TileSpmem only allowed on TEC, but got ") << s;
}
```

### Step-by-Step

| Step | Action | Decompile anchor | Confidence |
|---|---|---|---|
| 1 | Walk `Block::getParentOp` up to the `LLVM::LLVMFuncOp` (TypeID-matched) | parent loop @ `0x135b8400`; `TypeIDResolver<LLVMFuncOp>` compare | CONFIRMED |
| 2 | Read `sc.sequencer` inherent attr (string, len 12) | `getInherentAttr` @ `0x1d8cc800`, called `@0x135b8479` | CONFIRMED |
| 3 | Compare to `"execute"` — len 7 and bytes `0x63657865`/`0x65747563` | compare `@0x135b84c5` | CONFIRMED |
| 4 | If no tile id supplied: `getI32Type` then `tpu_tileid::create` | `getI32Type` @ `0x1d853c40`; `tpu_tileid::create` @ `0x149883a0` `@0x135b85f6` | CONFIRMED |
| 5 | `LLVMPointerType::get(ctx, 0xca)` — Spmem ptr type | `0x1746eb40` `@0x135b8610` | CONFIRMED |
| 6 | `tpu_addrspacecast_spmem::create(rw, loc, spmemPtr, base, tileId)` | `0x146d67e0` `@0x135b8629` | CONFIRMED |
| 7 | Set out AS = `202` (`0xca`) | store `@0x135b8639` | CONFIRMED |
| — | Non-TEC path emits the execute-only diagnostic | string `"DMA to or from TileSpmem only allowed on TEC, but got "` | CONFIRMED |

> **GOTCHA — the lowering synthesises the tile id when none is passed.** The driver takes the candidate tile id as an in/out argument. If the call site already produced a `tpu_tileid` it reuses it; otherwise it *builds one in place* (step 4). Both branches end at the same emit. A reimplementer must not assume the tile id always arrives pre-built — the lowering is responsible for materialising the `STILEID` read at the cast site when the source IR did not.

> **NOTE — `sc.sequencer` lives on the function, not the op.** The gate reads the *enclosing `LLVMFuncOp`*'s inherent attribute, walking out of nested blocks first (step 1). A TileSpmem cast in a non-`execute` function is not a tile cast at all — it is the error path. This is the structural reason the arity split below tracks the sequencer exactly.

---

## Tile-ID Composition: Where the `i32` Comes From

### Purpose

The "tile-id composition" is the production of operand 1. It is not arithmetic; it is a single register read lowered to an `i32` value. This unit documents that producer.

### `TileIdOp` → `tpu_tileid`

The MLIR `sc_tpu.tile_id` op (`TileIdOp`) lowers through a generic register-read pattern:

```c
// ReadIntegerRegisterOpLowering<TileIdOp, tpu_tileid>::matchAndRewrite @ 0x1359c7c0
LogicalResult matchAndRewrite(TileIdOp op, Adaptor, ConversionPatternRewriter &rw) {
    Type i32 = rw.getI32Type();                       // 0x1d853c40
    Value v  = tpu_tileid::create(rw, op->loc, i32);  // 0x149883a0
    rw.replaceOp(op, v);                              // vtable slot *(*rw+8)
    return success();
}
```

`tpu_tileid::create` builds an op with **zero operands and one `i32` result**:

```c
// mlir::sparse_core::tpu_tileid::create @ 0x149883a0
Value tpu_tileid::create(OpBuilder &b, Location loc, Type resultTy) {
    OperationState st(loc, "llvm_tpu.tileid", 15);    // NO addOperands calls
    st.addType(resultTy);                             // result type only
    return b.create(st);                              // typeID-checked
}
```

The intrinsic reads the SparseCore scalar **tile-id register**: the per-generation MC op `STILEID.VRES` / `stileid.u32` (`SparseCoreScalarAlu1ReadRegisterTileid`, present on vfc/glc/gfc). So operand 1 of the cast is a *runtime* `i32` — the executing tile's index — not a compile-time immediate.

### Why It Is Not Arithmetic

The data-layout's AS7/8/9 reserve would, in an AMDGPU-style fat pointer, fold a descriptor and an offset into one wide value. The SparseCore does no such packing for the tile id (see [Fat Pointers (AS7/8/9)](fat-pointers-as789.md)). There is no `imul`/`shl`/`or` that merges the tile id into the base pointer in the lowering body above — the base flows through unchanged and the tile id is a distinct SSA value bound as operand 1. The pointer half is value-preserving across the cast (the off-tile re-tag path lowers to a value-preserving node; covered on [addrspacecast ISel](addrspacecast-isel.md)).

> **QUIRK — "composition" is operand binding, not bit packing.** Reimplementers coming from a fat-pointer ISA expect a tile id to occupy high bits of a wide pointer. Here the tile id is a sibling operand. The only "composition" is the `{base, tileId}` operand pair (the two `addOperands` calls in `::create`). The hardware consumes them as two physical things — a base register and a sequencer-lane select — never as one address word.

---

## The Emitted Node Sequence

### Purpose

This unit is the exact op sequence the lowering produces, end to end, with the operand structure of the cast op pinned from `::create`.

### What `tpu_addrspacecast_spmem::create` Builds

```c
// mlir::sparse_core::tpu_addrspacecast_spmem::create @ 0x146d67e0
//   (OpBuilder &b, Location loc, Type resultTy, Value base, Value tileId)
Value tpu_addrspacecast_spmem::create(b, loc, resultTy, base, tileId) {
    OperationState st(loc, "llvm_tpu.addrspacecast.spmem", 28);
    Value op0 = base;
    Value op1 = tileId;
    st.addOperands(&op0, 1);     // operand 0 = base pointer     (@0x146d67e0 first addOperands)
    st.addOperands(&op1, 1);     // operand 1 = tile-id i32      (second addOperands)
    st.addType(resultTy);        // result type (Spmem ptr)
    return b.create(st);         // typeID-checked
}
```

Two distinct `addOperands(..., 1, ...)` calls — one per operand — is the byte-level proof of operand order: **base first, tile id second**. The `_tec` twin is byte-identical with name `"llvm_tpu.addrspacecast.tec"` (len 26).

### The Full Sequence

For a TileSpmem load/store on the EXECUTE lane, the lowering emits, in order:

```text
1. %tid  = "llvm_tpu.tileid"() : () -> i32                      // tpu_tileid::create (if not already present)
                                                                //   = STILEID register read
2. %sp   = "llvm_tpu.addrspacecast.spmem"(%base, %tid)          // tpu_addrspacecast_spmem::create
              : (!ptr<0xc9 TileSpmem>, i32) -> !ptr<0xca Spmem>  //   op0=base, op1=tid; value-preserving on ptr
3.  ... downstream: the lowered TileSpmem load/store consumes
       %sp as a SparsecoreVectorBase MCOperand (scalar base reg)
       and the tile context as the SparseCoreTecBundle it sits in.
```

| Node | Op | Operands | Result | Confidence |
|---|---|---|---|---|
| 1 | `tpu_tileid` (`llvm_tpu.tileid`) | none | `i32` (STILEID) | CONFIRMED |
| 2 | `tpu_addrspacecast_spmem` | op0 = `%base` (`0xc9`), op1 = `%tid` (`i32`) | `!ptr<0xca>` (Spmem) | CONFIRMED |
| 3 | consume at ISA | base reg `MCOperand` + sequencer-lane bundle | — | HIGH |

### The Consumer

The lowered TileSpmem access is emitted by the SC `isa_emitter`:

```text
isa_emitter::EmitVectorLoadOrStore<SparsecoreVectorBase, SparsecoreVectorOffset,
    SparsecoreVectorMask, SparsecoreVectorStride,
    SparseCoreTecVectorLoad_TileSpmemLoad, SparseCoreTecBundle>
  glc instances @ 0x13a362c0 / 0x13a378c0 / 0x13a36000  (+ gfc twins)
  proto op types: SparseCoreTecVectorLoad_TileSpmemLoad,
                  SparseCoreTecVectorStore_TileSpmemStore (+ Indexed/CircularBuffer/PostUpdate)
```

The base flows in as a `SparsecoreVectorBase` `MCOperand` — a scalar base register — alongside offset/stride/mask. The per-tile context is the `SparseCoreTecBundle` (the EXECUTE sequencer-lane the instruction sits in), **not** an address-bit field. The Mosaic-side verify `"failed to verify that all of {tile, base} have same element type"` enforces that the tile and base share a pointee type.

> **NOTE — operand→MCOperand binding is structural, not single-getter-traced (HIGH).** That the cast's `%base` SSA result *is* the value the lowered load reads as its `SparsecoreVectorBase` rests on SSA def-use through the LLVM-dialect translation, not on one getter at the ISA-emit site. The `::create` operand order (op0=base) and the emitter reading a base-register `MCOperand` are both CONFIRMED; the end-to-end binding is HIGH. The exact `SparsecoreVectorBase`/`SparsecoreVectorOffset` field bit positions for the non-circular-buffer TileSpmem slot were not decoded here.

---

## The Arity Split: TEC Casts Carry a Tile Id, SCS/TC/TAC Do Not

### Purpose

The tile-id operand is present on exactly the TEC-lane casts. This unit pins that split from the function *signatures* (the most authoritative source — mangled in the symbol).

### The Discriminator

Each cast's `::create` signature ends in either `…Value` (one operand) or `…ValueValue` (mangled `ValueES6_`, two operands). The two-operand set is exactly the casts targeting the TEC (EXECUTE) lane, which addresses per-tile TileSpmem and needs the runtime tile select. The SCS / TC / TAC lanes are singular per core and address their memory without a per-tile index.

| `::create` (@VA) | Op name | Arity | Tile-id op? | Sequencer |
|---|---|---|:---:|---|
| `tpu_addrspacecast` `0x146d5ea0` | `…addrspacecast` | 1 | no | generic |
| `tpu_addrspacecast_scs` `0x146d5f80` | `…addrspacecast.scs` | 1 | no | SCS |
| `tpu_addrspacecast_scs_sflag_scs` `0x146d6060` | `…scs.sflag.scs` | 1 | no | SCS |
| `tpu_addrspacecast_sflag_tile_scs` `0x146d6140` | `…sflag.tile.scs` | 1 | no | SCS |
| `tpu_addrspacecast_sflag_tile_sflag_scs` `0x146d6220` | `…sflag.tile.sflag.scs` | 1 | no | SCS |
| `tpu_addrspacecast_sflag_tile_sflag_tec` `0x146d6300` | `…sflag.tile.sflag.tec` | **2** | **YES** | **TEC** |
| `tpu_addrspacecast_sflag_tile_tac` (NOperands<1>) | `…sflag.tile.tac` | 1 | no | TAC |
| `tpu_addrspacecast_sflag_tile_tec` `0x146d6400` | `…sflag.tile.tec` | **2** | **YES** | **TEC** |
| `tpu_addrspacecast_smem` `0x146d6500` | `…addrspacecast.smem` | **2** | **YES** | **TEC** |
| `tpu_addrspacecast_smem_tile_scs` `0x146d6600` | `…smem.tile.scs` | 1 | no | SCS |
| `tpu_addrspacecast_smem_tile_tec` `0x146d66e0` | `…smem.tile.tec` | **2** | **YES** | **TEC** |
| `tpu_addrspacecast_spmem` `0x146d67e0` | `…addrspacecast.spmem` | **2** | **YES** | **TEC** |
| `tpu_addrspacecast_tac` (NOperands<1>) | `…addrspacecast.tac` | 1 | no | TAC |
| `tpu_addrspacecast_tc` `0x146d68e0` | `…addrspacecast.tc` | 1 | no | TC |
| `tpu_addrspacecast_tec` `0x146d69c0` | `…addrspacecast.tec` | **2** | **YES** | **TEC** |
| `tpu_addrspacecast_tec_sflag_tec` `0x146d6ac0` | `…tec.sflag.tec` | **2** | **YES** | **TEC** |

The seven 2-operand casts (`_sflag_tile_sflag_tec`, `_sflag_tile_tec`, `_smem`, `_smem_tile_tec`, `_spmem`, `_tec`, `_tec_sflag_tec`) are precisely the TEC-targeting set; their signatures end in `ValueES6_`. The 1-operand casts end in `ValueE`. `_tac` has no separate `::create` body — its arity is read from the name plus the `NOperands<1>` trait.

> **QUIRK — `sflag.tile.*` exists in both lanes.** The sflag-tile cast appears as `_sflag_tile_tec` (2-op, TEC) and `_sflag_tile_scs` (1-op, SCS). Same logical operation, different sequencer ⇒ different arity. The `CastSflagPointerToSflagAny` driver (`0x135b8a00`) applies the identical `sc.sequencer` gate for the three sflag spaces. A reimplementer must select the cast variant by the enclosing function's sequencer, never by the cast's *name fragment* alone.

---

## Per-Generation Presence

| Mechanism | VF (vfc, v5e) | GL (glc, v5p) | GF (gfc, v6e) |
|---|:---:|:---:|:---:|
| `tpu_tileid` / `STILEID.VRES` register read | yes | yes | yes |
| 2-operand TEC tile-id casts (7 variants) | yes | yes | yes |
| 1-operand SCS/TC/TAC casts | yes | yes | yes |
| `CastTileSpmemPointerToSpmem` driver + `sc.sequencer` gate | yes | yes | yes |
| `IsOffTileMemory` predicate `(ms & ~0x10) != 2` | yes | yes | yes |
| TileSpmem load/store consumes `SparsecoreVectorBase` reg | yes | yes | yes |

The `tpu_addrspacecast_*` op family and the `CastTileSpmemPointerToSpmem` driver are shared across the SC generations; the tile-id `i32` register read (`STILEID`) is present on all three. The TAC sequencer is absent on gfc (Trillium has no TAC), but the TAC-lane *casts* are 1-operand on every generation regardless, so the arity split is generation-invariant.

---

## Limits and Open Items

| Item | Status | Confidence |
|---|---|---|
| `CastTileSpmemPointerToSpmem` lowering body (gate, tile-id synth, emit, out-AS) | full body decoded | CONFIRMED |
| `tpu_tileid::create` 0-operand / 1-`i32`-result | body decoded; no `addOperands` | CONFIRMED |
| `TileIdOp` → `tpu_tileid` lowering (getI32Type + create + replaceOp) | body decoded | CONFIRMED |
| Operand order op0=base, op1=tileId | two `addOperands(...,1,...)` calls, base then tileId | CONFIRMED |
| Arity split: 7 TEC casts 2-op, rest 1-op | mangled `::create` signatures (`ValueES6_` vs `ValueE`) | CONFIRMED |
| Sequencer gate == `"execute"` (len 7, `0x63657865`/`0x65747563`) | byte compare in body | CONFIRMED |
| On-tile predicate `(ms & ~0x10) != 2` | body decoded | CONFIRMED |
| Dst AS = `0xca` (202) Spmem for the spmem cast | store `@0x135b8639` | CONFIRMED |
| Tile id is a paired operand, not pointer bits | no pack arithmetic in body; value-preserving re-tag | CONFIRMED (this lowering) / see [addrspacecast ISel](addrspacecast-isel.md) for the ISD half |
| `%base` SSA result == load's `SparsecoreVectorBase` MCOperand | structural def-use, not single-getter-traced | HIGH |
| `_tac` arity (1-op) | name + `NOperands<1>` trait; no `::create` body | HIGH |
| The exact dst AS of the 1-op SCS/TC/TAC casts | only `_spmem`/`_smem`/`_tec` producers re-decoded; others rest on the sflag-promotion table | LOW |
| `SparsecoreVectorBase`/`Offset` field bit layout for the non-CB TileSpmem slot | not decoded here | LOW |
| SelectCode MatcherTable arm threading op#1 into the tile-select MCOperand | not byte-walked here | LOW — see [addrspacecast ISel](addrspacecast-isel.md) |

---

## Cross-References

- [Fat Pointers (AS7/8/9)](fat-pointers-as789.md) — the 160/128/192-bit non-integral structured-pointer reserve (AMDGPU buffer-fat-pointer ABI) and why no TPU op constructs it; the `CircularBufferDescriptor` (the SC's only structured pointer). This page's tile id deliberately does **not** use that reserve.
- [addrspacecast ISel](addrspacecast-isel.md) — the IR→ISD conversion, the value-preserving `LowerADDRSPACECAST` node (`MVT::i32`), and the `SelectCode` MatcherTable that pairs the tile-id operand into the lowered load/store.
- [SparseCore Architecture](architecture.md) — the SCS/TAC/TEC engine roles; why the EXECUTE (TEC) lane is the one that addresses per-tile TileSpmem.
- [SparseCore Overview](overview.md) — the embedding datapath this on-tile addressing serves.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the off-tile indirect-DMA datapath; the off-tile counterpart to the on-tile addressing closed here.
- [getSequencerType](getsequencertype.md) — how the SCS/TAC/TEC sequencer is decided, the same `sc.sequencer` distinction that gates this cast.
- [Scalar Opcode Enum](scalar-opcode-enum.md) — the `SparseCoreScalarAlu1ReadRegisterTileid` (`STILEID`) opcode in the surrounding scalar roster.
- [VectorLoad Slot](vectorload-slot.md) / [VectorStore Slot](vectorstore-slot.md) — the TileSpmem load/store slots that consume the re-tagged pointer as a `SparsecoreVectorBase` register.
- [CBREG Circular-Buffer Register](cbreg.md) — the `tile_spmem_cb` (MS `0x12`) on-tile variant the `& ~0x10` fold collapses onto the same on-tile class.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore pointers & DMA — [back to index](../index.md)
