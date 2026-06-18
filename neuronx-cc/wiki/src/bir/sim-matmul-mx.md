# Simulator Matmul and MX Quantize / Dequantize

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 twins ship the same logic with every VA drifted). The kernels live in `neuronxcc/starfish/lib/libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`, 36 MB, 16821 functions). For `.text` (`0xf1ad0..0x58e798`) and `.rodata` (`0x58f000..`) the virtual address equals the file offset, so every disassembly site, string literal, and `rodata` table below is read by file offset; `.data` is at VA `0x2293560` / file offset `0x2292560` (delta `0x1000`) and no struct on this page is `.data`-resident. Treat every address as version-pinned.*

## Abstract

`libBIRSimulator.so` is the functional reference interpreter the backend uses to validate a lowered BIR program before it is encoded to silicon. It walks the instruction stream through `bir::IRVisitor<birsim::InstVisitor>::visit` `@0x26b380`, a 110-case opcode switch ([7.34 Simulator Dispatch and State](sim-dispatch-state.md)), and for each opcode tail-calls a `visitInst<X>` kernel. This page documents the matmul *family* and the OCP-MXFP microscaling ops: how the simulator models the PE systolic array as an Eigen `fp32` matrix product, how it applies the E8M0 per-block shared exponent on the dequant side, and how the standalone `QuantizeMx` op computes that shared exponent and rounds each element into the FP4/FP8 grid. The bar is that a reimplementer can rebuild the simulator's *numeric* model of these ops — not the cycle-accurate silicon timing, which the simulator does not attempt, but the bit-level value each op produces.

The central design decision is that the simulator is **dtype-agnostic at the MAC**: every operand — `bf16`, `fp16`, `fp8`, `fp4`, `uint8` — is widened to `float32` by `bir::CastToNewDType` before any multiply, and the dot product runs in `fp32` through `Eigen::Product<Transpose<Map<float>>, Map<float>>`. The "narrow" datatypes never participate in arithmetic directly; they are unpacked to `float` first, and the MX shared-exponent scale is folded in as an `ldexpf` power-of-two. This is why the simulator can model six PE datatypes with one Eigen kernel: all the format-specific behavior is pushed into the cast and the scale application, both of which are explicit, traceable steps. The companion [7.35 Simulator Core Arithmetic](sim-core-arithmetic.md) owns the shared cast/accumulate primitives (`cast_to` `@0x28db30`, the runAP `+=` path); this page cross-references them rather than re-deriving them, and focuses on what the matmul and MX kernels compute *on top of* that core.

The page is organized by opcode. After the shared sim-state and memory model, it covers the dense matmul (opcode 8) and its `fp32` MAC, the MX matmul (opcode 95) and its `dequantizeMx` E8M0 path, the standalone MX quantize (opcode 96) and its `generateMxScales` / round-to-grid path, the PSUM accumulate semantics (the Idle/Zero/Accumulate triad shared by every matmul), the structured-sparse matmul (opcode 9), and the activation-accumulator drain (opcode 5). It closes with the function map and the gaps.

For reimplementation, the contract is:

- The PE-array numeric model: operands widened to `fp32`, `out = ifmapᵀ · weights` via Eigen, tiled in PE row/column groups, with `uint8` zero-point subtraction and DoubleRow/DoublePixel `K`-doubling.
- The MX dequant: the E8M0 byte → `ldexpf(x, E8M0 − 127)` per 32-element (8 partition × 4 column) block, and the block-major scale-tensor interleave.
- The MX quantize: the shared exponent `E8M0 = max(1, max(0, maxBiasedExp(block) − EMAX))`, the `×2^(127−E8M0)` pre-scale, and the per-format RNE-with-saturate narrow (FP4 local, FP8 delegated to `libBIR`).
- The PSUM accumulate command: how `getCalcStart`/`getCalcAccu` decode into the zero-on-start + `+=`-on-body triad, and the per-function `key 19` override.

| | |
|---|---|
| **Subject binary** | `libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`) |
| **Dispatch parent** | `bir::IRVisitor<birsim::InstVisitor>::visit` `@0x26b380` ([7.34](sim-dispatch-state.md)) |
| **Dense matmul** | opcode 8 → `visitInstMatmult` `@0x27f670` → `doMatmult<InstMatmult>` `@0x289450` |
| **MX matmul** | opcode 95 → `visitInstMatmultMx` `@0x27f680` (full inlined body) |
| **MX quantize** | opcode 96 → `visitInstQuantizeMx` `@0x1f6920` (full inlined body) |
| **MX dequant core** | `dequantizeMx` `@0x27b040` — `ldexpf(x, E8M0 − 127)`, 8×4 block |
| **MX scale-gen** | `generateMxScales` `@0x29e1d0` + `findMaxExponentPerInputBlock` `@0x29e150` |
| **MAC primitive** | `Eigen::Product<Transpose<Map<float>>, Map<float>>` (`sub_27F260`) |
| **EMAX table** | `byte_5F7898` = `02 00 00 08 00 0f 08 0f` (per-format max exponent) |

---

## 1. Sim-State and the Memory Model

### Purpose

Every matmul/MX kernel reads its operands from, and writes its result into, a `birsim::Memory` backing store through a fixed set of `InstVisitor` fields. Understanding the kernels requires understanding the three things they touch: the operand access patterns, the `fp32`-widened `MemoryObject` buffers, and the PSUM accumulator state.

### The visitor fields

`birsim::InstVisitor` holds, at fixed offsets, the state the kernels read and write. [7.34](sim-dispatch-state.md) owns the full field map; the matmul-relevant subset:

| Field | Offset | Type / Meaning | Confidence |
|---|---|---|---|
| `Memory*` | `this+0x138` (`*((QWORD*)this+39)`) | the tensor backing store; all reads/writes route through its vtable | CERTAIN |
| AP factory | `this+0x130` (`*((QWORD*)this+38)`) | used by the MX path to mint physical access patterns | CERTAIN |
| accumulator buf | `this+760` (and `**(this+95)` / `**(this+110)`) | the activation/engine running-accumulator buffers (drained by opcode 5, §7) | CERTAIN |
| PSUM-written set | `this+0xE48` (3656) | `DenseSet<MemoryLocation*>` of PSUM locations written this step (accumulate book-keeping) | HIGH |

A tensor value at rest is a `birsim::MemoryObject`: a `{data ptr, refcount, rows, cols, Dtype@+56, isPSUM@+72, psumBankSize}` heap object wrapping a flat element buffer. The decisive operation every kernel performs first is `MemoryObject::cast_to(obj, Dtype=16)` `@0x28db30` = `bir::CastToNewDType` to **`float32`** — operands are widened before the MAC. PSUM is not a separate memory; it is a region inside the destination `MemoryObject` flagged `isPSUM@+72`, and "accumulate" is a property of the *write*, not a distinct store ([7.35](sim-core-arithmetic.md) owns `cast_to` and the write path).

> **NOTE —** the simulator carries its **own** copies of the dtype tables. `qword_5F74E0` (the per-dtype byte-stride LUT) reads `01 01 02 01 01 01 01 01 04 04 …` and is byte-identical to the encoder's `qword_1DFC040` in `libwalrus`. The two binaries agree element-for-element, which is what makes the sim a faithful twin of the encoder ([1.10 PE Matmul Encoding](../isa/pe-matmul-encoding.md)). A reimplementer must not assume the sim shares a table with the backend at runtime — it has a private copy.

### The PE-array model

Each matmul kernel builds `Eigen::Map<Matrix<float,-1,-1,RowMajor>>` views over the `fp32` `MemoryObject` buffers and computes the dot product via `Eigen::Product<Transpose<Map>, Map>` (`sub_27F260` is the product-evaluate helper). The asserts name the operation verbatim — `lhs.cols() == rhs.rows() && "invalid matrix product"` (confirmed in the binary's string table). So the PE array is modeled as:

```text
out = ifmapᵀ · weights        (fp32 accumulation)
      stationary = weights (loaded into the array)
      moving     = ifmap   (streamed through)
```

This is the systolic MAC reduced to its arithmetic essence: the simulator does not model PE cells, fill/drain latency, or the wavefront — it computes the algebraic result the array would produce. Timing belongs to the perf simulator, not this functional one.

---

## 2. Dense Matmul (opcode 8)

### Purpose

`visitInstMatmult` evaluates the dense PE matmul: a `fp32` dot product of two operands, with optional `uint8` zero-point subtraction (integer quantized matmul), optional DoubleRow/DoublePixel `K`-doubling, and a PSUM accumulate command. It is the template all other matmul kernels follow.

### Entry Point

```text
visitInstMatmult @0x27f670          ── thunk
  └─ doMatmult<InstMatmult> @0x289450        ── control + accumulate decision (§2 + §5)
       └─ matmultImpl<InstMatmult> @0x2883a0  ── the fp32 MAC (§2 Algorithm)
       └─ transposeImpl<InstMatmult> @0x284020 ── CoreV4 transpose-stationary variant [HIGH]
       └─ Memory::writeAccumulate (vtable+88)  ── PSUM writeback (§5)
```

> **NOTE —** the two-copy artifact: `visitInstMatmult @0x27f670` is the high-address `.`-prefixed PLT thunk; the real body of `doMatmult<InstMatmult>` is `@0x289450`, and `matmultImpl` has a thunk `@0xf1810` plus the real body `@0x2883a0`. Always cross-check both VA frames before downgrading an address to "missing."

### Algorithm — control and the dtype gate

```c
function doMatmult_InstMatmult(I):              // sub_289450
    ifmapAP   = getOrCreatePhysicalAP(getIfmap(I))     // operand 0
    weightsAP = getOrCreatePhysicalAP(getWeights(I))   // operand 1
    // dtype gate — only float* or uint8 may enter the PE matmul:
    assert isFloatDtype(ifmapAP.type)   || ifmapAP.type   == Dtype::uint8   // "Invalid ifmap data type for matmult"
    assert isFloatDtype(weightsAP.type) || weightsAP.type == Dtype::uint8   // "Invalid weights data type for matmult"
    ifmapObj   = Memory.getMemObj(ifmapAP)             // vtable+24 → read as MemoryObject
    weightsObj = Memory.getMemObj(weightsAP)
    if weightsAP.isTensorIndirectDynamicAP():          // gather-weights path
        resolve indirect actual_ap (asDtypeMatrix, sub_27CD30)
    dstAP  = getInOutPhysicalAP(I, 0, /*isOutput=*/1)  // the PSUM destination
    result = MemoryObject()                            // accumulator scratch
    // CoreV4 transpose load (weights-stationary TRANSPOSE) iff target>20 AND I+440 set:
    if module.target > 20 && byte(I+440):
        transposeImpl_InstMatmult(...)                 // out = ifmap · weightsᵀ  [HIGH]
    else:
        matmultImpl_InstMatmult(...)                   // the dot product (below)
    // decode the PSUM accumulate command → writeAccumulate (§5):
    writeback(dstAP, result, I)
```

### Algorithm — the MAC

```c
function matmultImpl_InstMatmult(out, ifmapObj, weightsObj, ifmapAP, weightsAP, I):  // sub_2883a0
    cast_to(ifmapObj,   16)                  // widen ifmap   → fp32  (sim-core-arithmetic 7.35)
    cast_to(weightsObj, 16)                  // widen weights → fp32
    if ifmapAP.type == 17 /*float32r*/:      // fp32r reduced-precision operands
        assert weightsAP.type == float32r    // (mat_mult.cpp:0xC3)
        sub_2A4750(ifmap);  sub_2A4750(weights)   // fp32r round/truncate
    Ki = getNumElementsPerPartition(ifmapAP)        // contraction extent
    Pi = ifmapAP.Pattern[0].num                     // partition count
    Kw = getNumElementsPerPartition(weightsAP);  Pw = weightsAP.Pattern[0].num
    // ---- perf_mode: DoubleRow (4) / DoublePixel (1) double the K/partition ----
    perf = int(I + 720)                       // PE perf-mode enum @ I+0x2D0
    if perf == 4:  deinterleaveMatrices(weights, Pw, Kw, /*factor=*/2)   // de-pack 2 cols
    if perf == 4 || perf == 1:  Ki *= 2;  Pi *= 2     // silicon packs 2 logical rows/pixels per PE cycle
    // ---- per-operand uint8 zero-point subtraction (integer-matmul offset) ----
    if valid(I+800):  zpW = float(uint8(I+768));  for w in weights: w -= zpW   // SSE _mm_sub_ps
    if valid(I+760):  zpI = float(uint8(I+728));  for x in ifmap:   x -= zpI
    // ---- the dot product, tiled over PE row/col groups ----
    rowTile = int(I+400)   // num_row_groups   colTile = int(I+360)   // num_col_groups
    for rg in 0 .. Pi/rowTile:
        for cg in 0 .. rowTile/colTile:
            // Eigen TensorExecutor: out[rg,cg] = ifmapᵀ_slice · weights_slice
            run(TensorAssignOp(out_slice, ifmapᵀ_slice · weights_slice))
    assert lhs.cols() == rhs.rows()   // "invalid matrix product" — K must agree
```

The resulting numeric model of a non-MX matmul is:

```text
out[p_out, n] = Σ_k (ifmap[k, n] − zpI) · (weights[k, p_out] − zpW)     in fp32
```

with `k` over `Ki` (doubled for DoubleRow/Pixel), tiled in PE row/column groups, written to PSUM with the §5 accumulate command.

> **QUIRK —** DoublePixel (`perf==1`) doubles `Ki`/`Pi` *without* a de-interleave; DoubleRow (`perf==4`) first calls `deinterleaveMatrices(..., factor=2)` to un-pack the 2-column packing, *then* doubles. A reimplementation that doubles `K` for both but de-interleaves only one will produce a transposed-garbage product on the DoubleRow path. The factor is the silicon's per-cycle row/pixel packing; the sim un-packs it so the `fp32` product matches the array's effective contraction.

### Struct field offsets

`InstMatmult*` in-memory fields the kernels read directly:

| Offset | Field | Confidence |
|---|---|---|
| `I+440` | CoreV4 transpose flag | CERTAIN |
| `I+720` (`0x2D0`) | `perf_mode` enum (4 = DoubleRow, 1 = DoublePixel) | CERTAIN |
| `I+728` / `I+760` | ifmap zero-point (`u8`) / its variant validity guard | CERTAIN |
| `I+768` / `I+800` | weights zero-point (`u8`) / its variant validity guard | CERTAIN |
| `I+360` / `I+400` | `num_col_groups` / `num_row_groups` (PE tiling) | CERTAIN |
| `I+280` | `pow2 → psumZeroBankNums` (# PSUM banks the start clears, §5) | CERTAIN |

`getCalcStart`/`getCalcAccu` are `bir::InstMatmultBase` getters (PLT thunks into `libBIR`, not raw fields here).

---

## 3. MX Matmul (opcode 95)

### Purpose

`visitInstMatmultMx` evaluates the OCP-MXFP scaled matmul: the same `fp32` MAC as §2, but each operand is an FP4/FP8 mantissa stream paired with an E8M0 per-block shared-exponent tensor. The kernel unpacks the x4 lane container, casts the mantissas to `fp32`, applies the E8M0 scale via `dequantizeMx` (§4), de-interleaves, and runs the Eigen product. This is the functional twin of the `LdWeightMx`(`0x1009`) + `MatmulMx`(`0x100A`) bundle the CoreV4 encoder emits ([1.10 PE Matmul Encoding](../isa/pe-matmul-encoding.md)).

### Entry Point

`visitInstMatmultMx @0x27f680` is the **full inlined body**, not a thin thunk (`0xedXXX` is the `.`-prefixed PLT copy). It takes four operands, exactly the four of the HLO `ScaledMatmul`: ifmap data, weights data, ifmap scales (E8M0), weights scales (E8M0).

### Algorithm

```c
function visitInstMatmultMx(I):                 // sub_27f680
    ifmapAP        = getOrCreatePhysicalAP(I.getIfmap())
    weightsAP      = getOrCreatePhysicalAP(I.getWeights())
    ifmapScaleAP   = getOrCreatePhysicalAP(I.getIfmapScales())     // E8M0 tensor
    weightsScaleAP = getOrCreatePhysicalAP(I.getWeightsScales())   // E8M0 tensor
    (ifmapObj, weightsObj, ifmapScaleObj, weightsScaleObj) = Memory.getMemObj(each)
    if weightsAP.isTensorIndirectDynamicAP():  resolve gather (asDtypeMatrix, sub_27CD30)
    // ---- x4 UNPACK extent (the FP4/FP8-x4 lane container) ----
    Pi = ifmapAP.Pattern[0].num >> 3            // partitions / 8
    Ki = getNumElementsPerPartition(ifmapAP)
    if ifmapAP.type == 2 /*fp4_e2m1_x4*/ || (ifmapAP.type - 8) <= 1 /*8=fp8e4m3_x4, 9=fp8e5m2_x4*/:
        Ki *= 4                                  // x4: 4 logical elems per container
    // (same for weights: Kw *= 4 iff dtype ∈ {2,8,9})
    // ---- cast packed data → fp32, reorder E8M0 scales to block-major ----
    ifmapScales   = new u8[(Ki>>2) * Pi];   copyScalesFromArray<u8>(ifmapScaleObj, …)   // @0x29e5e0
    weightsScales = new u8[(Kw>>2) * Pw];   copyScalesFromArray<u8>(weightsScaleObj, …)
    cast_to(ifmapObj, 16);  cast_to(weightsObj, 16)   // unpack FP4/FP8 mantissa → fp32
    // ---- APPLY E8M0 PER-BLOCK SCALE (the silicon dequant, §4) ----
    ifmapF   = dequantizeMx(Pi*8, Ki, ifmapObj.data,   ifmapScales)     // sub_27b040
    weightsF = dequantizeMx(Pw*8, Kw, weightsObj.data, weightsScales)
    // ---- de-interleave with factor 4 (MX is always x4) ----
    deinterleaveMatrices(ifmapF,   rows, Ki, /*factor=*/4)
    deinterleaveMatrices(weightsF, rows, Kw, /*factor=*/4)
    // ---- the dot product (Eigen, fp32) ----
    assert lhs.cols() == rhs.rows()    // "invalid matrix product"
    result = ifmapFᵀ · weightsF        // sub_27F260
    // ---- writeback identical to §2/§5 ----
    writeback(getInOutPhysicalAP(I,0,1), result, I)   // getCalcStart/getCalcAccu → writeAccumulate
```

The MX matmul numeric model is therefore:

```text
out[p,n] = Σ_k  ldexpf(ifmap_fp[k,n], Si[block(p,k)] − 127)
              · ldexpf(weight_fp[k,p], Sw[block(p,k)] − 127)        in fp32
```

where `ifmap_fp`/`weight_fp` are the FP4/FP8 mantissa values cast to `float`, `Si`/`Sw` are the E8M0 per-block shared exponents, and `block(p,k)` groups 8 partitions × 4 elements = the 32-element OCP-MXFP block (§4).

> **QUIRK —** the x4-ness lives in the *dtype*, not in a scalar field. `Ki *= 4` fires only for dtypes `{2, 8, 9}`; `Pi` is pre-divided by 8 (`>> 3`) because the MX partition dimension is the block dimension. The same `{2,8,9}` test drives `NumElem×4` in the encoder ([1.10](../isa/pe-matmul-encoding.md)) and the data layout in [1.05 MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md). A reimplementer keying off a config flag instead of the dtype will mis-size every MX operand.

---

## 4. The MX Dequant Core — `dequantizeMx`

### Purpose

`dequantizeMx` applies one E8M0 byte per 32-element block as a power-of-two scale. It is the single function that turns "FP4/FP8 mantissa + shared exponent" into the `fp32` value the MAC consumes, and its index arithmetic *defines* the block geometry the rest of the MX pipeline obeys.

### Algorithm

```c
function dequantizeMx(rows, K, float* dataF, u8* scale):     // sub_27b040
    out = new float[rows * K]
    for r in 0 .. rows:
        for c in 0 .. K:
            out[r*K + c] = ldexpf( dataF[r*K + c],  scale[ (K/4)*(r/8) + (c/4) ] − 127 )
    return out
```

### Decode

The disassembly at `0x27b040` confirms every constant: `shr $0x3` (`r/8`), `shr $0x2` (`c/4`) to form the scale index, `sub $0x7f,%edi` (subtract 127), then `call ldexpf@plt` `@0x27b0fd`. From this:

- **Block geometry.** The scale index `(K/4)·(r/8) + (c/4)` means one E8M0 byte is shared by every (8 partitions) × (4 columns) tile = **32 elements** — the OCP-MXFP `block_size` (the HLO legalize pass enforces "block_size must be 32"; the dtype tables call 32 "8 part × 4 elem"). [CERTAIN]
- **Bias removal.** `scale − 127` removes the `float8_e8m0fnu` bias (E8M0 bias = 127); `ldexpf(x, e) = x · 2^e`, so the shared block exponent is applied as a power-of-two scale. This is bit-exact OCP MX dequant: `value = 2^(E8M0 − 127) · mantissa`. [CERTAIN]
- **Mantissa is pre-unpacked.** The FP4/FP8 mantissa is already widened to `fp32` by `cast_to(…, 16)` *before* `dequantizeMx`; the x4 lane container (dtypes 2/8/9) was expanded by the `Ki *= 4` extent and `deinterleaveMatrices(factor=4)`. `dequantizeMx` sees a contiguous `fp32` grid. [CERTAIN]

### The scale-tensor interleave

`copyScalesFromArray<u8>` `@0x29e5e0` re-orders the raw scale-AP bytes into block-major layout (`idx → (i & 3) + 32·(i >> 2)`) so `dequantizeMx` sees a contiguous scale grid. This is the exact inverse of the write-side interleave the quantize op applies (§6.1), making the quantize→dequant round-trip index-exact.

---

## 5. PSUM Accumulate Semantics — Idle / Zero / Accumulate

### Purpose

Every matmul kernel ends with `Memory.writeAccumulate(dstAP, result, startReset, I, accInto)` (Memory vtable slot +88; `PhysicalMemory` override `@0x1760f0`). The two booleans handed in are the decoded `EngineAccumulationType` — they implement the silicon's group-head/group-body/idle PSUM accumulation. [7.35](sim-core-arithmetic.md) owns the underlying write/`+=` mechanics; this section documents the *decision*.

### Decoding the command

```c
// at the end of doMatmult / visitInstMatmultMx:
CalcStart = I.getCalcStart()              // bir::InstMatmultBase getter (PLT → libBIR)
if !CalcStart:  CalcStart = I.getCalcAccu()   // start OR accumulate ⇒ "engaged"
startOnly = I.getCalcStart()
// per-FUNCTION override: look up attribute key 19 ("auto psum accumulate") in
// Function.attrs (the 0x13-bucket walk); if present (a bool variant via ToBoolVisitor)
// it FORCES the accumulate-into vs reset decision; else use {CalcStart, startOnly^1}.
(startReset, accInto) = key19_present ? key19_override : {CalcStart, startOnly ^ 1}
Memory.writeAccumulate(dstAP, result, startReset, I, accInto)
```

The triad the two booleans encode:

| Command | `getCalcStart` | `getCalcAccu` | `startReset` (a4) | `accInto` (a6) | Effect |
|---|---|---|---|---|---|
| Group head | 1 | * | 1 | 0 | zero PSUM, then write product |
| Group body | 0 | 1 | 0 | 1 | skip reset, PSUM `+=` product |
| Idle | 0 | 0 | 0 | 0 | plain overwrite (no reset, normal write) |

### The reset path

```c
function PhysicalMemory_writeAccumulate(dstAP, result, startReset, I, accInto):  // sub_1760f0
    if startReset:                            // = getCalcStart
        loc = cast<MemoryLocation>(dstAP.location)
        if loc.isAllocated():
            switch loc.memType:               // matmul start must target PSUM
                case 16 (SB):   assert !loc.isSB()    // "!curMemLoc.isSB()"
                case 8  (DRAM): assert !loc.isDRAM()  // "!curMemLoc.isDRAM()"
                case != 32:     assert false
            // gate: if state==10 consult key19 (ToBoolVisitor) else proceed:
            resetAccumulation(psumObj, dstAP, I, /*usePerBank=*/state>39, /*fill=*/accInto)
        else:
            createIfNotExist(dstAP)           // fresh DenseMap entry
        MemoryObject::resetAccumulation(psumObj)   // no-arg reset on the object
    return Memory::write(dstAP, result, I, /*accMode=*/0)   // vtable+72 → writeLocal
```

```c
function MemoryObject_resetAccumulation(I, dstAP):     // sub_2931f0  (the ZERO op)
    assert CurMemLoc.isPSUM() && isAllocated() && this.isPSUM()  // memory_object.cpp:0x323
    assert isa<InstMatmultBase>(I)            // op==95 || (op-7) <= 2
    psumZeroBankNums = pow(2, u8(I+280))      // # PSUM banks to clear, from I+280
    if psumZeroBankNums != 1:
        assert arch_supports_multibank        // " do not support psum multibank"
    basePart = I.basePartition > 0 ? I.basePartition : dstAP.getBasePartitionInUnderlyingMemory()
    for part in 0 .. numPartitions:
        dst = psumBase + partStride*part + offset
        assert StartAddr < num_bytes() && StartAddr + overwrittenBytes <= num_bytes()  // :0x34E
        memset(dst, /*byte=*/accInto /*normally 0*/, partBytes)
```

So `resetAccumulation` zeroes the PSUM region with a per-partition `memset` — the "Zero" command; with `accInto = 0` this is an exact accumulator clear. The `+=` ("Accumulate") path lives in `runAP` `@0x298390`: at line 75 `v41 = isa<InstMatmultBase>(I)`, and at line 78 `if (write && this.isPSUM()) v41 = isPSUM` selects the accumulate-into branch — when the writing instruction is a matmul **and** the destination is PSUM, `runAP` does `dst[i] += src[i]` (fp32 add) instead of overwrite ([7.35](sim-core-arithmetic.md) details this). A NaN guard (`canInstReadFromUninitMem(I) || !result.hasNaN()`) protects against uninitialised-PSUM reads.

> **QUIRK —** `I+280` is `log2` of the PSUM bank count the start clears, not the count itself — `psumZeroBankNums = pow(2, byte(I+280))`. This is the multi-bank (DoublePixel) accumulation width: a start matmul can clear 1, 2, 4, … banks in one shot. A reimplementer reading `I+280` as a literal bank count will clear `2^N` banks instead of `N`.

> **NOTE —** the per-function attribute `key 19` ("auto psum accumulate") can override the start/accumulate split at run-time (`ToBoolVisitor` over a `boost::variant`). The pass that *authors* key 19 is a `libwalrus`/HLO concern, not the simulator; the sim only *reads* it. The exact enum→bool decode of `getCalcStart`/`getCalcAccu` lives in `libBIR` (PLT thunks `@0x22b0a80` / `@0x22b1618`), so the bit-level mapping is taken on faith from the "Idle/Zero/Accumulate" model. [GAP]

---

## 6. MX Quantize (opcode 96)

### Purpose

`visitInstQuantizeMx` is the standalone inverse of §4: it turns a BF16/F16 tensor into a 2-tuple `{x4-packed FP4/FP8 data, E8M0 per-block scale}`. It computes the shared exponent that §4 later applies, pre-scales each element down by that exponent, and RNE-narrows the result into the destination FP4/FP8 grid with saturation. This is the simulator twin of the CoreV4 `S3DMX1_QUANT` encoder (wire opcode `0xE3`) and the HLO `QuantizeMX_*` custom-call.

### Entry Point and operands

`visitInstQuantizeMx @0x1f6920` is the full inlined body (`0xed230` is the PLT copy). Three BIR operands — 1 input, 2 outputs — exactly the HLO 2-tuple:

```c
v60 = getInOutPhysicalAP(I, 0, 0)   // INPUT   : BF16/F16 source tensor
v62 = getInOutPhysicalAP(I, 0, 1)   // OUTPUT 0: quantized DATA (FP4/FP8 x4)
v66 = getInOutPhysicalAP(I, 1, 1)   // OUTPUT 1: E8M0 SCALE tensor (uint8)
```

### Algorithm — the pipeline

```c
function visitInstQuantizeMx(I):                // sub_1f6920
    NEPP_out = getNumElementsPerPartition(v62)  // data-out free extent
    if v62.type == 2 || (v62.type - 8) <= 1:  NEPP_out *= 4    // x4 dtypes {2,8,9}
    cast_to(inputMemObj, 16)                    // widen BF16/F16 → fp32
    copyArrayTo2DVector<float>(input → v69)     // partition-major 2D
    doQuantizeMx(v69, v71 /*out data*/, v73 /*out scale*/, v62.type)    // §6.1 + §6.2
    copy2DVectorToArray<float>(v71 → fp32 data buffer)
    copy2DScaleVectorToArray<uchar>(v73 → E8M0 buffer)   // block-major interleave (§6.1)
    // stamp FP8ConversionConfig on the data MemoryObject:
    cfg.byte[0] = 1                             // SATURATE ON, unconditional (@0x1f6ff4)
    cfg.byte[1] = (FP8ConvConfig[1] from GOT)   // nan-suppress; if 0 forced to 1
    Memory::write(v62, dataMemObj, I, 0)        // vtable+72 → the fp32→FP4/FP8 NARROW (§6.2)
    Memory::write(v66, scaleMemObj, I, 0)       // write E8M0 scale tensor
```

### Algorithm — `doQuantizeMx` (scale generation + apply)

```c
function doQuantizeMx(in2D, outData2D, scale2D, Dtype dt):    // sub_29e380
    v6 = (dt - 2) <= 7 ? byte_5F7898[dt - 2] : 0              // EMAX = per-format max-exp
    emaxBias = uint8( (MxAlternativeEmaxEn == 0) + v6 - 1 )   // default: emaxBias = v6
    generateMxScales(in2D, scale2D, emaxBias)                 // §6.1
    return quantizeDataApplyingMxScales(in2D, outData2D, scale2D)   // §6.1
```

`byte_5F7898 @0x5F7898` = `02 00 00 08 00 0f 08 0f` (xxd-verified) is the OCP per-format max-normal exponent, indexed `dt − 2`:

| Dtype | Format | EMAX | Max-normal magnitude |
|---|---|---|---|
| 2 | `float4_e2m1fn_x4` | 2 | 6.0 |
| 5 | `float8_e4m3fn` | 8 | 448 |
| 7 | `float8e5` (e5m2) | 15 | 57344 |
| 8 | `float8_e4m3fn_x4` | 8 | 448 |
| 9 | `float8_e5m2_x4` | 15 | 57344 |

(Dtypes 3/4/6 read EMAX 0 — legacy/scale-type, not OCP MX targets.) `MxAlternativeEmaxEn` (a `libBIR`-imported global) drops EMAX by 1 when set; default mode (0) uses raw EMAX.

#### 6.1 The shared-exponent computation

```c
function findMaxExponentPerInputBlock(a1, rowGroup, colBlock):   // sub_29e150 → biased fp32 exp (u8)
    for partRow in 0 .. 8:                          // 8 partition-rows (stride 24 = sizeof vector<float>)
        for col in 4*colBlock .. 4*colBlock + 4:    // 4 columns
            bits = *(DWORD*)(row_data + 4*col)       // fp32 bit-pattern
            expByte[partRow*4 + (col & 3)] = bits >> 23   // biased exponent byte (shr edx,17h; mov dl)
    return max over the 32 expBytes                 // cmovb max-reduce
```

```c
function generateMxScales(in2D, scale2D, u8 emaxBias):   // sub_29e1d0
    nColBlk = (cols >> 2);  nRowGrp = (rows >> 3)        // cols/4 × rows/8 blocks
    for rowGroup in 0 .. nRowGrp:
        for colBlock in 0 .. nColBlk:
            v8 = u8(findMaxExponentPerInputBlock(in2D, rowGroup, colBlock)) - emaxBias
            if v8 < 0:  v8 = 0                  // CLAMP-LOW  → never a negative shared exponent
            scaleByte = u8(v8)
            if scaleByte == 0:  scaleByte = 1   // ZERO-FLOOR → all-zero/tiny block guard
            scale2D[rowGroup][colBlock] = scaleByte
```

The shared exponent per 8×4 block is `E8M0 = max(1, max(0, maxBiasedExp(block) − emaxBias))`, always in `[1, 255]`. `findMaxExponentPerInputBlock` is a `fp32` biased-exponent **bit-extract** (`bits >> 23`, byte-truncated so the sign in bit 31 is dropped — magnitude-exponent, sign-independent), **not** `ilogbf`; a denormal `fp32` reads exponent byte 0. The block is the same 8 partition × 4 column = 32 elements as `dequantizeMx`. [CERTAIN — disassembly `shr edx,0x17; mov [rcx+rsi-0x20],dl; cmovb`]

> **GOTCHA —** the two clamps compose in order: the signed `maxExp − emaxBias` is first floored to 0, *then* a resulting 0 byte is bumped to 1. The zero-floor is the all-zero / sub-tiny-block guard — a fully-zero block would otherwise carry E8M0 byte 0 (= `2^-127`); forcing it to 1 (= `2^-126`) keeps the stored scale a valid non-zero shared exponent and stops dequant from ever multiplying by exactly `2^-127`. Skip either clamp and the round-trip diverges on edge blocks.

#### 6.2 The pre-scale and narrow

```c
function quantizeDataApplyingMxScales(in2D, out2D, scale2D):   // sub_29e2a0
    for row in 0 .. rows:
        for col in 0 .. cols:
            E8M0 = scale2D[row >> 3][col >> 2]          // same 8×4 block index as §6.1
            out2D[row][col] = float_bits((254 - E8M0) << 23) * in2D[row][col]
```

`float_bits((254 − E8M0) << 23) = 2^((254 − E8M0) − 127) = 2^(127 − E8M0)`, so `out = in · 2^(127 − E8M0) = in / 2^(E8M0 − 127)` — the **exact inverse** of `dequantizeMx`'s `ldexpf(x, E8M0 − 127)` (§4). The pre-scale maps each element into the format's normal range before the narrow. The disassembly confirms `mov $0xfe,%ebp` (254), `sub %r14d,%edx; shl $0x17,%edx` (`(254−E8M0)<<23`), `mulss`. The 254 (not 255) is because the multiplier is built directly as an `fp32` exponent field with bias 127; the §6.1 zero-floor keeps `(254 − E8M0) ≤ 253`, so the multiplier is always a finite normal `fp32` (never Inf). [CERTAIN]

#### 6.3 Round-to-grid per format

The narrow itself splits by format. FP4 is local; FP8 is delegated to `libBIR`:

| Dtype | Format | Narrow | Round | Saturate |
|---|---|---|---|---|
| 2 | `float4_e2m1fn` | LOCAL `sub_2A3530` (§ below) | RNE | to 6.0 (`or $0x7,%eax`) |
| 5 / 8 | `float8_e4m3fn` | `libBIR` `CastToNewDType` → `sub_4B1F40` | RNE | to 448 (`0x7E`) when `sat=1` |
| 7 / 9 | `float8_e5m2` | `libBIR` `CastToNewDType` → `sub_4B2160` | RNE | to 57344 (`0x7B`) when `sat=1` |

The FP4 e2m1 grid is `{0, ±0.5, ±1, ±1.5, ±2, ±3, ±4, ±6}` (decoded from `sub_2A3F30`: `e2m1 = {1 sign, 2 exp, 1 mantissa, bias 1}`, `fp32_exp = e2m1_exp + 126`; `±0.5` is the subnormal `0x3F000000`). The local encoder `sub_2A3530` (disassembly-verified):

- **NaN** (`ucomiss xmm0,xmm0; jp` `@0x2a3547`) → signed zero nibble (FP4 has no NaN).
- **Overflow** (`lea -0x7f(%rax),%ecx; cmp $0x2,%ecx; jg` `@0x2a355a` — unbiased exp > 2, i.e. `|x| ≥ 8`) → `or $0x7,%eax` `@0x2a3632` = saturate to nibble `0x7` = `sign|6.0`. No Inf.
- **RNE** round-bias `0x400000` (½ ULP at FP4's 1 mantissa bit) gated by guard bit 21 with round-to-even (`@0x2a3594..`). Hard RNE — no rounding-mode field.

The simulator has **no** local canonical-FP8 narrow; `Memory::write(v62)` → `cast_and_reshape` `@0x28f1f0` → `cast_to` `@0x28db30` → `bir::CastToNewDType` (PLT thunk `@0xed930` → `@0x22b1230`, a `libBIR` import) does the e4m3fn/e5m2 round/saturate, driven by the `FP8ConvConfig` stamped above (`saturate byte[0] = 1`). The canonical constants (448 = `0x7E`, 57344 = `0x7B`, RNE bias) live in the imported `libBIR` (`float_converter.cpp`, the same source path baked into this binary), so the delegation is CERTAIN and the FP8 constants are HIGH for this binary. The §6.1 EMAX (8/15) bias is what guarantees the pre-scaled block max (§6.2) lands ≤ these maxima, so the narrow rarely saturates.

#### 6.4 The x4 pack and scale interleave

The E8M0 scale tensor is written `[partitions/8, cols/4]` with a block-major reorder: `copy2DScaleVectorToArray<uchar>` `@0x29e570` sends scale-row `i` to output slot `(i & 3) + 32·(i >> 2)` — the exact index `copyScalesFromArray` (§4) reads back, making the layout symmetric. The "32" stride is the 8-part × 4-col block granularity in scale space; the 4-bit `(i & 3)` group is the MXMEM_PATTERN1D scale-stream selector ([1.05 MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md)).

FP4 packs two nibbles per byte with an intra-x4 lane permutation: `cast_fp32_to_float4e2m1fn_x4` `@0x2a72c0` (asserts `count % 4 == 0`) calls `sub_2A3530` to encode then `sub_2A6A80` to pack, and the latter applies `sub_2A3500` — a deinterleave-by-`2·stride` that reverses the linear index within each `2·stride` window and pairs even/odd offset-by-2. So the four logical FP4 sub-elements of an x4 container are placed in a stride-dependent permuted order, **not** plain little-endian nibble order. The permutation formula is CERTAIN; the concrete `stride`/`parity` (from the AP via `sub_2A58A0`) is HIGH — they are read at role level but not pinned to a NEFF byte fixture. FP8-x4 (dtypes 8/9) carries the x4-ness in the AP (`num×4`, `u32` container stride 4), not a nibble pack — FP8 bytes are 1-per-element contiguous.

---

## 7. Structured-Sparse Matmul (opcode 9) and Accumulator Drain (opcode 5)

### `visitInstMatmultSparse @0x1f0a10`

The structured-sparse matmul takes three input operands plus the output: ifmap (cast to `fp32`), weights (cast to `fp32`), and a sparse index/metadata tensor (integer-cast to `int16`). Hard constraints, verbatim from `inst_visitor.cpp`:

```text
assert numActiveRows % 4 == 0    "Error: num_active_rows should be divisible by 4."
assert numActiveRows == 128      "Only the 128-active-rows case is supported!"
```

The kernel loops **32** sub-tiles, each advancing the operand APs by `NumElementsPerPartition`; per tile it forces `Pattern[0].step = 4` and `Pattern[0].num = 32 · NumElementsPerPartition` (the dense `K` is 32× the active `K`). For each operand it builds an `std::map<int,int>` of `{active element index → packed position}` via `bir::linearizedElementIndices()`, then the inner MAC walks an access-pattern descriptor (`v48[0..9]`, multi-dim strides/nums = the sparse-mask geometry) and accumulates only the unmasked positions: `acc = weightsF[… + j·Kw] · ifmapF[map_position] + acc` (`fp32 +=`). The `int16` index tensor selects which of the 32× compressed rows are active — the 128-active-rows / `%4` / ×32 numbers are the 2:4-style structured-sparsity geometry the PE engine decodes. Writeback is identical to §5. [CERTAIN]

### `visitInstReadActivationAccumulator @0x1d2ae0`

This drains the PE/activation-engine accumulator register into a tensor output — the explicit "read accumulator" instruction:

```c
function visitInstReadActivationAccumulator(I):    // sub_1d2ae0
    dstAP = getInOutPhysicalAP(I, 0, 1)
    acc   = MemoryObject(dstAP.getShape(), fp32)
    memcpy(acc.data, **(this+760), acc.byteCount)  // copy the live accumulator buffer
    Memory.write(dstAP, acc, I, /*accMode=*/0)      // vtable+72 = plain write (NOT +=)
    byte(this+872) = 1                              // mark accumulator-read / pending-reset
```

It reads the simulator's running activation accumulator (`this+760`, filled by `doEngineAccumReduce @0x1f9900`) and writes it out plain (overwrite), then flags the accumulator consumed. `doEngineAccumReduce`'s `EngineAccumulationType` codes are `1 = Zero/Idle` (`memset(acc,0)`), `3`/`5 = LoadAccumulate` ("an instruction that does a LoadAccumulate must provide an immediate value to load"); the accumulator output must be `fp32` ("OutRes.getType() == Dtype::float32"), and the reduce op is a `std::function<float(float,float)>` looked up from a map keyed by the ALU op. [CERTAIN visitor / HIGH filler]

---

## 8. Encoder / ISA Agreement

The simulator computes exactly what the CoreV4 encoder emits. The invariants line up element-for-element:

| Invariant | Encoder ([1.10](../isa/pe-matmul-encoding.md)) | Simulator (this page) |
|---|---|---|
| x4 unpack dtypes | `NumElem×4` iff dtype ∈ {2,8,9} | `Ki *= 4` iff dtype ∈ {2,8,9} (§3) |
| dtype byte-stride | `qword_1DFC040` = `1 1 2 1 1 1 1 1 4 4 …` | `qword_5F74E0` = same (byte-identical) |
| E8M0 scale apply | E8M0 per-block, tag 3 = uint8 | `ldexpf(data, E8M0 − 127)`, bias 127 (§4) |
| block geometry | 32 = 8 part × 4 elem | scale idx `(K/4)·(r/8) + (c/4)` (§4) |
| scale_method | `scale_method = EMAX` | `byte_5F7898` per-format max-exp (§6) |
| MatmultMx bundle | `LdWeightMx 0x1009` + `MatmulMx 0x100A` | dequantize + deinterleave + `ifmapᵀ·w` (§3) |
| accumulate flag | PSUM AccumulateFlag | `getCalcStart`/`getCalcAccu` → `writeAccumulate` (§5) |

The quantize round-trip is internally consistent: `quantizeDataApplyingMxScales` multiplies by `2^(127−E8M0)` (§6.2), the exact inverse of `dequantizeMx`'s `ldexpf(x, E8M0−127)` (§4), at the identical `(r/8, c/4)` block index — so a tensor quantized by opcode 96 and consumed by opcode 95 reconstructs to within the FP4/FP8 grid resolution. The full HLO → NKI → MLIR → BIR → sim contract and the OCP-MXFP legality rules are owned by the numerics chapter ([9.8 MX Microscaling](../numerics/mx-microscaling.md), [9.9 MX-Matmul Legality](../numerics/mx-matmul-legality.md), planned) and the codegen side by [7.26 Codegen Matmul + MX + QuantizeMx Leaves](codegen-matmul-mx.md); this page is the numeric-model layer between them.

---

## 9. Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `visitInstMatmult` (thunk) | `0x27f670` | → `doMatmult<InstMatmult>` | CERTAIN |
| `doMatmult<InstMatmult>` | `0x289450` | control + accumulate decision | CERTAIN |
| `matmultImpl<InstMatmult>` | `0x2883a0` | `fp32` MAC + zero-point + perf_mode loop | CERTAIN |
| `transposeImpl<InstMatmult>` | `0x284020` | CoreV4 transpose-stationary | HIGH |
| `visitInstMatmultMx` | `0x27f680` | MX fp4/fp8 dequant + matmul | CERTAIN |
| `dequantizeMx` | `0x27b040` | `ldexpf(x, E8M0−127)` per 8×4 block | CERTAIN |
| `visitInstQuantizeMx` | `0x1f6920` | opcode-96 standalone quantize | CERTAIN |
| `doQuantizeMx` | `0x29e380` | EMAX pick + generate + apply | CERTAIN |
| `generateMxScales` | `0x29e1d0` | E8M0 = clamp(maxExp − EMAX), floor 1 | CERTAIN |
| `findMaxExponentPerInputBlock` | `0x29e150` | max biased fp32 exp over 8×4 block | CERTAIN |
| `quantizeDataApplyingMxScales` | `0x29e2a0` | data × `2^(127−E8M0)` pre-scale | CERTAIN |
| `copyScalesFromArray<u8>` | `0x29e5e0` | scale reorder → block-major (read) | CERTAIN |
| `copy2DScaleVectorToArray<uchar>` | `0x29e570` | E8M0 → tensor, `(i&3)+32(i>>2)` (write) | CERTAIN |
| `cast_fp32_to_float4e2m1fn_x4` | `0x2a72c0` | FP4 narrow + pack driver (`count%4`) | CERTAIN |
| ` └ fp32→e2m1 nibble encode` | `0x2a3530` | RNE + saturate-to-6.0 (`or $0x7,%eax`) | CERTAIN |
| ` └ x4 nibble pack` | `0x2a6a80` | 2 nibbles/byte + lane remap | CERTAIN |
| ` └ lane-order remap` | `0x2a3500` | deinterleave-by-`2·stride` | CERTAIN |
| ` └ e2m1 nibble → fp32 decode` | `0x2a3f30` | grid `{0,.5,1,1.5,2,3,4,6}` | CERTAIN |
| `bir::CastToNewDType` (PLT→libBIR) | `0xed930→0x22b1230` | canonical FP8 narrow | CERTAIN (import) |
| `visitInstMatmultSparse` | `0x1f0a10` | structured-sparse matmul (128 rows, ×32) | CERTAIN |
| `visitInstReadActivationAccumulator` | `0x1d2ae0` | drain activation accumulator | CERTAIN |
| `doEngineAccumReduce` | `0x1f9900` | engine accumulator filler | HIGH |
| `deinterleaveMatrices` | `0x27ae90` | de-interleave (factor 2 / 4) | CERTAIN |
| `PhysicalMemory::writeAccumulate` | `0x1760f0` | start-reset + write (vtable+88) | CERTAIN |
| `MemoryObject::resetAccumulation(AP)` | `0x2931f0` | ZERO PSUM (per-partition memset) | CERTAIN |
| `MemoryObject::cast_to` | `0x28db30` | `CastToNewDType` (→ fp32 for MAC) | CERTAIN |
| `MemoryObject::writeAP / runAP` | `0x298a90 / 0x298390` | PSUM `+=` (matmul path) | CERTAIN |
| `getNumElementsPerPartition` | `0x22b04a8` | PLT thunk → libBIR | CERTAIN |
| `getCalcStart / getCalcAccu` | `0x22b0a80 / 0x22b1618` | PLT thunks → libBIR | CERTAIN |
| `byte_5F7898` (EMAX / format max-exp) | `0x5F7898` | `02 00 00 08 00 0f 08 0f` | CERTAIN |
| `qword_5F74E0` (dtype byte-stride) | `0x5F74E0` | `1 1 2 1 1 1 1 1 4 4 …` | CERTAIN |

---

## 10. Gaps and Verification Ceiling

Five claims were re-verified directly against `libBIRSimulator.so` (md5 `f3acdcba…`): the md5 itself; the EMAX table `byte_5F7898 = 02 00 00 08 00 0f 08 0f` (xxd); the `dequantizeMx` body (`shr $0x3` / `shr $0x2` index, `sub $0x7f,%edi`, `call ldexpf@plt`); the FP4 encoder saturate (`ucomiss`/`jp` NaN, `cmp $0x2; jg`, `or $0x7,%eax`, `0x400000` round-bias); and the quantize pre-scale (`mov $0xfe`, `sub; shl $0x17`, `mulss`). All five hold at the disassembly level. The five MX function VAs were confirmed against the IDA `native_exports` mangled-symbol table.

What remains below CERTAIN:

- **G1 — `EngineAccumulationType` bit-decode.** `getCalcStart`/`getCalcAccu`/`getNumElementsPerPartition` are PLT thunks into `libBIR`; their exact enum→bool mapping lives there, not in the simulator. Their *result-use* is certain; the bit decode is taken on faith from the Idle/Zero/Accumulate model. [GAP]
- **G2 — canonical FP8 narrow constants.** `bir::CastToNewDType` is a `libBIR` import (`0x22b1230` stub, decompile-failed). The e4m3fn 448 = `0x7E` / e5m2 57344 = `0x7B` / RNE-bias / NaN-code constants are HIGH for this binary — they live in the imported `libBIR` (same `float_converter.cpp` source baked in), not re-disassembled in `libBIRSimulator`, which contains no canonical FP8 narrow at all.
- **G3 — intra-x4 lane parameterization.** The FP4 lane permutation *formula* (`sub_2A3500`, deinterleave-by-`2·stride`) is CERTAIN; the concrete `stride`/`parity` per tensor (derived from the AP by `sub_2A58A0`) is HIGH — read at role level, not pinned to a NEFF byte fixture. Same residual the encoder and dtype-table reports left open.
- **G4 — `transposeImpl` index math.** The CoreV4 transpose-stationary variant (§2) is read at structure level (tiled transpose into the result `MemObj`, `out = ifmap · weightsᵀ`) but not element-indexed. [HIGH]
- **G5 — `key 19` authoring.** The per-function "auto psum accumulate" attribute is *read* at the call sites (`ToBoolVisitor` over a `boost::variant`); the pass that *sets* it is a `libwalrus`/HLO concern, not traced here.
- **G6 — `findMaxExponentPerInputBlock` / `generateMxScales` clamps vs HW.** The CLAMP-LOW and ZERO-FLOOR are byte-exact; whether silicon uses the identical all-zero-block convention is inferred from the arithmetic, not cross-checked against a hardware MX reference (no in-binary spec beyond the OCP URL string).

---

## Cross-References

- [Simulator Dispatch and State](sim-dispatch-state.md) — 7.34, the 110-case `visit` switch and `InstVisitor` field map these kernels run inside
- [Simulator Core Arithmetic](sim-core-arithmetic.md) — 7.35, the shared `cast_to` / accumulate / `runAP` `+=` primitives this page builds on
- [Codegen Matmul + MX + QuantizeMx Leaves](codegen-matmul-mx.md) — 7.26, the `libwalrus` leaves that *lower* these ops into BIR (the producer of what the sim consumes)
- [PE Matmul Encoding](../isa/pe-matmul-encoding.md) — 1.10, the CoreV4 PE encoder this sim is the functional twin of (dense / sparse / MX / quantize)
- [MXMEM_PATTERN1D](../isa/mxmem-pattern1d.md) — 1.05, the MX data + E8M0 scale-stream wire layout
- [MX Microscaling](../numerics/mx-microscaling.md) — 9.8 (planned), the OCP-MXFP spec and E8M0 semantics this page realizes numerically
- [MX-Matmul Legality](../numerics/mx-matmul-legality.md) — 9.9 (planned), the block-size / scale-method legality rules
