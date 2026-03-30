# Type Legalization

> **Prerequisites:** Familiarity with [SelectionDAG](selectiondag.md), [NVPTX register classes](../reference/register-classes.md), and [LLVM type system basics](https://llvm.org/docs/LangRef.html#type-system). Understanding of the [compilation pipeline](../pipeline/overview.md) up to instruction selection is assumed.

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

Type legalization is the SelectionDAG phase that rewrites every DAG node whose result or operand type is illegal for the target into equivalent sequences of legal-type operations. In upstream LLVM this logic spans four source files (`LegalizeTypes.cpp`, `LegalizeIntegerTypes.cpp`, `LegalizeFloatTypes.cpp`, `LegalizeVectorTypes.cpp`) totaling roughly 16,000 lines. In CICC v13.0, NVIDIA ships all of it as a single 348KB monolithic function -- `sub_20019C0` -- the largest function in the SelectionDAG address range and among the largest in the entire binary. Operation legalization follows in a separate 169KB function (`sub_1FFB890`), and vector split/scalarize dispatchers fan out into an additional 25+ worker functions.

The monolithic structure is either an LTO inlining artifact (all four upstream `.cpp` files collapsed by link-time optimization) or a deliberate choice for branch-prediction locality. The functional behavior is a faithful reproduction of upstream LLVM's `DAGTypeLegalizer`, but the legality tables, legal-type set, and vector legalization rules are heavily NVPTX-specific.

| | |
|---|---|
| **Type legalizer monolith** | `sub_20019C0` (348KB, 10,739 lines) |
| **Operation legalizer** | `sub_1FFB890` (169KB) |
| **SplitVectorResult** | `sub_2029C10` (dispatcher, 190 cases) |
| **SplitVectorOperand** | `sub_202E5A0` (dispatcher, 157 cases) |
| **ScalarizeVectorResult** | `sub_2036110` |
| **ScalarizeVectorOperand** | `sub_2035F80` |
| **WidenVector** | `sub_2036AE0` (31KB, limited NVPTX usage) |
| **ExpandIntegerResult** | `sub_201BB90` (75KB, 632 case labels) |
| **PromoteIntegerResult** | `sub_2000100` (45KB) |
| **PerformExpensiveChecks** | `sub_2010FB0` (62KB, debug verifier) |
| **NVPTXTargetLowering init** | `sub_3314670` (73KB, table population) |
| **Upstream** | `LegalizeTypes.cpp`, `LegalizeIntegerTypes.cpp`, `LegalizeFloatTypes.cpp`, `LegalizeVectorTypes.cpp` |

## Pipeline Position

Type legalization runs as the first major SelectionDAG transformation after the initial DAG is built by SelectionDAGBuilder (`sub_2081F00`). The full sequence:

1. **SelectionDAGBuilder** converts LLVM IR to an initial DAG with potentially illegal types
2. **DAG Combiner** (`sub_F20C20`) runs initial combines
3. **DAGTypeLegalizer** (`sub_20019C0`) iterates until all types are legal -- this page
4. **LegalizeDAG** (`sub_1FFB890`) legalizes operations on now-legal types
5. **DAG Combiner** runs again to clean up
6. **Instruction selection** (`sub_3090F90`) pattern-matches the final legal DAG

The type legalizer iterates to a fixpoint: each pass may create new nodes with illegal types (e.g., splitting a vector creates two half-width vectors that may themselves be illegal), so the worklist loops until every node in the DAG has only legal result and operand types.

## NVPTX Legal Type Model

The legal type set is defined in the `NVPTXTargetLowering` constructor (`sub_3314670`, 73KB) which populates the action table at offset `+2422`. NVPTX has a narrow set of legal types dictated by the PTX register file:

| Register Class | Legal MVTs |
|---|---|
| Int1Regs (`%p`) | `i1` |
| Int16Regs (`%rs`) | `i16` |
| Int32Regs (`%r`) | `i32` |
| Int64Regs (`%rd`) | `i64` |
| Float32Regs (`%f`) | `f32` |
| Float64Regs (`%fd`) | `f64` |
| Int16HalfRegs (`%h`) | `f16`, `bf16` |
| Int32HalfRegs (`%hh`) | `v2f16`, `v2bf16`, `v2i16`, `v4i8` |
| Int128Regs (`%rq`) | `i128` (SM 70+) |

For the complete register class table (vtable addresses, PTX types, encoded IDs, copy opcodes) see [Register Classes](../reference/register-classes.md#the-nine-register-classes).

The critical constraint: `Int32HalfRegs` is the **only** vector register class. It holds exactly 32 bits of packed data. The only legal vector types are those that pack into 32 bits:

- **`v2f16`** -- two `f16` values in one 32-bit register
- **`v2bf16`** -- two `bf16` values (SM 80+)
- **`v2i16`** -- two `i16` values in one 32-bit register
- **`v4i8`** -- four `i8` values in one 32-bit register

Every other vector type (`v4f32`, `v2f32`, `v8i32`, `v4f16`, `v2f64`, etc.) is illegal and must be split, scalarized, or expanded during type legalization. There is no packed `float32` SIMD on NVPTX -- this is a fundamental architectural constraint.

### SM-Gated Type Legality

The legal type set changes with the SM version. The constructor at `sub_3314670` queries subtarget features and conditionally marks types legal or illegal:

| SM Range | Legal Types Added | Legalization Change |
|---|---|---|
| SM < 53 | (base: `i1`, `i16`, `i32`, `i64`, `f32`, `f64`) | `f16` ops promoted to `f32`; no legal vectors |
| SM 53--69 | Scalar `f16` | `v2f16` legal for ld/st but packed arithmetic is Custom/Expand |
| SM 70+ | `v2f16` packed arithmetic, `i128` | `f16x2` PTX instructions (`add.f16x2`, `mul.f16x2`, `fma.rn.f16x2`) |
| SM 80+ | `v2bf16` | `bf16x2` PTX instructions |
| SM 100+ | `e2m1x2` (FP4), `e2m3x2` (FP6), `e3m2x2` (FP6), `ue8m0x2` | Additional packed narrow FP types for tensor core feeders |

On SM 70+, `v2f16` operations marked `Legal` or `Custom` in the action table map directly to packed PTX instructions, delivering 2x throughput versus scalarized `f16`. This is why CUDA `__half2` operations are efficient: the type stays packed through the entire pipeline. In contrast, `float4` is always fully scalarized to four independent `f32` operations on every SM generation.

## The Legality Table

### Primary Action Table (offset +2422)

The core data structure is a 2D array inside `NVPTXTargetLowering`:

```
action = *(uint8_t *)(TLI + 259 * VT + opcode + 2422)
```

Where:
- **TLI** = pointer to `NVPTXTargetLowering` object (loaded from `this->TLI` at `a1[1]`)
- **VT** = `SimpleVT` enum value (1--10 for scalar types, 14--109 for vector types)
- **opcode** = ISD opcode (0--258), capped at `0x102` by a guard check
- **259** = row stride (256 generic opcodes + 3 metadata bytes per VT row)

The action byte encodes:

| Value | Action | Meaning |
|---|---|---|
| `0` | Legal | Node is natively supported -- return immediately |
| `1` | Custom | Call `NVPTXTargetLowering::LowerOperation` (vtable slot #164, offset `+1312`) |
| `2` | Expand | Call LegalizeTypes, then ExpandNode (`sub_1FF6F70`) as fallback |
| `3` | LibCall | Call ExpandNode directly for library-call substitution |
| `4` | Promote | Find a larger legal type and rebuild the node at that type |

The legality check uses `(action & 0xFB) == 0` as the "legal" predicate. This means bit 2 is a don't-care -- a node with action byte `0x04` is still treated as legal in certain fast-path checks, which is the standard LLVM encoding where bit 2 flags "custom-but-legal" operations.

### Type-Supported Flag Array (offset +120)

A second structure at `TLI + 8*VT + 120` is a pointer array: non-null means the type `VT` is natively supported by the target. This provides a fast "is this type legal at all?" check before the per-opcode lookup.

### Promotion Action Table (offset +2681)

A 1D table indexed by opcode only (no VT dimension):

```
action = *(uint8_t *)(TLI + opcode + 2681)
```

Used for four specific opcodes: `BSWAP` (43), `CTLZ` (44), `CTTZ` (45), and `BITREVERSE` (199). Also used for opcode 204 (`CONCAT_VECTORS`) when the operand type is zero. This table encodes whether these operations should be promoted regardless of operand type.

### FSINCOS Action Table (offset +3976)

Another 1D table for `FSINCOS` (opcode 211):

```
action = *(uint8_t *)(TLI + opcode + 3976)
```

FSINCOS has unique legalization requirements because it produces two results (sin and cos simultaneously).

### Condition Code Action Table (offset +18112)

A packed 4-bit nibble table for condition-code-dependent operations (`FP_TO_SINT`, `FP_TO_UINT`, `SELECT_CC`, `BR_CC`):

```
base   = (VT_id >> 3) + 15 * condcode_type + 18112
action = (*(uint32_t *)(TLI + base * 4 + 12) >> (4 * (VT_id & 7))) & 0xF
```

The 15-entry stride per condition code allows per-CC/per-VT legalization decisions. Each nibble stores a 4-bit action code, so two VT actions pack into one byte. This is the standard LLVM condition-code action encoding, but the table is populated with NVPTX-specific rules (e.g., PTX's limited set of comparison predicates determines which CCs are legal for which types).

## SimpleVT Type Encoding

Types throughout the legalizer are encoded as a single byte, the `SimpleVT` enum:

| SimpleVT | Type | SimpleVT | Type |
|---|---|---|---|
| 0 | extended/custom | 7 | `i128` |
| 1 | `i1` | 8 | `f16` |
| 2 | `i2` (rare) | 9 | `f32` |
| 3 | `i8` | 10 | `f64` |
| 4 | `i16` | 14--55 | fixed-width vectors |
| 5 | `i32` | 56--109 | scalable vectors |
| 6 | `i64` | | |

The bitwidth-to-SimpleVT conversion pattern appears as a recurring code fragment at least 11 times in `sub_20019C0`:

```c
// Reconstructed from decompilation -- 11 instances in the function
if (bits == 32)       VT = 5;  // i32
else if (bits > 32) { VT = 6;  // i64 tentative
  if (bits != 64) { VT = 0;    // extended type
    if (bits == 128) VT = 7;   // i128
  }
} else {
  VT = 3;                      // i8 tentative
  if (bits != 8) VT = 4 * (bits == 16);  // i16 or 0
}
```

The vector type range 14--109 maps to scalar element types through a ~100-case switch block that also appears six times in the function body:

| MVT Range | Scalar Element | Description |
|---|---|---|
| 14--23 | `i2` (VT 2) | Fixed-width `v2i2`..`v1024i2` |
| 24--32 | `i8` (VT 3) | Fixed-width `v2i8`..`v256i8` |
| 33--40 | `i16` (VT 4) | Fixed-width `v2i16`..`v64i16` |
| 41--48 | `i32` (VT 5) | Fixed-width `v2i32`..`v64i32` |
| 49--54 | `i64` (VT 6) | Fixed-width `v2i64`..`v32i64` |
| 55 | `i128` (VT 7) | Fixed-width `v2i128` |
| 56--61 | `i2` (VT 2) | Scalable `nxv2i2`..`nxv64i2` |
| 62--67 | `i8` (VT 3) | Scalable `nxv2i8`..`nxv64i8` |
| 68--73 | `i16` (VT 4) | Scalable `nxv2i16`..`nxv64i16` |
| 74--79 | `i32` (VT 5) | Scalable `nxv2i32`..`nxv64i32` |
| 80--85 | `i64` (VT 6) | Scalable `nxv2i64`..`nxv64i64` |
| 86--88 | `f16` (VT 8) | Scalable `nxv2f16`..`nxv8f16` |
| 89--93 | `f32` (VT 9) | Scalable `nxv2f32`..`nxv32f32` |
| 94--97 | `f64` (VT 10) | Scalable `nxv2f64`..`nxv16f64` |
| 98--100 | `f16` (VT 8) | Fixed-width `v2f16`..`v8f16` (additional) |
| 101--105 | `f32` (VT 9) | Fixed-width `v2f32`..`v32f32` (additional) |
| 106--109 | `f64` (VT 10) | Fixed-width `v2f64`..`v16f64` (additional) |

This switch implements `getVectorElementType()` on the decompiled SimpleVT enum. Its six-fold repetition in the monolith accounts for a significant fraction of the function's 348KB size.

## The Four Legalization Actions

### Promote (Type Widening)

Promotion widens a narrow type to the nearest legal register width. The pattern is consistent across integer and FP promotion:

```
promoted_vt = TLI.getTypeToPromoteTo(opcode, VT)       // sub_1F40B60
extended    = DAG.getNode(ANY_EXTEND, DL, promoted_vt, input)   // opcode 143
result      = DAG.getNode(original_op, DL, promoted_vt, extended, ...)
truncated   = DAG.getNode(TRUNCATE, DL, original_vt, result)   // opcode 145
```

For integer promotion, `ANY_EXTEND` (opcode 143) or `ZERO_EXTEND` (opcode 144) widens the input depending on whether the high bits need defined values (unsigned operations use `ZERO_EXTEND`). For FP promotion, the pattern uses `FP_EXTEND`/`FP_ROUND` instead:

```
ext0 = DAG.getNode(FP_EXTEND, DL, promoted_vt, op0)
ext1 = DAG.getNode(FP_EXTEND, DL, promoted_vt, op1)
res  = DAG.getNode(FADD, DL, promoted_vt, ext0, ext1)
out  = DAG.getNode(FP_ROUND, DL, original_vt, res)
```

The `promote` path in `sub_1FFB890` contains approximately 30 opcode-specific expansion strategies. The custom-promotion BST (red-black tree at `TLI + 9257`/`9258`) stores `(opcode, VT)` pairs that override the default promotion target. When no BST entry exists, a linear scan walks upward from the current VT until it finds a type where the action is not Custom (i.e., Legal or Expand).

### Expand (Type Splitting)

Expansion splits a wide type into two halves and reassembles the result:

```
// i128 ADD expansion (simplified)
lo_a = DAG.getNode(EXTRACT_ELEMENT, DL, i64, a, 0)   // low half
hi_a = DAG.getNode(EXTRACT_ELEMENT, DL, i64, a, 1)   // high half
lo_b = DAG.getNode(EXTRACT_ELEMENT, DL, i64, b, 0)
hi_b = DAG.getNode(EXTRACT_ELEMENT, DL, i64, b, 1)
lo_r = DAG.getNode(ADD, DL, i64, lo_a, lo_b)
carry = ...  // carry detection via SETCC
hi_r = DAG.getNode(ADD, DL, i64, hi_a, hi_b)
hi_r = DAG.getNode(ADD, DL, i64, hi_r, carry)
result = DAG.getNode(BUILD_PAIR, DL, i128, lo_r, hi_r)
```

For CTLZ (case 53), expansion builds an all-ones mask, AND chain, and shift sequence. For SINT_TO_FP/UINT_TO_FP (cases 59/60), the helper `sub_20B5C20` performs iterative two-way splitting: it finds the half-type, builds the pair, and recursively legalizes each half.

The `ExpandIntegerResult` handler at `sub_201BB90` (75KB, 632 case labels) is itself a major function that dispatches expansion for specific opcodes including STORE (case 77), shifts (81--93), and atomics.

### Soften (Float-to-Integer Emulation)

Softening converts unsupported FP operations to integer-based library call sequences. On NVPTX this primarily affects `f128` (which has no hardware support on any SM) and `f16` on SM < 53. The softened path at `sub_2019DA0` (18KB) dispatches via the `SoftenedFloats` DenseMap.

The FADD/FMUL cases (74/75 in the main switch) compute twice the bit width, find the promoted FP type, and build `SUB` (opcode 54) / `SRL` (opcode 123) chains that implement the FP operation in integer arithmetic.

### Scalarize and Split Vector

Vector legalization proceeds through recursive halving:

```
v8f32  -> split -> 2x v4f32
v4f32  -> split -> 2x v2f32
v2f32  -> scalarize -> 2x f32    (v2f32 is NOT legal on NVPTX)

v4f16  -> split -> 2x v2f16     (LEGAL on SM 70+ -- stops here)
v8f16  -> split -> 2x v4f16 -> 4x v2f16

v4i8   -> LEGAL (packed in Int32HalfRegs, no split needed)
v8i8   -> split -> 2x v4i8     (one split, then legal)
```

The splitting strategy follows LLVM's standard approach:

1. **Determine half type**: `v4f32` splits to `v2f32` via `EVT::getVectorVT(scalar_element, count/2)` (`sub_1F58CC0`)
2. **Split operands**: Look up the `SplitVectors` DenseMap to get `{Lo, Hi}` halves from the input's own legalization
3. **Apply operation**: `Lo_result = DAG.getNode(opcode, DL, half_type, Lo_op1, Lo_op2)`, and similarly for `Hi`
4. **Record result**: Store `{Lo_result, Hi_result}` in the `SplitVectors` DenseMap via `sub_20167D0`

The critical observation for NVPTX: `v2f32` is **not** legal (no 64-bit packed float register class), so `v4f32` ends up fully scalarized to 4x `f32`. In contrast, `v4f16` on SM 70+ splits to 2x `v2f16` which is legal, enabling the `f16x2` packed instruction path.

## Master Opcode Dispatch (sub_20019C0)

The main body of `sub_20019C0` is a switch on `*(int16_t *)(node + 24)` -- the ISD opcode of the current SDNode. Approximately 50 cases are handled:

| Case | ISD Opcode | Action |
|---|---|---|
| 10 | `LOAD` | `legalizeLoad` -- type-aware load splitting |
| 11 | `STORE` | Iterative type demotion loop (see below) |
| 20--21, 26 | Generic arithmetic | Promote via `sub_1D38BB0` (getConstant) |
| 27 | `EXTRACT_ELEMENT` | Split + re-extract |
| 29 | `BUILD_PAIR` | Promote to `i32` |
| 48 | `BITCAST` | Promote or expand depending on `isSimple()` |
| 49 | `EXTRACT_SUBVECTOR` | Extract + rebuild via TRUNCATE (opcode 145) |
| 50 | `INSERT_SUBVECTOR` | Low/upper split via ANY_EXTEND (143) / ZERO_EXTEND_INREG (144) |
| 51 | `CONCAT_VECTORS` | Iterate operands, copy each to result list |
| 53 | `CTLZ` / `CTPOP` | Expand via mask-then-shift (AND=120, ADD=52) |
| 54 | `ATOMIC_CMP_SWAP` | Full promote path: check legality table, fallback to libcall |
| 55--56 | `SIGN_EXTEND_INREG` / `SMIN` | Legality check via `TLI + 259*VT + opcode + 2422` |
| 57--58 | `FP_TO_SINT` / `FP_TO_UINT` | Chain of promote + expand nodes |
| 59--60 | `SINT_TO_FP` / `UINT_TO_FP` | Iterative split via `sub_20B5C20` |
| 70, 72 | `FMINNUM` / `FMAXNUM` | BUILD_PAIR (opcode 0x89) reassembly |
| 74--75 | `FADD` / `FMUL` | Promote to wider FP type |
| 77 | `FMA` | Extend operands, FMA at wider type, round back |
| 105 | `BUILD_VECTOR` | Delegate to `sub_1FEC5F0` |
| 106 | `EXTRACT_VECTOR_ELT` | Check vector element count, dispatch |
| 108 | `MGATHER` / `MSCATTER` | Load/store with alignment fixup via `sub_20BD400` |
| 110 | `VSELECT` | Element-by-element type demotion loop |
| 112--113 | `SETCC` | Legality check with swapped-direction fallback |
| 114--117 | `VECREDUCE_*` | Opcode lookup in `dword_42FEAE0`, chain to VECREDUCE |
| 122--124 | `SHL` / `SRL` / `SRA` | Iterative width expansion |
| 125--126 | `ROTL` / `ROTR` | 4-way split: shift + mask + OR |
| 136 | `BR_CC` | Uses CC action table at offset `+18112` |
| 152 | `ATOMIC_LOAD_*` | Delegate to `sub_20B7F50` (atomic promote) |
| 153 | `ATOMIC_CMP_SWAP_WITH_SUCCESS` | Full CAS expansion with APInt mask |
| 199--200 | `INTRINSIC_W_CHAIN` / `INTRINSIC_WO_CHAIN` | TLI+112 check, intrinsic lowering dispatch |
| 211 | `UNDEF` | Replicate zero-constant to fill operand count |
| 243 | `TOKEN_FACTOR` | Duplicate single operand to all slots |

Cases not listed fall through to `LABEL_25` (node already legal or handled by a different legalization category).

### Store Iterative Demotion (Case 11)

The STORE case contains an explicit type-walking loop that searches downward for a legal store type:

```c
// Reconstructed from case 11, lines ~2077-2095
while ((vt_byte - 8) > 1) {          // while VT is not f16(8) or f32(9)
    --vt_byte;                        // try next smaller type
    if (TLI.getTypeAction(VT))        // sub_1D16180
        if (TLI.isOperationLegal(STORE, VT))
            break;                    // found a legal store type
}
```

This walks `i64` -> `i32` -> `i16` -> `i8` (or `f64` -> `f32` -> `f16`) until it finds a type the target can store natively, then emits a truncating store sequence via `sub_1D3C080` (getTruncStore).

### Atomic CAS Expansion (Cases 54, 153)

Atomic operations receive extensive legalization because PTX has limited atomic type support. The CAS expansion at case 153 (`ATOMIC_CMP_SWAP_WITH_SUCCESS`) builds APInt masks via `sub_16A4EF0`, constructs compare-and-swap loops, and handles the success flag as a separate result. The helper `sub_20B7E10` decides whether to use a CAS loop or a direct atomic based on the target SM's capabilities.

## Vector Legalization Workers

### SplitVectorResult (sub_2029C10)

This thin dispatcher reads the opcode from `*(uint16_t *)(node + 0x18)`, subtracts base `0x30` (48), and dispatches across 190 cases (opcodes 48--237) to SplitVecRes_XXX workers. Key handler categories:

| Handler | Cases | Description |
|---|---|---|
| `sub_20230C0` | FADD--FREM, SHL/SRA/SRL, int arith | Generic binary op split: split both inputs, apply op to each half |
| `sub_2028A10` | CONCAT, INSERT_ELT, load/store variants | Unary/multi-input split with reassembly |
| `sub_2025910` | Strict FP (cases 81--98) | Strict FP split with exception chain propagation |
| `sub_2023B70` | BUILD_VECTOR (case 104) | Split BUILD_VECTOR into two half-width constructs |
| `sub_2023F80` | CONCAT inner (case 107) | Trivial: return two operands as Lo and Hi |
| `sub_20293A0` | VECTOR_SHUFFLE (case 110, 10KB) | Decompose shuffle into sub-shuffles on half-width vectors |
| `sub_20251A0` | VSELECT, EXTRACT_ELT | Split condition mask along with operands |
| `sub_2025380` | Extending loads (cases 149--151) | Split load into two half-width loads |

Four handlers in the `0x214xxxx` range are NVPTX-specific split workers not present in upstream:

| Handler | Opcode | NVPTX-Specific Behavior |
|---|---|---|
| `sub_2146BB0` | CONCAT_VECTORS | Checks VT range 0x0E--0x6D for packed-type dispatch |
| `sub_2146C90` | SELECT_CC / BR_CC (2.7KB) | Multi-operand split with per-operand type classification |
| `sub_2147770` | FP_ROUND-like | NVPTX-specific FP rounding split |
| `sub_2147AE0` | BITCAST | NVPTX-specific bitcast split for packed registers |

After a handler returns, the dispatcher stores the `{Lo, Hi}` result pair in the `SplitVectors` DenseMap via `sub_20167D0` (hash = `37 * key`, quadratic probing, rehash at 75% load).

Fatal error on unhandled opcode: `"Do not know how to split the result of this operator!"` via `sub_16BD130`.

### SplitVectorOperand (sub_202E5A0)

Same dispatch pattern as `SplitVectorResult` but for operand-side legalization. Base opcode `0x65` (101), range 157 (opcodes 101--258). Notable inline handling for `FP_EXTEND`/`FP_ROUND` (cases 146--147, 152--153) that compares source and destination type sizes to choose the correct split strategy:

```c
// Inline in SplitVectorOperand, cases 146-147
src_size = getSizeInBits(src_vt);    // sub_2021900
dst_size = getSizeInBits(dst_vt);
if (dst_size < src_size)
    SplitVecOp_VSELECT(...)          // sub_202D8A0 -- shrinking
else
    SplitVecOp_Generic(...)          // sub_202A670 -- standard split
```

After the handler, `ReplaceAllUsesOfValueWith` (`sub_2013400`) substitutes the old node with the split result.

### Scalarize and Widen

`ScalarizeVectorResult` (`sub_2036110`) handles vector types that reduce to scalar. `ScalarizeVectorOperand` (`sub_2035F80`) has 80 cases starting from base opcode 106. These cover the final step when splitting has reduced a vector to width 1 or 2 elements, and those elements must become individual scalars.

`WidenVector` (`sub_2036AE0`, 31KB) sees limited use on NVPTX. Widening is only useful when the wider type is legal:

- Widening `v1f16` to `v2f16` is useful (promotes to legal packed type)
- Widening `v3i8` to `v4i8` is useful (promotes to legal packed type)
- Widening `v3f32` to `v4f32` is not useful (v4f32 is still illegal)

The `WidenVector` path uses the MVT lookup table at `word_4305480` to determine element counts and find the nearest wider legal vector type.

## Operation Legalization (sub_1FFB890)

After type legalization, operation legalization processes each node through a per-opcode action lookup. The same primary action table is used:

```
action = *(uint8_t *)(TLI + 259 * VT + opcode + 2422)
```

The dispatch:

| Action | Code | Path |
|---|---|---|
| Legal | 0 | Return immediately |
| Custom | 1 | `TLI->LowerOperation(node, DAG)` via vtable slot #164 (offset `+1312`) |
| Expand | 2 | `sub_20019C0` (LegalizeTypes), then `sub_1FF6F70` (ExpandNode) as fallback |
| LibCall | 3 | `sub_1FF6F70` (ExpandNode) directly |
| Promote | 4 | Find larger legal type, rebuild node |
| Special | 5+ | `sub_1FF9780` (ExpandLoad) or `sub_1FF5310` (LegalizeLoadOps) for load/store variants |

When Custom lowering returns NULL, the framework falls through to expansion. When it returns a different node, `ReplaceAllUsesWith` splices the replacement into the DAG and marks the old node dead (tombstone value `-2` in the worklist hash set).

The operation legalizer also contains an outer switch on the ISD opcode (`v11 = *(uint16_t *)(node + 24)`) for opcode-specific handling before the table lookup. Shift/rotate opcodes (81--98) are remapped to internal opcode numbers before the table lookup (e.g., case 81 maps to internal opcode 76, case 82 to 77). The opcode-specific dispatch covers approximately 30 opcode groups.

## How CUDA Vector Types Get Legalized

Tracing common CUDA types through the full legalization pipeline:

**`float4` (`v4f32`)** -- fully scalarized on every SM:
1. SplitVectorResult: `v4f32` -> 2x `v2f32`
2. ScalarizeVectorResult: `v2f32` -> 2x `f32` (no packed `f32` register class)
3. Final: 4 independent `f32` scalar operations
4. PTX: 4 separate `add.f32` / `mul.f32` instructions

**`half2` (`__half2` / `v2f16`)** -- stays packed on SM 70+:
1. Legal type, no splitting needed
2. Final: single `v2f16` packed operation
3. PTX: `add.f16x2`, `mul.f16x2`, `fma.rn.f16x2`

**`__nv_bfloat162` (`v2bf16`)** -- legal on SM 80+:
1. Same as `half2` but with `bf16x2` PTX instructions

**`float2` (`v2f32`)** -- scalarized, not packed:
1. ScalarizeVectorResult: `v2f32` -> 2x `f32`
2. No 64-bit packed float register class exists

**`v4f16` on SM 70+:**
1. SplitVectorResult: `v4f16` -> 2x `v2f16` (legal -- stops here)
2. Final: 2x `f16x2` packed operations (2x throughput vs scalarized)

**`v4f16` on SM < 53:**
1. Split: `v4f16` -> 2x `v2f16`
2. Scalarize: each `v2f16` -> 2x `f16`
3. Promote: each `f16` -> `FP_EXTEND` -> `f32`
4. Final: 4x `f32` operations with `FP_EXTEND`/`FP_ROUND` wrappers

**`double2` (`v2f64`):**
1. Scalarize: `v2f64` -> 2x `f64` (splitting would give `v1f64` which is scalar)

**Tensor core fragments** bypass vector legalization entirely. WMMA/MMA intrinsics represent matrix fragments as individual scalar registers, not LLVM vector types. However, packed conversion types used with tensor cores (`e4m3x2`, `e5m2x2`, `e2m1x2`, etc.) do pass through legalization and map to `Int32HalfRegs`.

## Verification Infrastructure

`sub_2010FB0` (62KB) implements `DAGTypeLegalizer::PerformExpensiveChecks`, gated by the `enable-legalize-types-checking` flag (registered at `ctor_341`). It validates nine DenseMap categories that track the state of every legalized value:

| Map | Content |
|---|---|
| `PromotedIntegers` | Values widened to a larger integer type |
| `ExpandedIntegers` | Values split into two halves |
| `SoftenedFloats` | FP values converted to integer representation |
| `PromotedFloats` | FP values widened to a larger FP type |
| `ExpandedFloats` | FP values split into halves |
| `ScalarizedVectors` | Vectors reduced to scalar elements |
| `SplitVectors` | Vectors split into `{Lo, Hi}` pairs |
| `WidenedVectors` | Vectors widened to a larger legal type |
| `ReplacedValues` | Values replaced by RAUW |

Diagnostic strings on verification failure: `"Processed value not in any map!"`, `"Value in multiple maps!"`, `"Value with legal type was transformed!"`.

## DAG Node Builder Subroutines

Key subroutines called from the type legalizer for constructing replacement DAG nodes:

| Function | Upstream Equivalent | Notes |
|---|---|---|
| `sub_1D309E0` | `DAG.getNode(opc, DL, VT, op)` | 1-operand (TRUNCATE, ANY_EXTEND, etc.) |
| `sub_1D332F0` | `DAG.getNode(opc, DL, VT, op1, op2)` | 2-operand |
| `sub_1D3A900` | `DAG.getNode(opc, DL, VT, op1, op2, op3)` | 3-operand (FMA) |
| `sub_1D38BB0` | `DAG.getConstant(val, DL, VT)` | Integer constant creation |
| `sub_1D38970` | `DAG.getConstant(APInt)` | Wide constant / all-ones mask |
| `sub_1D364E0` | `DAG.getUNDEF(VT)` | Undefined value |
| `sub_1D37440` | `DAG.getSetCC(DL, VT, LHS, RHS, CC)` | Comparison node |
| `sub_1D36A20` | `DAG.getSelectCC(DL, VT, ..., CC)` | Select-on-comparison |
| `sub_1D3BC50` | `DAG.getExtLoad(opc, DL, VT, ...)` | Extending load |
| `sub_1D3C080` | `DAG.getTruncStore(...)` | Truncating store |
| `sub_1D23890` | `DAG.ReplaceAllUsesWith(old, new)` | RAUW for result replacement |
| `sub_1FEB8F0` | `MVT::getSizeInBits(SimpleVT)` | Bit width from SimpleVT |
| `sub_1F58D40` | `EVT::getSizeInBits()` | Bit width from extended VT |
| `sub_1F58D30` | `EVT::getVectorNumElements()` | Vector element count |
| `sub_1F40B60` | `TLI.getTypeToPromoteTo(opc, VT)` | Promotion target lookup |
| `sub_1D16180` | `TLI.getTypeAction(VT)` | Action for type |
| `sub_1D16EF0` | `TLI.getCondCodeAction(CC, VT)` | Condition code legality |

## Result Accumulation and Worklist

Results from each legalization step are accumulated into a `SmallVector` of `{SDValue, SDValue}` pairs (node pointer + result index). The vector grows via `sub_16CD150` (`SmallVector::grow()`) when count exceeds capacity. After each pass, new nodes feed back into the worklist for iterative re-legalization until fixpoint -- all types are legal.

The worklist hash set uses open addressing with hash function `((id >> 9) ^ (id >> 4)) & (size - 1)` and grows at 75% load factor. Dead nodes are marked with sentinel `-2` (tombstone). The DenseMap instances used by the split/scalarize infrastructure use hash `37 * key` with quadratic probing.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM 20 | CICC v13.0 |
|---|---|---|
| Source organization | 4 files, ~16,000 lines total | 1 monolithic function, 10,739 lines (348KB) |
| Vector legal types | Target-dependent, often includes `v4f32`, `v2f64` | Only `v2f16`, `v2bf16`, `v2i16`, `v4i8` (32-bit packed) |
| `v2f32` | Legal on most targets (x86, ARM) | Illegal -- scalarized |
| Scalable vectors | Actively used (AArch64 SVE) | Encoded in tables but no SM target uses them |
| `i128` | Expanded on most targets | Legal on SM 70+ (Int128Regs / `.b128` / `%rq`) |
| NVPTX-specific split handlers | N/A | 4 functions in 0x214xxxx range for packed-type dispatch |
| Custom-promotion BST | Standard red-black tree | Same, at TLI offsets +9257/+9258 |
| Type-supported flag array | Pointer array at known offset | At `TLI + 8*VT + 120` |
| CC action table | 4-bit packed nibbles | Same encoding, NVPTX-specific CC legal set |

The monolithic structure means that code changes to any legalization category (integer promote, float soften, vector split) require recompilation of the entire 348KB function. In upstream LLVM, these are independent compilation units.

## Configuration

| Knob | Location | Default | Description |
|---|---|---|---|
| `enable-legalize-types-checking` | `ctor_341` | false | Enables `PerformExpensiveChecks` debug verifier |

No CICC-specific legalization knobs beyond the standard LLVM flag were found. The ptxas assembler has a related knob `MercuryDisableLegalizationOfTexToURBound` for texture-to-uniform-register legalization, but this operates at the assembler level, not in CICC.

## Key Functions

| Function | Address | Size | Role |
|---|---|---|---|
| Type legalizer monolith | `sub_20019C0` | 348KB | `DAGTypeLegalizer::run()` master dispatch |
| PromoteIntegerResult | `sub_2000100` | 45KB | Integer type promotion |
| PromoteFloatResult | `sub_2019DA0` | 18KB | Float type promotion / softening |
| ExpandFloatResult | `sub_201B410` | 11KB | Float type expansion |
| ExpandIntegerResult | `sub_201BB90` | 75KB | Integer type expansion (632 case labels) |
| Promote+expand dispatch | `sub_201E5F0` | 81KB | Secondary dispatch (441 case labels) |
| PerformExpensiveChecks | `sub_2010FB0` | 62KB | Debug verifier for 9 DenseMap categories |
| SplitVectorResult | `sub_2029C10` | 5KB | Dispatcher for 190 opcode cases |
| SplitVectorOperand | `sub_202E5A0` | 6KB | Dispatcher for 157 opcode cases |
| SplitVecRes_BinOp | `sub_20230C0` | -- | Generic binary op split |
| SplitVecRes_VECTOR_SHUFFLE | `sub_20293A0` | 10KB | Shuffle decomposition |
| ScalarizeVectorResult | `sub_2036110` | -- | Vector-to-scalar reduction |
| ScalarizeVectorOperand | `sub_2035F80` | -- | Operand scalarization (80 cases) |
| WidenVector | `sub_2036AE0` | 31KB | Vector widening (limited NVPTX use) |
| Operation legalizer | `sub_1FFB890` | 169KB | `LegalizeOp` per-node action dispatch |
| ExpandNode | `sub_1FF6F70` | 43KB | Full node expansion fallback |
| ExpandLoad | `sub_1FF9780` | 55KB | Load legalization |
| LegalizeLoadOps | `sub_1FF5310` | 41KB | Store splitting/coalescing |
| NVPTX split: CONCAT | `sub_2146BB0` | 219B | NVPTX-specific CONCAT_VECTORS split |
| NVPTX split: SELECT_CC | `sub_2146C90` | 2.7KB | NVPTX-specific SELECT_CC split |
| NVPTX split: FP_ROUND | `sub_2147770` | -- | NVPTX-specific FP rounding split |
| NVPTX split: BITCAST | `sub_2147AE0` | -- | NVPTX-specific bitcast split |
| NVPTXTargetLowering init | `sub_3314670` | 73KB | Populates legality tables |
| FP conversion split helper | `sub_20B5C20` | -- | Iterative SINT_TO_FP/UINT_TO_FP |
| Atomic promote helper | `sub_20B7F50` | -- | ATOMIC_LOAD promotion |
| CAS expansion decision | `sub_20B7E10` | -- | CAS loop vs direct atomic |
| Gather/scatter alignment | `sub_20BD400` | -- | MGATHER/MSCATTER alignment fixup |

## Reimplementation Checklist

1. **NVPTX legal type model.** Define the narrow set of legal types dictated by PTX register classes (i1, i16, i32, i64, f32, f64, f16, bf16, v2f16, v2bf16, v2i16, v4i8, i128), with SM-gated legality: f16 arithmetic on SM 53+, v2f16 packed ops on SM 70+, v2bf16 on SM 80+, FP4/FP6 packed types on SM 100+.
2. **Primary legality table population.** Build the 2D action table at `TLI + 259 * VT + opcode + 2422` with per-opcode-per-type action bytes (0=Legal, 1=Custom, 2=Expand, 3=LibCall, 4=Promote), plus the type-supported flag array at offset +120, the promotion action table at offset +2681, and the condition-code action table at offset +18112 with 4-bit packed nibbles.
3. **Four legalization actions.** Implement Promote (widen via ANY_EXTEND/ZERO_EXTEND, operate, TRUNCATE), Expand (split via shift-and-OR for integers, libcall for floats), Soften (integer emulation of unsupported FP types), and Scalarize/Split-Vector (decompose illegal vectors into scalar or half-width vector operations).
4. **Iterative fixpoint loop.** Run the type legalizer worklist until every node in the DAG has only legal result and operand types, since each pass may create new nodes with illegal types (e.g., splitting a vector creates half-width vectors that may themselves require further splitting).
5. **Vector legalization for NVPTX.** Handle the critical constraint that Int32HalfRegs is the only vector class (32 bits total): scalarize all vectors wider than 32 bits (v4f32, v2f32, v8i32, etc.) while keeping v2f16/v2bf16/v2i16/v4i8 legal. Implement the SplitVectorResult/SplitVectorOperand/ScalarizeVector dispatchers with their 190+/157+/~100 case switches.
6. **SimpleVT type encoding.** Implement the bitwidth-to-SimpleVT conversion (11 instances in NVIDIA's monolith) and the ~100-case vector-element-type switch (6 instances) mapping MVT ranges 14--109 to their scalar element types.

## Cross-References

- [SelectionDAG & Instruction Selection](./selectiondag.md) -- parent page covering the full SelectionDAG pipeline
- [NVPTX Target Infrastructure](../infra/nvptx-target.md) -- `NVPTXTargetLowering` constructor and TTI hooks
- [SM 70--89](../targets/sm70-89.md), [SM 90](../targets/sm90-hopper.md), [SM 100](../targets/sm100-blackwell.md) -- per-SM legal type details
- [DAG Node](../structs/dag-node.md) -- SDNode layout (opcode at +24, operands at +32, type at +40)
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- DenseMap mechanics used throughout legalization
