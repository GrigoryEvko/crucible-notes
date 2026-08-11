# KlirToBirCodegen: the data-movement leaf family — copy / DMA / load-store / gather / select / BN / transpose

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 ABI twins, drifting VAs). Subject binary: `neuronxcc/starfish/lib/libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`, 64,973,024 B) — the **Strand-I** back-end shared object that holds the `KlirToBirCodegen` bodies. `VA == file offset` for `.text` (`0x62d660+`) and `.rodata` (`0x1c72000+`), so every jump table and string literal below is read by file offset. `nki_klr_sim` is stripped and carries only PLT stubs for these symbols; every address cited here is the **real body** (an `nm -DC` `T`/`W` row `> 0xf00000`), not a thunk alias — beware the two-VA-frame artifact (each leaf has a sibling sidecar at a low `0x6xxxxx` internal frame; the real body is the `0xf2xxxx`/`0xf1xxxx` one). All evidence is `nm`, `objdump -d`, `strings`, and `.rodata` table decode against this binary.*

## Abstract

This page is the leaf-level specification of the **data-movement half** of `KlirToBirCodegen`,
the beta2 KLR→BIR lowering core. Where [7.21 the dispatch core](klir-codegen-dispatch.md)
documents how a `klr::Operator` node reaches its `codegen<Op>` leaf, this page documents what
**fourteen** of those leaves *do*: which `bir::Inst` they construct, which builder or
`insertElement<>` path they take, which klr fields bind to which operand, and which raw struct
offsets each leaf stamps on the node. The family covered here is everything that moves data
without computing on it (plus the two fused exceptions — DMA-FMA and BN-stats):

- **On-engine copy** — `NcCopy` → `InstTensorCopy` / `TensorCopyDynamic{Src,Dst}`.
- **Cross-space DMA** — `NcDmaCopy` → `InstDMACopy`, with the scatter fall-through
  `GenericIndirectSave`; the fused `DmaCompute` → DMA-FMA; and `DmaTranspose` →
  `InstDMACopy` carrying the transpose markers.
- **Register-addressed load/store** — `TensorLoad` → `InstTensorLoad`, `TensorStore` →
  `InstTensorSave`.
- **Indexed access** — `NcLocalGather` → `InstIndirectCopy`, `NcNGather` → `InstGather`,
  `NcRangeSelect` → `InstRangeSelect`, `SelectReduce` → `InstCopyPredicated`.
- **BN / reorganization** — `BatchNormStats` → `InstBNStats`, `BatchNormAggregate` →
  `InstBNStatsAggregate`, `Shuffle` → `InstStreamShuffle`, `Transpose` → `InstStreamTranspose`
  **or** matmul-against-identity.

The single highest-value structural fact on this page is the **three physically-distinct
transposes** (§6): a klr program can ask for a transpose three ways, and the three land on three
different hardware engines through three different BIR nodes. Confusing them is the most likely
reimplementation error, so they are pulled out as a QUIRK. The second is that **codegen resolves
`DGEType` at lowering time** (§3) — it is *not* left `Unassigned` for a later pass — while engine
and QoS *are* deferred. None of these leaves build access-pattern arithmetic themselves; they
delegate to the operand grammar of [7.22 codegenAccess](codegen-accesspattern.md) and
[7.23 the dynamic AP](codegen-dynamic-ap.md), and bind the resulting
`SmallVector<bir::APPair,4>` onto the node. This is the C++ beta2 mirror of the beta3
[BirCodeGenLoop DMA codegens](../nki/bircodegenloop.md); the two front-ends converge on the
same BIR node model.

| | |
|---|---|
| **Class** | `neuronxcc::backend::KlirToBirCodegen` (TU `klir_to_bir_codegen.cpp`) |
| **Binary** | `libwalrus.so` (Strand-I, cp310) |
| **Leaves** | 14 `codegen<Op>` bodies in the `0xf1d9e0`–`0xf30d40` band |
| **Dispatch** | `codegenStmt` → `codegenStmtOperWrapper` → `codegenOperator` (switch) → `codegenOperator<Op>Wrapper` → leaf (7.21) |
| **Operand grammar** | `codegenTensorRef` / `codegenAccess` / `codegenImmediate` / `codegenAluOp` / `codegenAccumCmd` (7.22–7.24) |
| **Downstream** | H06 LegalizeStridedDMA / LowerGenericIndirect → H33 engine-id assign → H37 `lower_dma` → J-encoders → sim |

---

## 1. The leaf roster — klr op → BIR node → builder path

Every leaf takes a `std::shared_ptr<klr::Op>` and returns a `bir::Instruction*`. The table is the
spine of the whole page; each row is verified from the leaf's disasm (the `insertElement<…>` /
`InstBuilder::add…` call site is read verbatim, the IT value cross-checked to the
[InstructionType table](instruction-type.md)).

| klr op | leaf VA | → BIR node (IT) | construction path |
|---|---|---|---|
| `NcCopy` | `0xf2d120` | `InstTensorCopy` (23) / `TensorCopyDynamicSrc` (24) / `TensorCopyDynamicDst` (25) | `addTensorCopyCustom` / `insertElement<…Dynamic{Src,Dst}>` |
| `NcDmaCopy` | `0xf2fa40` | `InstDMACopy` (32) / `InstGenericIndirectSave` (44) | `addUnaryInstruction<InstDMACopy>` / `insertElement` / fall-through |
| `DmaCompute` | `0xf1d9e0` | `InstDMACopy` (32, FMA form) | `addDmaCopyFma` |
| `DmaTranspose` | `0xf2dd10` | `InstDMACopy` (32, transpose markers) | `addInstruction<InstDMACopy>` / `insertElement` |
| `GenericIndirectSave` | `0xf2eb10` | `InstGenericIndirectSave` (44) | `insertElement<InstGenericIndirectSave>` |
| `TensorLoad` | `0xf26a60` | `InstTensorLoad` (75) | `insertElement<InstTensorLoad>` |
| `TensorStore` | `0xf266c0` | `InstTensorSave` (76) | `insertElement<InstTensorSave>` |
| `NcLocalGather` | `0xf2a9c0` | `InstIndirectCopy` (26) | `insertElement<InstIndirectCopy>` |
| `NcNGather` | `0xf2a590` | `InstGather` (92) | `insertElement<InstGather>` |
| `NcRangeSelect` | `0xf1c210` | `InstRangeSelect` (63) | `InstBuilder::addRangeSelectReduce` |
| `SelectReduce` | `0xf270a0` | `InstCopyPredicated` (52) | `insertElement<InstCopyPredicated>` |
| `BatchNormStats` | `0xf1add0` | `InstBNStats` (34) | `InstBuilder::addBNStats` |
| `BatchNormAggregate` | `0xf1b0d0` | `InstBNStatsAggregate` (35) | `InstBuilder::addBNAggr` |
| `Shuffle` | `0xf28130` | `InstStreamShuffle` (39) | `insertElement<InstStreamShuffle>` |
| `Transpose` | `0xf28fb0` | `InstStreamTranspose` (40) **or** `InstMatmult` (transpose) | `insertElement<…>` / `addMatmult` |

Two construction idioms run through the table, and the choice between them is itself a recoverable
fact about each node:

- **`InstBuilder::add…`** (the typed builder) is used where a BIR-library helper owns the
  node's allocation, naming, and operand binding in one call: `addTensorCopyCustom`,
  `addUnaryInstruction<InstDMACopy>`, `addDmaCopyFma`, `addRangeSelectReduce`, `addBNStats`,
  `addBNAggr`, `addMatmult`. These bodies are **extern** (defined in `libBIR.so`, imported `U`
  in `libwalrus`), so the codegen leaf only assembles the builder's arguments.
- **`NamedObjectContainer<BasicBlock,Instruction>::insertElement<InstX>`** is the raw insert: the
  leaf mints the node, then binds operands by hand with `addArgument<PhysicalAccessPattern>` /
  `addOutput<PhysicalAccessPattern>` and stamps the node's own fields directly. Used by the
  dynamic-copy subclasses, `InstGather`, `InstIndirectCopy`, `InstCopyPredicated`,
  `InstStreamShuffle`, `InstStreamTranspose`, `InstGenericIndirectSave`, `InstTensorLoad`,
  `InstTensorSave`. The raw path is what lets us read the exact struct offsets each leaf writes.

The wrapper layer (`codegenOperator<Op>Wrapper`) is a thin refcount shim that unwraps the inner
klr node and tail-calls the leaf; all naming / `OpDebugInfo` / device-print lives one level up in
`codegenStmtOperWrapper` (7.21), not here.

---

## 2. On-engine copy — `codegenNcCopy` → `InstTensorCopy` (and its dynamic twins)

`klr::NcCopy` is the **same-memory-space** copy (SBUF→SBUF / on-engine), distinct from a DMA. The
node carries four fields (`to_string` @ `0xf8d760`): `dst` @+0, `src` @+16, `dtype` @+32
(present-flag @+36), `engine` @+40.

The leaf first probes both operands with `neuronxcc::backend::isAccessDynamic` and forks on a
two-bit dynamic-ness signature:

```c
// codegenNcCopy @0xf2d120 — the dynamic-ness fork
bool dyn_dst = isAccessDynamic(NcCopy.dst);   // r15
bool dyn_src = isAccessDynamic(NcCopy.src);
if (dyn_dst && dyn_src)
    throw runtime_error("Only one of src and dst can have dynamic access in tensor_copy.");

if (!dyn_dst && !dyn_src) {                    // (A) both static
    InstArg dst = codegenTensorRef(NcCopy.dst);
    InstArg src = codegenTensorRef(NcCopy.src);
    name = returnAndIncrementId();             // "TensorCopy_"
    node = InstBuilder::addTensorCopyCustom(   // @0xf2d7d0 -> IT 23
               name, dst.MemLoc, src.MemLoc, dst.AP, src.AP, dst.size, src.size, srcLoc);
} else {                                       // (B) exactly one side dynamic
    // dynBirAp = castToBirAp(dynamic side);
    // assert dynBirAp->scalarOffset.has_value()      "Dynamic access must have scalarOffset"
    // if dynBirAp has only a vector offset -> throw
    //   "tensor_copy only supports scalar_offset for dynamic access, not vector_offset."
    if (dyn_dst)                               // src-dynamic subclass
        node = insertElement<InstTensorCopyDynamicSrc>(BB, "TensorCopyDynamicSrc_");  // @0xf2d40b IT 24
    else                                       // dst-dynamic subclass
        node = insertElement<InstTensorCopyDynamicDst>(BB, "TensorCopyDynamicDst_");  // @0xf2d938 IT 25
    // 3x addArgument + 1x addOutput; the dynamic AP carried as the scalar-offset arg;
    // node+0x110 variant tag guards a MaybeAffine<uint> reset, then node+0xF0 = computed size
    // sub_F20EC0(codegen+0x224, node)   // register into the per-function tracking list
}
*(uint*)(node + 0x90) = codegenEngine(NcCopy.engine);   // engine-id  @0xf2d5d5 / dynamic-path tail
return node;
```

Two notes that matter for reimplementation:

- **It never emits `GenericCopy` (IT 1).** The klr `NcCopy` path *always* lands on `TensorCopy`
  (23) or a `TensorCopyDynamic{Src,Dst}` (24/25). `GenericCopy` is an internal
  `InstAbstractCopy`-family node used by *other* lowerings and later passes; it is not a
  `codegen<NcCopy>` output.
- **`codegenNcCopy` sets engine and nothing else DMA-ish.** No `DGEType`, no QoS, no
  `compute_op` — it is an on-engine copy with no `InstDMA` base. The only post-binding writes are
  the engine-id at `+0x90` and (dynamic path) the size/affine reset.

The verbatim disasm confirms both string labels and the `addTensorCopyCustom` target:
`0xf2d3f3 lea rsi, "TensorCopyDynamicSrc_"`, `0xf2d40b call …insertElement<InstTensorCopyDynamicSrc>`,
`0xf2d7d0 call …addTensorCopyCustom`, `0xf2d91f lea rsi, "TensorCopyDynamicDst_"`. The
`scalarOffset` discipline of branch (B) is the same dynamic-AP contract documented in
[7.23](codegen-dynamic-ap.md).

---

## 3. Cross-space DMA — `codegenNcDmaCopy` → `InstDMACopy` (IT 32)

`klr::NcDmaCopy` is the DRAM↔SBUF transfer. Seven `to_string` fields (`0xf8c550`): `dst` @+0,
`src` @+16, `compute_op` @+32 (`DgeComputeOp`: 1=`none`, 2=`add`), `oobMode` @+40, `dgeMode` @+56
(`Nat`), `uniqueIndices` @+60 (`Bool`), `engine` @+64.

### 3.1 The top-level fork — main DMA vs scatter

The very first thing the leaf does is decide whether this is an ordinary DMA or a scatter, using
two comparisons read byte-exact from the disasm:

```asm
0xf2fa61:  cmp  dword ptr [rsi+20h], 1   ; compute_op == 1 (none / plain copy)
0xf2fa67:  cmp  byte  ptr [rsi+3Ch], 0   ; uniqueIndices != 0
```

```c
if (compute_op == 1 /*none*/ || uniqueIndices)   // -> the main InstDMACopy path (§3.2+)
else  node = codegenGenericIndirectSave(klr);    // @0xf2fa61 fork; call @0xf30268 -> IT 44 (§7)
```

So an *accumulating, non-unique-index* DMA is not a DMA at all — it is rerouted to the indirect
scatter leaf. `codegenNcDmaCopy` is the **only** caller of `codegenGenericIndirectSave`.
*Anchors: the fork bytes above, plus `call …codegenGenericIndirectSave` @ `0xf30268`.*

### 3.2 `DGEType` is resolved here — `translateDGEMode`

The decisive correctness fact: the leaf **sets** the DMA's `DGEType` at lowering time. It is not
left `Unassigned` for a later DGE pass.

```c
// translateDGEMode @0xf14b60
int translateDGEMode(uint m) {
    if (m > 3) fatal;
    static const uint tbl[4] = {3, 1, 2, 0};   // .rodata @ 0x1DE9730
    return tbl[m];                             // klr dgeMode -> bir::DGEType
}
// klr 0 -> 3 (Unassigned) | klr 1 -> 1 (SWDGE) | klr 2 -> 2 (HWDGE) | klr 3 -> 0 (None)
*(uint*)(node + 0xF8) = translateDGEMode(NcDmaCopy.dgeMode);   // mov [r12+0F8h],eax @0xf30193
```

`bir::DGEType` = {`None`0, `SWDGE`1, `HWDGE`2, `Unassigned`3}. The `+0xF8` field is the `InstDMA`
base `dge_type` (ctor default 3); a JSON-loaded DMA fills the *same* offset, so a codegen'd DMA and
a wire-loaded DMA are structurally identical at `+0xF8`. A later pass (H33) assigns only the DGE
*engine-id*, never the DGE *type*.

### 3.3 Dynamic-access legality, then build

```c
if (isAccessDynamic(dst) && isAccessDynamic(src))
    throw "Only one of the src and dst can contain dynamic access.";
if (isAccessDynamic(src) && DGEType == None) throw "DGE must be enabled when dynamic src access in DMA is used";
if (isAccessDynamic(dst) && DGEType == None) throw "DGE must be enabled when dynamic dst access in DMA is used";

if (neither side dynamic) {                              // STATIC path
    InstArg dst = codegenTensorRefImpl(NcDmaCopy.dst, 1);
    InstArg src = codegenTensorRefImpl(NcDmaCopy.src, 1);
    name = returnAndIncrementId();                       // "dma_copy_"
    node = InstBuilder::addUnaryInstruction<InstDMACopy>(// @0xf309c9
               codegen+0xE0, name, dst.MemLoc, src.MemLoc, src.AP, dst.AP,
               dst.size, src.size, dstDtype, srcDtype, srcLoc);
    // if src.getShape() != dst.getShape(): set both operand PhysicalAccessPatterns
    //   to getCanonicalPattern (canonicalize a same-shape DMA's AP)
} else {                                                 // DYNAMIC path (exactly one side)
    DynamicAPINFO dyn = tc_new(248); zero-init; dyn[+30] = 0xFFFFFFFF;  // sentinel
    assembleDynamicInfo(&dyn, dynamic-side TensorRef, InstArg);          // @0xf20230 (7.23)
    node = insertElement<InstDMACopy>(BB, …, "DynamicDMACopy-");
    // dynamic AP push-installed onto node+0xA0 (src AP holder) or node+0xC0 (dst AP holder);
    // the static side gets a MemoryLocation::fullAP-derived PhysicalAccessPattern;
    // DynamicAPINFO moved into node+0x1D8; non-dynamic operand added via addArgument.
    sub_F20EC0(codegen+0x224, node);
}
```

### 3.4 `compute_op`, engine, OOB — the tail attributes

```c
*(uint*)(node + 0xF8) = DGEType;                         // (§3.2)
switch (compute_op) {                                    // klr +0x20
  case 2 /*add*/:  *(uint*)(node + 0x178) = 4; break;    // mov [r12+178h],4 @0xf30328 — DMA accumulate (reduce cmd)
  case 1 /*none*/: break;                                 // plain copy
  default: throw "Unsupported DMA compute_op <n>";
}
uint eng = codegenEngine(NcDmaCopy.engine);              // klr +0x40
if (DGEType == HWDGE)      *(uint*)(node + 0x90) = eng;  // engine-id @0xf30318
else if (eng != 0)         throw "dma_copy engine can only be specified when HWDGE mode is used.";
*(bool*)(node + 0x130) = (klr.oob[+0x28] != 1);          // oob_is_err; node+0x150 variant tag reset first
return node;                                             // bir::InstDMACopy*, IT 32
```

The engine gate is the central asymmetry vs `NcCopy`: an explicit engine is **only** legal under
HW-DGE; SW/None DMAs leave engine-id at 0 for H33's `assign_*_engine` pass. **QoS is not set here**
— the `InstDMACopy` QoS lives at `+0x1D8` and stays at the ctor default `Unassigned`; on the
dynamic path `+0x1D8` is *repurposed* as the `DynamicAPINFO` holder before any QoS could be set, so
QoS is unconditionally a downstream concern. The `+0xF8`/`+0x178`/`+0x90`/`+0x130` writes are byte-anchored; the QoS-default conclusion follows from the absence of any write to `+0x1D8` on the static path.

---

## 4. Fused DMA — `codegenDmaCompute` → `InstDMACopy` via `addDmaCopyFma`

`klr::DmaCompute` is the **DMA-FMA**: a copy that also applies a fused scaled accumulate as the
descriptors stream. Four `to_string` fields (`0xf94020`): `dst` @+0, `srcs` @+16 (a
`std::list<TensorRef>`), `scales` @+40 (a `std::list<float-Immediate>`), `reduceOp` @+64
(`klr::AluOp`).

```c
// codegenDmaCompute @0xf1d9e0
if (codegenAluOp(DmaCompute.reduceOp) != 4)
    throw "dma_compute currently only supports nl.add as its reduce op!";   // hard-pinned to add(4)

InstArg dst = codegenTensorRef(DmaCompute.dst);
vector<InstArg> srcs;   for (TensorRef r : DmaCompute.srcs)  srcs.push_back(codegenTensorRef(r));
vector<float>   scales; for (Immediate i : DmaCompute.scales) {
    float s = codegenImmediate(i);
    if (!is_float_immediate(i)) throw "scale values for dma_compute must be floats";
    scales.push_back(s);
}                                                          // one scale PER source

name = returnAndIncrementId();                             // "DMACompute"
node = InstBuilder::addDmaCopyFma(                         // @0xf1e054
           name, ArrayRef<InstArg>(srcs), dst, ArrayRef<float>(scales), srcLoc);
return node;
```

Semantics: `dst = Σ_i ( scales[i] · srcs[i] )` with the reduce op pinned to `add`. The
`DgeComputeOp=add` single-source accumulate of §3.4 is the degenerate case; `DmaCompute` is the
multi-source scaled-sum generalization. The builder owns IT/engine/DGE assignment — the leaf
stamps **none** of `+0xF8`/`+0x90`/`+0x128`.

*Anchor: `addDmaCopyFma` signature — `(string const&, llvm::ArrayRef<InstArg>, InstArg, llvm::ArrayRef<float>, source_location const&)`.*

---

## 5. Descriptor transpose — `codegenDmaTranspose` → `InstDMACopy` + transpose markers

`klr::DmaTranspose` is the **DMA-descriptor** transpose (HBM↔SB, rank-4). Six `to_string` fields
(`0xf85ed0`): `dst` @+0, `src` @+16, `axes`, `dtype`, `dgeMode` @+44, `oobMode` @+48.

```c
// codegenDmaTranspose @0xf2dd10 — all four tail writes byte-anchored
InstArg dst = codegenTensorRef(DmaTranspose.dst);
InstArg src = codegenTensorRefImpl(DmaTranspose.src, 1);
if (dst.AP.rank != 4 || src.AP.rank != 4)
    throw "DMA Tranpose must have 4-D access pattern. Explicitly writing AP is required "
          "if slicing is not generating correct pattern";          // RANK-4 gate

// rebuild the src AP to a canonical 4-element pattern:
//   base = MemoryLocation::fullAP(dst MemLoc); copy APPair[0] verbatim;
//   if fullAP has < 4 pairs, PAD with a fixed filler from xmmword_1DD1FE0 (.rodata)
//   so the pattern is exactly 4-D (XZYW permutation expects [W,Z,Y,X] = AP[0..3]).

if (!isAccessDynamic(src))                                  // STATIC
    node = InstBuilder::addInstruction<InstDMACopy>(codegen+0xE0, "DMATranspose-", dst.AP, src.AP, …);
else {                                                      // DYNAMIC
    DynamicAPINFO dyn; assembleDynamicInfo(&dyn, src, …);
    node = insertElement<InstDMACopy>(BB, "DMATranspose-");
    // transpose AP push-installed onto node+0xA0/+0xC0; non-dynamic side via addArgument
    sub_F20EC0(codegen+0x224, node);
}

*(uint*)(node + 0x128) = 1;     // CopyMode = Transpose       mov [r12+128h],1   @0xf2e0c6
*(uint*)(node + 0x158) = 0x0B;  // TransposeOps = 11 = XZYW   mov [r12+158h],0Bh @0xf2e0d2
*(uint*)(node + 0xF8)  = translateDGEMode(DmaTranspose.dgeMode);  // DGEType  @0xf2e707
*(uint*)(node + 0x90)  = 1;     // engine = Pool(1)           mov [r12+90h],1    @0xf2e719
*(bool*)(node + 0x130) = (klr.oob[+48] != 1);                // node+0x150 variant tag reset first
return node;                                                 // bir::InstDMACopy*, IT 32
```

`XZYW` (TransposeOps 11) maps source `[W,Z,Y,X]` → dest `[X,Z,Y,W]` (perm `[3,1,2,0]`): swap the
outermost-vs-innermost axes. The engine is **pinned to Pool** directly — `codegenDmaTranspose` does
not call `codegenEngine`. The DGEType passes through whatever `dgeMode` maps to; the verifier
forbids HW-DGE on a transpose, so in practice it realises as None/SWDGE, but that is enforced
downstream, not hard-written here.

---

## 6. QUIRK — the three distinct transposes

> **The single most important distinction on this page.** A klr program can request a transpose
> three ways, and all three produce *different* BIR nodes on *different* hardware engines. There is
> no shared "transpose" lowering. A reimplementation that routes all transposes through one path is
> wrong.

| klr op | leaf | BIR node | engine | how the transpose is encoded | shape constraint |
|---|---|---|---|---|---|
| `Transpose` (fp32) | `codegenTranspose` @`0xf28fb0` | `InstStreamTranspose` (40) | **DVE / Scalar** | hard-wired 32×32 element⇄partition tile swap (no perm code) | ≤ 32×32 tile |
| `Transpose` (non-fp32) | `codegenTranspose` @`0xf28fb0` | `InstMatmult` (transpose flag) | **PE** | systolic Identity·input with `isTranspose` bit | 128×128 identity |
| `DmaTranspose` | `codegenDmaTranspose` @`0xf2dd10` | `InstDMACopy` (32) | **Pool / DMA** | `CopyMode=Transpose` @+0x128, `TransposeOps=XZYW` @+0x158 | rank-4 AP |

The DMA-descriptor path is §5. The other two share one leaf, `codegenTranspose`, which forks on the
**destination dtype**:

```c
// codegenTranspose @0xf28fb0
if (klr.Transpose.engine == 5 /*pe*/)                       // cmp [rsi+28h],5  @0xf28fd1
    throw "TransposeOp should only happen on DVE. The PE variant should have been legalized by the FE!";

MemoryLocation *out = codegenTensorRef(klr.dst);
MemoryLocation *in  = codegenTensorRef(klr.src);

if (out->getDtype() == 16 /*fp32*/) {                       // cmp dword [rax],0x10  @0xf2906e
    // PATH A — DVE 32x32 stream transpose
    name = returnAndIncrementId();                          // "StreamTranspose-"
    node = insertElement<InstStreamTranspose>(BB, name);    // @0xf2947d  IT 40
    addArgument<PhysicalAccessPattern>(node, in,  in.AP);
    addOutput  <PhysicalAccessPattern>(node, out, out.AP);
    _validateLegalStreamTranspose(node);                    // @0xf29554: AP[0].step > 32 -> throw
                                                            //   "Operand must be no larger than (32, 32) ..."
    sub_F20EC0(codegen+0x224, node);
} else {
    // PATH B — PE matmul against a 128x128 identity
    MemoryLocation *Id = getIdentityMatrix(out->getDtype());// @0xf29098 (§6.1)
    // build identity AP {P, e} and a moving-input AP with K pinned to 128
    name = returnAndIncrementId();                          // "matmult_TP_"
    node = InstBuilder::addMatmult(name, out, /*stationary=*/Id, /*moving=*/in,
                                   ap_out, ap_stationary, ap_moving, … );   // @0xf292f9
    InstMatmultBase::setCalcAuto(node);                     // PE + auto-accumulate
    *(uint*)(node + 0x1B8) = 1;  *(uint*)(node + 0x1D8) = 0; // isTranspose flag (+440=1, +472=0)
}
return node;
```

The guard is the linchpin: a `klr::Transpose` tagged `engine==pe(5)` is **illegal** here — the
front-end must already have rewritten a PE transpose into an explicit `nc_matmul`. So
`codegenTranspose` only ever emits the DVE stream form or the auto matmul-against-identity fallback;
neither touches `InstDMACopy`, `CopyMode`, or `TransposeOps`.

*Anchors: `codegenTranspose` @ `0xf28fb0` — the PE-guard, `getIdentityMatrix`, `addMatmult`, `insertElement<InstStreamTranspose>`, and `_validateLegalStreamTranspose` call sites.*

### 6.1 `getIdentityMatrix` @`0xf28910` — the lazy 128×128 identity

Path B needs an identity matrix; `getIdentityMatrix(bir::Dtype)` is a per-function, per-dtype memoised
generator:

```c
name = "COMPILER-GENERATED-id." + Dtype2string(dt);
Id = Function::getMemoryLocationByName(name);
if (Id) return Id;                                          // cache hit
Id = InstBuilder::addSBTensor(name);                        // 128x128 SBUF tensor
// boost.log WARNING: "Emitting dynamic Identity matrix generation code, this might affect performance."
//                    "You can bypass this by pass in an explicit identity matrix and use nc_matmul for transpose"
InstBuilder::addMemset(name + "_memset", Id, AP);           // zero the tensor
InstBuilder::addUnaryInstruction<InstTensorScalarAffineSelect>(
    name + "=IDAffineSelect(mask,fill=1)", Id, Id, …);      // set diagonal to 1.0f:
                                                            //   inst+404 = 0x3F800000 (1.0f), dtype +400 = 19
push Id onto function identity vector;
return Id;
```

The identity is `memset(0)` + an affine-select that fills the `p==e` diagonal with `1.0f`, then
reused across every matmul-transpose of the same dtype in the function. `Identity·inputᵀ` ⇒ `inputᵀ`
on the PE systolic array.

---

## 7. The scatter — `codegenGenericIndirectSave` @`0xf2eb10` → `InstGenericIndirectSave` (IT 44)

This is the index-driven scatter, reached **only** as the `codegenNcDmaCopy` fall-through (§3.1). It
takes the same `klr::NcDmaCopy` node, but treats `dst` as an index-addressed scatter target and
`src` as the data being scattered.

```c
// codegenGenericIndirectSave @0xf2eb10 — offsets byte-anchored
name = returnAndIncrementId();
strcpy(name, "IndirectSaveAccumulate-");                    // NOTE: name prefix is the INTENT tag,
                                                            //   not the IT — the node is GenericIndirectSave(44)
node = insertElement<InstGenericIndirectSave>(BB, name);    // @0xf2eb… IT 44
sub_F20EC0(codegen+0x224, node);

if (isAccessDynamic(NcDmaCopy.src))
    throw "When unique_index=False, dynamic access in src is not supported";  // data must be STATIC

// INDEX-VECTOR / INDIRECT-DIM bind -> indirect_dims vector @ +0x128
castToBirAp(NcDmaCopy.dst);
int coef = ap[+0x60];                                       // the IndirectDim coefficient
int *p = tc_new(4); *p = coef;
sub_F12EE0(node + 0x128, {p, p+1});                         // lea rdi,[r12+128h] @0xf2ec8d  push into indirect_dims

// RMW / accumulate bind -> op @ +0x140
switch (NcDmaCopy.compute_op /*+0x20*/) {                   // mov eax,[rax+20h]
  case 2 /*add*/: *(uint*)(node + 0x140) = 4; break;        // mov [r12+140h],4 @0xf2f240 — AluOpType add(4)
  case 1 /*none*/: break;                                   // leave +0x140 default -> OVERWRITE
  default: throw "Unsupported DMA compute_op " + to_string(compute_op);
}

// operand/AP grammar:
//   (a) codegenTensorRefImpl(dst) -> addArgument  : the DATA-side AP   (input #0)
//   (b) extractDynamicApToStandAlone(dst) -> addArgument : the INDEX AP (input #1)
//   (c) codegenTensorRefImpl(dst) -> output-base MemLoc ; getDtype -> elemBytes geometry
// elemBytes = qword_1DE98C0[Dtype] = {1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}
return node;                                                // NO DGEType / engine / QoS here
```

The two stamped fields are exactly the [InstructionType](instruction-type.md) IT-44
structural-compare fields (`+0x128` container, `+0x140` dword) and the JSON wire keys
`indirect_dims` / `op` — codegen and the wire reader agree byte-for-byte. The leaf produces only the
**generic** scatter; H06 `lower_generic_indirect` (`codegenIndirectLoadSave` @`0xb580e0`) later
expands it into `ReadVarAddr(41)` → `TensorScalarPtr` → `Memset` → `TensorScalarPtr` →
`InstIndirectSave(45)` (or `IndirectSaveAccumulate(46)` when `op != default`). The offsets are byte-anchored; the exact operand-to-arg mapping is read off the call-site argument order.

---

## 8. Register-addressed load/store — `TensorLoad` (75) / `TensorStore` (76)

`InstTensorLoad`/`InstTensorSave` are the **runtime/register-addressed** scalar load/store: the
address is held in a named `bir::RegisterAccess` (a signed-int scalar register minted on engine
`ALL`), not a compile-time AP. They are the indexed counterpart of the static-AP plain
`Load(19)`/`Save(22)`, and — unlike those — are **not** members of the DMA IT-mask. The two leaves
are byte-symmetric (both 815 B); the only differences flip read→write:

| | `codegenTensorLoad` @`0xf26a60` (IT 75) | `codegenTensorStore` @`0xf266c0` (IT 76) |
|---|---|---|
| node | `insertElement<InstTensorLoad>` | `insertElement<InstTensorSave>` |
| name | `"TensorLoad-"` | `"TensorStore-"` |
| `RegisterAccess` chain | INPUT head @ `+0x192` | OUTPUT head @ `+0x160` |
| AP bind | `addArgument` (source AP) | `addOutput` (dest AP) |

```c
// codegenTensorLoad @0xf26a60 (Store is symmetric)
if (!Function::getRegisterByName(regName))
    codegenRegister(regName, EngineType);              // @0xf141e0 — declare-or-mint named scalar reg
codegenTensorRef(srcRef);
name = returnAndIncrementId();                         // "TensorLoad-<n>"
node = insertElement<InstTensorLoad>(BB, name);        // IT 75
*(uint*)(node + 0x90) = 7;                             // engine = EngineType ALL  mov [r12+90h],7 @0xf26b94
sub_F20EC0(codegen+0x224, node);

RegisterAccess *ra = tc_new(72); RegisterAccess::RegisterAccess(ra, node);
// splice ra into the intrusive register-access list rooted at node+0x192 (Load) / node+0x160 (Store)
ra->setType(15);                                       // type 15 = SIGNED INT runtime address  (cmp vs Argument::setType @0xf26c14)
addArgument<PhysicalAccessPattern>(node, memloc, ap);  // Store: addOutput
return node;
```

Engine `ALL`(7) marks these as pre-lowering placeholders not yet engine-pinned; the register
supplies the dynamic offset, the AP only describes the region.

*Anchors: `mov [r12+90h],7` @ `0xf26b94`, the `Argument::setType` comparison @ `0xf26c14`, and both `insertElement` targets.*

---

## 9. Indexed gather/select — `LocalGather` / `NGather` / `RangeSelect` / `SelectReduce`

Four indexed-access leaves. None of them sets an `IndirectDim` field directly — the gather axis
lives inside the access pattern (`DynamicAPINFO+0x74`, [7.23](codegen-dynamic-ap.md)) that
`codegenTensorRef` builds; these leaves only bind those APs and stamp scalar fields.

### 9.1 `codegenNcLocalGather` @`0xf2a9c0` → `InstIndirectCopy` (26) — SBUF-local offset gather

The "local" gather is an SBUF→SBUF row-gather with a compile-time-validated 16-partition-block
geometry and a runtime u16 offset vector. Node layout: `out` @+0, `data` @+16, `index/offset` @+32,
`num_valid_indices` (Immediate) @+48.

```c
// codegenNcLocalGather @0xf2a9c0
out_ap   = codegenTensorRef(node[+0]);
data_ap  = codegenTensorRef(node[+16]);
index_ap = codegenTensorRef(node[+32]);
n_idx    = index_ap.numElements;
blocks   = (n_idx == 0) ? 0 : ((n_idx - 1) >> 4) + 1;    // ceil(n_idx / 16) — 16-partition blocks
imm      = codegenImmediate(node[+48]);
assert(holds_alternative<int>(imm) && "num_valid_indices for local gather must be an integer");
dt       = data_ap.getDtype();
numElePerIdxVal = (data_ap.eltsBytes / DTYPE_STRIDE_LUT[dt]) / blocks;   // LUT @0x1de98c0
assert(numElePerIdxVal == dst.getEltsPerPartition() / num_valid_indices);

name = returnAndIncrementId();                            // "LocalGather-<id>"
ic = insertElement<InstIndirectCopy>(BB, name);           // @0xf2abda  IT 26
ic.addArgument(data_ap);                                  // arg0 = data source
ic.addArgument(index_ap);                                 // arg1 = u16 offset vector
ic.addOutput  (out_ap);                                   // out0 = gathered rows
// num_valid_indices -> inst+0xF0 via variant<u16,QuasiAffineExpr>::_M_reset
finalize(codegen+0xE0, ic);
return ic;
```

The `DTYPE_STRIDE_LUT` @`0x1de98c0` = `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}` is byte-identical
to the simulator's stride table — codegen and the executable agree on element widths. Past
`num_valid_indices`, the sim fills `-1` and skips (the `readIndirect`→`doCopyIndirect` path).

### 9.2 `codegenNcNGather` @`0xf2a590` → `InstGather` (92) — per-partition int32 gather

```c
// codegenNcNGather @0xf2a590
out_ap   = codegenTensorRef(node[+0]);
data_ap  = codegenTensorRef(node[+16]);    // params / data source
index_ap = codegenTensorRef(node[+32]);    // int32 indices
g = insertElement<InstGather>(BB, "Gather-<id>");          // @0xf2a6d7  IT 92
g.addArgument(data_ap);  g.addArgument(index_ap);  g.addOutput(out_ap);
finalize(codegen+0xE0, g);
return g;                                   // out[p][i] = params[p][indices[p][i]]
```

`LocalGather` vs `NGather` is the two-flavor split: `IndirectCopy`(26) is a **u16 offset vector**,
16-partition blocks, compile-time `num_valid_indices` + geometry assert, `-1`-skip OOB; `Gather`(92)
is an **int32 index tensor**, per-partition, runtime bounds-assert, no valid count.

### 9.3 `codegenNcRangeSelect` @`0xf1c210` → `InstRangeSelect` (63) — dual-bound band-keep + reduce

The only one of the four that uses a typed builder. The demangled `addRangeSelectReduce` signature
*is* the operand contract:

```c
// InstBuilder::addRangeSelectReduce — signature read verbatim @0xf1c633
addRangeSelectReduce(
    InstArg in, variant<InstArg,float> bound0, variant<InstArg,float> bound1,
    InstArg rangeTensor, optional<InstArg> fill, int mode,
    optional<AluOpType> reduceOp, source_location const&);

// codegenNcRangeSelect @0xf1c210 assembles:
in     = codegenTensorRef(node[+0]);
accum  = codegenAccumCmd(node);
rangeT = codegenTensorRef(node[+0x18]);
cmp0   = codegenAluOp(node[+0x30]);   cmp1 = codegenAluOp(node[+0x38]);   redOp = codegenAluOp(node[+0x40]);
rs = addRangeSelectReduce(in, {bound0}, {bound1}, rangeT, fill?, mode, redOp, srcLoc);  // IT 63
```

Semantics (the windowing / attention-mask primitive): below `bound0` → `fill`; inside the
`[bound0,bound1]` band → keep `in[i]`; above → `fill`; with a `max` reduce over survivors. The
`int mode` parameter is stored at `InstRangeSelect+0xF0`. The store is pinned; what `mode` *means* is [INFERRED] — most plausibly the reduce/affine selector, by analogy to the `+0xF0` field on `IndirectCopy`.

### 9.4 `codegenSelectReduce` @`0xf270a0` → `InstCopyPredicated` (52) — predicated select + reduce

Despite the name, `SelectReduce` builds `InstCopyPredicated` (52) — a predicated copy fused with a
second-output reduce. The "select + reduce" fusion exists at two levels, and the two are separate
nodes: `RangeSelect`(63) bundles the dual-bound compare with a max-reduce, while `CopyPredicated`(52)
is the predicate-select that `SelectReduce` then pairs with a reduce-into output.

> **GOTCHA —** `SelectReduce` does **not** lower to `RangeSelect`, even though both are "select plus
> reduce". Route it to `InstCopyPredicated`(52).

```c
// codegenSelectReduce @0xf270a0
t0 = codegenTensorRef(node[+0]);    // dst of the predicated copy
t1 = codegenTensorRef(node[+16]);   // onTrue src
t2 = codegenTensorRef(node[+32]);   // predicate
cp = insertElement<InstCopyPredicated>(BB, "SelectReduce-<id>");   // @0xf271ef  IT 52
cp.addOutput(t0);  cp.addArgument(t1);  cp.addArgument(t2);
addOperandToInst(node[+0x30], cp);  // 4th operand
// fused reduce half: codegenAccumCmd + codegenAluOp + reduce-into TensorRef -> cp.addOutput(tR)
finalize(codegen+0xE0, cp);
return cp;
```

`CopyPredicated` is `out ← preds ? src : out`. Codegen emits it directly (rather than `Select`(51))
because `Select`(51) has **no machine emitter** — the `lower_select` pass exists precisely to split
`Select`→`GenericCopy`+`CopyPredicated`, so `codegenSelectReduce` takes the shortcut of emitting the
encodable form up front. The `Select`-not-encodable rationale is cross-checked against the `lower_select` pass.

These four are the indexed-access primitives behind MoE expert routing: the routed-token/expert
gather (`Gather`/`IndirectCopy`), the token mask (`RangeSelect`), and the predicated expert-output
combine (`CopyPredicated`); the scatter side is the `IndirectSaveAccumulate`(46) family.

---

## 10. BatchNorm + Shuffle — the reorganization leaves

### 10.1 `BatchNormStats` (34) / `BatchNormAggregate` (35) — identical codegen, different builder

The two BN leaves are **byte-for-byte the same control flow** — `codegenTensorRef(dst)`,
`codegenTensorRef(src)`, two `SmallVector<APPair,4>` via `sub_F12B40`, one builder call — differing
*only* in which builder they call. Both `klr` nodes carry the same 3-field layout `{dst, src, dtype}`.

```c
// codegenBatchNormStats @0xf1add0 -> addBNStats     // @0xf1af6f  IT 34
// codegenBatchNormAggregate @0xf1b0d0 -> addBNAggr  // @0xf1b26f  IT 35
// BOTH builders share the 7-arg signature:
//   add{BNStats,BNAggr}(MemoryLocation* dst, MemoryLocation* src,
//                       SmallVector<APPair,4> ap_dst, SmallVector<APPair,4> ap_src,
//                       uint apply_transpose, uint dtypeFlag, source_location const&);
```

So "Aggregate is BNStats with a different opcode" at codegen level — the merge math lives in the
executor, not the codegen. The geometry the builder records matches the sim contract exactly:

- **BNStats**: output AP = **6 elements/partition** (two interleaved Welford `(count,mean,M2)`
  triples). The first `uint` arg is `apply_transpose` (`MaybeAffine<bool>` @ inst `+0xF0`, variant
  tag `+0x110`); when true the verifier requires input `%32==0` elements *and* partitions (the 32-tile
  blocking for partition-axis reduction).
- **BNStatsAggregate**: input AP `%3==0` (K Welford triples to merge), output AP **exactly 2
  elements/partition** (mean, population variance). Carries no own JSON field.

The two distinct builders and their identical signatures are read directly. The `apply_transpose` / `dtypeFlag` decomposition of the two `uint` args is [INFERRED] from the BN wire schema, because the builder bodies are extern in `libBIR`.

### 10.2 `codegenShuffle` @`0xf28130` → `InstStreamShuffle` (39) — the 32-entry partition mask

```c
// codegenShuffle @0xf28130
dst = codegenTensorRef(klr.shuffle.dst);   src = codegenTensorRef(klr.shuffle.src);
vector<uint> mask;
for (Operand op : klr.shuffle.shuffleMask /*list @+32*/) {
    int m = codegenImmediate(op);
    assert(BYTE4(m) == 1 && "shuffle index must hold the IMMEDIATE alternative");  // static-only
    mask.push_back(m);
}
node = insertElement<InstStreamShuffle>(BB, "StreamShuffle-");   // IT 39
// re-emit mask into the shuffle-map container @ node+240 (40-byte stride MaybeAffine<uint> elems)
node.addArgument(src);   node.addOutput(dst);
sub_F20EC0(codegen+0x224, node);
return node;
```

`StreamShuffle` is a **partition** permutation within aligned 32-partition blocks
(`out_part[block+i] = in_part[block+mask[i]]`); the sim asserts a 32-entry mask, no dtype cast, each
entry `<32`. The mask must be static (the immediate-only assert) — that is the codegen's enforcement
of the "compile-time permutation" requirement. The `+240` / 40-byte-stride container is exactly the
`InstStreamShuffle::sameInst` structural-compare field.

---

## 11. Field-offset reference (the BIR node struct surface these leaves stamp)

| offset | field | who writes it | value(s) |
|---|---|---|---|
| `+0x90` (144) | engine-id | `NcCopy`, `NcDmaCopy` (HWDGE only), `DmaTranspose`, `TensorLoad/Store`, `Transpose` | `codegenEngine(…)` / Pool(1) / ALL(7) |
| `+0xF0` (240) | per-node scalar (`num_valid_indices` / `apply_transpose` / RangeSelect `mode`) | LocalGather, BNStats, RangeSelect | int / bool / `mode` |
| `+0xF8` (248) | `InstDMA::dge_type` | `NcDmaCopy`, `DmaTranspose` | `translateDGEMode` table `{3,1,2,0}` |
| `+0x128` (296) | `InstDMACopy::CopyMode` / `InstGenericIndirectSave::indirect_dims` | `DmaTranspose` / `GenericIndirectSave` | `1`=Transpose / index-dim vector |
| `+0x130` (304) | `InstDMACopy::oob_is_err` | `NcDmaCopy`, `DmaTranspose` | `klr.oob != 1` |
| `+0x140` (320) | `InstGenericIndirectSave::op` | `GenericIndirectSave` | `4`=add (else default → overwrite) |
| `+0x158` (344) | `InstDMACopy::TransposeOps` | `DmaTranspose` | `0x0B`=XZYW |
| `+0x160` (352) | OUTPUT `RegisterAccess` chain head | `TensorStore` | spliced `RegisterAccess` |
| `+0x178` (376) | `InstDMACopy` reduce/accumulate cmd | `NcDmaCopy` (compute_op=add) | `4` |
| `+0x192` (402) | INPUT `RegisterAccess` chain head | `TensorLoad` | spliced `RegisterAccess` |
| `+0x1B8` (440) | `InstMatmultBase::isTranspose` | `Transpose` (PE path) | `1` |
| `+0x1D8` (472) | `InstDMACopy` QoS / dynamic-AP holder | (default; dynamic path repurposes) | Unassigned / `DynamicAPINFO` |

The `+0xF8` / `+0x128` / `+0x158` offsets byte-coincide with the JSON wire keys
`dge_type` / `mode` / `transpose_op` — a codegen'd op and a JSON-loaded op are structurally identical
at those offsets. Every DMA / transpose / scatter / copy write in the table is byte-anchored; the register-chain heads at `+0x160`/`+0x192` are read from the splice sites rather than a direct store.

---

## 12. Downstream — how the family feeds the rest of the back-end

```
codegen<Op> (this page) -> bir::Inst (InstTensorCopy / InstDMACopy / InstGenericIndirectSave /
                            InstGather / InstIndirectCopy / InstRangeSelect / InstCopyPredicated /
                            InstBNStats / InstStreamShuffle / InstStreamTranspose / InstMatmult /
                            InstTensorLoad / InstTensorSave)
  -> H06  LegalizeStridedDMA (strided DMA -> SB-budget split) ;
          LowerGenericIndirect (IT 44 -> ReadVarAddr -> TSPtr -> Memset -> TSPtr -> IndirectSave 45/46)
  -> H33  assign_trigger_engine / assign_hwdge_engine / alloc_queues (fills engine-id +0x90, queues)
  -> H37  lower_dma / lower_dynamic_dma (InstDMACopy -> per-engine InstDMADescriptor{Copy,Transpose,
          Replicate} + InstDMATrigger; DGEType +0xF8 selects SW vs HW descriptor path)
  -> J-strand encoders (AccessPattern -> descriptor bytes; QoS via encodeISAOrRuntimeDMAQoS)
  -> sim (descriptor interval walk; writeAccumulate / cast_copy_or_accumulate for the add path;
          writeIndirect serial scatter for the indirect family; -1 sentinel skip)
```

The `DGEType` written here (§3.2, §5) decides SW (compiler-emitted descriptor) vs HW
(engine-generated) at `lower_dma`; the engine-id and QoS are filled by H33/J as noted. The indexed
ops (`Gather`/`IndirectCopy`/`RangeSelect`/`CopyPredicated`) emitted here are already *concrete*
(single resolved index axis), so they bypass the H06 generic-indirect expansion that only
`GenericIndirectSave`(44) needs.

---

## 13. Evidence anchors and limits

The byte anchors behind the page's structural claims, collected so the sections above can stay prose:

| Claim | Anchors |
|---|---|
| **Three distinct transposes** (§6) | `codegenTranspose` @`0xf28fb0`: PE-guard `cmp [rsi+28h],5` @`0xf28fd1`; `call getIdentityMatrix` @`0xf29098`; `call addMatmult` @`0xf292f9`; `insertElement<InstStreamTranspose>` @`0xf2947d`; `_validateLegalStreamTranspose` @`0xf29554`. `codegenDmaTranspose` @`0xf2dd10`: `mov [r12+128h],1` @`0xf2e0c6`, `mov [r12+158h],0Bh` @`0xf2e0d2`, `mov [r12+90h],1` @`0xf2e719` |
| **Gather operand binds** (§9) | `insertElement<InstGather>` @`0xf2a6d7`; `insertElement<InstIndirectCopy>` @`0xf2abda`; `insertElement<InstTensorCopyDynamicSrc>` @`0xf2d40b` |
| **BN stats / aggregate split** (§10.1) | `codegenBatchNormStats` @`0xf1add0` → `addBNStats` @`0xf1af6f`; `codegenBatchNormAggregate` @`0xf1b0d0` → `addBNAggr` @`0xf1b26f`; identical 7-arg `(MemoryLocation*, MemoryLocation*, SmallVector<APPair,4>, SmallVector<APPair,4>, uint, uint, source_location const&)` signature |
| **GenericIndirectSave reached only via the DMA fall-through** (§7) | `cmp [rsi+20h],1` / `cmp [rsi+3Ch],0` @`0xf2fa61`/`0xf2fa67`; `call codegenGenericIndirectSave` @`0xf30268`; `lea rdi,[r12+128h]` @`0xf2ec8d`; `mov [r12+140h],4` @`0xf2f240` |
| **DMA leaf node targets** (§§3–5, 8–9) | `addUnaryInstruction<InstDMACopy>` @`0xf309c9`; `addDmaCopyFma` @`0xf1e054`; `addTensorCopyCustom` @`0xf2d7d0`; `addRangeSelectReduce` @`0xf1c633`; `insertElement<InstCopyPredicated>` @`0xf271ef`; `insertElement<InstTensorLoad>` + `mov [r12+90h],7` @`0xf26b94` |

Three things on this page are **not** derived from the binary directly:

1. **The extern builder bodies.** `addBNStats`, `addBNAggr`, `addRangeSelectReduce`, `addDmaCopyFma`, `addTensorCopyCustom`, and `addMatmult` live in `libBIR.so`; their internal field stores are read off the wire schema, not disassembled. That is why the 6-vs-2 BN element/partition geometry and the two-`uint` BN argument split are flagged as inferred.
2. **The gather coefficient arithmetic.** These leaves bind the dynamic AP; the `IndirectDim` coefficient itself is resolved by the AP grammar ([7.23](codegen-dynamic-ap.md)) and the simulator, not re-derived here.
3. **`InstRangeSelect+0xF0`'s `int mode`.** The store is pinned, the semantic is not (§9.3).

Everything that determines *which BIR node a klr op becomes* and *which engine / DGEType / transpose-marker it carries* is read directly from the disassembly.

---

## 14. Cross-references

- [7.21 KlirToBirCodegen dispatch core & master routing table](klir-codegen-dispatch.md) — how each leaf is reached.
- [7.22 codegen AccessPattern primitive (`codegenAccess`)](codegen-accesspattern.md) — the AP grammar all leaves delegate to.
- [7.23 codegen Dynamic / Runtime AP (`assembleDynamicInfo` / `DynamicAPINFO`)](codegen-dynamic-ap.md) — where the gather axis and dynamic offsets live.
- [7.24 codegen Operand / Immediate / Register sub-encoders](codegen-sub-encoders.md) — `codegenImmediate` / `codegenAluOp` / `codegenAccumCmd` / `codegenRegister`.
- [6.5.12 BirCodeGenLoop DMA / Indirect Codegens](../nki/bircodegenloop.md) — the beta3 sibling of this DMA family.
- [InstructionType table](instruction-type.md) — IT 23/24/25/26/32/34/35/39/40/44/52/63/75/76/92.
- [BatchNorm-Family Encoding](../isa/batchnorm-encoding.md) — the ISA encoding of BNStats / Aggregate (the `apply_transpose` wire field).
- [TensorTensor / Copy / Cast / Select / Memset Encoding](../isa/tensortensor-encoding.md) — the ISA encoding of the copy/select/memset family.
- [DVE Datamove / Misc Encoding — Shuffle / Transpose / Gather / IndirectCopy / RangeSelect](../isa/dve-datamove-encoding.md) — the ISA encoding of the DVE datamove ops.
- [DMA-Family Encoding & Descriptors](../isa/dma-encoding.md) — the descriptor bytes the DMA family eventually emits.
