# Codegen Leaves: NcMatMul, MatMulMX, and the QuantizeMX DVE Op

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 twins present, every VA drifts). Subject binary: `neuronxcc/starfish/lib/libwalrus.so` — the **Strand-I** backend shared object that holds the `KlirToBirCodegen` leaf bodies. `.text` (`0xf00000+`) and `.rodata` (`0x1c72000+`) have **VA == file offset**, so disasm, string literals and jump tables are read by file offset. The `bir::Inst*` node templates the leaves call (`addMatmult`, `addArgument`, `insertElement`, `InstQuantizeMx` ctor) resolve into `libBIR.so` (statically linked into `nki_klr_sim`, also shipped as a sidecar `.so`); both are in the corpus. Every claim below is tagged **CONFIRMED** (read directly off this binary — `nm -DC`, `objdump -d -C`, `strings`), **STRONG** (multi-evidence), **INFERRED**, or **SPECULATIVE**.*

## Abstract

Three of the 60-odd per-op `codegen<Op>` leaves that hang off the [KlirToBirCodegen dispatch core](klir-codegen-dispatch.md) (7.21) lower the matmul *family* of `klr::Operator` nodes into BIR `bir::InstMatmult*` / `bir::InstQuantizeMx` instructions. They are the most interesting leaves on the matmul side because they expose, in three different binding shapes, how a KLR op's operand list maps onto the BIR instruction's argument/output vectors, and how the **OCP-MXFP microscaling** configuration is threaded through the BIR layer *without a single scalar config field* — entirely on operand **dtype** and **access-pattern** geometry.

| Leaf | klr op | BIR node (L1 IT tag) | operands | engine | mechanism |
|---|---|---|---|---|---|
| `codegenNcMatMul` `@0xf19590` | `klr::NcMatMul` (9 fields) | `bir::InstMatmult` (**8**) | **3** (out, stat, mov) | PE (3, by builder) | `InstBuilder::addMatmult` — one call binds all 3 |
| `codegenNcMatMulMX` `@0xf25320` | `klr::MatMulMX` (7 fields) | `bir::InstMatmultMx` (**95**) | **5** (out + 2 data + 2 scale) | PE (3, by hand) | `insertElement` + 4×`addArgument` + 1×`addOutput` |
| `codegenQuantizeMX` `@0xf25ad0` | `klr::QuantizeMX` (3 fields) | `bir::InstQuantizeMx` (wire `QuantizeMx`) | **3** (1 in + 2 out) | **DVE (5)**, by hand | `insertElement` + 1×`addArgument` + 2×`addOutput` |

All three are **pure operand-binding** lowerings: they attach `MemoryLocation*` + access-pattern + size (+ optional `Dtype`) to the new node, stamp a small number of scalar attributes, set the engine, and return. None of them compute scale, clamp, accumulate-chaining, or block geometry — those are silicon semantics (sim kernels, 6.5.11 / the numerics chapter) or later legalization passes (`set_first_matmults` H28, accumulate-group H31). The leaf's job ends at "the node exists, its operands are bound, its engine is set."

> **METHOD — bodies live in `libwalrus`, not `nki_klr_sim`.** A prior report attributed `codegenNcMatMulMX @0xf25320` to `nki_klr_sim`. `nm -DC nki_klr_sim` shows only **undefined** refs (`U bir::InstMatmultMx` ctor, `U bir::Hwm::getLatency*(InstMatmultMx)`) — the sim binary links the *node class* but contains **no `codegen*` body**. The address `0xf25320` is correct but it is the **libwalrus** VA. `nm -DC libwalrus.so` resolves all three leaf symbols as real `T` rows at the addresses tabled above. [CONFIRMED]

| | |
|---|---|
| **Class** | `neuronxcc::backend::KlirToBirCodegen` (TU `klir_to_bir_codegen.cpp`) |
| **Dispatch parent** | `codegenOperator` `@0xf30db0` → wrappers (below) → these three leaves (7.21) |
| **Plain wrapper** | `codegenOperatorNcMatMulWrapper` `@0xf19cf0` |
| **MX wrapper** | `codegenOperatorNcMatMulMXWrapper` `@0xf25a60` |
| **QuantizeMX wrapper** | `codegenOperatorQuantizeMXWrapper` `@0xf25ea0` |
| **Shared AP primitive** | `codegenTensorRef` `@0xf18b30` → `codegenTensorRefImpl` `@0xf18960` (7.22) |
| **Downstream encoders** | CoreV2/CoreV4 PE encoders — `generateMatmultMx @0x143ebd0`, `visitInstQuantizeMx @0x143dc60` (2.10) |

---

## 1. The shared lowering skeleton

All three leaves take a `std::shared_ptr<klr::Op>` (the `Ptr<…>` arg in `rsi`), dereference it to the heap-resident klr op object, and run the same four-phase skeleton:

```c
bir::Inst* codegen<Op>(KlirToBirCodegen* this, Ptr<klr::Op> op) {
    Op* Q = op.get();                                   // *rsi → klr op struct

    // (A) lower each klr operand TensorRef → a bir::InstArg aggregate
    InstArg a0 = codegenTensorRef(Q->operand0);         //  = { MemoryLocation*,
    InstArg a1 = codegenTensorRef(Q->operand1);         //      SmallVector<bir::APPair,4u> Pattern,
    ...                                                 //      uint size, optional<bir::Dtype> }

    // (B) mint the node + name, materialize it in the current BasicBlock
    uint id   = this->returnAndIncrementId();           // @0x612600, monotone name id
    string nm = "<prefix>" + id;                        // "matmult_" / "MatmulMx-" / "QuantizeMX-"
    Inst* I   = /* InstBuilder::addMatmult  OR  insertElement<InstX> */;

    // (C) bind operands  (builder does all at once  OR  per-operand addArgument/addOutput)
    // (D) stamp scalar attrs + engine, return I
    return I;
}
```

The `InstArg` aggregate returned by `codegenTensorRef` is the unit of operand binding everywhere on this page. `codegenTensorRefImpl @0xf18960` dispatches a `klr::TensorRef` on its kind: a TensorRef-kind goes to `codegenAccess` (the symbolic access-pattern builder, see [codegen AccessPattern Primitive](codegen-accesspattern.md), 7.22); an HBM-kind (`v12==4`) goes to `codegenHbm`; anything else `__assert_fail "Unsupported tensorRef type"` (`klir_to_bir_codegen.cpp` line `0x135`). The leaf never builds an access pattern itself — it only **copy-constructs** the `SmallVector<bir::APPair,4u> Pattern` out of the `InstArg` to hand to the builder or attach call. Each `bir::APPair` is `16 B {step:int64 @+0x00, num:int64 @+0x08}`; the operand `Dtype` rides separately in the `InstArg`'s `optional<bir::Dtype>` slot.

> **NOTE — name prefixes are real `.rodata` literals.** `strings libwalrus.so` returns exactly `matmult_`, `MatmulMx-`, `QuantizeMX-`. The `<prefix><id>` name is the instruction's `NamedObject` key; the BIR wire op-name (`Matmult` / `MatmultMx` / `QuantizeMx`, present verbatim in `libBIR.so`) is a *separate* string keyed off the node class, used by the JSON writer (7.x). Do not conflate the codegen name prefix with the wire op-name. [CONFIRMED]

---

## 2. `codegenNcMatMul` — the 3-operand builder path

`klr::NcMatMul` carries **9** fields (read off `klr::to_string @0xf8ae90`'s `.rodata` format literals): `dst, stationary, moving, isStationaryOneZero, isMovingZero, isTranspose, tilePosition, tileSize, perfMode`. The first three are the tensor operands.

### 2.1 Operand binding — 3 TensorRefs via the builder

`objdump -d -C` over `0xf19590..0xf19ced` shows the call multiset **`3× codegenTensorRef` → `1× returnAndIncrementId` → `1× addMatmult` → `1× setCalcAuto`**. [CONFIRMED]

| klr slot | klr field | role | → `InstArg` | builder slot |
|---|---|---|---|---|
| 0 | `dst` | PSUM accumulator (output) | lowered | `out` |
| 2 | `stationary` | **weights** — resident in the 128×128 PE systolic array | lowered | `stationary` |
| 4 | `moving` | **ifmap** — streamed through the array | lowered | `moving` |

The `stationary = weights / moving = ifmap` Neuron semantics are **INFERRED** from the builder's `{out, stationary, moving}` slot names plus the downstream verifier naming; the codegen text only proves the klr field order. The single builder call binds all three at once:

```c
v14 = bir::InstBuilder::addMatmult(
        name,
        out_memloc, stationary_memloc, moving_memloc,     // 3 MemoryLocation*
        ap_out, ap_stationary, ap_moving,                 // 3 SmallVector<APPair,4u>
        size_out, size_stat, size_mov,                    // 3 uint const&
        bool, bool, src_location);                        // 2 bool + boost::source_location
```

The `addMatmult` symbol is an **undefined import** resolved into `libBIR.so`; its demangled signature (from `nm -DC`) matches the call exactly: `addMatmult(std::string const&, bir::MemoryLocation* ×3, llvm::SmallVector<bir::APPair,4u> ×3, unsigned int const& ×3, bool, bool, boost::source_location const&)`. The builder is what allocates the `bir::InstMatmult` node, sets its **InstructionType tag = 8** (`Matmult`, see [Op-Family Enums](op-family-enums.md)), assigns **engine = PE (3)** internally, and attaches all three access patterns in one shot. [CONFIRMED — by the import signature + call-site register setup; the builder *body* behind the import is not re-read line-by-line, so engine/IT assignment is confirmed *by effect*, not by reading `addMatmult` itself — GAP]

### 2.2 Accumulate flags — deferred, only `setCalcAuto` here

`codegenNcMatMul` calls **only** `bir::InstMatmultBase::setCalcAuto(v14) @0xf1984a`. It never touches `CalcStart` / `CalcStop` / `CalcContinue` / `CalcAccu`. The PSUM accumulate chaining is derived entirely downstream:

- **H28 `set_first_matmults` `@0x157d340`** — the first writer of a PSUM `dst` gets `setCalcStart()` (zero-on-write); subsequent writers get `setCalcContinue()`.
- **H31 accumulate-group legalize `@0x16e3130`** — reads `getCalcAuto()` on each group member; all-auto ⇒ honour, else re-derive start/stop.

So the `CalcAuto` bit set here is the **upstream signal** those passes test: codegen marks the op auto-accumulate-eligible and leaves the chaining to the scheduler. The CoreV2 encoder later collapses `{Start, Stop, Accu, Auto}` onto a 3-bit accumulation byte (2.10). [CONFIRMED — the single `setCalcAuto` call is the only Calc* call in the body]

### 2.3 The three booleans + perf_mode + tile fields (byte-confirmed)

After the builder, three klr bytes are copied onto the node. Each target is a `MaybeAffine<bool>` (a `variant<bool, QuasiAffineExpr>`); the codegen runs the variant-reset visitor to clear any affine alternative before writing the scalar, and sets a "present" discriminator byte 0x20 higher. The three `movzbl` reads are byte-exact:

| disasm | klr byte | klr field | BIR field (node offset) |
|---|---|---|---|
| `0xf19852  movzbl 0x32(%rax),%edx` | 50 | `isTranspose` | `is_transpose` (`+0x1B8`) |
| `0xf19873  movzbl 0x31(%rax),%edi` | 49 | `isMovingZero` | `is_fmap_onezero` (`+0x1E0`) |
| `0xf19894  movzbl 0x30(%rax),%ecx` | 48 | `isStationaryOneZero` | `is_weight_onezero` (`+0x208`) |

(The crossing — `stationary`→`is_weight`, `moving`→`is_fmap` — is consistent with the weights/ifmap mapping in §2.1.) [CONFIRMED — three `movzbl` opcodes at the cited addresses]

**`perf_mode`** is the one nontrivial scalar transform:

```asm
0xf198b5   cmpl   $0x2, 0x68(%rax)         ; klr perfMode (field idx) == 2 ?
0xf198bb   movl   $0x1, 0x2d0(%r14)        ; iff so → InstMatmultBase.perf_mode = 1 (DoubleRow)
```

The codegen maps **only** klr `perfMode == 2` to BIR `perf_mode = DoubleRow(1)` at node `+0x2D0`; every other klr code leaves `perf_mode = None(0)` (the ctor default). The other `MatmultPerfMode` values (`DoubleColumn/DoublePixel/DoubleRowSwInterleave`, see [Op-Family Enums](op-family-enums.md)) are reachable via JSON but **not emitted by this lowering**. [STRONG — single `cmpl $0x2` / `movl $0x1` pair, no other perf_mode store in the body]

**Tile fields** are unconditional here (the klr fields are plain `std::list<int>`, not optionals):

| field | klr slot | assert | node offsets |
|---|---|---|---|
| `tile_size` | 10 | `size()==2`, line 915 (`0x393`) | `[0]→+0x230`, `[1]→+0x258` |
| `tile_position` | 7 | `size()==2`, line 924 (`0x39C`) | `[0]→+0x280`, `[1]→+0x2A8` |

These are the same `InstMatmultBase` `tile_size`/`tile_position` pair that H28 later reads to detect accumulate-group heads ("head iff `tilePosition[0] | tilePosition[1] == 0`"). [CONFIRMED — the two asserts cite `tileSizeVector.size()==2` / `tilePositionVector.size()==2` at lines 915/924]

---

## 3. `codegenNcMatMulMX` — the 5-operand hand-rolled path

`klr::MatMulMX` carries **7** fields: `dst, stationary, moving, stationaryScale, movingScale, tilePosition, tileSize`. There are **no booleans** and **no perfMode** — MX is always `transpose=false`, `onezero=false`, PE-engine; the "MX-ness" rides entirely on the **operand dtype** and the **two extra scale tensors**. The serialize shape (`MatMulMX_ser @0xf47430` = `serialize_tag(7)` + 5×`TensorRef_ser` + 2×`Option_List_Nat_ser`) confirms **exactly 5 tensor operands** + 2 optional Nat-list tile fields. [STRONG — serialize-tag arity]

### 3.1 Why MX bypasses the builder

`addMatmult` only knows the 3-operand `{out, stationary, moving}` shape with a 2-bool tail. It cannot express the 5-operand `{ifmap, weights, 2 scales, out}` layout nor a per-operand `optional<Dtype>`. So MX hand-rolls the insert and binds operands one at a time. The call multiset over `0xf25320..0xf256ff` is **`5× codegenTensorRef` → `1× returnAndIncrementId` → `1× insertElement<bir::InstMatmultMx>` → `4× addArgument` → `1× addOutput`**. [CONFIRMED — symbolic call census]

```c
BasicBlock* bb = *(this + 72);                                  // insert position
uint id = returnAndIncrementId();
string nm = "MatmulMx-" + id;
v23 = NamedObjectContainer<BasicBlock,Instruction>
        ::insertElement<bir::InstMatmultMx>(bb, ..., nm);       // @0xf3fa50, IT tag = 95
sub_F20EC0(*(this + 0x224), v23);                               // per-function matmul tracking-list push
```

`insertElement<bir::InstMatmultMx> @0xf3fa50` is a real weak `W` symbol in `libwalrus`; it inserts the node **directly** into the BasicBlock's instruction list (unlike the plain builder, which allocates and returns). The post-insert helper `sub_F20EC0` pushes the node into a side-table at `codegen+0x224` (a per-function matmul-tracking list) — the symbolic disasm renders this call as `_validateLegalStreamTranspose+0x50`, i.e. an unnamed local-static body adjacent to `_validateLegalStreamTranspose @0xf20e70`, confirming it is a function-local helper, not a public method. [STRONG — `insertElement` is `nm`-confirmed; `sub_F20EC0` identity is by adjacency]

### 3.2 Operand attach — 4×addArgument + 1×addOutput

Each attach is the generic variadic template
`bir::Instruction::addArgument<bir::PhysicalAccessPattern, bir::MemoryLocation*, llvm::SmallVector<bir::APPair,4u>, unsigned, std::optional<bir::Dtype>>(memloc, ap, size, dtype)` (`addArgument @0xf35e50`, `addOutput @0xf35d30`, both `nm`-confirmed `W` symbols). The positional order is:

| call | klr operand | role | BIR positional slot |
|---|---|---|---|
| `addArgument` | `moving` | ifmap **DATA** (x4-packed FP4/FP8) | `arg0` |
| `addArgument` | `stationary` | weights **DATA** (x4-packed FP4/FP8) | `arg1` |
| `addArgument` | `movingScale` | ifmap **SCALE** (E8M0) | `arg2` |
| `addArgument` | `stationaryScale` | weights **SCALE** (E8M0) | `arg3` |
| `addOutput` | `dst` | PSUM result | `out0` |

> **GOTCHA — the data pair is flipped relative to the plain builder.** The plain `addMatmult` takes `{out, stationary, moving}`, but MX binds **`moving` first** (`arg0 = ifmap`), `stationary` second (`arg1 = weights`). This is not an accident: it matches the silicon `LdWeightMx` + `MatmultMx` encode order and the consumer-side verifier `checkMatmultMxInputs @0x1007420`, which reads `arg0 = ifmap, arg1 = weights, arg2 = ifmap-scale, arg3 = weights-scale, out0 = PSUM`. The codegen never swaps; producer and verifier agree on the same order. [STRONG — argument census confirms 4 args before the single output; the verifier order is the cross-ref contract]

### 3.3 The MX scale binding — what makes MX "MX"

The two scale operands (`arg2 = movingScale`, `arg3 = stationaryScale`) are bound as **ordinary extra BIR arguments**. There is **no dedicated "scale" struct field** on the node — the scale is simply two more `PhysicalAccessPattern` operands. The microscaling configuration is expressed purely through operand attributes:

- **Scale operands** carry `Dtype = float8_e8m0fnu` (Dtype enum `6`) — an 8-bit shared block exponent — with shape `[P/8, F/4]`: one E8M0 byte per `8-partition × 4-column = 32-element` OCP-MXFP block (`block_size = 32`).
- **Data operands** (`arg0/arg1`) carry an x4-packed narrow dtype: `float4_e2m1fn_x4 (2)`, `float8_e4m3fn_x4 (8)`, or `float8_e5m2_x4 (9)` — the `dt==2 || (dt-8)<=1 ⇒ N*=4` packed set.

The dtype rides through the `optional<bir::Dtype>` last argument of `addArgument`/`addOutput` (pulled from each `InstArg`'s own `optional<Dtype>` slot) and lands in the operand's access-pattern `Dtype` field. **`block_size = 32` is not a BIR-JSON field** — it is implicit in the `[P/8, F/4]` scale shape; the BIR layer encodes MX as `{ DATA dtype = x4-narrow }` + `{ two extra E8M0 scale operands }` and nothing more. The CoreV4 encoder `generateLdweightMx @0x143e350` reads `arg2/arg3` as the per-block scales and emits `LdWeightMx (0x1009)` + `MatmultMx (0x100A)` (2.10). [CONFIRMED — the operand structure and engine byte; the dtype *values* are STRONG, inherited from the upstream TensorRef rather than forced by codegen, since the leaf passes `optional<Dtype>` straight through]

### 3.4 Engine + tile fields

```asm
0xf25663   movl   $0x3, 0x90(%r12)          ; node+0x90 = EngineType = 3 = PE (Tensor)
```

MX sets the engine **explicitly by hand** (no builder to do it). [CONFIRMED — single `movl $0x3,0x90` at `0xf25663`]

The body sets **no** Calc flags — not even `setCalcAuto`. MX leaves accumulate fully unset; H28's `order_column_tiled_mms` explicitly **skips** `MatmultMx(95)`/`MatmultSparse(9)` as column-tiled accumulate-group members (the MX op typically does a single full-tile matmul). [STRONG]

Tile fields are **optional-gated** here (the klr fields are `std::optional<List<Nat>>`):

```asm
0xf25673   cmpb   $0x0, 0x88(%rax)          ; tileSize.has_value     → if set: assert size==2 (line 2370), write +0x230/+0x258
0xf25774   cmpb   $0x0, 0x68(%rax)          ; tilePosition.has_value → if set: assert size==2 (line 2378), write +0x280/+0x2A8
```

Same `InstMatmultBase` offsets as the plain path; when the optional is empty, the field keeps its ctor default. [CONFIRMED — the two `cmpb $0x0` engaged-byte tests; asserts at lines 2370/2378]

---

## 4. `codegenQuantizeMX` — the 3-operand DVE producer

`klr::QuantizeMX` is the *producer* half of the MX chain: it quantizes BF16/FP16 input into `{packed FP8 data, E8M0 per-block scale}`, the two tensors that later become `MatMulMX` inputs. Its codegen leaf is the smallest of the three (962 B) and runs on the **DVE / Vector engine**, not PE.

### 4.1 The klr struct — 3 TensorRefs, zero scalars

`klr::QuantizeMX_des @0xf5d360` allocates a **64-byte** object and deserializes **exactly three** `shared_ptr<klr::TensorRef>` at byte offsets `0/16/32`:

| offset | klr field | role |
|---|---|---|
| `+0` | `dst` | quantized FP4/FP8 (x4-packed) DATA tensor |
| `+16` | `src` | input data tensor (BF16 / FP16) |
| `+32` | `dstScale` | E8M0 per-block exponent SCALE tensor |

Confirmed three ways: (a) `QuantizeMX_des` runs three `TensorRef_des` into `+0/+16/+32`; (b) `klr::to_string @0xf8e790` emits `"QuantizeMX(dst=…, src=…, dstScale=…)"` — field names verbatim; (c) `QuantizeMX_ser @0xf473c0` is `serialize_tag(0,3)` + **exactly three** `TensorRef_ser` calls, **no scalar fields**.

> **HEADLINE — `klr::QuantizeMX` carries NO block_size, NO EMAX, NO scale_method, NO dtype scalar.** The 64 bytes = `8 (vtbl) + 3×16 (TensorRefs) + 8 (pad)`. All MX configuration (32-element block geometry, 4-element packing, E8M0 scale stride, element dtypes) is encoded **entirely** inside the three TensorRefs' access patterns and element dtypes. This is corroborated at the BIR layer: `bir::InstQuantizeMx::readFieldsFromJson @0x40d090` is a bare `ret` (ICF-folded) — the node has **no op-specific BIR-JSON keys**. [CONFIRMED]

The on-wire serialize tag is `(0, 3)` → `LOBYTE 0xC3, WORD 0x0300`. The deserializer's check is byte-exact:

```asm
0xf5d396   cmpb   $0xc3, 0x1d(%rsp)         ; tag LOBYTE == 0xC3
0xf5d39d   cmpb   $0x0,  0x1e(%rsp)         ; tag high byte == 0x00  (WORD 0x0300)
```

[CONFIRMED — the two `cmpb` at `0xf5d396`]

### 4.2 The lowering — 1 input + 2 outputs, engine = DVE

The call multiset over `0xf25ad0..0xf25e92` is **`3× codegenTensorRef` → `1× returnAndIncrementId` → `1× insertElement<bir::InstQuantizeMx>` → `1× addArgument` → `2× addOutput`**, then the engine byte. [CONFIRMED — symbolic call census]

```c
bir::InstQuantizeMx* codegenQuantizeMX(KlirToBirCodegen* this, Ptr<QuantizeMX> q) {
    QuantizeMX* Q = q.get();

    // (A) lower the THREE klr TensorRefs  (note read order: src, dstScale, dst)
    InstArg srcArg   = codegenTensorRef(Q->src);        // [rax+0x10] = off16  (movdqu xmm0,[rax+10h] @0xf25ae8)
    InstArg scaleArg = codegenTensorRef(Q->dstScale);   // [rax+0x20] = off32  (movdqu xmm1,[rax+20h] @0xf25b43)
    InstArg dstArg   = codegenTensorRef(Q->dst);        // [rax+0x00] = off0   (mov rdx,[rax]        @0xf25b99)

    // (B) materialize the empty node
    string nm = "QuantizeMX-" + this->returnAndIncrementId();
    InstQuantizeMx* I =
        NamedObjectContainer<BasicBlock,Instruction>::insertElement<InstQuantizeMx>(bb, nm);  // @0xf3f280
    sub_F20EC0(this->instRegistry, I);                  // dataflow/use-map attach (same helper as §3.1)

    // (C) bind operands : 1 input + 2 outputs  (ORDER MATTERS)
    I->addArgument(srcArg.memloc,   srcArg.ap,   srcArg.size,   /*dtype*/nullopt);   // ins[0]  = src    (BF16/FP16)
    I->addOutput (dstArg.memloc,    dstArg.ap,    dstArg.size,   /*dtype*/nullopt);   // outs[0] = data   (FP8 x4)
    I->addOutput (scaleArg.memloc,  scaleArg.ap,  scaleArg.size, /*dtype*/nullopt);   // outs[1] = scale  (E8M0)

    // (D) stamp the engine
    *(int*)((char*)I + 0x90) = 5;                       // movl $0x5,0x90(%r12)  → EngineType = DVE (5)
    return I;
}
```

The `optional<Dtype>` 5th template arg is `nullopt` on all three attaches — the per-slot element dtype is taken from the operand's own `MemoryLocation`/AP, never overridden by the leaf. The engine store is byte-exact:

```asm
0xf25d50   movl   $0x5, 0x90(%r12)          ; node+0x90 = EngineType = 5 = DVE (Vector)
```

[CONFIRMED — `movl $0x5,0x90` at exactly `0xf25d50`; the symbolic disasm names all three `codegenTensorRef`, the `insertElement<bir::InstQuantizeMx>`, the one `addArgument` and two `addOutput`]

> **QUIRK — the read order and the bind order differ.** `codegenTensorRef` is called `src → dstScale → dst`, but the operands are *bound* `src(in) → dst(out0) → scale(out1)`. The bind order is the contract: input list `I+0xA0` gets `src`; the output list `I+0xC0` gets `data` at `outs[0]` and `scale` at `outs[1]`. The geometry/dtype verifier `checkQuantizeMxInstruction @0x1009a60` reads exactly that — `getOutput(0)` = the ×4-packed data, `getOutput(1)` = the E8M0 scale. [CONFIRMED — bind order from the call sequence; verifier slots from the cross-ref]

### 4.3 Operand slot → valid-dtype map (libBIR tables)

The leaf binds the operands; the verifier gates their dtypes against per-slot valid-dtype tables on the `InstQuantizeMx` class:

| klr field | BIR slot | list (node offset) | valid dtypes (libBIR table) |
|---|---|---|---|
| `Q->src` | `ins[0]` | `I+0xA0` | `getDataValidDtypes` = `{12 bf16, 13 fp16}` |
| `Q->dst` | `outs[0]` | `I+0xC0` | `getDst0ValidDtypes` = `{8 e4m3-x4, 9 e5m2-x4}` |
| `Q->dstScale` | `outs[1]` | `I+0xC0` | `getDst1ValidDtypes` = `{0 uint8, 6 e8m0fnu}` |

> **CORRECTION / discrepancy to flag.** `getDst0ValidDtypes` lists only `{8, 9}` (e4m3-x4, e5m2-x4), but the *geometry* verifier `checkQuantizeMxInstruction`'s packed-element gate (`v30==2 || (v30-8)<=1`) and the sim kernel both include `2` (fp4-e2m1-x4) in the x4 set `{2,8,9}`. So FP4 output is accepted by the packed-element geometry math but is **not** in the per-slot Dst0 allow-list of *this* 2.24 libBIR build. Reading: either FP4-output is routed through a different dst valid-set, or FP4-output was simply not enabled in this `getDst0ValidDtypes` while the generic ×4 geometry stays FP4-capable. [STRONG — discrepancy noted, not resolved on this build]

### 4.4 Block geometry — enforced, not carried

Because the klr op has no scalar config, the 32-element OCP-MXFP block geometry is **enforced by the verifier**, not written by codegen. `checkQuantizeMxInstruction @0x1009a60` (TU `inst_visitor.cpp`) asserts, against the bound operands' access patterns:

| ErrCode | assertion (verbatim) | meaning |
|---|---|---|
| 207 | `I.getEngine() == EngineType::DVE` | engine MUST be DVE(5) |
| 209 | `inAP[last].getStep()==1 && (numPacked==4 \|\| inAP[last].getNum()%4==0)` | inner dim contiguous, 4-packed |
| 210 | `inAP[i].getNum()==1 \|\| inAP[i].getStep()%2==0` | outer dims even step |
| 211 | `inAP.numElementsAccessed == numOutPackedElements` | in-count == out PACKED count (×4 iff out dt∈{2,8,9}) |
| 215 | `inAP.numPartitionsAccessed ∈ {32,64,96,128}` | partition counts (multiples of 32) |
| 264 | `scalesStartPartition%32<16 && %4==0` | scale start-partition alignment |
| 1821 | `dataStartPartition/32 == scalesStartPartition/32` | data + scale in the SAME 32-partition quadrant |
| 270 | `scalesOutAP.elemsPerPartition*4 == inAP.elemsPerPartition` | scale = ¼ the per-partition elements |

The "×4" relation appears twice (ErrCode 211 packed-element ×4, ErrCode 270 scale = 1 exponent per 4 data elements) and, combined with the `{32,64,96,128}` partition counts, **is** the 32-element OCP-MXFP block (`8-partition × 4-element`). This mirrors the MatMulMX consumer-side `checkMatmultMxInputs` (`data == 4×scale`, scale start `%32<16`, partitions ∈ `{32,64,128}`) — the same invariants on the producer side. [CONFIRMED — the verifier reads operand APs; codegen copies them verbatim and sets nothing]

### 4.5 Engine placement — DVE producer feeding a PE consumer

`EngineType` (per `bir::EngineType2string @0x47fa80`): `0 Unassigned, 1 Pool, 2 Activation, 3 PE, 4 DMA, 5 DVE, 6 SP, 7 ALL`. QuantizeMX is stamped **5 (DVE / Vector)**; its consumer MatMulMX is stamped **3 (PE / Tensor)**. The MX-quantize is a **vector-engine producer feeding a PE-engine matmul** — confirmed by both the codegen byte (`movl $0x5,0x90`) and verifier ErrCode 207 (`engine == DVE`). The full birverifier visit chain (`visitInstQuantizeMx @0xfba1f0`) also asserts `checkArchLevel(0x100000028)` — **arch ≥ CoreV4 (40)** — and `checkValidEngines({5})`; all operands are SBUF-resident (no DRAM). [CONFIRMED]

---

## 5. Plain vs MX vs QuantizeMX — the contrast table

| | `codegenNcMatMul` | `codegenNcMatMulMX` | `codegenQuantizeMX` |
|---|---|---|---|
| klr op | `klr::NcMatMul` (9 fields) | `klr::MatMulMX` (7 fields) | `klr::QuantizeMX` (3 fields) |
| BIR node / IT tag | `InstMatmult` / **8** | `InstMatmultMx` / **95** | `InstQuantizeMx` / wire `QuantizeMx` |
| operand count | **3** | **5** | **3** (1 in + 2 out) |
| binding mechanism | `InstBuilder::addMatmult` (one call) | `insertElement` + 4×`addArgument` + 1×`addOutput` | `insertElement` + 1×`addArgument` + 2×`addOutput` |
| operand roles | out=PSUM, stat=weights, mov=ifmap | arg0=ifmap, arg1=weights, arg2=ifmap-scale, arg3=weight-scale, out0=PSUM | ins0=src, outs0=data, outs1=scale |
| data dtype | normal (bf16/fp16/…) | x4-packed FP4/FP8 `{2,8,9}` | in: bf16/fp16; out: FP8-x4 `{8,9}` |
| scale operands | NONE | **TWO** E8M0 (Dtype 6), `[P/8,F/4]`, block=32 | **ONE** E8M0 output (Dtype `{0,6}`) |
| engine | PE (3, **by builder**) | PE (3, **by hand**, `+0x90=3`) | **DVE (5)**, by hand, `+0x90=5` |
| name prefix | `matmult_` | `MatmulMx-` | `QuantizeMX-` |
| boolean attrs | `is_transpose`/`is_fmap_onezero`/`is_weight_onezero` (bytes 50/49/48) | NONE (implicitly false) | NONE |
| perf_mode (`+0x2D0`) | klr `perfMode==2` → DoubleRow(1), else None | NOT written | n/a |
| tile fields | unconditional, assert `size==2` | optional-gated (bytes `0x88`/`0x68`) | n/a |
| Calc flags | `setCalcAuto()` only | NOTHING (H28 skips MX) | n/a |
| MX config storage | n/a | operand dtype + AP geometry (NO scalar field) | operand dtype + AP geometry (NO scalar field) |

The decisive observation for a reimplementer: **the MX microscaling configuration is never a BIR scalar field.** On both the MatMulMX consumer and the QuantizeMX producer, `block_size=32`, the 4-element packing, and the E8M0 scale stride are carried *only* by the operands' dtypes and access-pattern shapes (`readFieldsFromJson` is a bare `ret` for `InstQuantizeMx`; `MatMulMX` exposes no scale struct field). To reproduce this layer you bind two/three more operands with the right dtype and AP geometry and let the downstream verifier enforce the block invariants — you do **not** emit a config record.

---

## 6. The full MX chain (where these leaves sit)

```
[HLO]     QuantizeMX custom-call (BF16/F16 → packed FP8 + E8M0 scale)
            └ LegalizeQuantizeMX (hlo-opt) splits to 2-output custom-call (data, scale), block_size==32
[NKI/KLR]  NKI kernel emits klr::QuantizeMX {dst, src, dstScale} + klr::MatMulMX {dst, stat, mov, statScale, movScale}
            with the 32-block APs + element dtypes already baked in
[BIR codegen]  ← THIS PAGE
            • codegenQuantizeMX @0xf25ad0 : 3 TensorRefs → InstQuantizeMx, engine=DVE(5)
            • codegenNcMatMulMX @0xf25320 : 5 TensorRefs → InstMatmultMx, engine=PE(3)
            • codegenNcMatMul   @0xf19590 : 3 TensorRefs → InstMatmult(8), engine=PE(3, builder)
[verify]   visitInstQuantizeMx @0xfba1f0 → checkQuantizeMxInstruction @0x1009a60 (32-block, 4-pack, quadrant)
           checkMatmultMxInputs @0x1007420 (data == 4×scale, same invariants)
[encode]   CoreV4 generateLdweightMx @0x143e350 + generateMatmultMx @0x143ebd0 → LdWeightMx(0x1009)+MatmultMx(0x100A)
           CoreV4 visitInstQuantizeMx @0x143dc60 → QuantizeMx opcode 0xE3 / word 0x10E3
[sim]      birsim doQuantizeMx : FP4 grid clamp + E8M0 amax/EMAX scale (the silicon semantics)
```

---

## 7. Confidence ledger

**CONFIRMED** (decompiled C + byte-level disasm + `nm`/`strings` cross-ref on this binary):
- All three leaf symbols at the tabled addresses (`nm -DC libwalrus.so`); all three wrappers.
- Call census: plain = 3 `codegenTensorRef` + `addMatmult` + `setCalcAuto`; MX = 5 `codegenTensorRef` + `insertElement<InstMatmultMx>` + 4 `addArgument` + 1 `addOutput`; QuantizeMX = 3 `codegenTensorRef` + `insertElement<InstQuantizeMx>` + 1 `addArgument` + 2 `addOutput`.
- Engine bytes: MX `movl $0x3,0x90 @0xf25663`; QuantizeMX `movl $0x5,0x90 @0xf25d50`.
- Plain bool bytes `movzbl 0x32/0x31/0x30(%rax)` at `0xf19852/0xf19873/0xf19894`; perf_mode `cmpl $0x2,0x68(%rax)` + `movl $0x1,0x2d0(%r14)` at `0xf198b5/0xf198bb`.
- QuantizeMX deser tag `cmpb $0xc3,0x1d(%rsp) @0xf5d396`; 3-TensorRef struct (`dst@0, src@16, dstScale@32`).
- Name prefixes `matmult_` / `MatmulMx-` / `QuantizeMX-` and wire names `Matmult` / `MatmultMx` / `QuantizeMx` all present (`strings`).
- IT-tags 8 / 95 cross-confirmed against [Op-Family Enums](op-family-enums.md).

**STRONG**:
- Only klr `perfMode==2` is special-cased to DoubleRow; other codes → None.
- MX argument order (`moving`/ifmap before `stationary`/weights) matches the `checkMatmultMxInputs` verifier order.
- MX omits `setCalcAuto`; H28 excludes MatmultMx from column-tiled accumulate grouping.
- MX/QuantizeMX dtype values inherited from upstream TensorRef via `optional<Dtype>`, not forced by codegen.
- FP4-output `getDst0ValidDtypes={8,9}` discrepancy vs the FP4-capable ×4 geometry gate.

**INFERRED**:
- `stationary=weights / moving=ifmap` Neuron mapping (from the builder slot names + verifier naming; the codegen text only proves klr field order).

**GAP**:
- The `addMatmult` *body* (behind the libBIR import) — engine/IT assignment confirmed by effect (the import signature + the produced node), not by reading the builder line-by-line.

---

## Cross-references

- [7.21 KlirToBirCodegen Dispatch Core](klir-codegen-dispatch.md) — the 77-case routing these three leaves hang off.
- [7.22 codegen AccessPattern Primitive](codegen-accesspattern.md) — `codegenTensorRef`/`codegenAccess`, the AP the leaves bind.
- [7.24 Codegen Sub-Encoders](codegen-sub-encoders.md) — `addOperandToInst` and the operand-strand model.
- [Op-Family Enums & the L1/L2/L3 Crosswalk](op-family-enums.md) — `Matmult(8)`/`MatmultMx(95)` IT-tags, `MatmultPerfMode`.
- [The bir::Instruction Base Struct](instruction-base.md) — `+0xA0`/`+0xC0` input/output lists, the `+0x90` engine field.
- [6.5.11 BirCodeGenLoop Compute Codegens](../nki/bircodegen-compute.md) — the beta3 sibling matmul codegen onto the same BIR node model.
- [2.10 PE Matmul Encoding](../isa/pe-matmul-encoding.md) — the downstream CoreV2/CoreV4 `setupHeader` opcode encode (`LdWeightMx 0x1009` / `MatmultMx 0x100A` / `QuantizeMx 0x10E3`).
- [MX-FP8 Microscaling Legalization](../hlo-opt/mx-fp8-legalization.md) — the upstream HLO split that produces the `{data, scale}` operand pair.
- [MXFP Weight/Scale Load Infrastructure](../nki/mxfp-weight-scale-load.md) — the weight/scale load path the MX matmul consumes.
