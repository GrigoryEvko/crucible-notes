# Segmented Scan

> *Every address, reduction-string XOR constant, vtable slot, struct offset, operand name, and error string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`, not stripped) — from the `SegmentedScanOpLowering::matchAndRewrite` body, `SegmentedScanOp::build`/`create`/`getReductionOp`, the `FindAndEmitToUnusedPort<…SegmentedAddScanF32>` allocator, `XlaSparseDenseMatmulWithCsrInputOp::Compile`, and `MinibatchingDecomposition::CreateDynamicSliceCsr`. `.text`/`.rodata` VMA == file offset (base `0xe63c000`/`0x84a0000`); `.data.rel.ro` VMA−`0x200000` == offset (reloc addends read via `readelf -rW`). Addresses apply to this build; other versions differ.*

## Abstract

A `SegmentedScanOp` is a prefix scan that **resets its running accumulator at per-segment boundaries**, so a single hardware scan over a packed ragged batch produces one independent prefix per segment. It is the reduce primitive of the SparseCore embedding-`sum` lookup: rows gathered from a dense embedding table are concatenated into one long vector, and the segment-boundary operand — the CSR row-offsets of the sparse minibatch — partitions that vector into per-sample runs. The inclusive scan over each run, taken at its last lane, is the summed embedding row. This page documents the MLIR `SegmentedScanOp` lowering (`0x13589d40`), the segment-boundary operand binding that drives the reset, and the `XlaSparseDenseMatmulWithCsrInput` HLO custom-call chain that feeds it.

The lowering is a near-twin of the plain `ScanOpLowering` documented in [Scan Datapath](scan-datapath.md), and deliberately so: it reuses the *identical* 3-char reduction-string XOR switch (`sum`/`min`/`max`) and the *identical* element-type axis (`{i32, f32, i16, bf16}` identity-compared against the builder's canonical types). The structural delta is twofold. First, it binds a **second SSA operand** — the segment-id vector — where the plain scan binds a per-lane vector mask; `SegmentedScanOp::build` (`0x145fd4a0`) issues two unconditional `addOperands` calls, `(data, segment)` in order. Second, it has **no `i1`/`mprefix` count-active path** (a segment scan reduces data values; it does not population-count predicate bits). The emitted intrinsics are the segmented family `tpu_*_seg_scan*` — seven distinct codegen leaves, not the nine plain-scan leaves.

The decisive reimplementation fact is that the **segment boundary is a normal V read-port operand, not a side register and not a fixed port**. At ISA emit the data operand and the segment-id operand are each handed the lowest free port from a 7-entry greedy first-free allocator (`FindAndEmitToUnusedPort`, `0x13ab2aa0`); the segment-id lands in whatever port is free under the surrounding bundle's port pressure. The per-lane vector mask, by contrast, is a *separate* bundle field (`proto+0x38`), exactly as on the plain scan. A reimplementer who pins the segment-id to a fixed V-index, or who conflates it with the mask, mis-models the operand frame. This page traces the full HLO → dialect → intrinsic → ISA chain so a reimplementer can rebuild the embedding sum-lookup from the CSR custom-call down to the per-segment-reset scan.

For reimplementation, the contract is:

- **The lowering reuses the plain-scan reduction switch verbatim, then binds a second operand.** The reduction is decoded from a 3-char `StringRef` by constant XOR (`sum`=`0x7573|0x6d`, `min`=`0x696d|0x6e`, `max`=`0x616d|0x78`), the element type by identity-compare against `getI32Type`/`getF32Type`/`getI16Type`/`getBF16Type`. There is no enum, no `strcmp`. Same as the plain scan; the difference is the operand frame and the emitted intrinsic family.
- **`operand[0]` = data, `operand[1]` = segment boundary.** `SegmentedScanOp::build` adds both unconditionally (no `if(data)` guard, unlike `ScanOp::build`). The segment vector is the reset signal: where the segment id changes, the accumulator restarts.
- **Seven intrinsics are emitted, all `NOperands<2>`.** `tpu_add_seg_scan1xN{i,f}`, `tpu_add_half_seg_scan2xN` (`i16`/`bf16` share this packed-pair arm), `tpu_min_seg_scan1xN{i,f}`, `tpu_max_seg_scan1xN{i,f}`. `tpu_{min,max}_seg_scan2xN` are *registered but never codegen'd* — no `::create`. There is no `i1`/`mprefix` segmented path.
- **`i16`/`bf16` segmented-add is gated on a target capability.** The `sum` × `{i16,bf16}` arm calls vtable slot `+0x780` on the target subobject (`(**(ctx+0x68) + 1920)(…)`) and, on false, emits `"Currently seg scan add for bf16 is only supported"` and fails. A reimplementer on a generation without the bf16 ALU must reject the half-precision segmented sum.
- **The segment boundary rides a free V read port, not a fixed index or the mask.** The 7-port greedy first-free allocator (slots `+0x1c..+0x34`, present mask `+0x10`) assigns it whatever port is open. The per-lane mask is the separate `proto+0x38` field. Branch the operand routing on op identity.
- **The CSR row-offsets become the segment-id.** `XlaSparseDenseMatmulWithCsrInputOp::Compile` emits a `SparseDenseMatmulWithMinibatchingOp` custom-call; `MinibatchingDecomposition` slices the `concatenated_csr_pointers` into per-minibatch offset vectors; those offsets are the `SegmentedScanOp` `operand[1]`.

| | |
|---|---|
| **MLIR op** | `mlir::sparse_core::SegmentedScanOp` (SC dialect; `reduction_op` 3-char `StringAttr`) |
| **Lowering** | `SegmentedScanOpLowering::matchAndRewrite` `0x13589d40` — reduction × dtype → `tpu_*_seg_scan*` |
| **Build / create** | `SegmentedScanOp::build` `0x145fd4a0` (`addOperands(data)` then `addOperands(segment)`); `::create` `0x145fd5a0` |
| **Reduction read** | `SegmentedScanOp::getReductionOp` `0x145fd460` — property word `((w>>19)&0x10)+64` → `StringAttr::getValue` |
| **Result drain** | `LLVMStructType::getLiteral` `0x17471ae0` `{value, segment-id}` → `LLVM::ExtractValueOp` `0x1728c5a0` |
| **BF16 gate** | vtable `+0x780` (1920) on target subobject `(ctx+0x68)`; error `"Currently seg scan add for bf16 is only supported"` (`.rodata` `0x87036bf`, 49 B) |
| **ISA emit** | `EmitVectorResultUnop<…SegmentedAddScanF32>` (gfc `0x13aaf560`); port alloc `FindAndEmitToUnusedPort` `0x13ab2aa0` (gfc) / `0x13a4b680` (glc) |
| **ISA op (f32)** | `SparseCoreTecVectorExtended_SegmentedAddScanF32`, proto `inst` oneof `0x23` |
| **Dialect rewrite** | `PackedOperandsLowering ScanOpLowering<SegmentedScanOp>` `0x135f3000` (unpack → re-create → pack) |
| **HLO front-end** | `XlaSparseDenseMatmulWithCsrInputOp::Compile` `0xe650800` → custom-call `SparseDenseMatmulWithMinibatchingOp` (35 B), 7 operands |
| **CSR → segment-id** | `MinibatchingDecomposition::CreateDynamicSliceCsr` `0x13489ea0` slices `concatenated_csr_pointers` |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the `SegmentedScanOp` lowering, the segment-boundary reset, and the CSR matmul chain.** The plain-scan mask datapath, the two M-register bands, the post-scan `VectorSelect`, and the `i1`/`mprefix` count path live in [Scan Datapath](scan-datapath.md) and are not repeated. The `SegmentedAddScan` ISA operand frame, `VpackFormat` capability matrix, and the full segmented-scan proto oneof case map live in [Segmented Add-Scan](segmented-add-scan.md). The VEX bundle bit positions live in [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md). They are cross-linked, not duplicated.

---

## The Segment-Boundary Reset Model

### Purpose

Fix the semantic model before the lowering detail, because it is what distinguishes a segmented scan from the plain scan in [Scan Datapath](scan-datapath.md). A plain inclusive scan over a lane vector `x[0..N)` produces `y[i] = x[0] ⊕ x[1] ⊕ … ⊕ x[i]` for an associative reduction `⊕`. A *segmented* scan adds a parallel **segment-id vector** `s[0..N)`: the accumulator resets to the reduction identity whenever the segment id changes between adjacent lanes, so the scan never carries a value across a boundary.

### The reset rule

```text
Segmented inclusive scan — the accumulator restarts at each boundary
  data    x:  [ a0  a1  a2 | b0  b1 | c0  c1  c2  c3 ]
  segment s:  [  0   0   0 |  1   1 |  2   2   2   2 ]   (CSR-derived segment ids)
                          ^boundary ^boundary
  inclusive:  [ a0  a0⊕a1  a0⊕a1⊕a2 | b0  b0⊕b1 | c0  c0⊕c1  …  c0⊕c1⊕c2⊕c3 ]
              └── per-segment prefix; the carry does NOT cross a '|' ───────┘

  reset rule:  acc[i] = (s[i] == s[i-1]) ? acc[i-1] ⊕ x[i]   // continue the run
                                         : identity ⊕ x[i]    // s changed → restart
```

For the embedding sum-lookup the reduction `⊕` is `add` and each segment is one sample's bag of gathered embedding rows. The summed embedding for a sample is the inclusive scan value at the **last lane of its segment** — that is, the per-segment total. The CSR row-offsets define exactly where one sample's run ends and the next begins, which is why the offsets *are* the segment ids (see [The CSR Matmul Chain](#the-csr-matmul-chain)).

> **QUIRK — the segment operand resets the accumulator; it does not mask lanes.** A reader who has read [Scan Datapath](scan-datapath.md) knows the plain scan's `operand[1]` is a per-lane M-register mask that gates *which lanes participate*. The segmented scan's `operand[1]` is a *value* vector that the hardware compares lane-to-lane to decide *where to restart the carry*. They occupy the same SSA slot index but mean opposite things and route to different bundle fields. The mask still exists on a segmented scan (the separate `proto+0x38` field), but it is not the segment operand.

### Inclusive vs exclusive

The emitted intrinsics are the inclusive forms (`tpu_*_seg_scan*`): position `i` includes `x[i]`. The exclusive variant — `y[i] = identity ⊕ x[0] ⊕ … ⊕ x[i-1]`, excluding `x[i]`, resetting to identity at the first lane of each segment — is not a distinct intrinsic in this build; an exclusive segmented scan is synthesized by the front-end as a shift of the inclusive result (the exclusive seed is the per-segment identity at the boundary). The hardware primitive is inclusive. (The shift-to-exclusive rewrite is a front-end pattern, not on this page; LOW for its exact lowering.)

---

## The MLIR Lowering

### Purpose

`SegmentedScanOpLowering::matchAndRewrite` (`0x13589d40`) rewrites a `sparse_core::SegmentedScanOp` into one segmented-scan intrinsic, chosen by the reduction string and the result element type, then drains the `{value, segment-id}` result struct. It is a `ConvertOpToLLVMPattern`; the dispatch body is a flat reduction-string-XOR → element-type cascade, structurally identical to the plain `ScanOpLowering` (`0x1358ab00`) minus the `i1` arm and plus the segment operand.

### Entry Point

```text
sparse_core::SegmentedScanOp  (NOperands<2>, reduction_op StringAttr)
  └─ SegmentedScanOpLowering::matchAndRewrite           (0x13589d40)   ── reduction × dtype → intrinsic
       ├─ SegmentedScanOp::getReductionOp               (0x145fd460)   ── 3-char StringRef
       ├─ VectorType::getElementType                                   ── result element type
       ├─ Builder::get{I32,F32,I16,BF16}Type            (0x1d853c40 / 0x1d853980 / 0x1d853c20 / 0x1d853680)
       ├─ VectorType::get(i1, …)                        (0x1d894100)   ── per-lane boundary mask type
       ├─ LLVMStructType::getLiteral                    (0x17471ae0)   ── {value, segment-id} result pair
       ├─ tpu_*_seg_scan*::create                                      ── the 7 emitted leaves (TABLE)
       └─ LLVM::ExtractValueOp::create                  (0x1728c5a0)   ── pull value(idx0)/segment-id back
```

### Algorithm

The reduction string is decoded by a constant XOR over the 3 bytes — `len == 3` then `(word0 ^ K0) | (byte2 ^ K1) == 0`. The element type is fetched from the converted result `VectorType` and compared for pointer-identity against the builder's canonical types. There is **no** `i1` special case — the first branch is `sum`, not the count-active path:

```c
function SegmentedScanOpLowering_matchAndRewrite(op, adaptor):   // 0x13589d40
    red = op.getReductionOp()                       // StringRef, 3 chars (0x145fd460)
    elt = getElementType(converted_result_type)     // i32 / f32 / i16 / bf16

    // --- sum: (word0 ^ 0x7573) | (byte2 ^ 0x6d) == 0 ---       (line 84)
    if len(red) == 3 && red == "sum":
        if   elt == i32:  emit tpu_add_seg_scan1xNi          // 0x146d5c40  (line 254)
        elif elt == f32:  emit tpu_add_seg_scan1xNf          // 0x146d5a80  (line 273)
        elif elt in {i16, bf16}:
            // BF16/I16 capability gate — vtable +0x780 (1920) on target subobject (ctx+0x68)
            if !(**(ctx+0x68) + 1920)(ctx+0x68):              // line 289
                emitError("Currently seg scan add for bf16 is only supported")   // 0x87036bf, 49 B
                return failure
            emit tpu_add_half_seg_scan2xN                     // 0x146d45c0  (line 302)
        else: return failure

    // --- max: (word0 ^ 0x616d) | (byte2 ^ 0x78) == 0 ---       (line 87)
    elif len(red) == 3 && red == "max":
        if   elt == f32:  emit tpu_max_seg_scan1xNf          // 0x14730e00  (line 118)
        elif elt == i32:  emit tpu_max_seg_scan1xNi          // 0x14730fc0  (line 137)
        else: return failure                                  // no i16/bf16 max-seg arm

    // --- min: (word0 ^ 0x696d) | (byte2 ^ 0x6e) == 0 ---       (line 142)
    elif len(red) == 3 && red == "min":
        if   elt == f32:  emit tpu_min_seg_scan1xNf          // 0x147316c0  (line 177)
        elif elt == i32:  emit tpu_min_seg_scan1xNi          // 0x14731880  (line 213)
        else: return failure                                  // no i16/bf16 min-seg arm
    else:
        return failure   // xor ebx,ebx — unknown reduction string

    // --- result drain: the intrinsic returns an LLVMStructType{value, segment-id} ---
    struct_ty = LLVMStructType::getLiteral({value_vec, i1_seg_vec})   // 0x17471ae0
    value   = ExtractValueOp(struct_result, idx=0)        // 0x1728c5a0  (lines 218 / 394)
    seg_id  = ExtractValueOp(struct_result, idx=1)
    replaceOp(op, value)
    return success
```

The XOR immediates are the little-endian byte triples read directly off the `cmp` lines: `"sum"` = `s u`=`0x7573`, `m`=`0x6d`; `"max"` = `m a`=`0x616d`, `x`=`0x78`; `"min"` = `m i`=`0x696d`, `n`=`0x6e`. These are byte-identical to the plain `ScanOpLowering` constants — the two lowerings share the reduction vocabulary.

> **CORRECTION (P-3-423) — the reduction strings are `sum`/`min`/`max`, not `add`/`min`/`max`.** An earlier write-up inferred the add-reduction kind as the string `"add"`. The `matchAndRewrite` XOR test at `0x13589d8f` compares against `0x7573|0x6d` = `"sum"`. The canonical reduction-kind string for the embedding sum-lookup is `"sum"`. (Byte-confirmed; the plain `ScanOpLowering` uses the identical immediate.)

### The reduction × element-type → intrinsic map

Every arm is byte-anchored to a specific `tpu_*_seg_scan*::create` call site in the lowering body:

| reduction | result elt | → intrinsic | `::create` @ | gate | Confidence |
|---|---|---|---|---|---|
| `sum` | `i32` | `tpu_add_seg_scan1xNi` | `0x146d5c40` | — | CONFIRMED |
| `sum` | `f32` | `tpu_add_seg_scan1xNf` | `0x146d5a80` | — | CONFIRMED |
| `sum` | `i16` | `tpu_add_half_seg_scan2xN` | `0x146d45c0` | `+0x780` must be true | CONFIRMED |
| `sum` | `bf16` | `tpu_add_half_seg_scan2xN` | `0x146d45c0` | `+0x780` must be true | CONFIRMED |
| `max` | `f32` | `tpu_max_seg_scan1xNf` | `0x14730e00` | — | CONFIRMED |
| `max` | `i32` | `tpu_max_seg_scan1xNi` | `0x14730fc0` | — | CONFIRMED |
| `min` | `f32` | `tpu_min_seg_scan1xNf` | `0x147316c0` | — | CONFIRMED |
| `min` | `i32` | `tpu_min_seg_scan1xNi` | `0x14731880` | — | CONFIRMED |
| any | other elt | `emitError` → failure | — | — | CONFIRMED |

`i16` and `bf16` share the single `tpu_add_half_seg_scan2xN` arm (the packed-pair `PartialSum` widen). `min` and `max` have **no** `i16`/`bf16` emitted arm: `tpu_{min,max}_seg_scan2xN` are registered as dialect ops with the `NOperands<2>` trait but carry **no `::create`/`::build`** — declared-but-uncodegen'd. The lowering never produces a 2xN min/max segmented scan; only `add` has the half/2xN widen.

> **NOTE — only `add` has the half-precision segmented widen, and it is target-gated.** The `i16`/`bf16` segmented-sum arm is the *only* path that touches the `2xN` packed form, and it is legal only when the target's vtable slot `+0x780` (the bf16-ALU / EUP lane-width capability) returns true. The subobject pointer at `(ctx+0x68)` is set by `LowerToSparseCoreLlvmPass::lowerFunc` (`0x13568280`) to the codegen target's `+0x8` sub-object. On a generation without the native bf16 lane the lowering emits `"Currently seg scan add for bf16 is only supported"` (`.rodata` `0x87036bf`, 49 bytes) and fails — so the SC bf16 segmented sum is restricted to the gen that owns the bf16 ALU. This gate is the segmented-scan twin of the plain scan's identical `+0x780` check in [Scan Datapath](scan-datapath.md).

### Why no `i1` count path

The plain `ScanOpLowering` special-cases an `i1` (boolean) input to the `tpu_mprefix` population-count-prefix primitive (see [Scan Datapath](scan-datapath.md#the-i1-count-active-path)). The segmented lowering has **no** such arm: its first branch is `sum`, and the element-type axis is `{i32, f32, i16, bf16}` only. A segmented scan reduces data *values* partitioned by segment; it does not count predicate bits across a boundary. A reimplementer porting the plain-scan dispatch must drop the `i1` arm for the segmented variant.

---

## The Operand Frame

### Purpose

The lowering's behavior is only half the story; the other half is how the two SSA operands are bound at build time and routed at ISA emit. The segment boundary is the second operand, and its binding — both the MLIR `build` order and the ISA port allocation — is what a reimplementer must reproduce exactly to feed the reset signal to the hardware.

### Build order — data first, segment second

`SegmentedScanOp::build` (`0x145fd4a0`) takes `(OpBuilder, OperationState, Type result, Value data, Value segment, StringAttr reduction)` and issues **two unconditional** `addOperands` calls in order:

```c
// SegmentedScanOp::build(OpBuilder, OperationState&, Type, Value data, Value segment, StringAttr red)
//   (0x145fd4a0)
addOperands(state, &data,    1);     // operand[0] = data           (line 16)
addOperands(state, &segment, 1);     // operand[1] = segment boundary (line 17)
state.properties.reduction_op = red; // StringAttr property
```

Contrast `ScanOp::build` (`0x145f92e0`, documented in [Scan Datapath](scan-datapath.md#entry-point)): it *guards* the data operand (`if (data) addOperands(data)`) and then adds the per-lane vector mask as `operand[1]`. The segmented build has no guard and binds the segment vector as `operand[1]`. `getReductionOp` (`0x145fd460`) reads the `reduction_op` `StringAttr` from the op's inline-or-out-of-line property word: offset `((property_word >> 19) & 0x10) + 64`, then `StringAttr::getValue`.

> **GOTCHA — `operand[1]` means a vector mask for `ScanOp` and a segment boundary for `SegmentedScanOp`.** Both are SSA `operand[1]`, but the plain scan routes it to the in-scan M-register mask field (`proto+0x38`) and the segmented scan routes it to a V read port (a *value* operand). A reimplementer wiring the operand frame must branch on the op identity, not assume `operand[1]` is always the mask. The per-lane mask still exists on a segmented scan as the separate `proto+0x38` field; the segment operand is in addition to it, not instead of it.

### ISA emit — segment-id rides a free V read port

At ISA emit, `EmitVectorResultUnop<…SegmentedAddScanF32>` (gfc `0x13aaf560`) reads the MCInst operands and routes them. The per-lane mask comes from `operand[1]` → `GetVectorMask` → field `proto+0x38` with present-bit `proto+0x11 |= 1` (identical to the plain scan). The *data* value comes from `operand[2]` → `GetVregno` → `FindAndEmitToUnusedPort` — and so does the segment-id: each is assigned the next free port.

`FindAndEmitToUnusedPort` (gfc `0x13ab2aa0`, glc `0x13a4b680`) is a **greedy first-free** allocator over a 7-entry `btree_set` of unused `SparsecoreVregReadPort` (ports 0..6). It pops the lowest free port, then a `switch (port)` writes the resolved `Vregno` into one of seven contiguous struct slots and sets the matching present-bit:

```c
// FindAndEmitToUnusedPort<SparsecoreVregReadPort, …SegmentedAddScanF32>   (0x13ab2aa0 gfc)
port = btree_set_pop_lowest(unused_ports);            // erase the lowest free port
switch (port):                                        // line 29
    case 0:  inst[0x1c] = vregno; inst[0x10] |= 0x02; break;
    case 1:  inst[0x20] = vregno; inst[0x10] |= 0x04; break;
    case 2:  inst[0x24] = vregno; inst[0x10] |= 0x08; break;
    case 3:  inst[0x28] = vregno; inst[0x10] |= 0x10; break;
    case 4:  inst[0x2c] = vregno; inst[0x10] |= 0x20; break;   // line 53
    case 5:  inst[0x30] = vregno; inst[0x10] |= 0x40; break;
    case 6:  inst[0x34] = vregno; inst[0x10] |= 0x80; break;
    // (>6 is impossible: btree only holds 0..6)
if (unused_ports.empty()):                            // out of ports
    RetCheckFailSlowPath("!port_is_free.empty()");    // line 98 (isa_emitter_utils.h:3025)
statusor.port = port;                                 // returned port enum
```

| port id | struct slot (Vregno) | present-bit (`inst[0x10] \|=`) | returned enum |
|---|---|---|---|
| 0 | `+0x1c` | `0x02` | 0 |
| 1 | `+0x20` | `0x04` | 1 |
| 2 | `+0x24` | `0x08` | 2 |
| 3 | `+0x28` | `0x10` | 3 |
| 4 | `+0x2c` | `0x20` | 4 |
| 5 | `+0x30` | `0x40` | 5 |
| 6 | `+0x34` | `0x80` | 6 |

> **CORRECTION (P-3-423) — there are 7 V read ports and the segment-id is NOT a fixed V1.** An earlier write-up inferred a fixed `V1` segment port and a "6 ports / V0@+0x1c, V1@+0x24, V2@+0x28, Vmask@+0x2c" map. The decompile shows a `switch` over **seven** ports (0..6) writing slots `+0x1c..+0x34`, allocated greedily first-free from a `btree_set`. The segment-id lands in whatever port is free under the surrounding bundle's port pressure — its physical V-index is a function of allocator state, not a constant. The Vmask is a *separate* field at `+0x38`; the earlier map conflated the port pool with the mask. (Byte-confirmed; both gfc and glc share the allocator.)

The `SegmentedAddScanF32` ISA op is proto `inst` oneof case `0x23` (the accessor `mutable_segmented_add_scan_f32` at `0x13aaf600` compares `proto+0x58 == 0x23`). The bf16 form is `SegmentedAddScanBf16PartialSumBf16` (oneof `0x2e`) — the gfc/glc proto pool has only the `Bf16PartialSumBf16` form, not a wider `PartialSumF32` segmented variant. The full 18-entry segmented-scan oneof case map and the per-port bit packing in the final instruction word are owned by [Segmented Add-Scan](segmented-add-scan.md) / [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md); this page anchors only the operand-routing mechanism.

---

## The CSR Matmul Chain

### Purpose

The segmented scan does not appear in isolation — it is the reduce stage of the SparseCore embedding sum-lookup, and the segment-id it reads is the CSR (compressed-sparse-row) row-offset vector of the sparse minibatch. This section traces the chain from the `XlaSparseDenseMatmulWithCsrInput` HLO op down to the `SegmentedScanOp`, so a reimplementer can see *where the segment boundaries come from*. CSR is the standard sparse-embedding format: `row_pointers[k]` is the offset into the flat `(token_id, sample_id, gain)` arrays where sample `k`'s bag of looked-up rows begins, so the row-pointers *are* the per-sample segment boundaries.

### Stage 1 — the HLO custom-call

`XlaSparseDenseMatmulWithCsrInputOp::Compile` (`0xe650800`) is the XLA op-kernel for the DLRM embedding sum-lookup. It reads five named inputs in order, validates the minibatch count, builds a `FrontendAttributes` string-map backend-config, and emits one `xla::CustomCall` with 7 operands:

```c
function XlaSparseDenseMatmulWithCsrInputOp_Compile(ctx):   // 0xe650800
    row_pointers     = ctx.Input("row_pointers")              // line 124 — CSR offsets = segment boundaries
    sorted_sample_ids= ctx.Input("sorted_sample_ids")         // line 126 — per-id output row
    sorted_token_ids = ctx.Input("sorted_token_ids")          // line 128 — per-id embedding-table row (gather idx)
    sorted_gains     = ctx.Input("sorted_gains")              // line 130 — per-id combiner gain (weight)
    embedding_table  = ctx.Input("embedding_table")           // line 132 — the dense matrix gathered from
    num_minibatches  = ctx.Input("num_minibatches_per_physical_sparse_core")  // line 226, validated scalar

    fe = FrontendAttributes()                                 // line 280
    fe["_xla_compute_type"]                = "sparse"         // lines 287-288
    fe["_xla_quantization_low_value"]      = attr.quant_low
    fe["_xla_quantization_high_value"]     = attr.quant_high
    fe["_xla_quantization_num_buckets_value"] = attr.quant_num_buckets
    fe["_xla_enable_full_hbm_sort"]        = "false"          // lines 851-852 (default)

    CustomCall(builder, "SparseDenseMatmulWithMinibatchingOp",  // line 907; 35 B, 7 operands
               operands={row_pointers, sorted_token_ids, sorted_sample_ids,
                         sorted_gains, embedding_table, num_minibatches, activations_init},
               …, backend_config=fe)
```

The custom-call target name `"SparseDenseMatmulWithMinibatchingOp"` (35 bytes) is assembled inline from a 32-byte `.rodata` prefix plus a 4-byte `"ngOp"` tail (`strcpy(buf+31, "ngOp")`, line 883). The 7th operand (`activations_init`, the accumulator initializer) is read structurally from the operand span, not from a distinct `Input()` call (INFERRED = `activations_init`; matches the decomposed forward `operand[6]`).

| # | named input | role | Confidence |
|---|---|---|---|
| 0 | `row_pointers` | CSR row-offsets = per-sample segment boundaries | CONFIRMED |
| 1 | `sorted_sample_ids` | per-id output (minibatch) row index | CONFIRMED |
| 2 | `sorted_token_ids` | per-id embedding-table row index (the gather index) | CONFIRMED |
| 3 | `sorted_gains` | per-id scale / combiner gain (weight) | CONFIRMED |
| 4 | `embedding_table` | the dense embedding matrix being gathered from | CONFIRMED |
| (scalar) | `num_minibatches_per_physical_sparse_core` | bounds the minibatch count (validated scalar) | CONFIRMED |

### Stage 2 — minibatching decomposition slices the CSR offsets

The `SparseDenseMatmulWithMinibatchingOp` custom-call is rewritten by two SC HLO passes: `MinibatchingDecomposition` (`AddPass` `0x1306d5c0`) and `EmbeddingDataFormattingDecomposer` (`AddPass` `0x1095b6a0`). `MinibatchingDecomposition::CreateDynamicSliceCsr` (`0x13489ea0`) slices and pads the concatenated CSR row-pointers per minibatch — it builds `DynamicSlice`, `Binary` (op 75 = add-style index arithmetic), and `CustomCall` HLO ops, bounding the slice via `sparse_dense_matmul_decomposer_util::GetPaddedRowCount`:

```c
function CreateDynamicSliceCsr(comp, csr_tuple, …, minibatch, …):   // 0x13489ea0
    RET_CHECK(csr_tuple.size() > kCsrTupleRowPtrIndex)          // line 285
    padded = GetPaddedRowCount(util, target, minibatch)         // line 83
    …
    idx  = CreateBinary(comp, 75, …)                            // lines 130/146/162 — slice index math
    sliced = DynamicSlice(comp, concatenated_csr_pointers, idx, padded)
    return CreateCustomCall(comp, …, sliced, …)                 // lines 112/262
```

The decomposed **forward** op's `operand[0]` is `concatenated_csr_pointers` (`ForwardPassArgSpec::kForwardPassOperandNames`, `.data.rel.ro` `0x21937d80`, reloc-resolved). Those per-sample row offsets *are* the segment boundaries: each output activation row aggregates one contiguous run of gathered embedding rows, and the run boundaries are the CSR offsets.

| # | decomposed forward operand | role |
|---|---|---|
| 0 | `concatenated_csr_pointers` | **the segment-id source** (sliced by `CreateDynamicSliceCsr`) |
| 1 | `concatenated_embedding_ids` | gather indices |
| 2 | `concatenated_sample_ids` | output rows |
| 3 | `concatenated_gains` | combiner weights |
| 4 | `num_mini_batches_per_sparse_core` | scalar |
| 5 | `embedding_table` | the dense matrix |
| 6 | `activations_init` | accumulator init |

The backward (grad) op (`kBackwardPassOperandNames` `0x21938320`) adds `tables` / `gradients` / `hyperparameters` (the optimizer state — SGD/Adam/Ftrl/Adagrad/AdagradMomentum families) atop the same CSR/id/sample/gain operands, accumulating the per-segment gradient over the same CSR segment structure.

### Stage 3 — the dialect SegmentedScanOp and the full datapath

`PackedOperandsLowering`'s `ScanOpLowering<SegmentedScanOp>` (`0x135f3000`) is the rewrite that *builds* the dialect `SegmentedScanOp` (via `SegmentedScanOp::create`, `0x145fd5a0`): it unpacks `bf16`/sub-byte operands, re-creates the `SegmentedScanOp` with the packed `(data, segment-id)` operands and the `reduction_op` `StringAttr`, then packs the results. The final `SegmentedScanOpLowering` (the body in [The MLIR Lowering](#the-mlir-lowering)) then lowers it to the intrinsic.

```text
The complete embedding sum-lookup HLO → SC dialect → intrinsic → ISA datapath
  HLO op-kernel   XlaSparseDenseMatmulWithCsrInputOp::Compile  (0xe650800)
                    └─ CustomCall "SparseDenseMatmulWithMinibatchingOp" (7 operands, frontend-attrs)
  HLO pass A      MinibatchingDecomposition::CreateDynamicSliceCsr  (0x13489ea0)
                    └─ slice/pad concatenated_csr_pointers → per-minibatch segment-id vector
  HLO pass B      EmbeddingDataFormattingDecomposer  (0x1095b6a0)   ── activations stack/unstack
  SC dialect      sparse_core::SegmentedScanOp(data, segment-id=CSR-offsets, reduction_op="sum")
                    build 0x145fd4a0 / create 0x145fd5a0
  dialect rewrite PackedOperandsLowering ScanOpLowering<SegmentedScanOp>  (0x135f3000)
                    └─ unpack bf16 → re-create SegmentedScanOp → pack results
  dialect→intr    SegmentedScanOpLowering::matchAndRewrite  (0x13589d40)   ── reduction × dtype switch
                    └─ tpu_add[_half]/_min/_max_seg_scan{1xNf,1xNi,2xN}     (+0x780 gate for bf16)
  ISA op          SparseCoreTecVectorExtended_SegmentedAddScan{dtype}  (oneof add_f32=0x23)
                    EmitVectorResultUnop gfc 0x13aaf560 ; FindAndEmitToUnusedPort 0x13ab2aa0
                    └─ per-segment inclusive prefix-sum that RESETS at each CSR-offset boundary
  result drain    LLVM::ExtractValueOp  (0x1728c5a0)   ── value(idx0) / segment-id(idx1)
```

> **NOTE — the CSR offsets drive the reset; the gather drives the data.** The two halves of the sum-lookup map onto the two `SegmentedScanOp` operands: the `embedding_table` gather (indexed by `sorted_token_ids`, scaled by `sorted_gains`) produces the *data* vector (`operand[0]`); the `row_pointers` produce the *segment-id* vector (`operand[1]`). The inclusive segmented scan over the gathered rows, read at each segment's last lane, is the per-sample summed embedding. The exact slice arithmetic in `CreateDynamicSliceCsr` (the start/stride from `num_minibatches` and `GetPaddedRowCount`) was seen as `DynamicSlice` + `Binary` + `CustomCall` but the index expressions were not fully decoded (LOW for the literal transform).

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `SegmentedScanOpLowering::matchAndRewrite` | `0x13589d40` | reduction × dtype → `tpu_*_seg_scan*`; bf16 `+0x780` gate; no `i1` path | CONFIRMED |
| `SegmentedScanOp::build` | `0x145fd4a0` | `addOperands(data)` then `addOperands(segment)` — operand[1]=boundary | CONFIRMED |
| `SegmentedScanOp::create` | `0x145fd5a0` | builds `(data, segment, reductionStr)` | CONFIRMED |
| `SegmentedScanOp::getReductionOp` | `0x145fd460` | property word `((w>>19)&0x10)+64` → `StringAttr::getValue` | CONFIRMED |
| `ScanOpLowering<SegmentedScanOp>` (PackedOperands) | `0x135f3000` | unpack bf16 → re-create `SegmentedScanOp` → pack results | CONFIRMED |
| `LowerToSparseCoreLlvmPass::lowerFunc` | `0x13568280` | sets `(pattern+0x68)=(Target+0x8)`, the bf16-ALU capability holder | CONFIRMED |
| `EmitVectorResultUnop<…SegmentedAddScanF32>` | `0x13aaf560` | gfc emit; op[1]→mask `+0x38`, op[2]→`FindAndEmitToUnusedPort` | CONFIRMED |
| `FindAndEmitToUnusedPort<…SegmentedAddScanF32>` | `0x13ab2aa0` (gfc) / `0x13a4b680` (glc) | 7-port greedy first-free; slots `+0x1c..+0x34`, present `+0x10` | CONFIRMED |
| `mutable_segmented_add_scan_f32` | `0x13aaf600` | proto `inst` oneof `0x23` accessor (`proto+0x58 == 0x23`) | CONFIRMED |
| `XlaSparseDenseMatmulWithCsrInputOp::Compile` | `0xe650800` | 5 named inputs → `SparseDenseMatmulWithMinibatchingOp` custom-call, 7 operands | CONFIRMED |
| `XlaSparseDenseMatmulWithCsrInputOp` ctor | `0xe650140` | reads `table_name`/`input_size`/`num_sc_per_logical_device`/quant attrs | CONFIRMED |
| `GetMaxIdsAndUniques` | `0xe651fa0` | delegates to `GetMaxIdsAndUniquesExternal` — gather/dedup window bounds | CONFIRMED |
| `MinibatchingDecomposition::CreateDynamicSliceCsr` | `0x13489ea0` | slices `concatenated_csr_pointers` per minibatch (`GetPaddedRowCount`) | CONFIRMED |
| `EmbeddingDataFormattingDecomposer` AddPass | `0x1095b6a0` | activations stack/unstack decomposition | CONFIRMED |
| `tpu_add_seg_scan1xNi` / `1xNf` `::create` | `0x146d5c40` / `0x146d5a80` | `sum` i32 / f32 segmented leaves | CONFIRMED |
| `tpu_add_half_seg_scan2xN::create` | `0x146d45c0` | `sum` i16/bf16 packed-pair leaf (gated) | CONFIRMED |
| `tpu_min_seg_scan1xNf` / `1xNi` `::create` | `0x147316c0` / `0x14731880` | `min` f32 / i32 segmented leaves | CONFIRMED |
| `tpu_max_seg_scan1xNf` / `1xNi` `::create` | `0x14730e00` / `0x14730fc0` | `max` f32 / i32 segmented leaves | CONFIRMED |
| `tpu_{min,max}_seg_scan2xN` | (registered) | `NOperands<2>` trait, **no `::create`** — declared-but-uncodegen'd | CONFIRMED |

---

## Considerations

- **The lowering is the plain scan minus `i1`, plus a second operand.** Reuse the `sum`/`min`/`max` XOR switch and the `{i32,f32,i16,bf16}` axis from [Scan Datapath](scan-datapath.md), drop the `i1`/`mprefix` arm, bind `operand[1]` as the segment vector instead of the mask, and emit the `tpu_*_seg_scan*` family.
- **`operand[1]` is the reset signal, not a mask.** It is a *value* vector compared lane-to-lane in hardware to restart the carry at boundaries. The per-lane mask is the separate `proto+0x38` field, still present on every segmented scan. Branch operand routing on op identity.
- **The segment-id rides a free V read port (7-port greedy allocator), not a fixed index.** Slots `+0x1c..+0x34`, present mask `+0x10`. Data and segment-id each take the next free port; the physical index depends on bundle port pressure.
- **`i16`/`bf16` segmented-add is the only half-precision arm and it is target-gated.** Check vtable `+0x780` on the target subobject; emit `"Currently seg scan add for bf16 is only supported"` and fail on a gen without the bf16 ALU. `min`/`max` have no half arm at all; `tpu_{min,max}_seg_scan2xN` exist as ops but have no codegen.
- **The bf16 segmented ISA op is `SegmentedAddScanBf16PartialSumBf16` (oneof `0x2e`).** The gfc/glc proto pool has no `PartialSumF32` segmented form. (CORRECTS an earlier `PartialSumF32` claim, which referred to the plain scan or another gen.)
- **The CSR row-offsets are the segment boundaries.** `row_pointers` → `concatenated_csr_pointers` → sliced per-minibatch → `SegmentedScanOp` `operand[1]`. The gathered, gain-scaled embedding rows are `operand[0]`. The inclusive segmented sum at each segment's last lane is the summed embedding.
- **The intrinsic is inclusive.** Exclusive segmented scans are synthesized by the front-end from the inclusive result; the hardware primitive does not expose an exclusive form.
- **Unmapped / LOW.** The literal `CreateDynamicSliceCsr` index arithmetic (start/stride from `num_minibatches` + `GetPaddedRowCount`); the exclusive-from-inclusive front-end rewrite; the per-port bit positions in the final packed ISA word (the emitter slots `+0x1c..+0x34` are known; the encode bit-ranges are owned by [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md)); whether any later gen exposes a `Bf16PartialSumF32` segmented variant.

---

## Related Components

| Name | Relationship |
|---|---|
| `SegmentedScanOpLowering` (`0x13589d40`) | the reduction × dtype switch; emits the `tpu_*_seg_scan*` family with the bf16 `+0x780` gate |
| `SegmentedScanOp::build`/`create` (`0x145fd4a0` / `0x145fd5a0`) | binds `(data, segment)` — the segment-boundary operand order |
| `FindAndEmitToUnusedPort` (`0x13ab2aa0`) | the 7-port greedy allocator that routes the segment-id to a free V read port |
| `XlaSparseDenseMatmulWithCsrInputOp::Compile` (`0xe650800`) | the HLO front-end whose CSR `row_pointers` become the segment-id |
| `MinibatchingDecomposition::CreateDynamicSliceCsr` (`0x13489ea0`) | slices `concatenated_csr_pointers` into the per-minibatch segment vector |
| `PackedOperandsLowering ScanOpLowering<SegmentedScanOp>` (`0x135f3000`) | builds the dialect `SegmentedScanOp` (unpack → create → pack) before the final lowering |

## Cross-References

- [Scan Datapath](scan-datapath.md) — the plain `ScanOp` lowering, the in-scan M-register mask (`proto+0x38`), the two M-register bands, the post-scan `VectorSelect`, and the `i1`/`mprefix` count path this page's segmented variant deliberately omits.
- [Segmented Add-Scan](segmented-add-scan.md) — the `SegmentedAddScan` ISA operand frame, the `VpackFormat` dtype-attribute capability matrix the `2xN`/`half` form rides, and the full 18-entry segmented-scan proto oneof case map (`add_f32`=`0x23`, `bf16`=`0x2e`).
- [Embedding Minibatching](embedding-minibatching.md) — the minibatching decomposition and the `concatenated_csr_pointers` provenance the segment-id is sliced from.
- [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md) — the bundle bit positions the emitter slots `+0x1c..+0x34` and the mask `proto+0x38` encode into; the sub-opcode map.
- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/reduce slot the segmented scan emits into; the VEX opcode roster and V read ports.
- [M-Register Predicate Word (M0–M31)](m-register-predicate.md) — the predicate word the separate `proto+0x38` mask field selects, distinct from the segment operand.
- [VectorLoad Slot](vectorload-slot.md) — the read-side slot that gathers the embedding rows this scan reduces.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the VEX/`VectorAlu` opcode roster and the opcode-recovery model.
- [SparseCore Overview](overview.md) — the three SC engine classes and where the TEC vector segmented-scan datapath sits.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore datapath (embeddings) — [back to index](../index.md)
